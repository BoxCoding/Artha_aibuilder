"""Cross-application (portfolio) fraud checks.

A forger who edits one statement is caught by the per-file rules. A forger who reuses one genuine
statement for several applicants, or runs many applications through one bank account, is not: each
file looks clean on its own. These checks compare every new application with every earlier one.

Identifiers are compared as keyed hashes (HMAC-SHA256 with the service secret), so the fingerprints
table can be read without revealing a PAN, a mobile number or an account.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.domain.models import Case, Evidence, Signal

VELOCITY_WINDOW_DAYS = 30
VELOCITY_LIMIT = 3  # this many earlier applications by the same PAN in the window is a finding


def _h(kind: str, value: str) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    return hmac.new(key, f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()


def _norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def fingerprint(case: Case) -> tuple[str, list[tuple[str, str]]]:
    applicant = case.applicant
    owner = _h("owner", applicant.pan if applicant else case.owner_uid)
    items: list[tuple[str, str]] = []
    for doc in case.documents:
        items.append(("doc", _h("doc", doc.source_sha256 or doc.sha256)))
        if doc.header.account_last4 and doc.header.bank_name:
            items.append(("account", _h("account", f"{_norm(doc.header.bank_name)}|{doc.header.account_last4}|"
                                                   f"{_norm(doc.header.account_holder)}")))
    if applicant:
        items.append(("pan", _h("pan", applicant.pan)))
        items.append(("mobile", _h("mobile", applicant.mobile)))
    return owner, sorted(set(items))


def network_signals(case: Case, store) -> list[Signal]:  # type: ignore[no-untyped-def]
    owner, items = fingerprint(case)
    store.set_fingerprints(case.case_id, owner, items)
    matches = store.match_fingerprints(case.case_id, items)
    if not matches:
        return []

    def others(kind: str, same_owner: bool) -> set[str]:
        return {m["case_id"] for m in matches if m["kind"] == kind and (m["owner_hash"] == owner) == same_owner}

    ev_id = "ev-network"
    if case.documents:
        case.evidence[ev_id] = Evidence(evidence_id=ev_id, doc_id=case.documents[0].doc_id, page=1, kind="COMPUTED",
                                        description="Cross-application comparison with earlier applications")
    anchor = [ev_id] if case.documents else []
    header = [f"ev-{d.doc_id}-header" for d in case.documents if f"ev-{d.doc_id}-header" in case.evidence]
    signals: list[Signal] = []

    reused = others("doc", same_owner=False)
    if reused:
        signals.append(Signal(
            signal_id="sig-fr-xapp-001-0", code="FR-XAPP-001", category="FRAUD", severity="HIGH", confidence=0.9,
            detector="RULE", corroborated=True, evidence_ids=(header or anchor),
            title="Same statement file in another applicant's application",
            explanation=(f"The identical statement file was submitted in {len(reused)} other application(s) by a "
                         "different applicant. A genuine statement belongs to one account holder.")))
    shared_account = others("account", same_owner=False)
    if shared_account:
        signals.append(Signal(
            signal_id="sig-fr-xapp-002-0", code="FR-XAPP-002", category="FRAUD", severity="MEDIUM", confidence=0.65,
            detector="RULE", corroborated=True, evidence_ids=(header or anchor),
            title="Bank account used by another applicant",
            explanation=(f"The same bank account appears in {len(shared_account)} application(s) from a different "
                         "PAN. Family businesses sometimes share accounts; confirm who operates it.")))
    shared_mobile = others("mobile", same_owner=False)
    if shared_mobile:
        signals.append(Signal(
            signal_id="sig-fr-xapp-003-0", code="FR-XAPP-003", category="FRAUD", severity="MEDIUM", confidence=0.55,
            detector="RULE", corroborated=True, evidence_ids=anchor or header,
            title="Mobile number used by another applicant",
            explanation=(f"This mobile number was given in {len(shared_mobile)} application(s) under a different PAN. "
                         "Agents sometimes enter their own number; confirm the applicant's contact details.")))
    cutoff = datetime.now(timezone.utc) - timedelta(days=VELOCITY_WINDOW_DAYS)
    recent = {m["case_id"] for m in matches if m["kind"] == "pan" and _aware(m["created_at"]) >= cutoff}
    if len(recent) >= VELOCITY_LIMIT:
        signals.append(Signal(
            signal_id="sig-fr-vel-001-0", code="FR-VEL-001", category="RISK", severity="MEDIUM", confidence=0.6,
            detector="RULE", corroborated=True, evidence_ids=anchor or header,
            title="Many applications in a short time",
            explanation=(f"This PAN has {len(recent)} other applications in the last {VELOCITY_WINDOW_DAYS} days. "
                         "Check for loan stacking or repeated attempts with different documents.")))
    return [s for s in signals if s.evidence_ids]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
