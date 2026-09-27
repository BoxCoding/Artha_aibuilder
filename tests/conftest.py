"""Test configuration.

By default the whole suite runs through the SQL store on a throwaway SQLite file, so every API
test also exercises persistence. Override with:

    ARTHA_TEST_DATABASE_URL=memory                                   # the in-memory store
    ARTHA_TEST_DATABASE_URL=postgresql+psycopg://.../artha_test      # a real PostgreSQL

The PostgreSQL database is wiped at the start of the run.
"""
from __future__ import annotations

import os
import tempfile

import pytest

_target = os.environ.get("ARTHA_TEST_DATABASE_URL", "sqlite")
os.environ["APP_ENV"] = "test"
os.environ["DEMO_MODE"] = "true"
os.environ["LLM_MODE"] = "offline"
os.environ["PIPELINE_MODE"] = "inline"
os.environ.pop("RECEIPT_SIGNING_SEED", None)
if _target == "memory":
    os.environ.pop("DATABASE_URL", None)
elif _target == "sqlite":
    _dir = tempfile.mkdtemp(prefix="artha-test-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_dir}/artha.db"
else:
    os.environ["DATABASE_URL"] = _target

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

if os.environ.get("DATABASE_URL"):
    from app.db import get_engine, metadata  # noqa: E402

    _engine = get_engine()
    if _engine.dialect.name == "postgresql":
        metadata.drop_all(_engine)
    metadata.create_all(_engine)


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Rate limits are real in tests; they just do not carry over from one test to the next."""
    from app.api.security import reset_rate_limiter

    if os.environ.get("DATABASE_URL"):
        from sqlalchemy import delete

        from app.db import get_engine, rate_limits

        with get_engine().begin() as conn:
            conn.execute(delete(rate_limits))
    reset_rate_limiter()
    yield


# A well-formed applicant for tests that create cases. PAN, GSTIN and names are invented.
APPLICANT = {
    "full_name": "Meena Devi", "mobile": "9876543210", "email": "meena@example.com", "pan": "ABCPD1234E",
    "business_name": "Meena's Home Kitchen", "constitution": "proprietorship", "industry": "Food and restaurants",
    "business_start_year": 2019, "address_line": "12 Temple Street", "city": "Bengaluru", "state": "Karnataka",
    "pincode": "560001", "loan_purpose": "Working capital",
}
