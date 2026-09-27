"""Portable decision receipts.

A lending decision today is a black box that lives inside the lender. The applicant is told an
outcome and has nothing they can carry anywhere: not to a second lender, not to an ombudsman, not
to a court. If the lender's records later disagree with what the applicant was told, the applicant
has no way to prove anything.

A receipt fixes that. It is a compact, canonical JSON record of what was decided, on what basis,
against which documents, by whom, signed with the issuer's Ed25519 key. It can be verified by
anyone holding the public key, offline, with fifteen lines of Python and no access to our systems
— `tools/verify_receipt.py` does exactly that and imports nothing from this application.

Two design rules make it safe to hand out:

* **No personal data beyond what the holder already knows.** Documents appear as SHA-256 digests,
  never content. There are no transaction rows, no account numbers, no narrations. A receipt that
  leaked would tell an attacker nothing they could use.
* **It commits the lender, not the borrower.** It records the model and policy versions in force,
  the audit chain head at the moment of decision, and how many memo claims the groundedness gate
  removed. Those are the facts a lender would prefer to forget later, which is exactly why they
  belong in the applicant's copy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.domain.models import Case

log = logging.getLogger("artha.receipt")

RECEIPT_VERSION = "artha-receipt.v1"


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Byte-exact serialisation, so an independent verifier reproduces the same digest.

    Sorted keys, no insignificant whitespace, UTF-8, no ASCII escaping. Any change to this
    function is a breaking change to every receipt ever issued, which is why the version string
    is part of the payload.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class SignedReceipt:
    payload: dict[str, Any]
    payload_sha256: str
    signature_b64: str
    public_key_b64: str
    key_id: str

    def as_dict(self) -> dict[str, Any]:
        return {"payload": self.payload, "payload_sha256": self.payload_sha256,
                "signature": self.signature_b64, "public_key": self.public_key_b64, "key_id": self.key_id}


class ReceiptSigner:
    """Holds the issuing key. In production the seed comes from Secret Manager; in development an
    ephemeral key is generated and the fact is logged loudly, because a receipt signed by a key
    that disappears on restart is worthless."""

    def __init__(self, seed_b64: str | None = None) -> None:
        if seed_b64:
            seed = base64.b64decode(seed_b64)
            if len(seed) != 32:
                raise ValueError("receipt signing seed must be 32 bytes, base64 encoded")
            self._key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
            self.ephemeral = False
        else:
            self._key = ed25519.Ed25519PrivateKey.generate()
            self.ephemeral = True
            log.warning("receipt_signing_key_ephemeral",
                        extra={"detail": "no RECEIPT_SIGNING_SEED configured; receipts will not verify after restart"})

    @property
    def public_key_b64(self) -> str:
        raw = self._key.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                                  format=serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode("ascii")

    @property
    def key_id(self) -> str:
        return hashlib.sha256(self.public_key_b64.encode("ascii")).hexdigest()[:16]

    def sign(self, payload: dict[str, Any]) -> SignedReceipt:
        body = canonical_bytes(payload)
        signature = self._key.sign(body)
        return SignedReceipt(payload=payload, payload_sha256=hashlib.sha256(body).hexdigest(),
                             signature_b64=base64.b64encode(signature).decode("ascii"),
                             public_key_b64=self.public_key_b64, key_id=self.key_id)


def verify(receipt: dict[str, Any], public_key_b64: str | None = None) -> tuple[bool, str]:
    """Verify a receipt. Returns (ok, reason). Pure function: no application state is consulted."""
    try:
        payload = receipt["payload"]
        signature = base64.b64decode(receipt["signature"])
        key_b64 = public_key_b64 or receipt["public_key"]
        key = ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(key_b64))
    except (KeyError, ValueError, TypeError) as exc:
        return False, f"malformed receipt: {type(exc).__name__}"

    body = canonical_bytes(payload)
    if hashlib.sha256(body).hexdigest() != receipt.get("payload_sha256"):
        return False, "payload digest does not match the receipt body"
    try:
        key.verify(signature, body)
    except InvalidSignature:
        return False, "signature does not verify against this public key"
    if payload.get("receipt_version") != RECEIPT_VERSION:
        return False, f"unknown receipt version {payload.get('receipt_version')!r}"
    return True, "valid"


def build_payload(case: Case, audit_head: str, audit_events: int) -> dict[str, Any]:
    """Assemble the receipt body. Everything here is either public to the applicant already or a
    digest; no transaction content, no account numbers, no narrations."""
    decision = case.decision
    if decision is None:
        raise ValueError("a receipt can only be issued for a decided case")

    memo = case.memo
    cited = {e for section in (memo.sections if memo else []) for claim in section.claims
             for e in claim.evidence_ids}
    challenge = case.model_challenge or {}

    return {
        "receipt_version": RECEIPT_VERSION,
        "issued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "issuer": "ARTHA prototype (synthetic data)",
        "case": {
            "case_id": case.case_id,
            "business_name": case.business_name,
            "requested_amount": str(case.requested_amount),
            "tenure_months": case.tenure_months,
            "jurisdiction": case.jurisdiction,
            "consent_notice_version": case.consent_notice_version,
        },
        "decision": {
            "outcome": decision.final_outcome.value,
            "system_recommendation": decision.system_recommendation.value,
            "approved_amount": str(decision.approved_amount) if decision.approved_amount else None,
            "conditions": list(decision.conditions),
            "decided_at": decision.decided_at.isoformat(timespec="seconds"),
            "overrode_the_recommendation": decision.override,
            "override_reason_recorded": bool(decision.reason),
            "second_approver_present": bool(decision.second_approver),
            "decided_by_a_human": True,
        },
        "basis": {
            "policy_id": case.policy.policy_id if case.policy else None,
            "policy_version": case.policy.policy_version if case.policy else None,
            "scorecard_version": case.score.scorecard_version if case.score else None,
            "challenger_model_version": challenge.get("version"),
            "risk_band": case.score.band if case.score else None,
            "probability_of_default": case.score.pd if case.score else None,
            "reason_codes": list(case.policy.reason_codes) if case.policy else [],
            "policy_clauses_applied": list(case.policy.triggered_rules) if case.policy else [],
            "models_disagreed": bool(challenge.get("disagrees")),
        },
        "evidence": {
            "documents": sorted(
                ({"filename": d.filename, "sha256": d.sha256, "pages": d.page_count, "read_by": d.parser}
                 for d in case.documents), key=lambda d: d["sha256"]),
            "evidence_records": len(case.evidence),
            "transactions_read": len(case.transactions),
            "memo_claims_published": sum(len(s.claims) for s in memo.sections) if memo else 0,
            "memo_claims_removed_by_gate": len(memo.dropped_claims) if memo else 0,
            "evidence_items_cited": len(cited),
        },
        "integrity_findings": sorted(
            ({"code": s.code, "severity": s.severity, "corroborated": s.corroborated}
             for s in case.signals if s.category == "FRAUD"), key=lambda s: s["code"]),
        "audit": {"chain_head": audit_head, "events_recorded": audit_events},
        "privacy": {
            "contains_transaction_data": False,
            "contains_account_numbers": False,
            "documents_referenced_by_digest_only": True,
        },
    }
