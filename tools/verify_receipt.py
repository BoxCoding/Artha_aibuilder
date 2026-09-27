#!/usr/bin/env python3
"""Verify an ARTHA decision receipt, independently of ARTHA.

This file imports nothing from the application on purpose. Anyone holding a receipt and the
issuer's public key can check it offline, on any machine, without an account, an API call, or
any cooperation from the lender. That is the whole point: a receipt you can only verify by asking
the issuer whether it is genuine is not evidence.

    python tools/verify_receipt.py receipt.json
    python tools/verify_receipt.py receipt.json --public-key <base64>

Exit code 0 means valid. Requires only the `cryptography` package.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys

RECEIPT_VERSION = "artha-receipt.v1"


def canonical_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify(receipt: dict, public_key_b64: str | None) -> tuple[bool, str]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric import ed25519

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


def describe(payload: dict) -> str:
    decision, basis, evidence = payload["decision"], payload["basis"], payload["evidence"]
    lines = [
        f"  Applicant        {payload['case']['business_name']} ({payload['case']['case_id']})",
        f"  Requested        {payload['case']['requested_amount']} over {payload['case']['tenure_months']} months",
        f"  Outcome          {decision['outcome']}"
        + (f", {decision['approved_amount']} approved" if decision.get("approved_amount") else ""),
        f"  Recommended      {decision['system_recommendation']}"
        + ("  [underwriter overrode it]" if decision["overrode_the_recommendation"] else ""),
        f"  Decided          {decision['decided_at']} by a human"
        + (", countersigned" if decision["second_approver_present"] else ""),
        f"  Basis            band {basis['risk_band']}, policy {basis['policy_version']}, "
        f"scorecard {basis['scorecard_version']}",
        f"  Reason codes     {', '.join(basis['reason_codes']) or 'none'}",
        f"  Documents        {len(evidence['documents'])}, referenced by SHA-256 digest only",
        f"  Memo claims      {evidence['memo_claims_published']} published, "
        f"{evidence['memo_claims_removed_by_gate']} removed by the groundedness gate",
        f"  Audit chain      head {payload['audit']['chain_head'][:16]}…, "
        f"{payload['audit']['events_recorded']} events",
    ]
    if payload["integrity_findings"]:
        codes = ", ".join(f"{f['code']}({f['severity'].lower()})" for f in payload["integrity_findings"])
        lines.append(f"  Integrity        {codes}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("receipt", help="path to the receipt JSON file, or - for stdin")
    parser.add_argument("--public-key", help="base64 Ed25519 public key; defaults to the one inside the receipt")
    parser.add_argument("--quiet", action="store_true", help="print nothing; use the exit code")
    args = parser.parse_args()

    raw = sys.stdin.read() if args.receipt == "-" else open(args.receipt, encoding="utf-8").read()
    receipt = json.loads(raw)
    ok, reason = verify(receipt, args.public_key)

    if args.quiet:
        return 0 if ok else 1
    if ok:
        print(f"VALID  signed by key {receipt.get('key_id', '?')}\n")
        print(describe(receipt["payload"]))
        if not args.public_key:
            print("\nNote: verified against the key embedded in the receipt. To prove it came from the"
                  "\nlender you expect, re-run with --public-key from their published key endpoint.")
    else:
        print(f"INVALID  {reason}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
