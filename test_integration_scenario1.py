"""Integration test for SPEC.md acceptance scenario 1.

Register two agents. One sends a task; the other claims and completes it; the
sender reads the result. Unlike test_agent_relay.py, this starts a real uvicorn
process against a real PostgreSQL database (a throwaway one created for the
run) and talks to it over HTTP, so the whole stack (server, routes, auth,
database, lease recovery loop) is exercised.

Set RELAY_TEST_BASE_URL to run the same flow against an API that is already
running instead, such as the Kubernetes deployment.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from conftest import TEST_DATABASE_URL

ROOT = Path(__file__).parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def database_url():
    """A fresh, empty database on the test server, dropped afterwards."""

    name = f"relay_it_{uuid.uuid4().hex[:12]}"
    admin_url = make_url(TEST_DATABASE_URL)
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield admin_url.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="module")
def base_url(request):
    external = os.getenv("RELAY_TEST_BASE_URL")
    if external:
        # Test an API that is already running (for example the Kubernetes
        # deployment behind `kubectl port-forward`). It registers new agents
        # there, so use a disposable environment.
        assert httpx.get(f"{external}/ready", timeout=5).status_code == 200
        yield external.rstrip("/")
        return
    database_url = request.getfixturevalue("database_url")
    log_path = Path(tempfile.mkdtemp()) / "server.log"
    port = free_port()
    env = {**os.environ, "RELAY_DATABASE_URL": database_url}
    env.pop("RELAY_ENROLLMENT_SECRET", None)
    log = log_path.open("w")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--port", str(port)],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.fail(f"server exited early:\n{log_path.read_text()}")
            try:
                if httpx.get(f"{url}/ready", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.2)
        else:
            pytest.fail("server did not become ready")
        yield url
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def register(client: httpx.Client, name: str) -> tuple[str, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    assert data["token"].startswith("agt_")
    return data["agent_id"], {"Authorization": f"Bearer {data['token']}"}


def test_scenario_1_two_agents_exchange_task_and_result(base_url):
    with httpx.Client(base_url=base_url, timeout=10) as client:
        assert client.get("/health").json() == {"status": "ok"}

        sender_id, sender = register(client, "alice")
        recipient_id, recipient = register(client, "uppercase")

        # Sender submits a task addressed to the recipient.
        sent = client.post("/api/v1/tasks", headers=sender, json={"to": recipient_id, "input": "hello relay"})
        assert sent.status_code == 201
        task_id = sent.json()["task_id"]
        assert sent.json()["status"] == "queued"

        # Before the recipient claims, the sender sees a queued task with no result.
        queued = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
        assert queued["status"] == "queued"
        assert queued["output"] is None and queued["finished_at"] is None

        # Only the addressed agent can claim it.
        assert client.post("/api/v1/tasks/claim", headers=sender, json={"wait_seconds": 0}).status_code == 204
        claim = client.post("/api/v1/tasks/claim", headers=recipient, json={"worker_id": "w1", "wait_seconds": 0})
        assert claim.status_code == 200
        claimed = claim.json()
        assert claimed["task_id"] == task_id
        assert claimed["from"] == sender_id
        assert claimed["input"] == "hello relay"
        assert claimed["attempt"] == 1

        # While claimed, the sender sees the task as processing.
        processing = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
        assert processing["status"] == "processing"

        # Recipient submits its result.
        done = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient,
            json={"claim_token": claimed["claim_token"], "output": claimed["input"].upper()},
        )
        assert done.status_code == 200
        assert done.json() == {"task_id": task_id, "status": "completed"}

        # The sender reads the result: this is the status the sender sees.
        result = client.get(f"/api/v1/tasks/{task_id}", headers=sender).json()
        assert result["status"] == "completed"
        assert result["output"] == "HELLO RELAY"
        assert result["error"] is None
        assert result["from"] == sender_id and result["to"] == recipient_id
        assert result["attempt_count"] == 1
        assert result["finished_at"] is not None

        # Delivery history records one completed attempt and never leaks tokens.
        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender).json()["items"]
        assert [(a["attempt"], a["worker_id"], a["outcome"]) for a in attempts] == [(1, "w1", "completed")]
        assert "claim_token" not in attempts[0]

        # A third agent cannot read the task or its result.
        _other_id, other = register(client, "mallory")
        assert client.get(f"/api/v1/tasks/{task_id}", headers=other).status_code == 404

        # Repeat the exact dashboard flow: the page is served, and the API
        # calls it makes as either participant show the finished task.
        page = client.get("/")
        assert page.status_code == 200 and "Agent token" in page.text
        assert "<h1>Agent Relay v2</h1>" in page.text
        for headers in (sender, recipient):
            agents = client.get("/api/v1/agents?limit=100", headers=headers).json()["items"]
            assert {"alice", "uppercase"} <= {a["name"] for a in agents}
            assert all("token" not in a for a in agents)
        sent_list = client.get("/api/v1/tasks?direction=sent&limit=100", headers=sender).json()["items"]
        received_list = client.get("/api/v1/tasks?direction=received&limit=100", headers=recipient).json()["items"]
        for items in (sent_list, received_list):
            row = next(t for t in items if t["task_id"] == task_id)
            assert (row["status"], row["input"], row["output"]) == ("completed", "hello relay", "HELLO RELAY")
