"""Real-world statement intake, customer details, the loan-officer channel, administration and the
local-model provider."""
from __future__ import annotations

import asyncio
import io
import time
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from app.api.main import app
from app.documents.pdf_reader import ParsedRow
from app.documents.universal_reader import parse_date, parse_money, read_universal, reconciles
from app.domain.kyc import ApplicantProfile, valid_gstin
from app.domain.models import Role
from app.domain.users import get_accounts
from tests.bank_layouts import LAYOUTS, make_transactions, render, statement
from tests.fixtures import APPLICANT

STRONG = "correct horse battery staple"


def staff(role: Role) -> TestClient:
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@lender.example"
    get_accounts().register(email, f"Test {role.value}", STRONG, role=role)
    client = TestClient(app)
    assert client.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    return client


def customer() -> TestClient:
    client = TestClient(app)
    email = f"cust-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post("/api/register", json={"email": email, "name": "Meena Devi", "password": STRONG}).status_code == 201
    return client


def new_case(client: TestClient, applicant: dict | None = None, amount: int = 200000) -> str:
    response = client.post("/api/cases", json={"applicant": applicant or APPLICANT, "requested_amount": amount,
                                               "tenure_months": 24, "language": "en", "consent": True})
    assert response.status_code == 201, response.text
    return response.json()["case_id"]


def wait_until_processed(client: TestClient, case_id: str) -> dict:
    for _ in range(160):
        body = client.get(f"/api/cases/{case_id}").json()
        if body["status"] not in ("PROCESSING", "DRAFT"):
            return body
        time.sleep(0.25)
    raise AssertionError("pipeline did not finish")


# --------------------------------------------------------------------------- universal reader

@pytest.mark.parametrize("layout", LAYOUTS)
def test_universal_reader_reads_real_world_layouts_exactly(layout: str) -> None:
    data, truth, opening = statement(layout)
    parsed = read_universal(data)
    assert not parsed.needs_ai, parsed.reason
    assert len(parsed.rows) == len(truth)
    for row, txn in zip(parsed.rows, truth):
        assert (row.txn_date, row.debit, row.credit, row.balance) == (txn.day, txn.debit, txn.credit, txn.balance)
    assert parsed.opening_balance == opening
    assert parsed.account_holder == "PRIYA NAIR" and parsed.account_last4 == "7891"
    assert parsed.period_from == truth[0].day and parsed.period_to == truth[-1].day
    assert all(r.amount_bbox and r.amount_font for r in parsed.rows)  # forensic rules need these


def test_wrapped_narrations_are_rejoined() -> None:
    data, truth, _ = statement("hdfc_like")
    parsed = read_universal(data)
    long_ones = [(r, t) for r, t in zip(parsed.rows, truth) if len(t.narration) > 45]
    assert long_ones
    for row, txn in long_ones:
        assert row.narration.replace(" ", "") == txn.narration.replace(" ", "")


def test_a_tampered_real_layout_is_still_caught() -> None:
    """A deposit typed over without repairing the balances must break reconciliation - and the reader
    must still accept the file, because one break is exactly what forensics exists to find."""
    from app.documents.ingest import attach_parsed_statement
    from app.domain.models import Case
    from app.forensics.rules import run_rules

    opening = Decimal("84500.00")
    rows = make_transactions(11, 70, opening)
    victim = next(i for i, t in enumerate(rows) if t.credit and i > 10)
    rows[victim].credit += Decimal("150000.00")  # the balance column is left as issued
    data = render("sbi_like", rows, opening)
    parsed = read_universal(data)
    assert not parsed.needs_ai
    case = Case(case_id="c-tamper", owner_uid="u", business_name="X", requested_amount=Decimal("300000"),
                tenure_months=24)
    attach_parsed_statement(case, "d1", "s.pdf", data, parsed, "universal")
    codes = {s.code for s in run_rules(case)}
    assert "FR-BAL-001" in codes


def test_a_misread_is_refused_rather_than_accused() -> None:
    rows = [ParsedRow(page=1, row_index=i, txn_date=date(2026, 4, 1 + i), narration="x", reference="",
                      debit=Decimal("0"), credit=Decimal("100.00"), balance=Decimal(str(1000 + i * 37)),
                      row_bbox=[0, 0, 0, 0], amount_bbox=None, amount_font=None, amount_size=None, balance_font=None,
                      balance_size=None) for i in range(20)]
    assert not reconciles(rows, Decimal("1000"))


@pytest.mark.parametrize("text,expected", [("01/04/26", date(2026, 4, 1)), ("1 Apr 2026", date(2026, 4, 1)),
                                           ("01-Apr-2026", date(2026, 4, 1)), ("01.04.2026", date(2026, 4, 1)),
                                           ("2026-04-01", date(2026, 4, 1)), ("31/13/2026", None)])
def test_date_formats(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize("text,expected", [("1,23,456.78", (Decimal("123456.78"), None)),
                                           ("5,000.00Cr", (Decimal("5000.00"), "cr")),
                                           ("5,000.00(Dr)", (Decimal("5000.00"), "dr")),
                                           ("(250.00)", (Decimal("-250.00"), None)),
                                           ("000012345", None), ("CR", None)])
def test_money_formats(text: str, expected: tuple | None) -> None:
    assert parse_money(text) == expected


# --------------------------------------------------------------------------- password-protected uploads

def _encrypted(data: bytes, password: str) -> bytes:
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(password, algorithm="AES-256")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_password_protected_statements_are_decrypted_and_previewed() -> None:
    import hashlib

    from app.domain.store import get_store

    client = customer()
    case_id = new_case(client)
    plain, truth, _ = statement("hdfc_like")
    locked = _encrypted(plain, "PRIY1990")
    files = {"file": ("statement.pdf", locked, "application/pdf")}
    missing = client.post(f"/api/cases/{case_id}/documents", files=files)
    assert missing.status_code == 422 and "password" in missing.json()["detail"].lower()
    wrong = client.post(f"/api/cases/{case_id}/documents", files=files, data={"password": "nope"})
    assert wrong.status_code == 422
    right = client.post(f"/api/cases/{case_id}/documents", files=files, data={"password": "PRIY1990"})
    assert right.status_code == 200, right.text
    preview = right.json()["preview"]
    assert preview["readable"] and preview["transactions"] == len(truth)
    assert preview["account_holder"] == "PRIYA NAIR" and preview["account_last4"] == "7891"
    doc = get_store().get(case_id).documents[0]
    assert doc.decrypted and doc.source_sha256 == hashlib.sha256(locked).hexdigest()


def test_scans_are_refused_at_upload_without_a_vision_model() -> None:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(50, 50, 400, 600)  # a page with no text layer, like a scan
    c.showPage()
    c.save()
    client = customer()
    case_id = new_case(client)
    response = client.post(f"/api/cases/{case_id}/documents", files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")})
    assert response.status_code == 422 and "scanned" in response.json()["detail"]


def test_documents_can_be_removed_before_submission() -> None:
    client = customer()
    case_id = new_case(client)
    data, _, _ = statement("icici_like")
    doc_id = client.post(f"/api/cases/{case_id}/documents", files={"file": ("s.pdf", data, "application/pdf")}).json()["doc_id"]
    assert client.delete(f"/api/cases/{case_id}/documents/{doc_id}").status_code == 200
    assert client.get(f"/api/cases/{case_id}").json()["documents"] == []


# --------------------------------------------------------------------------- customer details

def test_customer_details_are_validated() -> None:
    client = customer()
    for field, bad in (("pan", "ABC1234"), ("mobile", "12345"), ("pincode", "012345"), ("gstin", "29ABCDE1234F1Z0"),
                       ("udyam_number", "UDYAM-12")):
        body = {"applicant": APPLICANT | {field: bad}, "requested_amount": 200000, "tenure_months": 24,
                "consent": True}
        assert client.post("/api/cases", json=body).status_code == 422, field
    firm_pan = APPLICANT | {"pan": "ABCFD1234E"}  # a firm PAN for a proprietorship
    assert client.post("/api/cases", json={"applicant": firm_pan, "requested_amount": 200000, "tenure_months": 24,
                                           "consent": True}).status_code == 422


def test_gstin_checksum_and_consistency_warnings() -> None:
    assert valid_gstin("27AAPFU0939F1ZV") and not valid_gstin("27AAPFU0939F1ZX")
    profile = ApplicantProfile(**(APPLICANT | {"constitution": "partnership", "pan": "AAPFU0939F",
                                              "gstin": "27AAPFU0939F1ZV"}))
    assert any("Maharashtra" in w for w in profile.warnings())  # registered in MH, address in Karnataka
    assert profile.masked()["pan"] == "AAXXX0939F"


# --------------------------------------------------------------------------- loan officer channel

def test_loan_officer_onboards_a_customer_and_sees_the_instant_recommendation() -> None:
    officer = staff(Role.LOAN_OFFICER)
    applicant = APPLICANT | {"full_name": "Priya Nair", "business_name": "Nair Tiffin Services"}
    case_id = new_case(officer, applicant)
    data, _, _ = statement("kotak_like", seed=21, count=140)
    upload = officer.post(f"/api/cases/{case_id}/documents", files={"file": ("kotak.pdf", data, "application/pdf")})
    assert upload.status_code == 200 and upload.json()["preview"]["bank"] == "Kotak Mahindra Bank"
    assert officer.post(f"/api/cases/{case_id}/submit").status_code == 200
    body = wait_until_processed(officer, case_id)
    assert body["status"] == "READY_FOR_REVIEW", body.get("failure_reason")
    assert body["channel"] == "assisted" and body["policy"]["outcome"]
    assert body["documents"][0]["parser"] == "universal"
    mine = officer.get("/api/my/cases").json()["cases"]
    row = next(r for r in mine if r["case_id"] == case_id)
    assert row["recommendation"] == body["policy"]["outcome"] and row["band"]
    # Officers cannot sanction, and cannot see other officers' customers or the credit queue.
    assert officer.post(f"/api/cases/{case_id}/decision", json={"outcome": "APPROVE"}).status_code == 403
    assert officer.get("/api/queue").status_code == 403
    assert staff(Role.LOAN_OFFICER).get(f"/api/cases/{case_id}").status_code == 404
    # A credit officer sees it in the queue and decides it.
    credit = staff(Role.UNDERWRITER)
    assert any(c["case_id"] == case_id for c in credit.get("/api/queue").json()["cases"])


def test_a_statement_in_someone_elses_name_triggers_verification_not_decline() -> None:
    officer = staff(Role.LOAN_OFFICER)
    case_id = new_case(officer, APPLICANT)  # applicant Meena Devi; the statement belongs to PRIYA NAIR
    data, _, _ = statement("icici_like", seed=5, count=140)
    officer.post(f"/api/cases/{case_id}/documents", files={"file": ("s.pdf", data, "application/pdf")})
    officer.post(f"/api/cases/{case_id}/submit")
    body = wait_until_processed(officer, case_id)
    codes = {s["code"] for s in body["signals"]}
    assert "ID-NAME-001" in codes
    assert body["policy"]["outcome"] != "DECLINE"
    assert "POL-META-VERIFY" in body["policy"]["triggered_rules"]


# --------------------------------------------------------------------------- administration

def test_first_run_setup_then_team_management() -> None:
    from app.domain.users import get_accounts as accounts

    anonymous = TestClient(app)
    if accounts().needs_setup():
        created = anonymous.post("/api/setup", json={"email": f"admin-{uuid.uuid4().hex[:6]}@lender.example",
                                                     "name": "First Admin", "role": "risk_admin", "password": STRONG})
        assert created.status_code == 201
    again = TestClient(app).post("/api/setup", json={"email": "late@lender.example", "name": "Late", "role": "risk_admin",
                                                     "password": STRONG})
    assert again.status_code == 409

    admin = staff(Role.RISK_ADMIN)
    email = f"lo-{uuid.uuid4().hex[:6]}@lender.example"
    made = admin.post("/api/admin/users", json={"email": email, "name": "Field Officer", "role": "loan_officer",
                                                "password": STRONG})
    assert made.status_code == 201
    officer = TestClient(app)
    assert officer.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    assert admin.post(f"/api/admin/users/{made.json()['uid']}/disable").status_code == 200
    assert officer.get("/api/me").status_code == 401  # disabling revokes live sessions
    assert customer().get("/api/admin/users").status_code == 403


def test_changing_password_keeps_this_session_and_ends_others() -> None:
    email = f"pw-{uuid.uuid4().hex[:6]}@example.com"
    here, there = TestClient(app), TestClient(app)
    here.post("/api/register", json={"email": email, "name": "Pass Word", "password": STRONG})
    there.post("/api/session", json={"email": email, "password": STRONG})
    assert here.post("/api/me/password", json={"current_password": "wrong one", "new_password": "x" * 20}).status_code == 400
    assert here.post("/api/me/password", json={"current_password": STRONG,
                                               "new_password": "a brand new passphrase"}).status_code == 200
    assert here.get("/api/me").status_code == 200
    assert there.get("/api/me").status_code == 401


# --------------------------------------------------------------------------- local model provider

def test_ollama_provider_enforces_the_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.gateway import LLMGateway, OllamaClient, UsageMeter, inline_refs
    from app.ai.schemas import ExplanationDraft
    from app.config import Settings

    gateway = LLMGateway(Settings(llm_mode="ollama", ollama_model="gemma3:1b", _env_file=None))
    assert gateway.enabled and gateway.provider == "ollama" and not gateway.supports_tools
    assert not gateway.supports_vision
    seen: dict = {}

    async def fake_chat(self, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        return ('{"outcome_sentence": "Approved.", "reasons": ["a"], "next_steps": ["b"], "key_terms": []}', 42)

    monkeypatch.setattr(OllamaClient, "chat", fake_chat)
    meter = UsageMeter(budget=1000)
    result = asyncio.run(gateway.structured(prompt_id="explanation", schema=ExplanationDraft, meter=meter,
                                            contents=["material"]))
    assert result.outcome_sentence == "Approved." and meter.used == 42
    assert "$ref" not in str(seen["schema"]) and seen["model"] == "gemma3:1b"
    assert "$defs" not in inline_refs(ExplanationDraft.model_json_schema())


def test_ollama_outage_falls_back_to_approved_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.ai.explain import build_explanation
    from app.ai.gateway import LLMGateway, OllamaClient, UsageMeter
    from app.config import Settings
    from tests.test_artha import _decided_case

    async def down(self, **kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("ollama is not running")

    monkeypatch.setattr(OllamaClient, "chat", down)
    gateway = LLMGateway(Settings(llm_mode="ollama", _env_file=None))
    explanation = asyncio.run(build_explanation(_decided_case(), "en", gateway, UsageMeter(budget=10_000)))
    assert explanation.author == "templates"


def test_memo_quality_gate_rejects_repetitive_narrow_drafts() -> None:
    from app.ai.memo import clean_questions, quality_gate
    from app.domain.models import Claim, CreditMemo, MemoSection, Outcome

    repeated = "The monthly surplus is ₹1,72,031, suggesting a reasonable level of financial stability."
    memo = CreditMemo(version=1, author="ollama", recommended_outcome=Outcome.REFER, sections=[
        MemoSection(key="summary", title="Summary", claims=[
            Claim(claim_id=f"c-{i}", text=repeated, evidence_ids=["ev-computed-inflow"]) for i in range(4)])])
    gated, reason = quality_gate(memo)
    assert reason and sum(len(s.claims) for s in gated.sections) == 1 and len(gated.dropped_claims) == 3
    loop = " ".join(["The 'RC-SHORT-HISTORY' signal needs to be analyzed."] * 30)
    cleaned = clean_questions([loop, loop, "Confirm the GST turnover."])
    assert len(cleaned) == 2 and len(cleaned[0]) < 120


def test_the_recommendation_is_published_before_the_memo() -> None:
    officer = staff(Role.LOAN_OFFICER)
    case_id = new_case(officer, APPLICANT | {"full_name": "Priya Nair"})
    data, _, _ = statement("sbi_like", seed=9, count=140)
    officer.post(f"/api/cases/{case_id}/documents", files={"file": ("s.pdf", data, "application/pdf")})
    officer.post(f"/api/cases/{case_id}/submit")
    body = wait_until_processed(officer, case_id)
    risk = next(p for p in body["progress"] if p["step"] == "risk_engine")
    assert risk["detail"]["outcome"] == body["policy"]["outcome"] and "pd" in risk["detail"]
