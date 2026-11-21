"""Shared test data (kept out of conftest so importing it has no side effects)."""
# A well-formed applicant for tests that create cases. PAN, GSTIN and names are invented.
APPLICANT = {
    "full_name": "Meena Devi", "mobile": "9876543210", "email": "meena@example.com", "pan": "ABCPD1234E",
    "business_name": "Meena's Home Kitchen", "constitution": "proprietorship", "industry": "Food and restaurants",
    "business_start_year": 2019, "address_line": "12 Temple Street", "city": "Bengaluru", "state": "Karnataka",
    "pincode": "560001", "loan_purpose": "Working capital",
}

# A password that passes the strength policy; test accounts only.
STRONG = "correct horse battery staple"


def staff(role):  # type: ignore[no-untyped-def]
    """A signed-in TestClient for a new staff account with this role."""
    import uuid

    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.domain.users import get_accounts

    email = f"{role.value}-{uuid.uuid4().hex[:8]}@lender.example"
    get_accounts().register(email, f"Test {role.value}", STRONG, role=role)
    client = TestClient(app)
    assert client.post("/api/session", json={"email": email, "password": STRONG}).status_code == 200
    return client
