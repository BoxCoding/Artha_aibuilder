"""Database schema and engine.

SQLAlchemy Core rather than an ORM: the domain model is a Pydantic `Case` document, and the store
persists it as JSON alongside a handful of indexed columns the queue and risk console filter on.
That keeps business code identical across the in-memory and SQL stores, which is the seam the
prototype was designed around.

The same schema runs on PostgreSQL (production) and SQLite (local single-process use and tests).
Alembic migrations in `migrations/` are the source of truth for production; `create_all` exists
for tests and for `python -m app.cli init-db` on a throwaway database.
"""
from __future__ import annotations

import threading
from functools import lru_cache

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Engine, Index, Integer, LargeBinary, MetaData,
                        String, Table, Text, create_engine, event)
from sqlalchemy.dialects.postgresql import JSONB

from app.config import Settings, get_settings

metadata = MetaData()

# JSONB on PostgreSQL (indexable, compact), plain JSON elsewhere.
JsonType = JSON().with_variant(JSONB(), "postgresql")

cases = Table(
    "cases", metadata,
    Column("case_id", String(40), primary_key=True),
    Column("owner_uid", String(120), nullable=False, index=True),
    Column("status", String(40), nullable=False, index=True),
    Column("business_name", String(120), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, index=True),
    Column("version", Integer, nullable=False, default=1),
    Column("summary", JsonType, nullable=False),   # denormalised fields the queue and risk console read
    Column("document", JsonType, nullable=False),  # the full Case, as Case.model_dump(mode="json")
)

documents = Table(
    "document_blobs", metadata,
    Column("case_id", String(40), primary_key=True),
    Column("doc_id", String(40), primary_key=True),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", Integer, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

audit_log = Table(
    "audit_log", metadata,
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    # The exact ISO string that went into the hash. Stored as text so a database's timestamp
    # precision or time-zone handling can never make a genuine chain fail verification.
    Column("at", String(40), nullable=False),
    Column("actor", String(120), nullable=False),
    Column("role", String(40), nullable=False),
    Column("action", String(60), nullable=False),
    Column("resource", String(160), nullable=False),
    Column("detail_hash", String(64), nullable=False),
    Column("prev_hash", String(64), nullable=False),
    Column("event_hash", String(64), nullable=False, unique=True),
)

callbacks = Table(
    "callback_requests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("case_id", String(40), nullable=False, index=True),
    Column("uid", String(120), nullable=False),
    Column("note", String(300), nullable=False, default=""),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("handled", Boolean, nullable=False, default=False),
)

users = Table(
    "users", metadata,
    Column("uid", String(120), primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(120), nullable=False),
    Column("role", String(40), nullable=False),
    Column("password_hash", Text, nullable=True),  # null only for demo accounts
    Column("is_demo", Boolean, nullable=False, default=False),
    Column("active", Boolean, nullable=False, default=True),
    Column("session_version", Integer, nullable=False, default=1),
    Column("failed_logins", Integer, nullable=False, default=0),
    Column("locked_until", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
    # Optional, self-declared, used only to choose the video guide's presenter. Never copied onto an
    # application, never sent to a model, never used in any credit decision.
    Column("gender", String(20), nullable=True),
)

jobs = Table(
    "pipeline_jobs", metadata,
    Column("job_id", String(40), primary_key=True),
    Column("case_id", String(40), nullable=False, index=True),
    Column("state", String(20), nullable=False),  # queued | running | done | failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_until", DateTime(timezone=True), nullable=True),
    Column("worker_id", String(80), nullable=True),
    Column("last_error", String(300), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
Index("ix_pipeline_jobs_claim", jobs.c.state, jobs.c.lease_until)

outbox = Table(
    "outbox_events", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("topic", String(60), nullable=False),
    Column("key", String(60), nullable=False),
    Column("payload", JsonType, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False, default=0),
)
Index("ix_outbox_unpublished", outbox.c.published_at, outbox.c.id)

# Keyed hashes (HMAC with the service secret) of identifiers that link applications: statement files,
# bank accounts, PAN, mobile. Lets the pipeline find the same document or account under a different
# applicant without storing the identifiers themselves a second time.
fingerprints = Table(
    "fingerprints", metadata,
    Column("id", BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True),
    Column("case_id", String(40), nullable=False, index=True),
    Column("kind", String(20), nullable=False),
    Column("value_hash", String(64), nullable=False),
    Column("owner_hash", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_fingerprints_lookup", fingerprints.c.kind, fingerprints.c.value_hash)

rate_limits = Table(
    "rate_limits", metadata,
    Column("bucket", String(200), primary_key=True),
    Column("window_start", BigInteger, primary_key=True),
    Column("hits", Integer, nullable=False),
)


def make_engine(url: str, settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    if url.startswith("sqlite"):
        engine = create_engine(url, echo=settings.db_echo, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(url, echo=settings.db_echo, pool_pre_ping=True, pool_size=settings.db_pool_size,
                         max_overflow=settings.db_max_overflow, pool_recycle=1800, future=True)


@lru_cache
def get_engine() -> Engine:
    url = get_settings().database_url
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return make_engine(url)


def sync_sqlite_schema(engine: Engine) -> None:
    """Local SQLite only: create missing tables, then add missing nullable columns to existing ones, so a
    developer's database keeps working after an upgrade. PostgreSQL schemas change only through Alembic."""
    from sqlalchemy import inspect, text

    metadata.create_all(engine)
    inspector = inspect(engine)
    with sqlite_write_lock, engine.begin() as conn:
        for table in metadata.sorted_tables:
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in present and column.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" '
                                      f"{column.type.compile(engine.dialect)}"))


def is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


# SQLite has a single writer; serialising writers in-process avoids "database is locked" under the
# inline pipeline's worker threads. PostgreSQL uses row locks instead and never takes this.
sqlite_write_lock = threading.RLock()
