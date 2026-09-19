FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install locked runtime dependencies first so this layer caches across code changes.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN useradd --system --create-home relay
USER relay
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# RELAY_DATABASE_URL must point at PostgreSQL (see compose.yaml).
# 0.0.0.0 is required: uvicorn's default 127.0.0.1 is unreachable through `docker run -p`.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
