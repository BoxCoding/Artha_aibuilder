"""The video guide's presenter follows the person's optional, self-declared gender, and that gender never
reaches an application, an event, a model or a credit decision."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.api.main import app
from tests.fixtures import APPLICANT

STRONG = "correct horse battery staple"


def register(gender: str | None) -> TestClient:
    client = TestClient(app)
    body = {"email": f"p-{uuid.uuid4().hex[:8]}@example.com", "name": "Test Person", "password": STRONG}
    if gender is not None:
        body["gender"] = gender
    assert client.post("/api/register", json=body).status_code == 201
    return client


@pytest.mark.parametrize("gender,presenter", [("male", "female"), ("female", "male"), ("other", "female"),
                                              (None, "female"), ("not-a-value", "female")])
def test_presenter_is_the_other_gender_or_female_by_default(gender: str | None, presenter: str) -> None:
    me = register(gender).get("/api/me").json()
    assert me["guide_presenter"] == presenter
    assert me["gender"] == (gender if gender in ("male", "female", "other") else None)


def test_profile_update_changes_the_presenter() -> None:
    client = register(None)
    assert client.post("/api/me/profile", json={"gender": "female"}).json() == {"gender": "female", "guide_presenter": "male"}
    assert client.get("/api/me").json()["guide_presenter"] == "male"
    assert client.post("/api/me/profile", json={"gender": None}).json()["guide_presenter"] == "female"


def test_gender_never_reaches_the_application_events_or_staff_views() -> None:
    from app.domain.store import get_store

    client = register("female")
    case_id = client.post("/api/cases", json={"applicant": APPLICANT, "requested_amount": 200000, "tenure_months": 24,
                                              "language": "en", "consent": True}).json()["case_id"]
    case = get_store().get(case_id)
    assert "gender" not in case.model_dump_json()
    assert "gender" not in json.dumps(client.get(f"/api/cases/{case_id}").json())
    assert all("gender" not in json.dumps(e["payload"]) for e in get_store().pending_events(5000) if e["key"] == case_id)


def test_paid_liveavatar_uses_the_presenters_avatar_and_sandbox_keeps_its_one_avatar() -> None:
    from app.ai.liveavatar import avatar_for
    from app.config import Settings

    paid = Settings(_env_file=None, liveavatar_sandbox=False, liveavatar_avatar_id_female="f-1", liveavatar_avatar_id_male="m-1")
    assert avatar_for(paid, "female") == "f-1" and avatar_for(paid, "male") == "m-1"
    sandbox = Settings(_env_file=None, liveavatar_sandbox=True, liveavatar_avatar_id_male="m-1")
    assert avatar_for(sandbox, "male") == sandbox.liveavatar_avatar_id


def test_an_existing_sqlite_database_gains_the_new_column(tmp_path) -> None:
    from app.db import sync_sqlite_schema

    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as conn:  # a users table from before the gender column existed
        conn.execute(text("CREATE TABLE users (uid VARCHAR(120) PRIMARY KEY, email VARCHAR(254) NOT NULL, "
                          "name VARCHAR(120) NOT NULL, role VARCHAR(40) NOT NULL, password_hash TEXT, is_demo BOOLEAN NOT NULL, "
                          "active BOOLEAN NOT NULL, session_version INTEGER NOT NULL, failed_logins INTEGER NOT NULL, "
                          "locked_until DATETIME, created_at DATETIME NOT NULL, last_login_at DATETIME)"))
    sync_sqlite_schema(engine)
    assert "gender" in {c["name"] for c in inspect(engine).get_columns("users")}


def test_presenter_photos_are_served() -> None:
    client = TestClient(app)
    for kind in ("female", "male"):
        response = client.get(f"/static/guide/presenter-{kind}.webp")
        assert response.status_code == 200 and response.content[:4] == b"RIFF"
