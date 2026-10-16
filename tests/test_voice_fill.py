"""Filling the application form from what the customer says: extracted by code, checked by the customer."""
from __future__ import annotations

import pytest

from app.domain.models import Role
from app.forms.voice_fill import extract
from tests.test_intake_and_roles import customer, staff

FULL = ("My name is Priya Nair, mobile nine eight four five zero one two three four five, PAN A B C P N 1 2 3 4 K, "
        "email priya at example dot com, business name Nair Tiffin Services, food and restaurants, proprietorship, "
        "started in 2019, address 12 Temple Street Jayanagar, Bengaluru, Karnataka, pin code 560041, "
        "I need 3 lakh for 24 months, purpose working capital")


def test_a_whole_sentence_fills_every_field() -> None:
    assert extract(FULL)["fields"] == {
        "full_name": "Priya Nair", "mobile": "9845012345", "pan": "ABCPN1234K", "email": "priya@example.com",
        "business_name": "Nair Tiffin Services", "industry": "Food and restaurants", "constitution": "proprietorship",
        "business_start_year": "2019", "address_line": "12 Temple Street Jayanagar", "city": "Bengaluru",
        "state": "Karnataka", "pincode": "560041", "requested_amount": 300000, "tenure_months": "24",
        "loan_purpose": "Working Capital"}


def test_a_different_order_and_phrasing() -> None:
    fields = extract("Hi this is Arjun Patel, my phone number is 98240 33412, born on 12 April 1988, my shop is "
                     "called Patel Kirana Store in Ahmedabad Gujarat 380001, I want 2.5 lakh for 2 years")["fields"]
    assert fields["full_name"] == "Arjun Patel" and fields["mobile"] == "9824033412"
    assert fields["date_of_birth"] == "1988-04-12" and fields["business_name"] == "Patel Kirana Store"
    assert (fields["city"], fields["state"], fields["pincode"]) == ("Ahmedabad", "Gujarat", "380001")
    assert fields["requested_amount"] == 250000 and fields["tenure_months"] == "24"
    assert fields["industry"] == "Retail trade"


def test_hinglish_name() -> None:
    fields = extract("mera naam Ravi Kumar hai mobile 9 8 1 1 2 2 3 3 4 4 pan card number fnqpg1123j")["fields"]
    assert fields == {"full_name": "Ravi Kumar", "mobile": "9811223344", "pan": "FNQPG1123J"}


@pytest.mark.parametrize("field,said,expected", [
    ("pan", "a b c p n one two three four k", "ABCPN1234K"),
    ("mobile", "double nine eight seven six five four three two one", "9987654321"),
    ("mobile", "plus 91 98450 12345", "9845012345"),
    ("email", "meena dot devi at gmail dot com", "meena.devi@gmail.com"),
    ("date_of_birth", "12th of march 1990", "1990-03-12"),
    ("date_of_birth", "05/11/1985", "1985-11-05"),
    ("requested_amount", "three lakh", 300000),
    ("requested_amount", "two point five lakh", 250000),
    ("tenure_months", "three years", "36"),
    ("pincode", "five six zero zero four one", "560041"),
    ("business_start_year", "since twenty nineteen", None),  # "twenty nineteen" is not read as a year: type it
    ("business_start_year", "2019", "2019"),
])
def test_dictating_one_field(field: str, said: str, expected: object) -> None:
    assert extract(said, field)["fields"].get(field) == expected


@pytest.mark.parametrize("field,said", [("mobile", "12345"), ("pan", "hello"), ("pincode", "abc"),
                                        ("tenure_months", "5 months"), ("date_of_birth", "31 February 1990")])
def test_values_that_are_not_valid_are_left_for_the_customer_to_type(field: str, said: str) -> None:
    assert extract(said, field)["fields"] == {}


def test_api_is_for_customers_and_loan_officers_and_rejects_unknown_fields() -> None:
    body = customer().post("/api/form/voice", json={"transcript": FULL}).json()
    assert body["fields"]["pan"] == "ABCPN1234K"
    officer = staff(Role.LOAN_OFFICER)
    assert officer.post("/api/form/voice", json={"transcript": "PAN abcpn1234k", "field": "pan"}).json()["fields"] == \
        {"pan": "ABCPN1234K"}
    assert customer().post("/api/form/voice", json={"transcript": "x", "field": "password"}).status_code == 422
    assert staff(Role.UNDERWRITER).post("/api/form/voice", json={"transcript": "x"}).status_code == 403


@pytest.mark.parametrize("field,said,expected", [
    ("requested_amount", "३ लाख", 300000), ("requested_amount", "2.5 लाख", 250000),
    ("tenure_months", "24 महीने", "24"), ("tenure_months", "2 साल", "24"), ("mobile", "९८४५० १२३४५", "9845012345"),
])
def test_hindi_answers_to_saarthis_questions(field: str, said: str, expected: object) -> None:
    assert extract(said, field)["fields"].get(field) == expected
