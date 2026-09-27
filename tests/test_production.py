"""Tests for the production layer: configuration guard, accounts, sessions, CSRF, persistence,
the durable job queue and the worker."""
from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.main import app
from app.config import ConfigurationError, Settings
from app.domain.models import Case, CaseStatus, Role
from app.domain.users import AccountService, AuthError, InMemoryUserRepository

needs_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs the SQL store")
STRONG = "correct horse battery staple"


def unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def register(client: TestClient, email: str | None = None, password: str = STRONG) -> str:
    email = email or unique_email()
    response = client.post("/api/register", json={"email": email, "name": "Asha Traders", "password": password})
    assert response.status_code == 201, response.text
    return email


def new_case(client: TestClient) -> str:
    response = client.post("/api/cases", json={"business_name": "Prod Co", "requested_amount": 200000,
                                               "tenure_months": 24, "language": "en", "consent": True})
    assert response.status_code == 201, response.text
    return response.json()["case_id"]


# --------------------------------------------------------------------------- configuration

def test_production_refuses_unsafe_configuration() -> None:
    with pytest.raises(ConfigurationError) as caught:
        Settings(app_env="prod", _env_file=None, database_url=None, demo_mode=True, pipeline_mode="inline",
                 receipt_signing_seed=None)
    message = str(caught.value)
    for fragment in ("SECRET_KEY", "DEMO_MODE", "DATABASE_URL", "RECEIPT_SIGNING_SEED", "PIPELINE_MODE"):
        assert fragment in message


def test_production_accepts_a_safe_configuration() -> None:
    settings = Settings(app_env="prod", _env_file=None, secret_key="x" * 48, demo_mode=False,
                        database_url="postgresql+psycopg://artha@db/artha", receipt_signing_seed="A" * 44,
                        pipeline_mode="queue", llm_mode="vertex")
    assert settings.is_prod


# --------------------------------------------------------------------------- accounts

def service(**kwargs: int) -> AccountService:
    return AccountService(InMemoryUserRepository(), **kwargs)


def test_passwords_are_hashed_and_verified() -> None:
    accounts = service()
    user = accounts.register("A@Example.com ", "Asha", STRONG)
    assert user.email == "a@example.com"
    assert user.password_hash and STRONG not in user.password_hash
    assert accounts.authenticate("a@example.com", STRONG, demo_mode=False).uid == user.uid
    with pytest.raises(AuthError):
        accounts.authenticate("a@example.com", "wrong password here", demo_mode=False)


@pytest.mark.parametrize("password", ["short", "aaaaaaaaaaaaaaaa", " padded password "])
def test_weak_passwords_are_rejected(password: str) -> None:
    with pytest.raises(ValueError):
        service().register("b@example.com", "Bala", password)


def test_repeated_failures_lock_the_account() -> None:
    accounts = service(max_failures=3, lockout_seconds=600)
    accounts.register("c@example.com", "Chitra", STRONG)
    for _ in range(3):
        with pytest.raises(AuthError):
            accounts.authenticate("c@example.com", "not the password", demo_mode=False)
    with pytest.raises(AuthError, match="Too many"):
        accounts.authenticate("c@example.com", STRONG, demo_mode=False)  # even the right password


def test_demo_accounts_only_work_in_demo_mode() -> None:
    accounts = service()
    accounts.seed_demo_accounts()
    assert accounts.authenticate("meena@demo.artha", None, demo_mode=True).role == Role.BORROWER
    with pytest.raises(AuthError):
        accounts.authenticate("meena@demo.artha", None, demo_mode=False)


def test_reserved_demo_domain_cannot_be_registered() -> None:
    with pytest.raises(ValueError):
        service().register("someone@demo.artha", "Someone", STRONG)


# --------------------------------------------------------------------------- sessions over HTTP

def test_register_sign_in_and_sign_out_everywhere() -> None:
    first, second = TestClient(app), TestClient(app)
    email = register(first)
    assert first.get("/api/me").json()["role"] == "borrower"
    assert second.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    assert second.get("/api/me").status_code == 200
    first.delete("/api/session")
    assert second.get("/api/me").status_code == 401  # revoked on the other device too


def test_wrong_password_and_unknown_email_look_identical() -> None:
    client = TestClient(app)
    email = register(TestClient(app))
    wrong = client.post("/api/session", json={"email": email, "password": "definitely not it"})
    unknown = client.post("/api/session", json={"email": unique_email(), "password": "definitely not it"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_duplicate_registration_is_refused() -> None:
    email = register(TestClient(app))
    again = TestClient(app).post("/api/register", json={"email": email, "name": "Other", "password": STRONG})
    assert again.status_code == 409


def test_login_is_rate_limited_per_email() -> None:
    client = TestClient(app)
    email = register(TestClient(app))
    codes = [client.post("/api/session", json={"email": email, "password": "wrong wrong wrong"}).status_code
             for _ in range(12)]
    assert 429 in codes


def test_a_borrower_cannot_read_another_borrowers_case() -> None:
    """With real accounts the prototype's weaker version of this test can now be exact."""
    owner, other = TestClient(app), TestClient(app)
    register(owner)
    register(other)
    case_id = new_case(owner)
    assert owner.get(f"/api/cases/{case_id}").status_code == 200
    assert other.get(f"/api/cases/{case_id}").status_code == 404
    assert other.delete(f"/api/cases/{case_id}").status_code == 404
    assert other.get(f"/api/cases/{case_id}/receipt").status_code == 404


def test_cross_site_writes_are_refused() -> None:
    client = TestClient(app)
    register(client)
    evil = client.post("/api/cases", headers={"Origin": "https://evil.example"},
                       json={"business_name": "X Co", "requested_amount": 100000, "tenure_months": 24,
                             "consent": True})
    assert evil.status_code == 403
    same = client.post("/api/cases", headers={"Origin": "http://testserver"},
                       json={"business_name": "X Co", "requested_amount": 100000, "tenure_months": 24,
                             "consent": True})
    assert same.status_code == 201


def test_public_config_hides_demo_accounts_when_demo_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.main as main

    real = main.get_settings()
    monkeypatch.setattr(main, "get_settings", lambda: real.model_copy(update={"demo_mode": False}))
    body = TestClient(app).get("/api/config").json()
    assert body["demo_mode"] is False and body["demo_users"] == []


def test_sample_statements_are_unavailable_outside_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.main as main

    client = TestClient(app)
    register(client)
    case_id = new_case(client)
    real = main.get_settings()
    monkeypatch.setattr(main, "get_settings", lambda: real.model_copy(update={"demo_mode": False}))
    assert client.post(f"/api/cases/{case_id}/sample", json={"persona": "meena"}).status_code == 404


def test_unhandled_errors_return_a_reference_not_a_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.main as main

    def boom() -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(main, "get_accounts", boom)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/api/session", json={"email": "x@example.com", "password": "whatever at all"})
    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.headers["x-request-id"] in response.json()["detail"]


# --------------------------------------------------------------------------- SQL store

def _case(case_id: str | None = None) -> Case:
    return Case(case_id=case_id or f"case-{uuid.uuid4().hex[:10]}", owner_uid="u-1", business_name="Durable Co",
                requested_amount=Decimal("150000"), tenure_months=24)


@needs_db
def test_cases_survive_a_restart() -> None:
    from app.db import get_engine
    from app.domain.sql_store import SqlStore

    case = _case()
    SqlStore(get_engine()).add(case)
    SqlStore(get_engine()).put_file(case.case_id, "doc-1", b"%PDF-1.7 bytes")
    reopened = SqlStore(get_engine())  # a fresh instance, as after a deploy
    assert reopened.get(case.case_id).requested_amount == Decimal("150000")
    assert reopened.get_file(case.case_id, "doc-1") == b"%PDF-1.7 bytes"


@needs_db
def test_mutate_is_all_or_nothing() -> None:
    from app.db import get_engine
    from app.domain.sql_store import SqlStore

    store = SqlStore(get_engine())
    case = _case()
    store.add(case)
    with pytest.raises(RuntimeError):
        with store.mutate(case.case_id) as live:
            live.business_name = "Half Written"
            raise RuntimeError("abort")
    assert store.get(case.case_id).business_name == "Durable Co"


@needs_db
def test_sql_audit_chain_detects_tampering() -> None:
    from app.db import audit_log, get_engine
    from app.domain.sql_store import SqlStore

    store = SqlStore(get_engine())
    event = store.audit("u", "borrower", "ACTION", "res", {"i": 1})
    store.audit("u", "borrower", "ACTION", "res", {"i": 2})
    assert store.verify_audit_chain()
    with get_engine().begin() as conn:
        conn.execute(update(audit_log).where(audit_log.c.seq == event.seq).values(action="MUTATED"))
    try:
        assert not store.verify_audit_chain()
    finally:
        with get_engine().begin() as conn:
            conn.execute(update(audit_log).where(audit_log.c.seq == event.seq).values(action="ACTION"))
    assert store.verify_audit_chain()


@needs_db
def test_concurrent_audit_writes_keep_one_chain() -> None:
    from app.db import get_engine
    from app.domain.sql_store import SqlStore

    store = SqlStore(get_engine())
    _, before = store.audit_head()
    threads = [threading.Thread(target=lambda i=i: [store.audit("u", "r", "PARALLEL", f"r-{i}", {"n": n})
                                                    for n in range(5)]) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    _, after = store.audit_head()
    assert after - before == 30
    assert store.verify_audit_chain()


@needs_db
def test_retention_sweep_in_sql() -> None:
    from app.db import cases, get_engine
    from app.domain.sql_store import SqlStore

    store = SqlStore(get_engine())
    fresh, stale = _case(), _case()
    store.add(fresh)
    store.add(stale)
    with get_engine().begin() as conn:
        conn.execute(update(cases).where(cases.c.case_id == stale.case_id)
                     .values(updated_at=datetime.now(timezone.utc) - timedelta(days=90)))
    assert store.sweep_expired(30 * 86400) >= 1
    assert store.get(fresh.case_id)
    with pytest.raises(KeyError):
        store.get(stale.case_id)


# --------------------------------------------------------------------------- job queue and worker

@needs_db
def test_job_queue_claims_once_and_recovers_lost_leases() -> None:
    from app.db import get_engine, jobs
    from app.pipeline.jobs import JobQueue

    queue = JobQueue(get_engine(), lease_seconds=60, max_attempts=2)
    case_id = f"case-{uuid.uuid4().hex[:10]}"
    job = queue.enqueue(case_id)
    assert queue.enqueue(case_id) == job  # idempotent while active
    claims = [queue.claim("w1") for _ in range(20)]
    mine = [c for c in claims if c and c.job_id == job]
    assert len(mine) == 1 and mine[0].attempt == 1
    # The worker dies: its lease lapses and another worker takes over.
    with get_engine().begin() as conn:
        conn.execute(update(jobs).where(jobs.c.job_id == job)
                     .values(lease_until=datetime.now(timezone.utc) - timedelta(seconds=1)))
    retaken = [c for c in (queue.claim("w2") for _ in range(20)) if c and c.job_id == job]
    assert retaken and retaken[0].attempt == 2
    assert not queue.heartbeat(job, "w1")  # the dead worker has lost it
    assert queue.heartbeat(job, "w2")
    queue.finish(job, "w2", ok=True)
    assert queue.state(job) == "done"


@needs_db
def test_exhausted_jobs_park_the_case_for_a_person() -> None:
    from app.db import get_engine, jobs
    from app.domain.store import get_store
    from app.pipeline.jobs import JobQueue
    from app.worker import GAVE_UP, Worker

    store = get_store()
    case = _case()
    case.status = CaseStatus.PROCESSING
    store.add(case)
    queue = JobQueue(get_engine(), lease_seconds=60, max_attempts=1)
    job = queue.enqueue(case.case_id)
    with get_engine().begin() as conn:
        conn.execute(update(jobs).where(jobs.c.job_id == job).values(attempts=1))
    claim = next(c for c in (queue.claim("w") for _ in range(50)) if c and c.job_id == job)
    assert claim.exhausted
    asyncio.run(Worker(queue=queue, concurrency=1).process(claim))
    parked = store.get(case.case_id)
    assert parked.status == CaseStatus.NEEDS_MANUAL_PROCESSING and parked.failure_reason == GAVE_UP


@needs_db
def test_queue_mode_runs_a_case_end_to_end_through_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.pipeline.workflow as workflow
    from app.db import get_engine
    from app.pipeline.jobs import JobQueue
    from app.worker import Worker

    real = workflow.get_settings()
    monkeypatch.setattr(workflow, "get_settings", lambda: real.model_copy(update={"pipeline_mode": "queue"}))
    queue = JobQueue(get_engine(), lease_seconds=60, max_attempts=3)
    monkeypatch.setattr("app.pipeline.jobs.get_queue", lambda: queue)

    client = TestClient(app)
    register(client)
    case_id = new_case(client)
    # Demo sample statements are the only documents a test can produce without a real bank.
    assert client.post(f"/api/cases/{case_id}/sample", json={"persona": "meena"}).status_code == 200
    submitted = client.post(f"/api/cases/{case_id}/submit")
    assert submitted.status_code == 200 and submitted.json()["job"].startswith("job-")
    assert client.post(f"/api/cases/{case_id}/submit").status_code == 409  # a double click queues once
    assert client.get(f"/api/cases/{case_id}").json()["status"] == "PROCESSING"

    worker = Worker(queue=queue, concurrency=1)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        claim = queue.claim(worker.worker_id)
        if claim and claim.case_id == case_id:
            asyncio.run(worker.process(claim))
            break
        if claim:  # someone else's leftover job from an earlier test
            queue.finish(claim.job_id, worker.worker_id, ok=True)
    assert client.get(f"/api/cases/{case_id}").json()["status"] == "READY_FOR_REVIEW"


# --------------------------------------------------------------------------- rendering

def test_concurrent_page_renders_do_not_crash_the_process() -> None:
    """PDFium aborts the process if two threads enter it at once. The prototype rendered pages on
    parallel threads without a lock and crashed as soon as a browser loaded a multi-page statement."""
    from concurrent.futures import ThreadPoolExecutor

    from app.documents.render import render_crop, render_page_png
    from app.synthetic.generator import persona_statement, render_pdf

    data = render_pdf(persona_statement("meena"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        pages = list(pool.map(lambda n: render_page_png(data, n % 3 + 1, 1.0 + n / 100), range(16)))
        crops = list(pool.map(lambda _: render_crop(data, 1, [100, 100, 200, 400]), range(8)))
    assert all(p.startswith(b"\x89PNG") for p in pages + crops)
    with pytest.raises(IndexError):
        render_page_png(data, 999)


def test_the_shipped_challenger_model_actually_loads() -> None:
    """The prototype shipped the artifact but not scikit-learn, so the challenger (and the
    POL-MODEL-CHALLENGE control) silently switched off and its tests skipped. Fail loudly instead."""
    from app.risk.ml_model import METRICS_PATH, MODEL_PATH, load_model

    assert MODEL_PATH.exists() and METRICS_PATH.exists()
    model = load_model()
    assert model is not None, "risk model artifact present but failed to load - check ML dependencies"
    assert 0.0 <= model.predict_pd({}) <= 1.0


def test_borrowers_list_only_their_own_applications_without_internals() -> None:
    owner, other = TestClient(app), TestClient(app)
    register(owner)
    register(other)
    mine = new_case(owner)
    new_case(other)
    listed = owner.get("/api/my/cases").json()["cases"]
    assert [c["case_id"] for c in listed] == [mine]
    assert not {"band", "recommendation", "integrity_findings"} & set(listed[0])
    staff = TestClient(app)
    staff.post("/api/session", json={"email": "arun@demo.artha"})
    assert staff.get("/api/my/cases").status_code == 403
