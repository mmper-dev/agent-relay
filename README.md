# Agent Relay (SQLite starter)

Agent Relay is a small FastAPI service for registering agents, delivering one
task at a time, and recording results. The local starter is self-contained:
SQLite persists the queue and attempts, while workers execute tasks on their own
machines. The included worker deterministically returns `input.upper()`.

## Run it

```bash
uv sync
uv run uvicorn main:app --reload
```

Open <http://127.0.0.1:8000/> for the token-based local dashboard. The default
database is `./agent-relay.db`; set `RELAY_DATABASE_URL` to use another SQLite
file. `GET /health` is a liveness check and `GET /ready` verifies database
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

`database.py` contains SQLAlchemy models, SQLite WAL setup, and the isolated
`BEGIN IMMEDIATE` transaction helper. `storage.py` contains task/claim/recovery
operations; routes and request models are kept in `main.py` and `schemas.py`.
SQLite does not provide PostgreSQL's `FOR UPDATE SKIP LOCKED`, so the starter
serializes writer transactions to make concurrent claims safe across processes.
Students can port this storage seam to PostgreSQL later without changing the
HTTP protocol or lifecycle in `SPEC.md`.

Claims are at-least-once and leased for 60 seconds by default. Heartbeats extend
an active lease. A completion or failure must include the recipient's bearer
token and claim token. Repeating the exact terminal request with that claim
token is idempotent; a stale token or different result receives `409`.

## Running on PostgreSQL

The storage layer now supports both backends from one code path.  Point
`RELAY_DATABASE_URL` (or `DATABASE_URL`) at PostgreSQL and nothing else changes:

```bash
docker run -d --name relay-pg -p 5432:5432 \
  -e POSTGRES_USER=relay -e POSTGRES_PASSWORD=relay -e POSTGRES_DB=relay postgres:16

RELAY_DATABASE_URL=postgresql://relay:relay@localhost:5432/relay \
  uv run uvicorn main:app --reload
```

Use the ordinary `postgresql://` URI.  SQLAlchemy reads a URL scheme as
`dialect+driver` and maps a bare `postgresql://` to psycopg2, which this project
does not install -- so `_normalize_database_url` rewrites it to
`postgresql+psycopg://` (psycopg 3).  Deployments can keep handing over the
standard URI that every other tool understands.

### What changes between the backends

SQLite has no `FOR UPDATE`, so `immediate_transaction` reserves the single
writer slot with `BEGIN IMMEDIATE` and every mutating operation is serialized.
PostgreSQL runs them concurrently and uses per-row locks instead, applied at
each call site through `lock_rows`:

| operation | PostgreSQL locking | why |
| --- | --- | --- |
| `claim_one` | `FOR UPDATE SKIP LOCKED` | a second worker must step over a row being claimed, not wait for it |
| `recover_expired_in_session` | `FOR UPDATE SKIP LOCKED` | replicas' recovery passes must not fight over the same task |
| `heartbeat` | blocking `FOR UPDATE` | this is about one specific task; waiting is correct |
| `commit_terminal` | blocking `FOR UPDATE` | races recovery for one row; exactly one must win |
| `create_task` | none | `uq_task_sender_idempotency` arbitrates; the loser is handed the winner's task |
| `authenticate` | none | touches only its own agent row |

Every operation that takes more than one lock takes them in the order
task -> attempt, which is what keeps concurrent claims and recovery from
deadlocking.

`init_db` wraps `create_all` in a PostgreSQL advisory lock so several replicas
booting together cannot race on DDL.  Pool sizing is configurable through
`RELAY_DB_POOL_SIZE`, `RELAY_DB_MAX_OVERFLOW` and `RELAY_DB_POOL_TIMEOUT`.

## Containers

`Dockerfile` builds a multi-stage image: `uv` installs dependencies in a builder
stage, and only the resulting virtualenv is copied into the runtime image. It
runs as uid 10001, with `/app` root-owned so the process cannot rewrite its own
code; writable state goes to `/data`, and shared worker credentials to `/creds`.

Build it without build attestations, or `kind load` later fails with an opaque
missing-content-digest error:

```bash
docker build --provenance=false --sbom=false -t agent-relay:dev .
```

`compose.yaml` runs the relay, PostgreSQL and the workers as separate services:

```bash
docker compose up -d --scale worker=3
docker compose ps
docker compose down       # keeps the data volume; `down -v` destroys it
```

Three things in there are worth reading:

- the relay waits on a `pg_isready` healthcheck (`condition: service_healthy`),
  not a sleep;
- PostgreSQL keeps its data in a named volume, so it survives `down`;
- `worker-register` is a one-shot service that registers the `uppercase` agent
  once and writes its credentials to a shared volume. Every worker replica then
  mounts the same identity. Without it, each replica registers a *different*
  agent, nothing errors, and scaling silently does nothing.

## Kubernetes (kind)

`k8s/` holds the same system as Kubernetes objects: PostgreSQL as a StatefulSet
with a headless Service and a PersistentVolumeClaim, schema creation as a Job,
the relay as a Deployment of two behind a Service, and the workers as a
Deployment of three. `/health` drives the liveness probe and `/ready` the
readiness probe, so losing the database drains traffic instead of restarting
pods.

The worker identity is a Secret here rather than a shared volume, because a
`ReadWriteOnce` claim is single-node and workers can be scheduled anywhere. A
registration Job publishes it, using a ServiceAccount scoped to `get` and
`create` on secrets and nothing else.

```bash
kind create cluster --name relay
kind load docker-image agent-relay:dev --name relay   # the cluster cannot see local images
kubectl apply -f k8s/
kubectl port-forward svc/relay 8000:8000
```

## Continuous integration

`.github/workflows/ci.yml` runs two jobs. `test` runs the suite twice: once on
SQLite, once against a PostgreSQL service container. `deploy` declares
`needs: test`, so a failing test means it never starts and whatever is already
deployed keeps running untouched.

The deploy job tags each image with a timestamp and commit sha — a fixed tag
would leave Kubernetes seeing no change and deploying nothing — loads it into
kind, updates both Deployments, and blocks on `kubectl rollout status` so a
crash-looping deploy is reported as a failure rather than a success.

Run it locally with [act](https://nektosact.com/); `.actrc` supplies a runner
image that has `docker` and `curl`:

```bash
act push
```

Note this workflow deploys to a local kind cluster, so it is meant for `act`. A
hosted run would push the image to a registry and deploy to a real cluster.

## Verify

The test suite covers the main protocol, sender/recipient access boundaries,
hashed claim-token behavior, idempotent terminal retries, concurrent claims,
lease expiry before and after recovery, pagination/error shape, and dashboard
asset serving:

```bash
uv run pytest -q

# the same suite against PostgreSQL
RELAY_DATABASE_URL=postgresql://relay:relay@localhost:5432/relay uv run pytest -q
```

The suite must pass on both.  The concurrency test is the one that matters:
disable `lock_rows` and run it on PostgreSQL and it fails with a duplicate
`uq_attempt_task_number`, because two workers claimed the same task.

Tests default to a scratch database at `/tmp/agent-relay-test.db` so they
don't reset your dev server's `./agent-relay.db`. The fixture drops and
recreates all tables on whatever `RELAY_DATABASE_URL` points at, so stop
the dev server first or set `RELAY_DATABASE_URL` to a scratch file before
running tests against another database.

This starter intentionally does not include Docker, Kubernetes, CI, external
brokers, an LLM, or a PostgreSQL implementation. Those are deployment and
student-port concerns rather than part of the local relay protocol.
