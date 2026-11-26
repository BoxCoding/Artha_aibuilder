"""Account Aggregator FI data (decrypted ReBIT deposit JSON) goes through the same upload and pipeline as a PDF."""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import Decimal

from app.documents.account_aggregator import read_account_aggregator
from app.documents.universal_reader import reconciles
from app.domain.models import Role
from tests.fixtures import staff
from tests.test_coverage import applicant, run_case


def aa_payload(count: int = 150, seed: int = 3, *, upper_keys: bool = False) -> bytes:
    """Fictional FI data in a provider wrapper, newest first as some FIPs send it."""
    rnd = random.Random(seed)
    balance, day, txns = Decimal("40000.00"), date(2025, 10, 1), []
    for n in range(count):
        day += timedelta(days=rnd.random() < 0.3)
        credit = rnd.random() < 0.35
        amount = Decimal(rnd.randint(100, 30000 if credit else 9000)) + Decimal(rnd.randint(0, 99)) / 100
        balance += amount if credit else -amount
        txns.append({"txnId": f"T{n:05d}", "type": "CREDIT" if credit else "DEBIT", "mode": "UPI",
                     "amount": f"{amount:.2f}", "currentBalance": f"{balance:.2f}",
                     "transactionTimestamp": f"{day.isoformat()}T10:{n % 60:02d}:00+05:30",
                     "valueDate": day.isoformat(), "narration": f"UPI/SAMPLE PAYEE {n % 9}/test@upi/payment",
                     "reference": f"REF{n:06d}"})
    account = {"type": "deposit", "maskedAccNumber": "XXXXXXXX0001", "version": "1.1",
               "profile": {"holders": {"type": "SINGLE", "holder": [{"name": "TEST CUSTOMER"}]}},
               "summary": {"currentBalance": f"{balance:.2f}", "type": "SAVINGS"},
               "transactions": {"startDate": "2025-10-01", "endDate": day.isoformat(),
                                "transaction": list(reversed(txns))}}
    if upper_keys:
        account = {"Profile": account["profile"], "Transactions": {"@startDate": "2025-10-01",
                                                                   "Transaction": account["transactions"]["transaction"]},
                   "@maskedAccNumber": "XXXXXXXX0001"}
    return json.dumps({"fips": [{"fipName": "Test Bank", "accounts": [{"data": {"account": account}}]}]}).encode()


def test_aa_data_is_read_in_date_order_and_reconciles() -> None:
    for upper in (False, True):
        parsed = read_account_aggregator(aa_payload(upper_keys=upper))
        assert not parsed.needs_ai and len(parsed.rows) == 150
        assert parsed.bank_name == "Test Bank" and parsed.account_last4 == "0001"
        assert parsed.rows[0].txn_date <= parsed.rows[-1].txn_date
        assert reconciles(parsed.rows, parsed.opening_balance)
    assert read_account_aggregator(b'{"hello": 1}').needs_ai


def test_aa_upload_runs_the_whole_pipeline_and_bad_json_is_refused() -> None:
    officer = staff(Role.LOAN_OFFICER)
    body = run_case(officer, applicant(business_name="AA Data Test"), aa_payload())
    assert body["status"] == "READY_FOR_REVIEW", body.get("failure_reason")
    assert body["transaction_count"] == 150
    doc = body["documents"][0]
    assert doc["parser"] == "account_aggregator" and doc["pages"] == 0
    assert officer.get(f"/api/cases/{body['case_id']}/documents/{doc['doc_id']}/pages/1").status_code == 404

    cid = officer.post("/api/cases", json={"applicant": applicant(), "requested_amount": 100000, "tenure_months": 12,
                                           "language": "en", "consent": True}).json()["case_id"]
    bad = officer.post(f"/api/cases/{cid}/documents", files={"file": ("aa.json", b'{"fips": []}', "application/json")})
    assert bad.status_code == 422


def perfios_payload(count: int = 120) -> bytes:
    """A fictional Perfios statement report: signed amounts, oldest first, plus Perfios's own summary we ignore."""
    aa = json.loads(aa_payload(count))["fips"][0]["accounts"][0]["data"]["account"]["transactions"]["transaction"]
    xns = [{"date": t["valueDate"], "narration": t["narration"], "chqNo": "", "category": "Transfer",
            "amount": float(t["amount"]) * (1 if t["type"] == "CREDIT" else -1), "balance": float(t["currentBalance"])}
           for t in reversed(aa)]
    return json.dumps({"customerInfo": {"name": "TEST CUSTOMER", "bank": "Test Bank"},
                       "summaryInfo": {"instName": "Test Bank, India", "accNo": "000000000001",
                                       "total": {"totalCredit": 1.0}},
                       "accountXns": [{"accountNo": "000000000001", "xns": xns}]}).encode()


def test_perfios_report_is_read_from_its_transactions_not_its_summary() -> None:
    from app.documents.intake import read_deterministic

    parsed, reader = read_deterministic(perfios_payload())
    assert reader == "perfios" and not parsed.needs_ai and len(parsed.rows) == 120
    assert parsed.bank_name == "Test Bank" and parsed.account_last4 == "0001"
    assert reconciles(parsed.rows, parsed.opening_balance)
    assert sum(r.credit for r in parsed.rows) > 1  # recomputed, not Perfios's figure


def test_api_push_needs_the_lenders_key_and_only_reaches_its_own_cases() -> None:
    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.api.security import integration_key
    from app.config import get_settings

    officer = staff(Role.LOAN_OFFICER)
    cid = officer.post("/api/cases", json={"applicant": applicant(), "requested_amount": 100000, "tenure_months": 12,
                                           "language": "en", "consent": True}).json()["case_id"]
    url = f"/api/integrations/cases/{cid}/statements"
    machine = TestClient(app)  # no session cookie: a server, not a browser
    settings = get_settings()
    assert machine.post(url, content=perfios_payload()).status_code == 401
    assert machine.post(url, content=perfios_payload(), headers={"X-API-Key": "llk_x.bad"}).status_code == 401
    other = integration_key("some-other-lender", settings)
    assert machine.post(url, content=perfios_payload(), headers={"X-API-Key": other}).status_code == 404
    key = {"X-API-Key": integration_key("default", settings)}
    assert machine.post(url, content=b"%PDF-1.4", headers=key).status_code == 415
    ok = machine.post(url + "?filename=perfios.json", content=perfios_payload(), headers=key)
    assert ok.status_code == 200, ok.text
    assert ok.json()["preview"]["reader"] == "perfios" and ok.json()["preview"]["transactions"] == 120
    docs = officer.get(f"/api/cases/{cid}").json()["documents"]
    assert [d["filename"] for d in docs] == ["perfios.json"]
    assert officer.get("/api/integrations/key").status_code == 403  # risk admin only
    assert staff(Role.RISK_ADMIN).get("/api/integrations/key").json()["api_key"].startswith("llk_")
