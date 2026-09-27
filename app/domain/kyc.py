"""Applicant (KYC) details: validation and consistency checks against the uploaded statements.

What is collected is what an NBFC needs to underwrite an MSME working-capital loan and nothing more.
Aadhaar numbers are deliberately *not* collected or stored: UIDAI regulations restrict storing them,
and identity verification belongs to an e-KYC / CKYC integration, not a free-text field.

Format checks follow the issuing authorities' published structures:
  * PAN    - AAAAA9999A; the 4th character is the holder type (P individual, F firm, C company, ...)
  * GSTIN  - 2-digit state code + the PAN + entity number + 'Z' + a mod-36 check character
  * Udyam  - UDYAM-XX-00-0000000
Passing a format check proves the number is well formed, not that it is real or belongs to the
applicant. That verification (NSDL PAN, GSTN, Udyam APIs) is an integration point, flagged in the UI.
"""
from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PAN_RE = re.compile(r"^[A-Z]{3}[ABCFGHJLPT][A-Z]\d{4}[A-Z]$")
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
UDYAM_RE = re.compile(r"^UDYAM-[A-Z]{2}-\d{2}-\d{7}$")
MOBILE_RE = re.compile(r"^[6-9]\d{9}$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

PAN_HOLDER_TYPES = {"P": "Individual", "F": "Firm / LLP", "C": "Company", "H": "HUF", "A": "Association of persons",
                    "T": "Trust", "B": "Body of individuals", "L": "Local authority", "J": "Artificial juridical person",
                    "G": "Government"}

STATES: dict[str, str] = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}


class Constitution(StrEnum):
    PROPRIETORSHIP = "proprietorship"
    PARTNERSHIP = "partnership"
    LLP = "llp"
    PRIVATE_LIMITED = "private_limited"
    OTHER = "other"


# The PAN holder type each constitution should carry, when the PAN is the business's own.
CONSTITUTION_PAN_TYPE = {Constitution.PARTNERSHIP: "F", Constitution.LLP: "F", Constitution.PRIVATE_LIMITED: "C"}

INDUSTRIES = ("Retail trade", "Wholesale trade", "Food and restaurants", "Manufacturing", "Transport and logistics",
              "Construction", "Services", "Agriculture and allied", "Textiles and apparel", "Healthcare",
              "Education", "Other")


def gstin_check_char(first14: str) -> str:
    """GSTN's mod-36 check character over the first 14 characters."""
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for i, ch in enumerate(first14):
        value = chars.index(ch) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    return chars[(36 - total % 36) % 36]


def valid_gstin(gstin: str) -> bool:
    return bool(GSTIN_RE.match(gstin)) and gstin[:2] in STATES and gstin_check_char(gstin[:14]) == gstin[14]


def mask_pan(pan: str | None) -> str | None:
    return f"{pan[:2]}XXX{pan[5:9]}{pan[9]}" if pan and len(pan) == 10 else pan


def mask_mobile(mobile: str | None) -> str | None:
    return f"XXXXXX{mobile[-4:]}" if mobile and len(mobile) == 10 else mobile


class ApplicantProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    full_name: str = Field(min_length=2, max_length=120)
    mobile: str
    email: str | None = Field(default=None, max_length=254)
    pan: str
    date_of_birth: date | None = None
    business_name: str = Field(min_length=2, max_length=120)
    constitution: Constitution = Constitution.PROPRIETORSHIP
    industry: str = Field(default="Other", max_length=60)
    business_start_year: int | None = Field(default=None, ge=1900, le=2100)
    udyam_number: str | None = None
    gstin: str | None = None
    address_line: str = Field(min_length=3, max_length=200)
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(min_length=2, max_length=60)
    pincode: str
    loan_purpose: str = Field(default="Working capital", max_length=200)

    @field_validator("full_name", "business_name", "city", "state")
    @classmethod
    def _single_spaced(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("mobile")
    @classmethod
    def _mobile(cls, v: str) -> str:
        digits = re.sub(r"\D", "", v)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if not MOBILE_RE.match(digits):
            raise ValueError("Enter a 10-digit Indian mobile number.")
        return digits

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Enter a valid email address.")
        return v

    @field_validator("pan")
    @classmethod
    def _pan(cls, v: str) -> str:
        v = v.strip().upper()
        if not PAN_RE.match(v):
            raise ValueError("Enter a valid 10-character PAN, e.g. ABCPE1234F.")
        return v

    @field_validator("udyam_number")
    @classmethod
    def _udyam(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().upper().replace(" ", "")
        if not UDYAM_RE.match(v):
            raise ValueError("Udyam registration numbers look like UDYAM-MH-12-0012345.")
        return v

    @field_validator("gstin")
    @classmethod
    def _gstin(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().upper().replace(" ", "")
        if not valid_gstin(v):
            raise ValueError("This GSTIN is not valid (check the state code and the last character).")
        return v

    @field_validator("pincode")
    @classmethod
    def _pin(cls, v: str) -> str:
        v = re.sub(r"\D", "", v)
        if not PIN_RE.match(v):
            raise ValueError("Enter a 6-digit PIN code.")
        return v

    @field_validator("date_of_birth")
    @classmethod
    def _adult(cls, v: date | None) -> date | None:
        if v is None:
            return None
        today = date.today()
        age = today.year - v.year - ((today.month, today.day) < (v.month, v.day))
        if not 18 <= age <= 100:
            raise ValueError("The applicant must be at least 18 years old.")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ApplicantProfile:
        if self.constitution == Constitution.PROPRIETORSHIP and self.pan[3] != "P":
            raise ValueError("A proprietor's PAN is an individual PAN (its 4th character is P).")
        return self

    def warnings(self) -> list[str]:
        """Inconsistencies worth a human's attention that are not grounds to refuse the form."""
        notes: list[str] = []
        if self.gstin:
            if self.constitution == Constitution.PROPRIETORSHIP and self.gstin[2:12] != self.pan:
                notes.append("The GSTIN is registered to a different PAN than the proprietor's.")
            state = STATES.get(self.gstin[:2])
            if state and state.lower() != self.state.lower():
                notes.append(f"The GSTIN is registered in {state}, but the address is in {self.state}.")
        expected = CONSTITUTION_PAN_TYPE.get(self.constitution)
        if expected and self.pan[3] not in (expected, "P"):
            notes.append(f"The PAN holder type ({PAN_HOLDER_TYPES.get(self.pan[3], self.pan[3])}) does not match "
                         f"a {self.constitution.value.replace('_', ' ')}.")
        return notes

    def masked(self) -> dict[str, object]:
        data = self.model_dump(mode="json")
        data["pan"] = mask_pan(self.pan)
        data["mobile"] = mask_mobile(self.mobile)
        return data


# --------------------------------------------------------------------------- name matching

_NOISE = {"mr", "mrs", "ms", "m/s", "ms.", "shri", "smt", "sri", "kumari", "dr", "the", "and", "&", "pvt", "private",
          "ltd", "limited", "llp", "co", "company", "enterprises", "enterprise", "traders", "trading", "stores",
          "store", "industries", "services", "prop", "proprietor"}


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", name.lower()) if len(t) > 1 and t not in _NOISE}


def names_match(statement_name: str, *candidates: str) -> bool:
    """True if the statement's account holder plausibly is the applicant or the business.
    Tolerates initials, titles, word order and business suffixes; requires real overlap."""
    held = _tokens(statement_name)
    if not held:
        return True  # nothing readable to compare; absence of evidence is not a finding
    for candidate in candidates:
        wanted = _tokens(candidate or "")
        if not wanted:
            continue
        overlap = held & wanted
        if overlap and len(overlap) >= min(len(held), len(wanted), 2):
            return True
        if len(overlap) >= 1 and (len(held) == 1 or len(wanted) == 1):
            return True
    return False
