"""Shared test data (kept out of conftest so importing it has no side effects)."""
# A well-formed applicant for tests that create cases. PAN, GSTIN and names are invented.
APPLICANT = {
    "full_name": "Meena Devi", "mobile": "9876543210", "email": "meena@example.com", "pan": "ABCPD1234E",
    "business_name": "Meena's Home Kitchen", "constitution": "proprietorship", "industry": "Food and restaurants",
    "business_start_year": 2019, "address_line": "12 Temple Street", "city": "Bengaluru", "state": "Karnataka",
    "pincode": "560001", "loan_purpose": "Working capital",
}
