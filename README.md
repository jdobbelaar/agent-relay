# Agent Relay

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. PostgreSQL persists the queue and
attempts, while workers execute tasks on their own machines. The included
worker deterministically returns `input.upper()`.

## Run it

With Docker (API and PostgreSQL together):

```bash
docker compose up -d --build
```

The dashboard and API are at <http://127.0.0.1:8080/>. PostgreSQL data lives in
the `agent-relay_postgres-data` volume, so it survives `docker compose down`
(add `-v` to wipe it). The database is also published on `127.0.0.1:5434` for
`psql` and the tests. Override the ports with `API_HOST_PORT` and
`POSTGRES_HOST_PORT`, and the credentials with `POSTGRES_USER`,
`POSTGRES_PASSWORD` and `POSTGRES_DB` (the defaults are for local use only).

On a local Kubernetes cluster (kind), using the manifests in `k8s/`:

```bash
kind create cluster --name agent-relay
docker build -t agent-relay:local .
kind load docker-image agent-relay:local postgres:16-alpine --name agent-relay
kubectl --context kind-agent-relay apply -k k8s/
kubectl --context kind-agent-relay -n agent-relay rollout status deploy/agent-relay
kubectl --context kind-agent-relay -n agent-relay port-forward svc/agent-relay 8090:8000
```

The dashboard is then at <http://127.0.0.1:8090/> (pick another local port if
8090 is taken). PostgreSQL runs as a StatefulSet with a 1Gi persistent volume,
and both workloads have readiness probes. Run the acceptance test against the
cluster with `RELAY_TEST_BASE_URL=http://127.0.0.1:8090 uv run pytest
test_integration_scenario1.py`. Tear it down with `kind delete cluster --name
agent-relay`. `k8s/secret.yaml` holds local-development credentials only.

The CI workflow (`.github/workflows/ci.yml`) runs the starter tests and the
integration test against a PostgreSQL service container. Only if they pass, it
builds an image tagged `ci-<commit>-<UTC time>`, loads it into the kind cluster,
points the Deployment at it, and waits for the rollout. Run it locally with
[act](https://github.com/nektos/act) once the cluster above exists (kind creates
the `kind` Docker network that `.actrc` attaches the runner to):

```bash
act push
```

The deploy job only runs when the `DEPLOY_TO_KIND` variable is `true`, which
`.actrc` sets, so on GitHub-hosted runners (which have no cluster) only the
tests run.

Or run the API on your machine against that database:

```bash
docker compose up -d postgres
uv sync
RELAY_DATABASE_URL=postgresql+psycopg://relay:relay@127.0.0.1:5434/relay \
  uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard.
`RELAY_DATABASE_URL` (or `DATABASE_URL`) must be a PostgreSQL URL; plain
`postgresql://` URLs are accepted and use the psycopg 3 driver. `GET /health` is a liveness check and `GET /ready` verifies database
connectivity and schema (it queries the real tables, so a wiped volume
reports not-ready instead of passing with zero tables).

Register two identities and send a task:

```bash
alice=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"alice"}')
bob=$(curl -sS -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' -d '{"name":"uppercase"}')
```

The response contains each agent's secret `token` once. Keep it outside source
control. Use `Authorization: Bearer <token>` for all subsequent API calls;
registration is the only unauthenticated endpoint. For a shared installation,
set `RELAY_ENROLLMENT_SECRET` and send it as `X-Enrollment-Secret` when
registering.

## Run the deterministic worker

The worker can register itself and save credentials in a mode-0600 JSON file:

```bash
uv run python main.py worker \
  --base-url http://127.0.0.1:8000 \
  --name uppercase \
  --credentials ./uppercase-credentials.json \
  --worker-id laptop-1
```

For failure/redelivery demonstrations, make local execution intentionally slow
and stop the process after one completion:

```bash
uv run python main.py worker --credentials ./uppercase-credentials.json \
  --slow-seconds 75 --worker-id slow-laptop
```

The worker heartbeats during long work. Killing it leaves the claim leased;
after the 60-second lease expires, another worker can claim the task with a new
token and incremented attempt number. `RELAY_LEASE_SECONDS` and
`RELAY_MAX_ATTEMPTS` are configurable server settings.

An existing credential can also be supplied explicitly (the token is not
written to disk):

```bash
uv run python main.py worker --agent-id agent_123 --token agt_… --worker-id laptop-2
```

## Storage and delivery behavior

`database.py` contains the SQLAlchemy models, the engine, and the task row-lock
helper. `storage.py` contains task/claim/recovery operations; routes and request
models are kept in `main.py` and `schemas.py`. Concurrent claims use
`FOR UPDATE SKIP LOCKED`, so workers each take a different queued task without
waiting on one another. Heartbeat, completion, and lease recovery lock the task
row first (always task, then attempt) so a race has one consistent outcome and
cannot deadlock.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
docker compose up -d postgres   # if it isn't already running
uv run pytest -q
```

Tests run against the separate `relay_test` database that Compose creates, so
they never touch your real data. `conftest.py` ignores `RELAY_DATABASE_URL` on
purpose, because the fixtures drop and recreate every table; set
`RELAY_TEST_DATABASE_URL` to use a different scratch server. The integration
test also creates and drops its own throwaway database. `relay_test` is only
created when the data volume is first initialized; on an older volume run
`docker compose exec postgres psql -U relay -c "CREATE DATABASE relay_test"`.

This starter intentionally does not include Kubernetes, CI, external message
brokers, or an LLM.
