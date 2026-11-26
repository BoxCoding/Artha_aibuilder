"""Long statements (up to 40 pages) are read by the model in page chunks and joined in page order."""
from __future__ import annotations

import asyncio

from app.ai import document_ai
from app.ai.gateway import UsageMeter
from app.ai.schemas import ExtractedTxn, StatementExtraction
from app.documents.universal_reader import read_universal, reconciles
from tests.icici_like import build


class ChunkReadingGateway:
    """Stands in for the model: reads whatever PDF chunk it is given, page numbers relative to that chunk."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    async def structured(self, *, prompt_id, schema, contents, meter, model=None, thinking=None, attempts=2):  # type: ignore[no-untyped-def]
        parsed = read_universal(contents[0].data)
        self.calls.append(parsed.page_count)
        return StatementExtraction(
            bank_name="ICICI", account_holder="TEST", account_last4="0001", period_from=None, period_to=None,
            opening_balance=None,
            transactions=[ExtractedTxn(page=r.page, date=r.txn_date.isoformat(), narration=r.narration, reference="",
                                       debit=str(r.debit), credit=str(r.credit), balance=str(r.balance))
                          for r in parsed.rows])


def test_a_forty_page_statement_is_read_in_chunks_and_joined_in_order() -> None:
    gateway = ChunkReadingGateway()
    parsed = asyncio.run(document_ai.extract_with_gemini(build(40), gateway, UsageMeter(budget=10**9)))  # type: ignore[arg-type]
    assert gateway.calls == [document_ai.CHUNK_PAGES] * (40 // document_ai.CHUNK_PAGES)
    assert len(parsed.rows) == 440 and not parsed.needs_ai
    assert [r.page for r in parsed.rows] == sorted(r.page for r in parsed.rows) and parsed.rows[-1].page == 40
    assert [r.row_index for r in parsed.rows] == list(range(440))
    assert reconciles(parsed.rows, parsed.opening_balance)


def test_a_short_statement_is_one_call() -> None:
    gateway = ChunkReadingGateway()
    parsed = asyncio.run(document_ai.extract_with_gemini(build(3), gateway, UsageMeter(budget=10**9)))  # type: ignore[arg-type]
    assert gateway.calls == [3] and len(parsed.rows) == 33


def test_uploads_up_to_forty_pages_are_accepted() -> None:
    import pytest
    from fastapi import HTTPException

    from app.api.security import validate_upload
    from app.config import get_settings

    settings = get_settings()
    assert validate_upload("statement.pdf", build(40), settings, None).page_count == 40
    with pytest.raises(HTTPException):
        validate_upload("statement.pdf", build(41), settings, None)
