"""SQLite database setup and durable Agent Relay models.

This module is intentionally the only place that knows about SQLite connection
pragmas and its writer-lock transaction.  The rest of the application talks to
the models through :mod:`storage`; replacing this module with a PostgreSQL
engine and a row-locking claim transaction is the planned student exercise.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def _normalize_database_url(url: str) -> str:
    """Accept the standard PostgreSQL URI and route it to psycopg 3.

    Deployments hand out ``postgresql://`` -- the libpq form that psql, cloud
    panels, Compose and Kubernetes Secrets all speak.  SQLAlchemy reads the
    scheme as ``dialect+driver`` and maps a bare ``postgresql://`` to psycopg2,
    which this project does not depend on.  Pin the driver here rather than
    making every environment know which Python package happens to be vendored.
    """

    if url.startswith("postgres://"):  # legacy scheme, dropped in SQLAlchemy 1.4
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _database_url() -> str:
    return _normalize_database_url(
        os.getenv("RELAY_DATABASE_URL") or os.getenv("DATABASE_URL") or "sqlite:///./agent-relay.db"
    )


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DATABASE_URL = _database_url()
IS_SQLITE = DATABASE_URL.startswith("sqlite")
IS_POSTGRES = DATABASE_URL.startswith("postgresql")
LEASE_SECONDS = positive_int("RELAY_LEASE_SECONDS", 60)
MAX_ATTEMPTS = positive_int("RELAY_MAX_ATTEMPTS", 5)
RECOVERY_INTERVAL_SECONDS = max(1, positive_int("RELAY_RECOVERY_INTERVAL_SECONDS", 5))
MAX_BODY_BYTES = positive_int("RELAY_MAX_BODY_BYTES", 256 * 1024)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_db_time(value: datetime) -> datetime:
    """SQLite's DateTime implementation is most portable with naive UTC."""

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_time(value: datetime | None) -> str | None:
    value = db_time(value)
    if value is None:
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sent_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.sender_id", back_populates="sender", passive_deletes=True
    )
    received_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.recipient_id", back_populates="recipient", passive_deletes=True
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("sender_id", "idempotency_key", name="uq_task_sender_idempotency"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sender: Mapped[Agent] = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_tasks")
    recipient: Mapped[Agent] = relationship("Agent", foreign_keys=[recipient_id], back_populates="received_tasks")
    attempts: Mapped[list[Attempt]] = relationship(
        "Attempt", back_populates="task", cascade="all, delete-orphan", order_by="Attempt.attempt_number"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number", name="uq_attempt_task_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    terminal_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    terminal_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task: Mapped[Task] = relationship("Task", back_populates="attempts")


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


engine_kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
if IS_POSTGRES:
    # Every relay replica keeps its own pool.  SQLAlchemy's default of 5 plus 10
    # overflow is too small once a fleet of workers long-polls /tasks/claim, and
    # too small for the concurrent-claim test below, so make it explicit.
    engine_kwargs.update(
        {
            "pool_size": positive_int("RELAY_DB_POOL_SIZE", 10),
            "max_overflow": positive_int("RELAY_DB_MAX_OVERFLOW", 20),
            "pool_timeout": positive_int("RELAY_DB_POOL_TIMEOUT", 30),
        }
    )
if _is_sqlite(DATABASE_URL):
    engine_kwargs.update({"connect_args": {"check_same_thread": False, "timeout": 30}})
    if DATABASE_URL in {"sqlite://", "sqlite:///:memory:"}:
        from sqlalchemy.pool import StaticPool

        engine_kwargs["poolclass"] = StaticPool

engine: Engine = create_engine(DATABASE_URL, **engine_kwargs)

if _is_sqlite(DATABASE_URL):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)


_SCHEMA_LOCK_KEY = 0x41475246  # arbitrary but stable 64-bit key ("AGRF")


def init_db() -> None:
    """Create the schema once, even when several replicas boot together.

    ``create_all`` emits CREATE TABLE IF NOT EXISTS, but PostgreSQL takes
    exclusive DDL locks: two API pods starting in the same second can deadlock
    or raise a duplicate-table error.  A transaction-scoped advisory lock lets
    the first replica win; the rest wait, then find the tables already there.
    """

    if IS_POSTGRES:
        with engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _SCHEMA_LOCK_KEY})
            Base.metadata.create_all(connection)
        return
    Base.metadata.create_all(engine)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def immediate_transaction() -> Generator[Session, None, None]:
    """Open the writer transaction that claims, heartbeats and recovery share.

    The two backends reach the same guarantee by opposite means.  SQLite has no
    ``FOR UPDATE``, so ``BEGIN IMMEDIATE`` reserves the single writer slot up
    front and serializes every mutating operation across processes.  PostgreSQL
    runs them concurrently and relies on per-row locks instead -- see
    :func:`lock_rows`, which each call site applies to the rows it actually
    needs.  Swapping one global lock for several row locks is the substance of
    the port: a plain transaction here with no locking there would let two
    workers claim the same task.
    """

    connection = engine.connect()
    session = Session(bind=connection, expire_on_commit=False, autoflush=True)
    try:
        if IS_SQLITE:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        yield session
        session.flush()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        session.close()
        connection.close()


def lock_rows(query: Any, *, skip_locked: bool = False) -> Any:
    """Apply row locking on backends that have it.

    No-op on SQLite, where ``immediate_transaction`` already serializes writers.
    On PostgreSQL ``SKIP LOCKED`` lets a second worker step over a row another
    transaction is already working on instead of queueing behind it, while a
    plain ``FOR UPDATE`` makes heartbeats and terminal submissions wait for the
    row they specifically need.
    """

    if not IS_POSTGRES:
        return query
    return query.with_for_update(skip_locked=skip_locked)


def recover_expired_in_session(db: Session, now: datetime) -> int:
    """Expire active leases and requeue/fail their tasks within ``db``."""

    now_db = as_db_time(now)
    # Read candidates without locking, then take locks in the order every other
    # operation uses: task first, then its attempt.  Locking the attempt first
    # would invert claim_one's order and let two PostgreSQL transactions
    # deadlock against each other.
    candidate_ids = list(
        db.scalars(
            select(Attempt.task_id)
            .where(Attempt.outcome == "processing", Attempt.lease_expires_at <= now_db)
            .order_by(Attempt.lease_expires_at, Attempt.id)
        )
    )
    count = 0
    for task_id in candidate_ids:
        # Skip rather than block: another replica's recovery pass or an
        # in-flight claim already owns this task, and will resolve it.
        task = db.scalar(lock_rows(select(Task).where(Task.id == task_id), skip_locked=True))
        if task is None:
            continue
        attempt = db.scalar(
            lock_rows(
                select(Attempt)
                .where(Attempt.task_id == task_id, Attempt.outcome == "processing")
                .order_by(Attempt.attempt_number.desc())
                .limit(1)
            )
        )
        if attempt is None:
            continue
        if attempt.lease_expires_at > now_db:
            # Re-read under the lock: a heartbeat may have renewed the lease
            # between the unlocked scan above and acquiring the row.
            continue
        attempt.outcome = "expired"
        attempt.finished_at = now_db
        if task.status == "processing":
            if task.attempt_count >= MAX_ATTEMPTS:
                task.status = "failed"
                task.error = "attempts_exhausted"
                task.output = None
                task.finished_at = now_db
            else:
                task.status = "queued"
                task.finished_at = None
        count += 1
    return count


def recover_expired() -> int:
    """Run one recovery pass and return the number of expired attempts."""

    with immediate_transaction() as db:
        return recover_expired_in_session(db, utcnow())


__all__ = [
    "Agent",
    "Attempt",
    "Base",
    "DATABASE_URL",
    "DEFAULT_PAGE_SIZE",
    "IS_POSTGRES",
    "IS_SQLITE",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BODY_BYTES",
    "MAX_PAGE_SIZE",
    "RECOVERY_INTERVAL_SECONDS",
    "Task",
    "as_db_time",
    "db_session",
    "db_time",
    "engine",
    "immediate_transaction",
    "init_db",
    "iso_time",
    "lock_rows",
    "recover_expired",
    "recover_expired_in_session",
    "utcnow",
]
