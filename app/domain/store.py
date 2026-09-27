"""Case repository.

The prototype ships an in-memory implementation (single Cloud Run instance, synthetic data,
nightly reset). The interface is deliberately small so a Firestore + Cloud Storage adapter can
replace it without touching business code - see docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from app.domain.models import AuditEvent, Case, PipelineEvent


class CaseNotFound(KeyError):
    pass


class InMemoryStore:
    def __init__(self, max_cases: int = 300) -> None:
        self._lock = threading.RLock()
        self._cases: OrderedDict[str, Case] = OrderedDict()
        self._files: dict[str, bytes] = {}
        self._audit: list[AuditEvent] = []
        self._callbacks: list[dict[str, str]] = []
        self._protected: set[str] = set()
        self._max = max_cases

    # ------------------------------------------------------------------ cases
    def add(self, case: Case, protected: bool = False) -> None:
        with self._lock:
            self._cases[case.case_id] = case
            if protected:
                self._protected.add(case.case_id)
            while len(self._cases) > self._max:
                victim = next((cid for cid in self._cases if cid not in self._protected), None)
                if victim is None:
                    break
                old = self._cases.pop(victim)
                for d in old.documents:
                    self._files.pop(f"{victim}/{d.doc_id}", None)

    def replace(self, case: Case) -> None:
        with self._lock:
            if case.case_id not in self._cases:
                raise CaseNotFound(case.case_id)
            self._cases[case.case_id] = case

    def get(self, case_id: str) -> Case:
        with self._lock:
            try:
                return self._cases[case_id]
            except KeyError as exc:
                raise CaseNotFound(case_id) from exc

    @contextmanager
    def mutate(self, case_id: str) -> Iterator[Case]:
        with self._lock:
            case = self.get(case_id)
            yield case
            case.updated_at = datetime.now(timezone.utc)

    def list(self) -> list[Case]:
        with self._lock:
            return list(self._cases.values())

    def add_event(self, case_id: str, event: PipelineEvent) -> None:
        with self._lock:
            self.get(case_id).events.append(event)

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

    # ------------------------------------------------------------------ audit (hash-chained, append-only)
    def audit(self, actor: str, role: str, action: str, resource: str, detail: dict[str, object]) -> AuditEvent:
        with self._lock:
            prev = self._audit[-1].event_hash if self._audit else "0" * 64
            at = datetime.now(timezone.utc)
            detail_hash = hashlib.sha256(json.dumps(detail, sort_keys=True, default=str).encode()).hexdigest()
            seq = len(self._audit) + 1
            event_hash = _chain_hash(seq, at, actor, role, action, resource, detail_hash, prev)
            event = AuditEvent(seq=seq, at=at, actor=actor, role=role, action=action, resource=resource,
                               detail_hash=detail_hash, prev_hash=prev, event_hash=event_hash)
            self._audit.append(event)
            return event

    def audit_head(self) -> tuple[str, int]:
        """The last hash in the chain and how many events it covers - what a receipt commits to."""
        with self._lock:
            return (self._audit[-1].event_hash if self._audit else "0" * 64), len(self._audit)

    def delete_case(self, case_id: str) -> bool:
        """Erase a case and its documents. Used for the data-deletion right and by retention."""
        with self._lock:
            case = self._cases.pop(case_id, None)
            if case is None:
                return False
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
            prev = "0" * 64
            for e in self._audit:
                expected = _chain_hash(e.seq, e.at, e.actor, e.role, e.action, e.resource, e.detail_hash, prev)
                if e.prev_hash != prev or e.event_hash != expected:
                    return False
                prev = e.event_hash
            return True


def _chain_hash(seq: int, at: datetime, actor: str, role: str, action: str, resource: str, detail_hash: str,
                prev: str) -> str:
    material = "|".join([str(seq), at.isoformat(), actor, role, action, resource, detail_hash, prev])
    return hashlib.sha256(material.encode()).hexdigest()


_store: InMemoryStore | None = None


def get_store() -> InMemoryStore:
    global _store
    if _store is None:
        from app.config import get_settings

        _store = InMemoryStore(get_settings().max_cases_in_memory)
    return _store
