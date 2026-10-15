"""Turn what a customer says into application-form fields.

Deterministic on purpose: personal details (PAN, mobile, address) are extracted by code on our server and
never sent to a language model. The customer then reviews every value before anything is saved, so a
mis-heard word is caught by a person rather than trusted.

Two modes: a whole sentence ("My name is Priya Nair, mobile 98450 12345, PAN A B C P N 1 2 3 4 K, ...") split
by cue words, or a single field ("pan" + "A B C P N 1 2 3 4 K") when the customer dictates into one box.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

STATES = ["Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chandigarh",
          "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa", "Gujarat", "Haryana",
          "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep",
          "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry",
          "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
          "West Bengal"]
STATE_ALIASES = {"orissa": "Odisha", "pondicherry": "Puducherry", "new delhi": "Delhi", "bombay": "Maharashtra",
                 "up": "Uttar Pradesh", "mp": "Madhya Pradesh", "tn": "Tamil Nadu", "j&k": "Jammu and Kashmir"}
INDUSTRY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Food and restaurants", ("food", "restaurant", "tiffin", "catering", "kitchen", "dhaba", "bakery", "cafe", "sweet")),
    ("Textiles and apparel", ("textile", "garment", "clothing", "apparel", "boutique", "tailor", "saree", "fabric")),
    ("Transport and logistics", ("transport", "logistics", "truck", "courier", "taxi", "delivery", "tempo")),
    ("Wholesale trade", ("wholesale", "distributor", "distribution", "stockist")),
    ("Retail trade", ("retail", "kirana", "general store", "grocery", "shop", "store", "mobile shop")),
    ("Manufacturing", ("manufactur", "factory", "fabrication", "workshop", "production")),
    ("Construction", ("construction", "builder", "contractor", "civil work")),
    ("Agriculture and allied", ("farm", "agri", "dairy", "poultry", "fishery", "crop")),
    ("Healthcare", ("clinic", "pharmacy", "medical", "chemist", "hospital", "diagnostic")),
    ("Education", ("school", "coaching", "tuition", "education", "training institute")),
    ("Services", ("service", "salon", "parlour", "repair", "consult", "agency", "studio")),
)
CONSTITUTION_WORDS = (("private_limited", ("private limited", "pvt ltd", "pvt. ltd", "private ltd")),
                      ("llp", ("llp", "limited liability partnership")),
                      ("partnership", ("partnership", "partners")),
                      ("proprietorship", ("proprietor", "proprietorship", "sole owner", "own business", "self employed")))
TENURES = (12, 18, 24, 36)

# Cue words that start a field, longest first so "business name" wins over "name".
CUES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("business_name", ("business name is", "business name", "my business is called", "business is called",
                       "my business is", "shop name is", "shop name", "shop is called", "company name", "firm name")),
    ("date_of_birth", ("date of birth is", "date of birth", "born on", "dob", "birthday")),
    ("business_start_year", ("business started in", "started in", "started the business in", "running since",
                             "established in", "since")),
    ("full_name", ("my name is", "my full name is", "name is", "mera naam", "मेरा नाम", "nama saya", "this is", "name")),
    ("mobile", ("mobile number is", "mobile number", "phone number is", "phone number", "contact number", "mobile",
                "phone", "number is")),
    ("email", ("email id is", "email is", "email address", "email id", "email", "e-mail", "mail id")),
    ("pan", ("pan number is", "pan card number", "pan number", "pan card", "pan is", "pan")),
    ("udyam_number", ("udyam number", "udyam registration", "udyam")),
    ("gstin", ("gst number", "gstin", "gst")),
    ("industry", ("industry is", "industry", "sector", "line of business")),
    ("address_line", ("address is", "address", "located at", "shop is at", "office is at")),
    ("city", ("city is", "city")),
    ("pincode", ("pin code is", "pin code", "pincode", "postal code", "zip code", "pin")),
    ("requested_amount", ("loan amount", "loan of", "amount is", "amount", "i need", "need a loan of", "i want",
                          "require", "borrow")),
    ("loan_purpose", ("purpose is", "purpose", "the loan is for", "it is for", "for buying", "to buy")),
)

UNITS = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m} | \
         {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}


DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
HINDI_UNITS = {"लाख": "lakh", "लाख़": "lakh", "हज़ार": "thousand", "हजार": "thousand", "करोड़": "crore",
               "महीने": "months", "महीना": "months", "साल": "years"}


def _spoken_digits(text: str) -> str:
    """'nine eight four five', 'double nine', 'twenty five' -> digits, so numbers can be matched."""
    text = text.translate(DEVANAGARI_DIGITS)
    for hindi, english in HINDI_UNITS.items():
        text = text.replace(hindi, f" {english} ")
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i].strip(",.")
        if w in ("double", "triple") and i + 1 < len(words) and words[i + 1].strip(",.") in UNITS:
            out.append(str(UNITS[words[i + 1].strip(",.")]) * (2 if w == "double" else 3))
            i += 2
            continue
        if w in TENS:
            value = TENS[w]
            nxt = words[i + 1].strip(",.") if i + 1 < len(words) else ""
            if nxt in UNITS and UNITS[nxt] < 10:
                value += UNITS[nxt]
                i += 1
            out.append(str(value))
        elif w in UNITS and w != "o":
            out.append(str(UNITS[w]))
        else:
            out.append(words[i])
        i += 1
    # "2 point 5" -> "2.5", so "two point five lakh" is 2.5 lakh and not 5 lakh.
    return re.sub(r"(\d+)\s+(?:point|dot)\s+(\d+)", r"\1.\2", " ".join(out))


def _digits(text: str) -> str:
    return re.sub(r"\D", "", _spoken_digits(text.lower()))


def _title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip(" ,.;:-")
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in text.split())


# --------------------------------------------------------------------------- one field each

def extract_mobile(text: str) -> str | None:
    digits = _digits(text)
    match = re.search(r"(?:91|0)?([6-9]\d{9})(?!\d)", digits)
    return match.group(1) if match else None


def extract_pan(text: str) -> str | None:
    # Spoken PANs arrive spaced out ("a b c p n 1 2 3 4 k") or with number words; join and look for the shape.
    joined = re.sub(r"[^A-Za-z0-9]", "", _spoken_digits(text.lower())).upper()
    match = re.search(r"[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z]", joined)
    return match.group(0) if match else None


def extract_email(text: str) -> str | None:
    spoken = re.sub(r"\s+at\s+|\s*@\s*", "@", text.lower())
    spoken = re.sub(r"\s+dot\s+|\s*\.\s*", ".", spoken)
    spoken = spoken.replace(" underscore ", "_").replace(" dash ", "-")
    match = re.search(r"[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)+", spoken.replace(" ", ""))
    return match.group(0) if match else None


def extract_pincode(text: str) -> str | None:
    match = re.search(r"(?<!\d)([1-9]\d{5})(?!\d)", _spoken_digits(text.lower()).replace(" ", ""))
    return match.group(1) if match else None


def extract_year(text: str) -> str | None:
    match = re.search(r"\b(19[5-9]\d|20\d{2})\b", _spoken_digits(text.lower()))
    return match.group(1) if match else None


def extract_date(text: str) -> str | None:
    t = _spoken_digits(text.lower())
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\s*,?\s*(19\d{2}|20\d{2})\b", t)
    if m and m.group(2) in MONTHS:
        day, month, year = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))
    else:
        m = re.search(r"\b(\d{1,2})[/\-. ](\d{1,2})[/\-. ](19\d{2}|20\d{2})\b", t)
        if not m:
            return None
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def extract_amount(text: str) -> int | None:
    t = _spoken_digits(text.lower()).replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(lakhs?|lacs?|lac|k|thousand|crore)\b", t)
    if m:
        factor = {"k": 1000, "thousand": 1000, "crore": 10_000_000}.get(m.group(2), 100_000)
        return int(float(m.group(1)) * factor)
    m = re.search(r"(?:₹|rs\.?|rupees|inr)\s*(\d{4,8})|(\d{5,8})\s*(?:rupees|rs)", t)
    if m:
        return int(m.group(1) or m.group(2))
    return None


def extract_tenure(text: str) -> str | None:
    t = _spoken_digits(text.lower())
    m = re.search(r"\b(\d{1,2})\s*(months?|month)\b", t)
    months = int(m.group(1)) if m else None
    if months is None:
        y = re.search(r"\b(\d(?:\.\d)?)\s*(years?|yrs?)\b", t)
        months = int(float(y.group(1)) * 12) if y else None
    return str(months) if months in TENURES else None


def extract_state(text: str) -> str | None:
    low = f" {text.lower()} "
    for state in sorted(STATES, key=len, reverse=True):
        if f" {state.lower()} " in low or f" {state.lower()}," in low or f" {state.lower()}." in low:
            return state
    for alias, state in STATE_ALIASES.items():
        if f" {alias} " in low:
            return state
    return None


def extract_industry(text: str) -> str | None:
    low = text.lower()
    for industry, words in INDUSTRY_WORDS:
        if any(w in low for w in words):
            return industry
    return None


def extract_constitution(text: str) -> str | None:
    low = text.lower()
    for value, words in CONSTITUTION_WORDS:
        if any(w in low for w in words):
            return value
    return None


def extract_name(text: str) -> str | None:
    t = re.sub(r"^(is|am|it's|its|called)\s+", "", text.strip(" ,.").lower())
    t = re.split(r"[,;.]|\s+(?:and|from|my|i am|i'm|aged?)\s+", t)[0]
    t = re.sub(r"\s+(hai|hain|hoon|hun|he|है|हूँ|हूं)$", "", t.strip())  # Hindi "... hai" ("is")
    words = [w for w in re.findall(r"[a-z.']+", t) if w not in ("mr", "mrs", "ms", "shri", "smt", "dr")]
    return _title(" ".join(words[:5])) if words else None


def extract_text(text: str) -> str | None:
    t = re.sub(r"^(is|am|are|at)\s+", "", text.strip(" ,.;:").strip())
    return _title(t)[:200] if len(t) >= 2 else None


def extract_business_name(text: str) -> str | None:
    """Up to the first comma or "in <place>": "Patel Kirana Store in Ahmedabad" -> "Patel Kirana Store"."""
    t = re.sub(r"^(is|called|named)\s+", "", text.strip(" ,.;:").strip(), flags=re.I)
    t = re.split(r"[,;]|\s+(?:in|at|located|based|which|and i|i need|i want)\s+", t, flags=re.I)[0]
    return _title(t)[:120] if len(t.strip()) >= 2 else None


FIELD_EXTRACTORS = {
    "full_name": extract_name, "mobile": extract_mobile, "email": extract_email, "pan": extract_pan,
    "date_of_birth": extract_date, "business_name": extract_business_name, "business_start_year": extract_year,
    "udyam_number": lambda t: (re.search(r"UDYAM[-A-Z0-9]+", t.upper().replace(" ", "")) or [None])[0],
    "gstin": lambda t: (re.search(r"\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]", re.sub(r"[^A-Za-z0-9]", "",
                                  _spoken_digits(t.lower())).upper()) or [None])[0],
    "industry": extract_industry, "constitution": extract_constitution, "address_line": extract_text,
    "city": lambda t: _title(" ".join(t.strip(" ,.").split()[:3])) if t.strip(" ,.") else None,
    "state": extract_state, "pincode": extract_pincode, "requested_amount": extract_amount,
    "tenure_months": extract_tenure, "loan_purpose": extract_text,
}


# --------------------------------------------------------------------------- whole sentence

def _segments(text: str) -> list[tuple[str, str]]:
    """Split a sentence at cue words: [(field, the words after its cue), ...]."""
    low = text.lower()
    hits: list[tuple[int, int, str]] = []
    taken: list[tuple[int, int]] = []
    for field, cues in CUES:
        for cue in sorted(cues, key=len, reverse=True):
            for m in re.finditer(rf"(?<![a-z]){re.escape(cue)}(?![a-z])", low):
                if any(s <= m.start() < e or s < m.end() <= e for s, e in taken):
                    continue
                hits.append((m.start(), m.end(), field))
                taken.append((m.start(), m.end()))
    hits.sort()
    out = []
    for i, (start, end, field) in enumerate(hits):
        stop = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        out.append((field, text[end:stop]))
    return out


def extract(transcript: str, field: str | None = None) -> dict[str, Any]:
    """Fields found in what the customer said. `field` restricts it to one box being dictated."""
    text = re.sub(r"\s+", " ", transcript).strip()
    found: dict[str, Any] = {}
    if field:
        value = FIELD_EXTRACTORS[field](text) if field in FIELD_EXTRACTORS else None
        return {"fields": {field: value} if value else {}, "heard": text}

    for name, chunk in _segments(text):
        if name in found:
            continue
        value = FIELD_EXTRACTORS[name](chunk)
        if value:
            found[name] = value
    # Details that are recognisable on their own, wherever they were said.
    whole = {"pan": extract_pan, "mobile": extract_mobile, "email": extract_email, "state": extract_state,
             "constitution": extract_constitution, "requested_amount": extract_amount, "tenure_months": extract_tenure}
    for name, fn in whole.items():
        if name not in found:
            value = fn(text)
            if value:
                found[name] = value
    if "industry" not in found:
        # Industry words in the business name are a weaker hint than words elsewhere, so look there last.
        rest = text.replace(found.get("business_name", ""), " ")
        found["industry"] = extract_industry(rest) or extract_industry(found.get("business_name", "")) or None
        if not found["industry"]:
            del found["industry"]
    if "pincode" not in found:
        rest = text
        for key in ("mobile", "requested_amount"):
            rest = rest.replace(str(found.get(key, "")), " ")
        value = extract_pincode(rest) if re.search(r"\b(pin|pincode|postal|zip)\b", text.lower()) else None
        if not value and found.get("state"):
            # "... Gujarat 380001": six digits right after the state.
            m = re.search(re.escape(found["state"]) + r"[\s,]*((?:\d\s*){6})(?!\d)", _spoken_digits(text), re.I)
            value = re.sub(r"\s", "", m.group(1)) if m else None
        if value:
            found["pincode"] = value
    # City: the word before the state, when the address was spoken as "..., Bengaluru, Karnataka ...".
    if "city" not in found and found.get("state"):
        m = re.search(r"([A-Za-z][A-Za-z ]{1,40}?)[ ,]+" + re.escape(found["state"]), text, re.I)
        if m:
            found["city"] = _title(m.group(1).split(",")[-1].split()[-1])
    if found.get("address_line"):
        # An address often runs on into the city, state and PIN; keep the street part.
        cut = found["address_line"]
        for key in ("city", "state", "pincode"):
            if found.get(key):
                cut = re.split(re.escape(str(found[key])), cut, flags=re.I)[0]
        found["address_line"] = cut.strip(" ,.") or found["address_line"]
    return {"fields": found, "heard": text}
