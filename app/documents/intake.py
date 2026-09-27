"""Statement intake: password-protected PDFs, and reading a statement the moment it is uploaded.

Most Indian banks email statements as password-protected PDFs (the password is usually derived from
the customer's name, date of birth or customer ID). The customer or officer types it once at upload.
We decrypt immediately, store the decrypted file, and never store the password. The forensic
properties of the *original* file (producer, dates, re-save count, SHA-256) are captured before
decryption, because re-writing the file would otherwise erase exactly the traces the metadata rule
looks for.

`read_deterministic` is the one place that decides which reader handles a statement: the template
reader for the known synthetic layout, then the universal reader for real bank layouts.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass, field

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from app.documents.pdf_reader import ParsedStatement, inspect_structure, read_statement
from app.documents.universal_reader import read_universal

RECOVERABLE_BY_MODEL = {"unknown_layout", "no_transactions_found", "balances_do_not_reconcile"}


class IntakeError(ValueError):
    """A problem with the uploaded file whose message is safe to show to the uploader."""

    def __init__(self, message: str, *, needs_password: bool = False) -> None:
        super().__init__(message)
        self.needs_password = needs_password


@dataclass
class PreparedFile:
    data: bytes
    page_count: int
    decrypted: bool = False
    source_sha256: str = ""
    source_metadata: dict[str, str] = field(default_factory=dict)
    source_incremental_updates: int = 0


def prepare(data: bytes, password: str | None) -> PreparedFile:
    """Open the PDF, decrypting it with the supplied password if it is protected."""
    source_sha = hashlib.sha256(data).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(data))
    except (PdfReadError, ValueError, OSError) as exc:
        raise IntakeError("This PDF could not be opened.") from exc
    if not reader.is_encrypted:
        return PreparedFile(data=data, page_count=len(reader.pages), source_sha256=source_sha)
    if not password:
        raise IntakeError("This statement is password protected. Enter the PDF password your bank gave you "
                          "(often based on your name, date of birth or customer ID).", needs_password=True)
    try:
        ok = reader.decrypt(password)
    except Exception as exc:  # noqa: BLE001 - unsupported cipher, malformed encryption dictionary
        raise IntakeError("This PDF uses encryption we cannot open. Download it again from net banking.") from exc
    if not ok:
        raise IntakeError("That password did not open the statement. Check it and try again.", needs_password=True)
    metadata = {str(k).lstrip("/"): str(v)[:200] for k, v in (reader.metadata or {}).items()}
    writer = PdfWriter(clone_from=reader)
    buf = io.BytesIO()
    writer.write(buf)
    return PreparedFile(data=buf.getvalue(), page_count=len(reader.pages), decrypted=True, source_sha256=source_sha,
                        source_metadata=metadata, source_incremental_updates=max(0, data.count(b"startxref") - 1))


def read_deterministic(data: bytes) -> tuple[ParsedStatement, str]:
    """Template reader first (exact for the layout it knows), then the universal reader."""
    parsed = read_statement(data)
    if not parsed.needs_ai:
        return parsed, "template"
    if parsed.reason in ("unknown_layout", "no_transactions_found"):
        universal = read_universal(data)
        return universal, "universal"
    return parsed, "template"


def preview(data: bytes) -> dict[str, object]:
    """What the uploader sees immediately: which bank, whose account, which period, how many rows."""
    parsed, reader = read_deterministic(data)
    credits = sum(r.credit for r in parsed.rows)
    debits = sum(r.debit for r in parsed.rows)
    return {
        "readable": not parsed.needs_ai,
        "reason": parsed.reason,
        "reader": reader,
        "bank": parsed.bank_name,
        "account_holder": parsed.account_holder,
        "account_last4": parsed.account_last4,
        "period_from": parsed.period_from.isoformat() if parsed.period_from else None,
        "period_to": parsed.period_to.isoformat() if parsed.period_to else None,
        "transactions": len(parsed.rows) if not parsed.needs_ai else 0,
        "total_credits": f"{credits:.2f}" if not parsed.needs_ai else None,
        "total_debits": f"{debits:.2f}" if not parsed.needs_ai else None,
        "pages": parsed.page_count,
    }


def unreadable_message(reason: str | None, *, model_available: bool, vision_available: bool) -> str | None:
    """None if the pipeline can still read the file (with a model); otherwise what to tell the uploader."""
    if reason == "no_text_layer":
        if vision_available:
            return None
        return ("This looks like a scanned or photographed statement. Please upload the PDF downloaded from your "
                "net banking or the bank's email - scans cannot be read automatically in this deployment.")
    if reason in RECOVERABLE_BY_MODEL:
        if model_available:
            return None
        if reason == "balances_do_not_reconcile":
            return ("We could read this statement, but its running balances do not add up consistently, so it "
                    "cannot be analysed automatically. Upload the original PDF from net banking.")
        return ("We could not find a transaction table in this statement. Upload the statement PDF downloaded "
                "from net banking; if it still fails, an officer can review it manually.")
    return None


def inspect(data: bytes) -> tuple[dict[str, str], int]:
    meta, incremental, _js, _enc, _pages = inspect_structure(data)
    return meta, incremental
