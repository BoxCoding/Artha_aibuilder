"""PostgreSQL / SQLite implementation of the case store.

Concurrency rules:

* `mutate()` takes a row lock (`SELECT ... FOR UPDATE`) for the whole read-modify-write, so two
  underwriters deciding the same case, or a decision racing the pipeline's publish, serialise
  rather than overwrite each other.
* The audit chain takes a transaction-scoped advisory lock before reading the head, so two API
  instances can never both append event N+1. The primary key on `seq` is the backstop.
* SQLite has no row locks; an in-process lock serialises writers instead (single process only).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Engine, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.db import audit_log, callbacks, cases, documents, fingerprints, is_postgres, jobs, outbox, sqlite_write_lock
from app.domain.models import AuditEvent, Case, CaseStatus
from app.domain.store import (GENESIS_HASH, CaseNotFound, build_audit_event, case_summary, first_broken_link,
                              owner_row, queue_row)

log = logging.getLogger("artha.store")
AUDIT_LOCK_KEY = 0x4152_5448  # "ARTH"


class SqlStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._pg = is_postgres(engine)

    # ------------------------------------------------------------------ helpers
    def _write_guard(self):  # type: ignore[no-untyped-def]
        return nullcontext() if self._pg else sqlite_write_lock

    @staticmethod
    def _columns(case: Case) -> dict[str, Any]:
        return {
            "owner_uid": case.owner_uid, "status": case.status.value, "business_name": case.business_name,
            "created_at": case.created_at, "updated_at": case.updated_at,
            "summary": case_summary(case), "document": case.model_dump(mode="json"),
        }

    # ------------------------------------------------------------------ cases
    def add(self, case: Case, protected: bool = False) -> None:  # noqa: ARG002 - retention handles lifetime
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(insert(cases).values(case_id=case.case_id, version=1, **self._columns(case)))

    def replace(self, case: Case) -> None:
        with self._write_guard(), self.engine.begin() as conn:
            result = conn.execute(update(cases).where(cases.c.case_id == case.case_id)
                                  .values(version=cases.c.version + 1, **self._columns(case)))
            if result.rowcount == 0:
                raise CaseNotFound(case.case_id)

    def get(self, case_id: str) -> Case:
        with self.engine.connect() as conn:
            row = conn.execute(select(cases.c.document).where(cases.c.case_id == case_id)).first()
        if row is None:
            raise CaseNotFound(case_id)
        return Case.model_validate(row.document)

    @contextmanager
    def mutate(self, case_id: str) -> Iterator[Case]:
        with self._write_guard(), self.engine.begin() as conn:
            row = conn.execute(select(cases.c.document).where(cases.c.case_id == case_id)
                               .with_for_update()).first()
            if row is None:
                raise CaseNotFound(case_id)
            case = Case.model_validate(row.document)
            yield case
            case.updated_at = datetime.now(timezone.utc)
            conn.execute(update(cases).where(cases.c.case_id == case_id)
                         .values(version=cases.c.version + 1, **self._columns(case)))

    def list(self) -> list[Case]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(cases.c.document).order_by(cases.c.updated_at.desc())).all()
        return [Case.model_validate(r.document) for r in rows]

    def list_queue(self, limit: int = 200) -> list[dict[str, Any]]:
        stmt = (select(cases.c.case_id, cases.c.business_name, cases.c.status, cases.c.updated_at, cases.c.summary)
                .where(cases.c.status != CaseStatus.DRAFT.value)
                .order_by(cases.c.updated_at.desc()).limit(limit))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [queue_row(r.case_id, r.business_name, r.status, r.updated_at, r.summary) for r in rows]

    def list_for_owner(self, owner_uid: str, limit: int = 50,
                       include_assessment: bool = False) -> list[dict[str, Any]]:
        stmt = (select(cases.c.case_id, cases.c.business_name, cases.c.status, cases.c.created_at,
                       cases.c.updated_at, cases.c.summary)
                .where(cases.c.owner_uid == owner_uid).order_by(cases.c.updated_at.desc()).limit(limit))
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [owner_row(r.case_id, r.business_name, r.status, r.created_at, r.updated_at, r.summary,
                          include_assessment) for r in rows]

    def ids_with_status(self, status: CaseStatus) -> list[str]:
        with self.engine.connect() as conn:
            return list(conn.execute(select(cases.c.case_id).where(cases.c.status == status.value)).scalars())

    def summaries(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(cases.c.case_id, cases.c.summary)).all()
        return [dict(r.summary) | {"case_id": r.case_id} for r in rows]

    def change_marker(self, case_id: str) -> int:
        with self.engine.connect() as conn:
            version = conn.execute(select(cases.c.version).where(cases.c.case_id == case_id)).scalar()
        if version is None:
            raise CaseNotFound(case_id)
        return int(version)

    # ------------------------------------------------------------------ files
    def put_file(self, case_id: str, doc_id: str, data: bytes) -> None:
        import hashlib

        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(insert(documents).values(case_id=case_id, doc_id=doc_id, data=data, size_bytes=len(data),
                                                  sha256=hashlib.sha256(data).hexdigest(),
                                                  created_at=datetime.now(timezone.utc)))

    def get_file(self, case_id: str, doc_id: str) -> bytes:
        with self.engine.connect() as conn:
            data = conn.execute(select(documents.c.data).where(documents.c.case_id == case_id,
                                                               documents.c.doc_id == doc_id)).scalar()
        if data is None:
            raise CaseNotFound(f"{case_id}/{doc_id}")
        return bytes(data)

    # ------------------------------------------------------------------ callbacks
    def add_callback(self, record: dict[str, str]) -> None:
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(insert(callbacks).values(case_id=record["case_id"], uid=record["uid"],
                                                  note=record.get("note", ""), handled=False,
                                                  at=datetime.now(timezone.utc)))

    def callbacks(self) -> list[dict[str, str]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(callbacks).order_by(callbacks.c.id)).all()
        return [{"case_id": r.case_id, "uid": r.uid, "note": r.note, "at": r.at.isoformat()} for r in rows]

    def callback_count(self) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(func.count()).select_from(callbacks)).scalar() or 0)

    # ------------------------------------------------------------------ audit
    def audit(self, actor: str, role: str, action: str, resource: str, detail: dict[str, object]) -> AuditEvent:
        for attempt in range(3):
            try:
                with self._write_guard(), self.engine.begin() as conn:
                    if self._pg:
                        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_LOCK_KEY})
                    head = conn.execute(select(audit_log.c.seq, audit_log.c.event_hash)
                                        .order_by(audit_log.c.seq.desc()).limit(1)).first()
                    seq, prev = (head.seq + 1, head.event_hash) if head else (1, GENESIS_HASH)
                    event = build_audit_event(seq, prev, actor[:120], role[:40], action[:60], resource[:160], detail)
                    conn.execute(insert(audit_log).values(
                        seq=event.seq, at=event.at.isoformat(), actor=event.actor, role=event.role,
                        action=event.action, resource=event.resource, detail_hash=event.detail_hash,
                        prev_hash=event.prev_hash, event_hash=event.event_hash))
                    return event
            except IntegrityError:
                # Only reachable without the advisory lock (SQLite across processes): retry on the new head.
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    def audit_head(self) -> tuple[str, int]:
        with self.engine.connect() as conn:
            head = conn.execute(select(audit_log.c.seq, audit_log.c.event_hash)
                                .order_by(audit_log.c.seq.desc()).limit(1)).first()
        return (head.event_hash, int(head.seq)) if head else (GENESIS_HASH, 0)

    def audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(audit_log).order_by(audit_log.c.seq.desc()).limit(limit)).all()
        return [AuditEvent(seq=r.seq, at=datetime.fromisoformat(r.at), actor=r.actor, role=r.role, action=r.action,
                           resource=r.resource, detail_hash=r.detail_hash, prev_hash=r.prev_hash,
                           event_hash=r.event_hash) for r in reversed(rows)]

    def audit_for(self, case_id: str, limit: int = 500) -> list[AuditEvent]:
        from sqlalchemy import or_

        with self.engine.connect() as conn:
            rows = conn.execute(select(audit_log).where(or_(audit_log.c.resource == case_id,
                                                            audit_log.c.resource.like(f"{case_id}/%")))
                                .order_by(audit_log.c.seq).limit(limit)).all()
        return [AuditEvent(seq=r.seq, at=datetime.fromisoformat(r.at), actor=r.actor, role=r.role, action=r.action,
                           resource=r.resource, detail_hash=r.detail_hash, prev_hash=r.prev_hash,
                           event_hash=r.event_hash) for r in rows]

    def verify_audit_chain(self) -> bool:
        """Recompute every link from the stored text. Streams rows, so memory stays flat."""
        with self.engine.connect() as conn:
            rows = conn.execution_options(yield_per=1000).execute(select(audit_log).order_by(audit_log.c.seq))
            broken = first_broken_link(rows)
        if broken is not None:
            log.error("audit_chain_broken", extra={"detail": f"seq={broken}"})
        return broken is None

    # ------------------------------------------------------------------ lifecycle
    def delete_case(self, case_id: str) -> bool:
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(delete(documents).where(documents.c.case_id == case_id))
            conn.execute(delete(jobs).where(jobs.c.case_id == case_id))
            conn.execute(delete(callbacks).where(callbacks.c.case_id == case_id))
            conn.execute(delete(fingerprints).where(fingerprints.c.case_id == case_id))
            return conn.execute(delete(cases).where(cases.c.case_id == case_id)).rowcount > 0

    def sweep_expired(self, max_age_seconds: int) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        with self.engine.connect() as conn:
            stale = conn.execute(select(cases.c.case_id).where(cases.c.updated_at < cutoff)).scalars().all()
        return sum(int(self.delete_case(cid)) for cid in stale)

    # ------------------------------------------------------------------ events (outbox)
    def emit(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(insert(outbox).values(topic=topic, key=key, payload=payload, attempts=0,
                                               created_at=datetime.now(timezone.utc)))

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(outbox).where(outbox.c.published_at.is_(None))
                                .order_by(outbox.c.id).limit(limit)).all()
        return [{"id": r.id, "topic": r.topic, "key": r.key, "payload": r.payload, "created_at": r.created_at,
                 "attempts": r.attempts} for r in rows]

    def mark_events(self, ids: list[int], published: bool) -> None:
        if not ids:
            return
        values: dict[str, Any] = {"attempts": outbox.c.attempts + 1}
        if published:
            values["published_at"] = datetime.now(timezone.utc)
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(update(outbox).where(outbox.c.id.in_(ids)).values(**values))

    def event_counts(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            total = int(conn.execute(select(func.count()).select_from(outbox)).scalar() or 0)
            pending = int(conn.execute(select(func.count()).select_from(outbox)
                                       .where(outbox.c.published_at.is_(None))).scalar() or 0)
        return {"published": total - pending, "pending": pending}

    # ------------------------------------------------------------------ fingerprints
    def set_fingerprints(self, case_id: str, owner_hash: str, items: list[tuple[str, str]]) -> None:
        now = datetime.now(timezone.utc)
        with self._write_guard(), self.engine.begin() as conn:
            conn.execute(delete(fingerprints).where(fingerprints.c.case_id == case_id))
            if items:
                conn.execute(insert(fingerprints), [{"case_id": case_id, "kind": k, "value_hash": v,
                                                     "owner_hash": owner_hash, "created_at": now} for k, v in items])

    def match_fingerprints(self, case_id: str, items: list[tuple[str, str]]) -> list[dict[str, Any]]:
        if not items:
            return []
        from sqlalchemy import and_, or_

        clause = or_(*[and_(fingerprints.c.kind == k, fingerprints.c.value_hash == v) for k, v in items])
        with self.engine.connect() as conn:
            rows = conn.execute(select(fingerprints).where(clause, fingerprints.c.case_id != case_id)).all()
        return [{"case_id": r.case_id, "kind": r.kind, "value_hash": r.value_hash, "owner_hash": r.owner_hash,
                 "created_at": r.created_at} for r in rows]

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - readiness must answer, not raise
            log.warning("database_unreachable", exc_info=True)
            return False
