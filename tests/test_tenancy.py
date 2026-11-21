"""Multi-tenancy: each lender sees only its own applications, people and numbers."""
from __future__ import annotations

import os
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.main import app
from app.domain.models import Case, Role
from app.domain.store import InMemoryStore
from app.domain.tenants import get_tenants
from app.domain.users import get_accounts
from tests.fixtures import APPLICANT, STRONG


def new_lender() -> str:
    return get_tenants().create(f"acme-{uuid.uuid4().hex[:8]}", "Acme Finance").tenant_id


def member(role: Role, tenant_id: str) -> TestClient:
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@acme.example"
    get_accounts().register(email, f"Acme {role.value}", STRONG, role=role, tenant_id=tenant_id)
    client = TestClient(app)
    assert client.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    return client


def demo(email: str) -> TestClient:
    client = TestClient(app)
    assert client.post("/api/session", json={"email": email}).status_code == 200
    return client


def apply(client: TestClient) -> str:
    response = client.post("/api/cases", json={"applicant": APPLICANT, "requested_amount": 200000,
                                               "tenure_months": 24, "language": "en", "consent": True})
    assert response.status_code == 201, response.text
    return response.json()["case_id"]


def test_staff_cannot_see_or_act_on_another_lenders_case() -> None:
    lender = new_lender()
    theirs = apply(demo("meena@demo.artha"))  # the default lender's customer
    officer, admin = member(Role.UNDERWRITER, lender), member(Role.RISK_ADMIN, lender)
    for path in (f"/api/cases/{theirs}", f"/api/cases/{theirs}/compliance"):
        assert officer.get(path).status_code == 404
    assert officer.post(f"/api/cases/{theirs}/decision", json={"outcome": "DECLINE", "reason": "x"}).status_code == 404
    assert officer.post(f"/api/cases/{theirs}/copilot", json={"question": "Why?"}).status_code == 404
    assert admin.post(f"/api/cases/{theirs}/compliance/clear",
                      json={"code": "AML-001", "note": "Reviewed: false positive."}).status_code == 404
    assert theirs not in {row["case_id"] for row in officer.get("/api/queue").json()["cases"]}
    # the default lender's own officer still reaches it
    assert demo("arun@demo.artha").get(f"/api/cases/{theirs}").status_code == 200


def test_customers_join_a_lender_through_its_link() -> None:
    lender = new_lender()
    customer = TestClient(app)
    body = {"email": f"c-{uuid.uuid4().hex[:8]}@example.com", "name": "Acme Customer", "password": STRONG}
    assert customer.post("/api/register", json=body | {"lender": "no-such-lender"}).status_code == 400
    assert customer.post("/api/register", json=body | {"lender": lender}).status_code == 201
    assert customer.get("/api/me").json()["lender"]["id"] == lender
    case_id = apply(customer)
    officer = member(Role.UNDERWRITER, lender)
    assert officer.get(f"/api/cases/{case_id}").status_code == 200
    assert demo("arun@demo.artha").get(f"/api/cases/{case_id}").status_code == 404


def test_lender_admins_manage_only_their_own_people() -> None:
    lender = new_lender()
    admin = member(Role.RISK_ADMIN, lender)
    created = admin.post("/api/admin/users", json={"email": f"o-{uuid.uuid4().hex[:8]}@acme.example",
                                                   "name": "Acme Officer", "role": "underwriter", "password": STRONG})
    assert created.status_code == 201
    assert get_accounts().repo.get(created.json()["uid"]).tenant_id == lender
    emails = {u["email"] for u in admin.get("/api/admin/users").json()["users"]}
    assert "arun@demo.artha" not in emails and created.json()["email"] in emails
    assert admin.post("/api/admin/users/arun@demo.artha/disable").status_code == 404
    overview = admin.get("/api/risk/overview").json()
    assert overview["cases_scored"] == 0 and all(e["actor"] != "arun@demo.artha" for e in overview["audit_tail"])


def test_fraud_fingerprints_never_match_across_lenders() -> None:
    stores = [InMemoryStore()]
    if os.environ.get("DATABASE_URL"):
        from app.db import get_engine
        from app.domain.sql_store import SqlStore

        stores.append(SqlStore(get_engine()))
    for store in stores:
        run, value = uuid.uuid4().hex[:6], f"hash-{uuid.uuid4().hex}"
        a1, a2, b1 = f"a1-{run}", f"a2-{run}", f"b1-{run}"
        for cid, tenant in ((a1, "alpha"), (a2, "alpha"), (b1, "beta")):
            store.add(Case(case_id=cid, owner_uid="u", tenant_id=tenant, business_name="B",
                           requested_amount=Decimal("100000"), tenure_months=24))
            store.set_fingerprints(cid, "owner", [("account", value)])
        assert {m["case_id"] for m in store.match_fingerprints(a1, [("account", value)])} == {a2}
        assert store.match_fingerprints(b1, [("account", value)]) == []
