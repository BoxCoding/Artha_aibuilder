"""Tests for the problem-statement coverage layer: event-driven processing (Pub/Sub), analytics
(BigQuery), cross-application fraud, compliance automation (KFS, AML, checklist), knowledge retrieval
(Vertex AI Search), Document AI, Vertex AI endpoint serving and personalised recommendations."""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.config import get_settings
from app.domain.models import Case, Role, Transaction
from app.domain.users import get_accounts
from app.gcp import GcpClient, GcpError, set_gcp
from tests.bank_layouts import statement
from tests.fixtures import APPLICANT

STRONG = "correct horse battery staple"


def staff(role: Role) -> TestClient:
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@lender.example"
    get_accounts().register(email, f"Test {role.value}", STRONG, role=role)
    client = TestClient(app)
    assert client.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    return client


def applicant(**over: str) -> dict:
    """A fresh applicant with a unique PAN and mobile, so cross-application checks start clean."""
    n = uuid.uuid4().int
    pan = f"ABCP{chr(65 + n % 26)}{n % 10000:04d}{chr(65 + (n // 7) % 26)}"
    return APPLICANT | {"pan": pan, "mobile": f"9{n % 10**9:09d}"} | over


def run_case(client: TestClient, app_details: dict, data: bytes, amount: int = 200000) -> dict:
    cid = client.post("/api/cases", json={"applicant": app_details, "requested_amount": amount, "tenure_months": 24,
                                          "language": "en", "consent": True}).json()["case_id"]
    assert client.post(f"/api/cases/{cid}/documents", files={"file": ("s.pdf", data, "application/pdf")}).status_code == 200
    assert client.post(f"/api/cases/{cid}/submit").status_code == 200
    for _ in range(160):
        body = client.get(f"/api/cases/{cid}").json()
        if body["status"] not in ("PROCESSING", "DRAFT"):
            return body
        time.sleep(0.25)
    raise AssertionError("pipeline did not finish")


@pytest.fixture
def gcp_mock():
    """A Google Cloud client whose HTTP calls are answered by a handler the test installs."""
    calls: list[httpx.Request] = []
    responses: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        for fragment, body in responses.items():
            if fragment in str(request.url):
                if isinstance(body, int):
                    return httpx.Response(body, json={"error": "x"})
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    set_gcp(GcpClient(transport=httpx.MockTransport(handler), token_provider=lambda: "test-token"))
    yield calls, responses
    set_gcp(None)


# --------------------------------------------------------------------------- Google Cloud client

def test_gcp_client_sends_bearer_token_and_raises_on_errors(gcp_mock) -> None:
    from app.gcp import get_gcp

    calls, responses = gcp_mock
    responses["ok.example"] = {"fine": True}
    responses["bad.example"] = 503
    assert get_gcp().call("POST", "https://ok.example/v1", {"a": 1}) == {"fine": True}
    assert calls[-1].headers["authorization"] == "Bearer test-token"
    with pytest.raises(GcpError):
        get_gcp().call("GET", "https://bad.example/v1")


# --------------------------------------------------------------------------- events / Pub/Sub

def test_events_flow_through_the_outbox_to_pubsub_without_personal_data(gcp_mock) -> None:
    from app.domain.store import get_store
    from app.events import PubSubSink, relay_once

    calls, responses = gcp_mock
    responses["pubsub.googleapis.com"] = {"messageIds": ["1"]}
    officer = staff(Role.LOAN_OFFICER)
    details = applicant(full_name="Priya Nair", business_name="Nair Tiffin Services")
    data, _, _ = statement("icici_like", seed=31, count=120)
    body = run_case(officer, details, data)
    pending = get_store().pending_events(1000)
    topics = {e["topic"] for e in pending if e["key"] == body["case_id"]}
    assert {"case.created", "document.uploaded", "case.submitted", "case.scored"} <= topics
    payloads = json.dumps([e["payload"] for e in pending])
    for secret in (details["pan"], details["mobile"], "Priya Nair", "PRIYA NAIR"):
        assert secret not in payloads
    sent = relay_once(limit=1000, sinks=[PubSubSink("projects/p/topics/ledgerlens-events")])
    assert sent >= 4
    message = json.loads(calls[-1].content)["messages"][0]
    decoded = json.loads(base64.b64decode(message["data"]))
    assert decoded["schema"] == "ledgerlens.event.v1" and message["attributes"]["event_id"]
    assert get_store().event_counts()["pending"] == 0 or all(
        e["key"] != body["case_id"] for e in get_store().pending_events(1000))


def test_a_failing_sink_leaves_events_pending() -> None:
    from app.domain.store import get_store
    from app.events import publish_event, relay_once

    class Broken:
        name = "broken"

        def send(self, events):  # type: ignore[no-untyped-def]
            raise RuntimeError("pubsub down")

    publish_event("case.submitted", "case-x", case_id="case-x", documents=1)
    before = get_store().event_counts()["pending"]
    assert relay_once(sinks=[Broken()]) == 0
    assert get_store().event_counts()["pending"] == before


# --------------------------------------------------------------------------- cross-application fraud

def test_the_same_statement_under_a_different_applicant_is_caught() -> None:
    officer = staff(Role.LOAN_OFFICER)
    data, _, _ = statement("sbi_like", seed=77, count=130)
    first = run_case(officer, applicant(full_name="Priya Nair"), data)
    assert "FR-XAPP-001" not in {s["code"] for s in first["signals"]}
    second = run_case(officer, applicant(full_name="Priya Nair", business_name="Other Front Co"), data)
    codes = {s["code"] for s in second["signals"]}
    assert "FR-XAPP-001" in codes
    assert second["policy"]["outcome"] == "REFER_INTEGRITY_REVIEW"


def test_reusing_a_mobile_across_pans_and_velocity_are_flagged() -> None:
    officer = staff(Role.LOAN_OFFICER)
    shared = applicant()
    run_case(officer, shared, statement("hdfc_like", seed=91, count=100)[0])
    other = applicant(mobile=shared["mobile"])
    codes = {s["code"] for s in run_case(officer, other, statement("hdfc_like", seed=92, count=100)[0])["signals"]}
    assert "FR-XAPP-003" in codes
    for seed in (93, 94, 95):
        last = run_case(officer, shared, statement("hdfc_like", seed=seed, count=100)[0])
    assert "FR-VEL-001" in {s["code"] for s in last["signals"]}


# --------------------------------------------------------------------------- compliance

def test_kfs_arithmetic() -> None:
    from app.compliance.kfs import build_kfs

    kfs = build_kfs("case-1", Decimal("100000"), Decimal("0.21"), 24, "Test Co")
    assert kfs["emi"] == "5139"
    assert kfs["processing_fee"] == "1000" and kfs["gst_on_fees"] == "180" and kfs["net_disbursed_amount"] == "98820"
    assert kfs["total_amount_repayable"] == str(5139 * 24) or abs(int(kfs["total_amount_repayable"]) - 5139 * 24) <= 24
    assert 21.0 < float(kfs["apr_pct"]) < 23.0  # fees push the APR above the nominal rate
    assert len(kfs["kfs_sha256"]) == 64 and kfs["cooling_off_days"] == get_settings().cooling_off_days


def test_watchlist_match_blocks_approval_until_compliance_clears_it_then_kfs_is_issued_and_accepted() -> None:
    officer = staff(Role.LOAN_OFFICER)
    details = applicant(full_name="Priya Nair", business_name="Blue Lotus Shell Traders")
    body = run_case(officer, details, statement("kotak_like", seed=51, count=140)[0], amount=150000)
    case_id = body["case_id"]
    assert "AML-SAN-001" in {s["code"] for s in body["signals"]}
    assert body["policy"]["outcome"] in ("REFER", "REFER_INTEGRITY_REVIEW")
    assert any(i["id"] == "aml" and i["status"] == "attention" for i in body["compliance"])

    credit, admin = staff(Role.UNDERWRITER), staff(Role.RISK_ADMIN)
    decision = {"outcome": "APPROVE", "approved_amount": 100000, "reason": "Verified business with site visit."}
    blocked = credit.post(f"/api/cases/{case_id}/decision", json=decision)
    assert blocked.status_code == 409 and "compliance" in blocked.json()["detail"]
    assert credit.post(f"/api/cases/{case_id}/compliance/clear", json={"code": "AML-SAN-001", "note": "x" * 12}).status_code == 403
    cleared = admin.post(f"/api/cases/{case_id}/compliance/clear",
                         json={"code": "AML-SAN-001", "note": "Different entity: PAN and address do not match the list."})
    assert cleared.status_code == 200
    assert credit.post(f"/api/cases/{case_id}/decision", json=decision).json()["status"] == "recorded"

    kfs = officer.get(f"/api/cases/{case_id}/kfs").json()
    assert kfs["sanctioned_amount"] == "100000" and float(kfs["apr_pct"]) > 0 and kfs["accepted_at"] is None
    assert officer.post(f"/api/cases/{case_id}/kfs/accept").json()["status"] == "accepted"
    report = credit.get(f"/api/cases/{case_id}/compliance").json()
    items = {i["id"]: i["status"] for i in report["checklist"]}
    assert items["kfs"] == "pass" and items["kfs_accept"] == "pass" and items["human"] == "pass"
    assert {"KFS_ACCEPTED", "COMPLIANCE_CLEARED", "DECISION_RECORDED"} <= {e["action"] for e in report["audit_trail"]}
    assert report["audit_chain_valid"]


def _txn(i: int, day: date, credit: str, narration: str, category: str) -> Transaction:
    return Transaction(txn_id=f"t{i}", doc_id="d1", page=1, row_index=i, txn_date=day, narration=narration,
                       reference="", credit=Decimal(credit), balance=Decimal("1000000"), evidence_id=f"ev-{i}",
                       category=category)


def test_counterparty_screening_and_reportable_cash() -> None:
    from app.compliance.aml import aml_signals

    case = Case(case_id="c-aml", owner_uid="u", business_name="Clean Co", requested_amount=Decimal("100000"),
                tenure_months=12)
    case.transactions = [_txn(1, date(2026, 5, 3), "40000.00", "NEFT CR/CRIMSON HAWALA EXCHANGE/settlement", "neft_imps_credit")]
    case.transactions += [_txn(10 + i, date(2026, 6, 1 + i), "200000.00", "CASH DEPOSIT BY SELF", "cash_deposit") for i in range(5)]
    codes = {s.code: s for s in aml_signals(case)}
    assert codes["AML-SAN-002"].evidence_ids == ["ev-1"] and codes["AML-SAN-002"].category == "COMPLIANCE"
    assert "2026-06" in codes["AML-CTR-001"].title and codes["AML-CTR-001"].severity == "MEDIUM"


# --------------------------------------------------------------------------- recommendations

def test_personalised_recommendations_reach_the_customer_after_the_decision() -> None:
    from app.advice.recommend import best_emi_day

    customer = TestClient(app)
    assert customer.post("/api/register", json={"email": f"c-{uuid.uuid4().hex[:6]}@example.com", "name": "Priya Nair",
                                                "password": STRONG}).status_code == 201
    body = run_case(customer, applicant(full_name="Priya Nair"), statement("icici_like", seed=61, count=150)[0], 900000)
    credit = staff(Role.UNDERWRITER)
    case = credit.get(f"/api/cases/{body['case_id']}").json()
    credit.post(f"/api/cases/{body['case_id']}/decision",
                json={"outcome": "DECLINE", "reason": "Testing recommendations after a decline."})
    view = customer.get(f"/api/cases/{body['case_id']}").json()
    kinds = [i["kind"] for i in view["recommendations"]["items"]]
    assert "emi_date" in kinds and view["recommendations"]["language"] == "en"
    assert all("{" not in i["text"] for i in view["recommendations"]["items"])
    assert case["recommendations"] == {} or isinstance(case["recommendations"], dict)
    probe = Case(case_id="c", owner_uid="u", business_name="b", requested_amount=Decimal("1"), tenure_months=12)
    probe.transactions = [_txn(i, date(2026, 5, 2) + timedelta(days=30 * (i % 3)), "1000.00", "UPI", "upi_credit")
                          for i in range(6)]
    assert best_emi_day(probe) == (5, 100)


# --------------------------------------------------------------------------- knowledge / Vertex AI Search

def test_knowledge_search_respects_audience_and_powers_the_copilot_and_coach() -> None:
    import asyncio

    from app.ai.copilot import ask
    from app.ai.explain import coach_reply
    from app.ai.gateway import LLMGateway, UsageMeter
    from app.config import Settings
    from app.domain.models import Explanation
    from app.knowledge.search import search

    assert search("why does a large approval need two people", "staff")[0]["source"] == "POL-FOUR-EYES"
    assert not any(r["source"].startswith("POL-") for r in search("four eyes threshold policy", "customer"))
    customer = TestClient(app)
    customer.post("/api/register", json={"email": f"k-{uuid.uuid4().hex[:6]}@example.com", "name": "Ka Ka",
                                         "password": STRONG})
    results = customer.get("/api/knowledge/search", params={"q": "what is APR"}).json()
    assert results["audience"] == "customer" and results["results"][0]["source"] == "glossary:APR"

    offline = LLMGateway(Settings(llm_mode="offline", _env_file=None))
    case = Case(case_id="c", owner_uid="u", business_name="b", requested_amount=Decimal("1"), tenure_months=12)
    answer = asyncio.run(ask(case, "What does the policy say about four-eyes approval?", offline))
    assert answer.author.startswith("knowledge") and "POL-FOUR-EYES" in json.dumps(answer.as_dict())
    expl = Explanation(language="en", outcome_sentence="x", reasons=[], next_steps=[], key_terms=[], contact="c",
                       integrity_review=False, author="templates")
    reply, author = asyncio.run(coach_reply(expl, "what is a cooling-off period?", offline, UsageMeter(budget=10)))
    assert author == "glossary" and "exit" in reply.reply


def test_vertex_ai_search_is_used_for_staff_when_configured(gcp_mock, monkeypatch) -> None:
    from app.knowledge.search import search

    calls, responses = gcp_mock
    responses["discoveryengine.googleapis.com"] = {"results": [{"id": "d1", "document": {"derivedStructData": {
        "title": "Credit policy manual §4.2", "link": "gs://policies/manual.pdf",
        "snippets": [{"snippet": "Approvals above five lakh require a second approver."}]}}}]}
    monkeypatch.setattr(get_settings(), "vertex_search_serving_config",
                        "projects/p/locations/global/collections/default_collection/engines/e/servingConfigs/default_search")
    hits = search("four eyes", "staff")
    assert hits[0]["engine"] == "vertex_ai_search" and "second approver" in hits[0]["text"]
    assert search("what is APR", "customer")[0]["engine"] == "local_bm25"  # customers never hit the policy index


# --------------------------------------------------------------------------- Document AI

def _docai_document() -> dict:
    header = ["Date", "Particulars", "Withdrawal", "Deposit", "Balance"]
    rows = [["01/04/2026", "Opening credit", "", "5,000.00", "15,000.00"],
            ["03/04/2026", "UPI/CR/MEHTA", "", "2,500.00", "17,500.00"],
            ["05/04/2026", "RENT", "7,000.00", "", "10,500.00"]]
    text, cells = "", []

    def cell(value: str, y: float) -> dict:
        nonlocal text
        start = len(text)
        text += value + "\n"
        return {"layout": {"textAnchor": {"textSegments": [{"startIndex": start, "endIndex": start + len(value)}]},
                           "boundingPoly": {"normalizedVertices": [{"x": 0.1, "y": y}, {"x": 0.9, "y": y + 0.02}]}}}

    head = {"cells": [cell(h, 0.1) for h in header]}
    body = [{"cells": [cell(v, 0.15 + 0.03 * i) for v in r]} for i, r in enumerate(rows)]
    cells.append({"headerRows": [head], "bodyRows": body})
    return {"text": text, "pages": [{"tables": cells}]}


def test_document_ai_tables_become_reconciled_transactions(gcp_mock, monkeypatch) -> None:
    from app.documents.document_ai import extract_with_document_ai
    from app.documents.universal_reader import reconciles

    calls, responses = gcp_mock
    responses["documentai.googleapis.com"] = {"document": _docai_document()}
    monkeypatch.setattr(get_settings(), "document_ai_processor", "projects/p/locations/us/processors/abc")
    data, _, _ = statement("icici_like", seed=1, count=5)
    parsed = extract_with_document_ai(data)
    assert [(r.debit, r.credit, r.balance) for r in parsed.rows] == [
        (Decimal("0"), Decimal("5000.00"), Decimal("15000.00")), (Decimal("0"), Decimal("2500.00"), Decimal("17500.00")),
        (Decimal("7000.00"), Decimal("0"), Decimal("10500.00"))]
    assert reconciles(parsed.rows, parsed.opening_balance) and parsed.rows[1].amount_bbox
    assert "us-documentai.googleapis.com" in str(calls[-1].url)
    assert json.loads(calls[-1].content)["rawDocument"]["mimeType"] == "application/pdf"


# --------------------------------------------------------------------------- Vertex AI endpoint

def test_challenger_is_served_from_vertex_when_configured_and_falls_back(gcp_mock, monkeypatch) -> None:
    from app.risk.engine import challenge_score, score

    calls, responses = gcp_mock
    monkeypatch.setattr(get_settings(), "vertex_risk_endpoint", "projects/p/locations/asia-south1/endpoints/42")
    features = {"nach_returns_6m": 0, "abb_to_inflow": 0.6, "inflow_cv": 0.2, "cash_deposit_share": 0.1,
                "low_balance_days_share": 0.0, "obligation_ratio": 0.1, "statement_span_months": 8, "inflow_trend": 0.0}
    result = score(features)
    responses["aiplatform.googleapis.com"] = {"predictions": [[0.97, 0.03]]}
    summary, _ = challenge_score(features, result)
    assert summary["served_by"] == "vertex_ai_endpoint" and summary["ml_pd"] == 0.03
    responses["aiplatform.googleapis.com"] = 503
    summary, _ = challenge_score(features, result)
    assert summary["served_by"] == "local"


# --------------------------------------------------------------------------- portfolio analytics

def test_portfolio_analytics_local_and_bigquery(gcp_mock, monkeypatch) -> None:
    from app.analytics.portfolio import local_portfolio, portfolio

    summaries = [{"case_id": "a", "scored": True, "band": "B", "pd": 0.05, "signal_codes": ["FR-XAPP-001"],
                  "integrity_findings": 1, "outcome": "APPROVE", "channel": "assisted", "industry": "Retail trade",
                  "state": "Karnataka", "created": date.today().isoformat()},
                 {"case_id": "b", "scored": True, "band": "D", "pd": 0.15, "signal_codes": ["AML-CTR-001"],
                  "integrity_findings": 0, "outcome": "DECLINE", "channel": "self", "created": date.today().isoformat()}]
    local = local_portfolio(summaries)
    assert local["approval_rate"] == 0.5 and local["fraud_flag_rate"] == 0.5 and local["compliance_alerts"] == 1
    assert local["cross_application_alerts"] == [{"case_id": "a", "codes": ["FR-XAPP-001"]}]
    assert local["daily_applications"][-1]["count"] == 2

    calls, responses = gcp_mock
    responses["bigquery.googleapis.com"] = {"schema": {"fields": [{"name": "applications"}, {"name": "scored"},
                                                                   {"name": "decided"}, {"name": "approval_rate"},
                                                                   {"name": "fraud_flag_rate"}]},
                                            "rows": [{"f": [{"v": "120"}, {"v": "118"}, {"v": "90"}, {"v": "0.61"},
                                                            {"v": "0.07"}]}]}
    monkeypatch.setattr(get_settings(), "analytics_backend", "bigquery")
    monkeypatch.setattr(get_settings(), "bigquery_project", "p")
    result = portfolio(summaries)
    assert result["backend"] == "bigquery" and result["applications"] == 120 and result["approval_rate"] == 0.61
    assert "`p.ledgerlens.events`" in json.loads(calls[0].content)["query"]
    assert result["cross_application_alerts"]  # case-level alerts still come from the application database
    admin = staff(Role.RISK_ADMIN)
    monkeypatch.setattr(get_settings(), "analytics_backend", "local")
    body = admin.get("/api/risk/portfolio").json()
    assert body["backend"] == "local" and "events" in body
