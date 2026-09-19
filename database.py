"""PostgreSQL database setup and durable Agent Relay models.

This module owns the engine, the session helper, and the row-locking helper used
to give each task one active lease.  The rest of the application talks to the
models through :mod:`storage`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


def _database_url() -> str:
    url = (
        os.getenv("RELAY_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql+psycopg://relay:relay@localhost:5432/relay"
    )
    # Bare postgres:// and postgresql:// URLs make SQLAlchemy pick psycopg2,
    # which is not installed; this project uses psycopg 3.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DATABASE_URL = _database_url()
LEASE_SECONDS = positive_int("RELAY_LEASE_SECONDS", 60)
MAX_ATTEMPTS = positive_int("RELAY_MAX_ATTEMPTS", 5)
RECOVERY_INTERVAL_SECONDS = max(1, positive_int("RELAY_RECOVERY_INTERVAL_SECONDS", 5))
MAX_BODY_BYTES = positive_int("RELAY_MAX_BODY_BYTES", 256 * 1024)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_db_time(value: datetime) -> datetime:
    """Timestamps are stored as naive UTC (``timestamp without time zone``)."""

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


# connect_timeout: fail fast when PostgreSQL is unreachable instead of hanging
# on the operating system's much longer TCP timeout.
engine: Engine = create_engine(
    DATABASE_URL, future=True, pool_pre_ping=True, connect_args={"connect_timeout": 5}
)

SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)


def init_db() -> None:
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


def lock_task(db: Session, task_id: str, *, skip_locked: bool = False) -> Task | None:
    """Select a task ``FOR UPDATE`` and return its latest committed state.

    Claim, heartbeat, terminal submission, and recovery all take this row lock
    before reading or changing a task's attempts, always in task-then-attempt
    order so they cannot deadlock.  Whichever transaction gets the lock first
    wins; the others wait (or, with ``skip_locked``, move on) and then see its
    committed result.
    """

    return db.scalar(
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )


def recover_expired_in_session(db: Session, now: datetime) -> int:
    """Expire active leases and requeue/fail their tasks within ``db``.

    Tasks another transaction currently holds locked are skipped; a later
    recovery pass picks them up if they are still expired.
    """

    now_db = as_db_time(now)
    task_ids = list(
        db.scalars(
            select(Attempt.task_id)
            .where(Attempt.outcome == "processing", Attempt.lease_expires_at <= now_db)
            .distinct()
        )
    )
    count = 0
    for task_id in task_ids:
        task = lock_task(db, task_id, skip_locked=True)
        if task is None:
            continue
        # Re-read under the lock: the attempt may have completed since the scan.
        expired = list(
            db.scalars(
                select(Attempt)
                .where(
                    Attempt.task_id == task_id,
                    Attempt.outcome == "processing",
                    Attempt.lease_expires_at <= now_db,
                )
                .order_by(Attempt.lease_expires_at, Attempt.id)
                .execution_options(populate_existing=True)
            )
        )
        for attempt in expired:
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

    with db_session() as db:
        return recover_expired_in_session(db, utcnow())


__all__ = [
    "Agent",
    "Attempt",
    "Base",
    "DATABASE_URL",
    "DEFAULT_PAGE_SIZE",
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
    "init_db",
    "iso_time",
    "lock_task",
    "recover_expired",
    "recover_expired_in_session",
    "utcnow",
]
