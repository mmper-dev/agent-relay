# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1: build the virtualenv.
# Nothing from this stage ships except /app/.venv, so uv, caches and any build
# tooling stay out of the final image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.2 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependency manifests only, on their own layer: Docker reuses the cached
# install on every build where these two files have not changed, which is
# nearly every build. Copying the source first would defeat that.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ---------------------------------------------------------------------------
# Stage 2: the image that actually runs.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

# A container process is root unless told otherwise, and that root is the
# host's root if it ever escapes the namespace. Run as nobody in particular.
RUN useradd --create-home --uid 10001 relay

# /app stays root-owned so the running process cannot rewrite its own code.
# State goes in /data, the one writable path, which a volume can be mounted
# over for persistence. Anything written here without a mount dies with the
# container - which is the correct default for a container.
RUN install -d -o relay -g relay /data

WORKDIR /app

COPY --from=builder --chown=relay:relay /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RELAY_DATABASE_URL="sqlite:////data/agent-relay.db"

COPY --chown=relay:relay *.py dashboard.html ./

USER relay

EXPOSE 8000

# Docker restarts nothing on its own, but an orchestrator reads this. Uses the
# relay's own /health endpoint; the slim image has no curl, so urllib it is.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

# 0.0.0.0, not 127.0.0.1: bind to the container's own loopback and no traffic
# from outside the container can ever reach it.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
