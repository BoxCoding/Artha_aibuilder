"""Case repository.

Two implementations share one interface:

* `InMemoryStore` - development and unit tests. Single process, lost on restart.
* `SqlStore` (app/domain/sql_store.py) - PostgreSQL in production, SQLite locally. Cases are
  persisted as JSON documents with indexed summary columns; documents as blobs; the audit log as
  a hash-chained append-only table.

`get_store()` picks the SQL store whenever DATABASE_URL is set. Business code never knows which
one it has. `mutate()` is transactional in both: if the body raises, nothing is written.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

from app.domain.models import AuditEvent, Case, CaseStatus


class CaseNotFound(KeyError):
    pass


GENESIS_HASH = "0" * 64


def case_summary(case: Case) -> dict[str, Any]:
    """The denormalised projection the queue and the risk console read, so neither has to load
    every transaction of every case to draw a table."""
    memo = case.memo
    decision = case.decision
    return {
        "requested_amount": str(case.requested_amount),
        "band": case.score.band if case.score else None,
        "recommendation": case.policy.outcome.value if case.policy else None,
        "integrity_findings": sum(1 for s in case.signals
                                  if s.category == "FRAUD" and s.severity in ("HIGH", "CRITICAL")),
        "signal_codes": [s.code for s in case.signals],
        "scored": case.score is not None,
        "decided": decision is not None,
        "override": ({"from": decision.system_recommendation.value, "to": decision.final_outcome.value,
                      "reason": decision.reason} if decision and decision.override else None),
        "memo": ({"revisions": memo.iterations - 1, "dropped": len(memo.dropped_claims),
                  "critic_issues": len(memo.critic_issues)} if memo else None),
        # dimensions for portfolio analytics (no personal data)
        "created": case.created_at.date().isoformat(),
        "channel": case.channel,
        "industry": case.applicant.industry if case.applicant else None,
        "state": case.applicant.state if case.applicant else None,
        "pd": round(case.score.pd, 5) if case.score else None,
        "outcome": decision.final_outcome.value if decision else None,
        "status": case.status.value,
    }


def queue_row(case_id: str, business_name: str, status: str, updated_at: datetime,
              summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case_id, "business_name": business_name, "status": status,
        "requested_amount": summary.get("requested_amount"), "updated_at": updated_at,
        "band": summary.get("band"), "recommendation": summary.get("recommendation"),
        "integrity_findings": summary.get("integrity_findings", 0),
        "awaiting_second_approval": status == CaseStatus.PENDING_SECOND_APPROVAL.value,
    }


def owner_row(case_id: str, business_name: str, status: str, created_at: datetime, updated_at: datetime,
              summary: dict[str, Any], include_assessment: bool = False) -> dict[str, Any]:
    """A row in "my applications". Customers never see band, recommendation or findings; the loan
    officer who sourced the case does (include_assessment)."""
    row = {"case_id": case_id, "business_name": business_name, "status": status,
           "requested_amount": summary.get("requested_amount"), "decided": bool(summary.get("decided")),
           "created_at": created_at, "updated_at": updated_at}
    if include_assessment:
        row |= {"band": summary.get("band"), "recommendation": summary.get("recommendation"),
                "integrity_findings": summary.get("integrity_findings", 0)}
    return row


class CaseStore(Protocol):
    def add(self, case: Case, protected: bool = False) -> None: ...
    def replace(self, case: Case) -> None: ...
    def get(self, case_id: str) -> Case: ...
    def mutate(self, case_id: str) -> Any: ...
    def list(self) -> list[Case]: ...
    def list_queue(self, limit: int) -> list[dict[str, Any]]: ...
    def list_for_owner(self, owner_uid: str, limit: int = 50,
                       include_assessment: bool = False) -> list[dict[str, Any]]: ...
    def summaries(self) -> list[dict[str, Any]]: ...
    def ids_with_status(self, status: CaseStatus) -> list[str]: ...
    def change_marker(self, case_id: str) -> int: ...
    def put_file(self, case_id: str, doc_id: str, data: bytes) -> None: ...
    def get_file(self, case_id: str, doc_id: str) -> bytes: ...
    def add_callback(self, record: dict[str, str]) -> None: ...
    def callbacks(self) -> list[dict[str, str]]: ...
    def callback_count(self) -> int: ...
    def audit(self, actor: str, role: str, action: str, resource: str, detail: dict[str, object]) -> AuditEvent: ...
    def audit_head(self) -> tuple[str, int]: ...
    def audit_events(self, limit: int = 100) -> list[AuditEvent]: ...
    def verify_audit_chain(self) -> bool: ...
    def audit_for(self, case_id: str, limit: int = 500) -> list[AuditEvent]: ...
    def delete_case(self, case_id: str) -> bool: ...
    def sweep_expired(self, max_age_seconds: int) -> int: ...
    def ping(self) -> bool: ...
    # events (outbox) and cross-application fingerprints
    def emit(self, topic: str, key: str, payload: dict[str, Any]) -> None: ...
    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]: ...
    def mark_events(self, ids: list[int], published: bool) -> None: ...
    def event_counts(self) -> dict[str, int]: ...
    def set_fingerprints(self, case_id: str, owner_hash: str, items: list[tuple[str, str]]) -> None: ...
    def match_fingerprints(self, case_id: str, items: list[tuple[str, str]]) -> list[dict[str, Any]]: ...


class InMemoryStore:
    def __init__(self, max_cases: int = 300) -> None:
        self._lock = threading.RLock()
        self._cases: OrderedDict[str, Case] = OrderedDict()
        self._versions: dict[str, int] = {}
        self._files: dict[str, bytes] = {}
        self._audit: list[AuditEvent] = []
        self._callbacks: list[dict[str, str]] = []
        self._protected: set[str] = set()
        self._max = max_cases
        self._events: list[dict[str, Any]] = []
        self._prints: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ cases
    def add(self, case: Case, protected: bool = False) -> None:
        with self._lock:
            self._cases[case.case_id] = case
            self._versions[case.case_id] = 1
            if protected:
                self._protected.add(case.case_id)
            while len(self._cases) > self._max:
                victim = next((cid for cid in self._cases if cid not in self._protected), None)
                if victim is None:
                    break
                self.delete_case(victim)

    def replace(self, case: Case) -> None:
        with self._lock:
            if case.case_id not in self._cases:
                raise CaseNotFound(case.case_id)
            self._cases[case.case_id] = case
            self._versions[case.case_id] = self._versions.get(case.case_id, 0) + 1

    def get(self, case_id: str) -> Case:
        with self._lock:
            try:
                return self._cases[case_id]
            except KeyError as exc:
                raise CaseNotFound(case_id) from exc

    @contextmanager
    def mutate(self, case_id: str) -> Iterator[Case]:
        """Yield a working copy; commit it only if the body completes, as the SQL store does."""
        with self._lock:
            working = self.get(case_id).model_copy(deep=True)
            yield working
            working.updated_at = datetime.now(timezone.utc)
            self.replace(working)

    def list(self) -> list[Case]:
        with self._lock:
            return list(self._cases.values())

    def list_queue(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            live = sorted((c for c in self._cases.values() if c.status != CaseStatus.DRAFT),
                          key=lambda c: c.updated_at, reverse=True)[:limit]
            return [queue_row(c.case_id, c.business_name, c.status.value, c.updated_at, case_summary(c))
                    for c in live]

    def list_for_owner(self, owner_uid: str, limit: int = 50,
                       include_assessment: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            mine = sorted((c for c in self._cases.values() if c.owner_uid == owner_uid),
                          key=lambda c: c.updated_at, reverse=True)[:limit]
            return [owner_row(c.case_id, c.business_name, c.status.value, c.created_at, c.updated_at,
                              case_summary(c), include_assessment) for c in mine]

    def summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            return [case_summary(c) | {"case_id": c.case_id} for c in self._cases.values()]

    def ids_with_status(self, status: CaseStatus) -> list[str]:
        with self._lock:
            return [c.case_id for c in self._cases.values() if c.status == status]

    def change_marker(self, case_id: str) -> int:
        with self._lock:
            if case_id not in self._cases:
                raise CaseNotFound(case_id)
            return self._versions.get(case_id, 0)

    # ------------------------------------------------------------------ files
    def put_file(self, case_id: str, doc_id: str, data: bytes) -> None:
        with self._lock:
            self._files[f"{case_id}/{doc_id}"] = data

    def get_file(self, case_id: str, doc_id: str) -> bytes:
        with self._lock:
            try:
                return self._files[f"{case_id}/{doc_id}"]
            except KeyError as exc:
                raise CaseNotFound(f"{case_id}/{doc_id}") from exc

    # ------------------------------------------------------------------ callbacks
    def add_callback(self, record: dict[str, str]) -> None:
        with self._lock:
            self._callbacks.append(record)

    def callbacks(self) -> list[dict[str, str]]:
        with self._lock:
            return list(self._callbacks)

    def callback_count(self) -> int:
        with self._lock:
            return len(self._callbacks)

    # ------------------------------------------------------------------ audit (hash-chained, append-only)
    def audit(self, actor: str, role: str, action: str, resource: str, detail: dict[str, object]) -> AuditEvent:
        with self._lock:
            prev = self._audit[-1].event_hash if self._audit else GENESIS_HASH
            event = build_audit_event(len(self._audit) + 1, prev, actor, role, action, resource, detail)
            self._audit.append(event)
            return event

    def audit_head(self) -> tuple[str, int]:
        """The last hash in the chain and how many events it covers - what a receipt commits to."""
        with self._lock:
            return (self._audit[-1].event_hash if self._audit else GENESIS_HASH), len(self._audit)

    def delete_case(self, case_id: str) -> bool:
        """Erase a case and its documents. Used for the data-deletion right and by retention."""
        with self._lock:
            case = self._cases.pop(case_id, None)
            if case is None:
                return False
            self._versions.pop(case_id, None)
            self._prints = [p for p in self._prints if p["case_id"] != case_id]
            for document in case.documents:
                self._files.pop(f"{case_id}/{document.doc_id}", None)
            self._protected.discard(case_id)
            return True

    def sweep_expired(self, max_age_seconds: int) -> int:
        """Delete cases past their retention window. The audit log is deliberately untouched: it
        holds hashes, not personal data, and it is what makes a decision reviewable later."""
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        with self._lock:
            stale = [cid for cid, case in self._cases.items()
                     if cid not in self._protected and case.updated_at.timestamp() < cutoff]
        return sum(int(self.delete_case(cid)) for cid in stale)

    def audit_events(self, limit: int = 100) -> list[AuditEvent]:
        with self._lock:
            return list(self._audit[-limit:])

    def verify_audit_chain(self) -> bool:
        with self._lock:
            return first_broken_link(self._audit) is None

    def audit_for(self, case_id: str, limit: int = 500) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._audit if e.resource == case_id or e.resource.startswith(f"{case_id}/")][-limit:]

    def ping(self) -> bool:
        return True

    # ------------------------------------------------------------------ events & fingerprints
    def emit(self, topic: str, key: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._events.append({"id": len(self._events) + 1, "topic": topic, "key": key, "payload": payload,
                                 "created_at": datetime.now(timezone.utc), "published_at": None, "attempts": 0})

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(e) for e in self._events if e["published_at"] is None][:limit]

    def mark_events(self, ids: list[int], published: bool) -> None:
        with self._lock:
            for e in self._events:
                if e["id"] in ids:
                    e["attempts"] += 1
                    if published:
                        e["published_at"] = datetime.now(timezone.utc)

    def event_counts(self) -> dict[str, int]:
        with self._lock:
            done = sum(1 for e in self._events if e["published_at"])
            return {"published": done, "pending": len(self._events) - done}

    def set_fingerprints(self, case_id: str, owner_hash: str, items: list[tuple[str, str]]) -> None:
        with self._lock:
            self._prints = [p for p in self._prints if p["case_id"] != case_id]
            now = datetime.now(timezone.utc)
            self._prints += [{"case_id": case_id, "kind": k, "value_hash": v, "owner_hash": owner_hash,
                              "created_at": now} for k, v in items]

    def match_fingerprints(self, case_id: str, items: list[tuple[str, str]]) -> list[dict[str, Any]]:
        wanted = set(items)
        with self._lock:
            return [dict(p) for p in self._prints
                    if p["case_id"] != case_id and (p["kind"], p["value_hash"]) in wanted]


# --------------------------------------------------------------------------- audit chain helpers

def chain_hash(seq: int, at_iso: str, actor: str, role: str, action: str, resource: str, detail_hash: str,
               prev: str) -> str:
    material = "|".join([str(seq), at_iso, actor, role, action, resource, detail_hash, prev])
    return hashlib.sha256(material.encode()).hexdigest()


def build_audit_event(seq: int, prev: str, actor: str, role: str, action: str, resource: str,
                      detail: dict[str, object]) -> AuditEvent:
    at = datetime.now(timezone.utc)
    detail_hash = hashlib.sha256(json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()
    event_hash = chain_hash(seq, at.isoformat(), actor, role, action, resource, detail_hash, prev)
    return AuditEvent(seq=seq, at=at, actor=actor, role=role, action=action, resource=resource,
                      detail_hash=detail_hash, prev_hash=prev, event_hash=event_hash)


def first_broken_link(events: Iterable[Any]) -> int | None:
    """Sequence number of the first audit event that does not chain from the one before, or None.
    `at` is a datetime in memory and the stored ISO text in SQL; both hash the same."""
    prev, expected_seq = GENESIS_HASH, 1
    for e in events:
        at = e.at if isinstance(e.at, str) else e.at.isoformat()
        expected = chain_hash(e.seq, at, e.actor, e.role, e.action, e.resource, e.detail_hash, prev)
        if e.seq != expected_seq or e.prev_hash != prev or e.event_hash != expected:
            return e.seq
        prev, expected_seq = e.event_hash, expected_seq + 1
    return None


_store: CaseStore | None = None
_store_lock = threading.Lock()


def get_store() -> CaseStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                from app.config import get_settings

                settings = get_settings()
                if settings.database_url:
                    from app.db import get_engine
                    from app.domain.sql_store import SqlStore

                    _store = SqlStore(get_engine())
                else:
                    _store = InMemoryStore(settings.max_cases_in_memory)
    return _store
