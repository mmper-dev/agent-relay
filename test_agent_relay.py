"""Protocol tests, run against whichever backend RELAY_DATABASE_URL names.

These tests exercise storage calls from multiple threads: the closest local
equivalent to several worker processes racing to claim an inbox.  The guarantee
comes from the database -- BEGIN IMMEDIATE on SQLite, FOR UPDATE SKIP LOCKED on
PostgreSQL -- never from a Python lock, so the same assertions must hold on
both.  Point RELAY_DATABASE_URL at a PostgreSQL instance to run them there:

    RELAY_DATABASE_URL=postgresql://relay:relay@localhost:5432/relay uv run pytest -q
"""

from __future__ import annotations

import os

# Default to a scratch DB so `pytest` never resets the dev server's
# `./agent-relay.db`. Respect an explicit RELAY_DATABASE_URL/DATABASE_URL
# (e.g. CI pointing at PostgreSQL), but otherwise isolate tests.
os.environ.setdefault("RELAY_DATABASE_URL", "sqlite:////tmp/agent-relay-test.db")

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

import main
from database import (
    MAX_ATTEMPTS,
    Attempt,
    Base,
    Task,
    as_db_time,
    db_session,
    engine,
    recover_expired,
    utcnow,
)
from storage import claim_one, create_task, heartbeat


@pytest.fixture(autouse=True)
def empty_database():
    # Resets whatever DB RELAY_DATABASE_URL points at. Defaults to the
    # scratch /tmp file above; never run against a DB with data you need.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def register(client: TestClient, name: str) -> tuple[dict, dict[str, str]]:
    response = client.post("/api/v1/agents", json={"name": name})
    assert response.status_code == 201
    data = response.json()
    return data, {"Authorization": f"Bearer {data['token']}"}


def test_protocol_idempotency_terminal_retry_and_auth_boundary():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "uppercase")
        sent = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert sent.status_code == 201
        duplicate = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "hello relay"},
        )
        assert duplicate.status_code == 201
        assert duplicate.json() == sent.json()
        conflict = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": "demo-1"},
            json={"to": recipient["agent_id"], "input": "different"},
        )
        assert conflict.status_code == 409

        task_id = sent.json()["task_id"]
        claim = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "worker-a", "wait_seconds": 0},
        )
        assert claim.status_code == 200
        claim_data = claim.json()
        assert "claim_token" in claim_data
        complete = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert complete.status_code == 200
        retry = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim_data["claim_token"], "output": "HELLO RELAY"},
        )
        assert retry.status_code == 200
        assert client.get(f"/api/v1/tasks/{task_id}", headers=recipient_headers).status_code == 200
        forbidden = client.get(f"/api/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {sender['token']}"})
        assert forbidden.status_code == 200  # sender is an authorized participant
        no_credentials = client.get("/api/v1/agents")
        assert no_credentials.status_code == 401
        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers).json()
        assert attempts["items"][0]["outcome"] == "completed"
        assert "claim_token" not in attempts["items"][0]


def test_atomic_claims_distribute_without_overlap():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, _recipient_headers = register(client, "recipient")
        for index in range(16):
            response = client.post(
                "/api/v1/tasks",
                headers=sender_headers,
                json={"to": recipient["agent_id"], "input": f"task-{index}"},
            )
            assert response.status_code == 201
        with ThreadPoolExecutor(max_workers=16) as pool:
            claims = list(pool.map(lambda index: claim_one(recipient["agent_id"], f"worker-{index}"), range(16)))
        claims = [claim for claim in claims if claim is not None]
        assert len(claims) == 16
        assert len({claim["task_id"] for claim in claims}) == 16
        with db_session() as db:
            processing = list(db.query(Task).filter(Task.status == "processing"))
            assert len(processing) == 16
            assert all(task.attempt_count == 1 for task in processing)


def test_expiry_requeues_and_old_token_is_stale_before_recovery():
    with TestClient(main.app) as client:
        _sender, sender_headers = register(client, "sender")
        recipient, recipient_headers = register(client, "recipient")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "recover me"},
        ).json()
        task_id = task["task_id"]
        first = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "dead", "wait_seconds": 0}
        ).json()
        with db_session() as db:
            attempt = db.query(Attempt).filter(Attempt.task_id == task_id).one()
            attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))
        stale = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": first["claim_token"], "output": "TOO LATE"},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_claim"
        assert main.recover_expired() == 1
        second = client.post(
            "/api/v1/tasks/claim", headers=recipient_headers, json={"worker_id": "replacement", "wait_seconds": 0}
        )
        assert second.status_code == 200
        assert second.json()["attempt"] == 2
        assert second.json()["claim_token"] != first["claim_token"]


def test_dashboard_is_asset_and_invalid_input_is_documented_error():
    with TestClient(main.app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "sessionStorage" in page.text
        missing_name = client.post("/api/v1/agents", json={})
        assert missing_name.status_code == 400
        assert missing_name.json()["error"]["code"] == "invalid_input"


def expire_lease(task_id: str) -> None:
    """Age the active lease so the next recovery pass treats it as dead."""

    with db_session() as db:
        attempt = (
            db.query(Attempt)
            .filter(Attempt.task_id == task_id, Attempt.outcome == "processing")
            .order_by(Attempt.attempt_number.desc())
            .first()
        )
        assert attempt is not None
        attempt.lease_expires_at = as_db_time(utcnow() - timedelta(seconds=1))


def test_parallel_idempotency_key_creates_one_task():
    """Two replicas racing on the same key must not both insert.

    The duplicate check and the insert are separate statements, so on PostgreSQL
    both callers can pass the check before either commits.  The unique
    constraint is what actually enforces this, and the loser must be handed the
    winner's task rather than an error.
    """

    with TestClient(main.app) as client:
        sender, _ = register(client, "sender")
        recipient, _ = register(client, "recipient")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: create_task(sender["agent_id"], recipient["agent_id"], "same input", "race-key"),
                    range(8),
                )
            )
        assert len({result["task_id"] for result in results}) == 1
        with db_session() as db:
            assert db.query(Task).filter(Task.sender_id == sender["agent_id"]).count() == 1


def test_heartbeat_holds_the_lease_against_other_workers():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, _ = register(client, "recipient")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "long job"},
        ).json()
        claim = claim_one(recipient["agent_id"], "slow-worker")
        assert claim is not None
        renewed = heartbeat(task["task_id"], recipient["agent_id"], claim["claim_token"])
        assert renewed >= claim["lease_expires_at"]
        # A renewed lease is an active lease: nobody else may take this task,
        # and recovery must leave it alone.
        assert claim_one(recipient["agent_id"], "other-worker") is None
        assert recover_expired() == 0


def test_attempt_limit_fails_the_task_and_removes_it_from_claims():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, sender_headers_recipient = register(client, "recipient")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "nobody finishes me"},
        ).json()
        task_id = task["task_id"]
        for expected_attempt in range(1, MAX_ATTEMPTS + 1):
            claim = claim_one(recipient["agent_id"], f"worker-{expected_attempt}")
            assert claim is not None, f"expected attempt {expected_attempt} to be claimable"
            assert claim["attempt"] == expected_attempt
            expire_lease(task_id)
            recover_expired()
        assert claim_one(recipient["agent_id"], "one-too-many") is None
        final = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers).json()
        assert final["status"] == "failed"
        assert final["error"] == "attempts_exhausted"
        assert final["attempt_count"] == MAX_ATTEMPTS


def test_unrelated_agent_cannot_read_or_claim_another_inbox():
    with TestClient(main.app) as client:
        sender, sender_headers = register(client, "sender")
        recipient, _ = register(client, "recipient")
        _outsider, outsider_headers = register(client, "outsider")
        task = client.post(
            "/api/v1/tasks",
            headers=sender_headers,
            json={"to": recipient["agent_id"], "input": "private"},
        ).json()
        task_id = task["task_id"]
        assert client.get(f"/api/v1/tasks/{task_id}", headers=outsider_headers).status_code == 404
        assert client.get(f"/api/v1/tasks/{task_id}/attempts", headers=outsider_headers).status_code == 404
        # The outsider's own inbox is empty, so claiming returns no work rather
        # than leaking the recipient's queued task.
        stolen = client.post(
            "/api/v1/tasks/claim", headers=outsider_headers, json={"worker_id": "thief", "wait_seconds": 0}
        )
        assert stolen.status_code == 204
