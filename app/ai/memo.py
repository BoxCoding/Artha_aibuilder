"""Credit memo: writer -> critic loop -> groundedness gate.

The writer is Gemini when AI mode is on, or a deterministic composer offline. The critic always
runs deterministic checks, and adds a Gemini review when available. The gate removes any claim
that cites nothing, cites an unknown ID, quotes an unapproved figure, or relies on a protected
attribute - so what the underwriter sees is traceable by construction.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.ai.bundle import build_bundle
from app.ai.gateway import LLMGateway, LLMUnavailable, UsageMeter
from app.ai.schemas import CriticDraft, MemoDraft
from app.domain.models import Case, Claim, CreditMemo, CriticIssue, MemoSection, Outcome

log = logging.getLogger("artha.memo")

SECTION_TITLES = {
    "summary": "Summary", "cashflow": "Business cash flow", "repayment_capacity": "Repayment capacity",
    "integrity": "Document integrity", "risks_and_mitigants": "Risks and mitigants", "conditions": "Conditions",
}
OUTCOME_LABEL = {
    Outcome.APPROVE: "approval", Outcome.APPROVE_WITH_CONDITIONS: "approval with conditions",
    Outcome.REFER: "referral to a senior underwriter", Outcome.DECLINE: "decline",
    Outcome.REFER_INTEGRITY_REVIEW: "referral to integrity review",
}
REASON_LABEL = {
    "RC-PAYMENT-RETURNS": "returned loan instalments", "RC-LOW-BALANCE-BUFFER": "thin balance buffer",
    "RC-FREQUENT-LOW-BALANCE": "frequent near-zero balances", "RC-VOLATILE-INCOME": "volatile monthly inflows",
    "RC-CASH-HEAVY-INCOME": "high share of cash income", "RC-HIGH-EXISTING-DEBT": "existing instalment burden",
    "RC-SHORT-HISTORY": "short statement history", "RC-DECLINING-INCOME": "declining inflows",
    "RC-DOCUMENT-VERIFICATION": "documents that could not be verified",
    "RC-LOW-REPAYMENT-CAPACITY": "low repayment capacity",
}
PROTECTED_TERMS = re.compile(
    r"\b(gender|female|male|woman|women|man's|wife|husband|widow|married|marital|religio\w*|hindu|muslim|christian|"
    r"sikh|jain|caste|dalit|brahmin|ethnic\w*|disab\w*|pregnan\w*|age of|years old)\b", re.I)
DATE_PATTERNS = re.compile(r"\b\d{1,2} [A-Z][a-z]{2} \d{4}\b|\b\d{4}-\d{2}(-\d{2})?\b|\bpage \d+\b|\b[A-Z]{2,4}-[A-Z0-9-]+\b")
NUMBER_TOKEN = re.compile(r"₹?\d[\d,]*(?:\.\d+)?%?")


# --------------------------------------------------------------------------- offline composer

def compose_offline(case: Case, drop: set[str] | None = None) -> CreditMemo:
    assert case.score and case.policy
    b = build_bundle(case)
    n = b["allowed_numbers"]
    ev = case.evidence
    header = next((k for k, v in ev.items() if v.kind == "HEADER"), next(iter(ev)))

    def cite(*ids: str) -> list[str]:
        return [i for i in ids if i in ev]

    sections: dict[str, list[tuple[str, list[str], list[str]]]] = {k: [] for k in SECTION_TITLES}
    p, s, f = case.policy, case.score, case.features
    eligible = p.outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS)
    ko_clauses = [c for c in p.triggered_rules if c not in ("POL-BANDS", "POL-SIZING", "POL-FOUR-EYES")]

    sections["summary"].append((f"{case.business_name} requested {n['requested_amount']} over {n['tenure_months']}. "
                                f"The policy engine recommends {OUTCOME_LABEL[p.outcome]}.",
                                cite(header), ["POL-BANDS", *ko_clauses]))
    sections["summary"].append((f"The scorecard places the application in band {n['band']}, with an estimated "
                                f"probability of default of {n['pd']}.", cite("ev-computed-inflow", "ev-computed-balance"),
                                ["POL-BANDS"]))
    if eligible:
        sections["summary"].append((f"The recommended amount is {n['recommended_amount']} over {n['policy_tenure']} at an "
                                    f"indicative {n['indicative_annual_rate']} a year.", [], ["POL-SIZING"]))

    sections["cashflow"].append((f"Across {n['statement_span_months']}, median monthly business inflow is "
                                 f"{n['median_monthly_inflow']}; {n['digital_share']} arrives digitally and "
                                 f"{n['cash_deposit_share']} as cash deposits.",
                                 cite("ev-computed-inflow", "ev-computed-cash"), []))
    sections["cashflow"].append((f"Monthly inflows vary by {n['inflow_cv']} around the average, and the later months "
                                 f"differ from the earlier months by {n['inflow_trend']}.", cite("ev-computed-inflow"), []))
    sections["cashflow"].append((f"The average daily balance is {n['avg_daily_balance']}, or {n['abb_to_inflow']} of a "
                                 f"typical month's inflow; the balance was very low on {n['low_balance_days_share']} of days.",
                                 cite("ev-computed-balance"), []))

    if f["existing_emi_monthly"] > 0:
        sections["repayment_capacity"].append((f"The statement shows existing loan instalments of about "
                                               f"{n['existing_emi_monthly']} a month, {n['obligation_ratio']} of inflow.",
                                               cite("ev-computed-emi"), []))
    else:
        sections["repayment_capacity"].append(("No existing loan instalments are visible in the statement.",
                                               cite("ev-computed-balance"), []))
    if f["nach_returns_6m"] > 0:
        sections["repayment_capacity"].append((f"{n['nach_returns_6m']} instalment(s) were returned unpaid, "
                                               f"{n['nach_returns_3m']} of them in the last three months.",
                                               cite("ev-computed-returns"), ["POL-BOUNCES"] if "POL-BOUNCES" in p.triggered_rules else []))
    sections["repayment_capacity"].append((f"Median monthly surplus after operating outflows is {n['monthly_surplus']}, "
                                           f"which supports a new instalment of up to {n['max_emi_capacity']} under the "
                                           "sizing rule.", cite("ev-computed-inflow"), ["POL-SIZING"]))

    fraud = [sig for sig in case.signals if sig.category == "FRAUD"]
    for sig in fraud:
        sections["integrity"].append((f"{sig.title}. {sig.explanation}", sig.evidence_ids,
                                      ["POL-INTEGRITY"] if "POL-INTEGRITY" in p.triggered_rules else []))
    if not fraud:
        sections["integrity"].append(("The document checks raised no integrity findings on the submitted statement.",
                                      cite(header), []))

    if s.reasons:
        labels = ", ".join(REASON_LABEL.get(r.code, r.code) for r in s.reasons[:3])
        sections["risks_and_mitigants"].append((f"The main scorecard risk drivers are {labels}.",
                                                cite("ev-computed-balance", "ev-computed-inflow"), ["POL-BANDS"]))
    for sig in (x for x in case.signals if x.category == "RISK"):
        sections["risks_and_mitigants"].append((f"{sig.title}. {sig.explanation}", sig.evidence_ids, []))
    for sig in (x for x in case.signals if x.category == "POSITIVE"):
        sections["risks_and_mitigants"].append((f"Mitigant: {sig.explanation}", sig.evidence_ids, []))

    for cond in p.conditions:
        clause = "POL-META-VERIFY" if "verify" in cond.lower() else "POL-BANDS"
        sections["conditions"].append((cond, [], [clause]))
    if p.outcome == Outcome.REFER_INTEGRITY_REVIEW:
        sections["conditions"].append(("Do not decide on the submitted documents; obtain the statement directly from the "
                                       "bank before any further assessment.", [], ["POL-INTEGRITY"]))
    if "POL-FOUR-EYES" in p.triggered_rules:
        sections["conditions"].append(("A second approver must countersign this approval.", [], ["POL-FOUR-EYES"]))

    questions = []
    codes = {sig.code for sig in case.signals}
    if "FR-TXN-002" in codes:
        questions.append("What was the source of the large cash deposits made just before the application?")
    if "RK-NACH-RETURNS" in codes:
        questions.append("Why were instalments returned, and have the arrears been cleared?")
    if "FR-META-001" in codes:
        questions.append("Can the applicant authorise a direct statement fetch from the bank?")

    memo = CreditMemo(version=1, author="offline_composer", recommended_outcome=p.outcome, open_questions=questions,
                      sections=[])
    for key, claims in sections.items():
        if not claims:
            continue
        built = [Claim(claim_id=f"c-{key}-{i}", text=text, evidence_ids=list(dict.fromkeys(e)),
                       policy_clause_ids=list(dict.fromkeys(pc))) for i, (text, e, pc) in enumerate(claims)]
        built = [c for c in built if c.claim_id not in (drop or set())]
        memo.sections.append(MemoSection(key=key, title=SECTION_TITLES[key], claims=built))
    return memo


# --------------------------------------------------------------------------- deterministic critic & gate

def _numbers(text: str) -> list[tuple[float, bool]]:
    text = DATE_PATTERNS.sub(" ", text)
    out: list[tuple[float, bool]] = []
    for token in NUMBER_TOKEN.findall(text):
        is_pct = token.endswith("%")
        raw = token.replace("₹", "").replace(",", "").rstrip("%")
        try:
            out.append((float(raw), is_pct))
        except ValueError:
            continue
    return out


def _allowed(case: Case, bundle: dict[str, Any]) -> list[tuple[float, bool]]:
    pool = " ".join(bundle["allowed_numbers"].values())
    pool += " " + " ".join(sig.explanation for sig in case.signals)
    pool += " " + " ".join(ev.description for ev in case.evidence.values() if ev.kind in ("METADATA", "CELL"))
    values = _numbers(pool)
    values += [(float(i), False) for i in range(0, 13)]  # small counts: rows, months, sections
    return values


def _matches(value: tuple[float, bool], allowed: list[tuple[float, bool]]) -> bool:
    v, is_pct = value
    for a, a_pct in allowed:
        if is_pct != a_pct:
            continue
        tolerance = 0.51 if is_pct else max(1.0, abs(a) * 0.005)
        if abs(v - a) <= tolerance:
            return True
    return False


def deterministic_critic(case: Case, memo: CreditMemo, bundle: dict[str, Any]) -> list[CriticIssue]:
    issues: list[CriticIssue] = []
    clauses = set(bundle["policy_clauses"])
    allowed = _allowed(case, bundle)
    cited: set[str] = set()
    all_text = ""
    for section in memo.sections:
        for claim in section.claims:
            all_text += " " + claim.text
            cited.update(claim.evidence_ids)
            if not claim.evidence_ids and not claim.policy_clause_ids:
                issues.append(CriticIssue(claim_id=claim.claim_id, issue_type="unsupported", severity="blocker",
                                          explanation="Claim cites no evidence or policy clause.", source="deterministic"))
            bad = [e for e in claim.evidence_ids if e not in case.evidence] + \
                  [c for c in claim.policy_clause_ids if c not in clauses]
            if bad:
                issues.append(CriticIssue(claim_id=claim.claim_id, issue_type="invalid_citation", severity="blocker",
                                          explanation=f"Cites unknown IDs: {', '.join(bad[:4])}.", source="deterministic"))
            wrong = [v for v in _numbers(claim.text) if not _matches(v, allowed)]
            if wrong:
                shown = ", ".join(f"{w[0]:g}{'%' if w[1] else ''}" for w in wrong[:3])
                issues.append(CriticIssue(claim_id=claim.claim_id, issue_type="numeric_mismatch", severity="major",
                                          explanation=f"Figure(s) not found in the computed case data: {shown}.",
                                          source="deterministic"))
            if PROTECTED_TERMS.search(claim.text):
                issues.append(CriticIssue(claim_id=claim.claim_id, issue_type="protected_attribute", severity="blocker",
                                          explanation="Claim refers to a protected attribute.", source="deterministic"))
    if memo.recommended_outcome != (case.policy.outcome if case.policy else None):
        issues.append(CriticIssue(claim_id=None, issue_type="policy_inconsistency", severity="blocker",
                                  explanation="Memo outcome differs from the policy engine result.", source="deterministic"))
    for sig in case.signals:
        if sig.category in ("FRAUD", "RISK") and sig.severity in ("HIGH", "CRITICAL") and not cited & set(sig.evidence_ids):
            if sig.title.lower() not in all_text.lower():
                issues.append(CriticIssue(claim_id=None, issue_type="omitted_material_risk", severity="major",
                                          explanation=f"{sig.severity} finding '{sig.title}' is not addressed.",
                                          source="deterministic"))
    return issues


def groundedness_gate(memo: CreditMemo) -> CreditMemo:
    remove = {i.claim_id for i in memo.critic_issues
              if i.claim_id and i.issue_type in ("unsupported", "invalid_citation", "numeric_mismatch", "protected_attribute")}
    for section in memo.sections:
        kept = [c for c in section.claims if c.claim_id not in remove]
        memo.dropped_claims.extend(c.claim_id for c in section.claims if c.claim_id in remove)
        section.claims = kept
    memo.sections = [s for s in memo.sections if s.claims]
    return memo


# --------------------------------------------------------------------------- Gemini writer & critic

def _from_draft(draft: MemoDraft, version: int, author: str = "gemini") -> CreditMemo:
    sections = []
    for section in draft.sections:
        claims = [Claim(claim_id=f"c-{section.key}-{i}", text=c.text[:700], evidence_ids=c.evidence_ids,
                        policy_clause_ids=c.policy_clause_ids) for i, c in enumerate(section.claims[:4])]
        sections.append(MemoSection(key=section.key, title=SECTION_TITLES.get(section.key, section.title), claims=claims))
    return CreditMemo(version=version, author=author, sections=sections,  # type: ignore[arg-type]
                      recommended_outcome=Outcome(draft.recommended_outcome),
                      open_questions=clean_questions(draft.open_questions))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9₹%.,]+", text.lower()) if len(w) > 2}


def _similar(a: str, b: str, threshold: float = 0.7) -> bool:
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and len(wa & wb) / len(wa | wb) >= threshold


def clean_questions(questions: list[str], limit: int = 5, max_chars: int = 280) -> list[str]:
    """Open questions are free text, so bound them: no run-on loops, no near-repeats."""
    kept: list[str] = []
    for question in questions:
        sentences: list[str] = []
        for sentence in re.split(r"(?<=[.?!])\s+", " ".join(question.split())):
            if sentence and not any(_similar(sentence, s) for s in sentences):
                sentences.append(sentence)
        text = " ".join(sentences)
        text = text if len(text) <= max_chars else text[:max_chars].rsplit(" ", 1)[0] + "…"
        if text and not any(_similar(text, k) for k in kept):
            kept.append(text)
    return kept[:limit]


def quality_gate(memo: CreditMemo) -> tuple[CreditMemo, str | None]:
    """Model-agnostic checks a figure whitelist cannot make: repeated claims are removed, and a memo
    that covers too little of the case is rejected. Returns the memo and, if rejected, why."""
    seen: list[str] = []
    for section in memo.sections:
        kept = []
        for claim in section.claims:
            if any(_similar(claim.text, s) for s in seen):
                memo.dropped_claims.append(claim.claim_id)
                continue
            seen.append(claim.text)
            kept.append(claim)
        section.claims = kept
    memo.sections = [s for s in memo.sections if s.claims]
    covered = {s.key for s in memo.sections}
    published = sum(len(s.claims) for s in memo.sections)
    if len(covered) < 3:
        return memo, f"it covered only {len(covered)} of the memo's sections"
    if published < max(5, len(memo.dropped_claims)):
        return memo, f"only {published} of {published + len(memo.dropped_claims)} claims survived review"
    return memo, None


async def write_and_review(case: Case, gateway: LLMGateway, meter: UsageMeter, max_iterations: int = 2) -> CreditMemo:
    bundle = build_bundle(case)
    if not gateway.enabled:
        memo = compose_offline(case)
        memo.critic_issues = deterministic_critic(case, memo, bundle)
        memo.critic_verdict = "REVISE" if any(i.severity in ("blocker", "major") for i in memo.critic_issues) else "PASS"
        return groundedness_gate(memo)

    settings = gateway.settings
    memo: CreditMemo | None = None
    issues_payload: list[dict[str, Any]] | None = None
    for iteration in range(1, max_iterations + 1):
        payload = build_bundle(case, critic_issues=issues_payload)
        try:
            draft = await gateway.structured(prompt_id="memo_writer", schema=MemoDraft, meter=meter,
                                             model=settings.model_reasoning, thinking="MEDIUM",
                                             contents=[json.dumps(payload, default=str, ensure_ascii=False)])
        except LLMUnavailable:
            memo = compose_offline(case)
            memo.open_questions.append("Gemini memo writer was unavailable; memo composed deterministically.")
            break
        memo = _from_draft(draft, iteration, gateway.provider)
        memo.iterations = iteration
        issues = deterministic_critic(case, memo, payload)
        try:
            review = await gateway.structured(
                prompt_id="critic", schema=CriticDraft, meter=meter, model=settings.model_reasoning, thinking="HIGH",
                contents=[json.dumps({"memo": memo.model_dump(mode="json"), "bundle": payload}, default=str,
                                     ensure_ascii=False)])
            known = {c.claim_id for s in memo.sections for c in s.claims}
            issues += [CriticIssue(claim_id=i.claim_id if i.claim_id in known else None, issue_type=i.issue_type,
                                   severity=i.severity, explanation=i.explanation[:400], source="llm")
                       for i in review.issues[:12]]
        except LLMUnavailable:
            pass
        memo.critic_issues = issues
        blocking = [i for i in issues if i.severity in ("blocker", "major")]
        memo.critic_verdict = "REVISE" if blocking else "PASS"
        if not blocking:
            break
        issues_payload = [i.model_dump() for i in blocking]
    assert memo is not None
    if memo.author == "offline_composer":
        memo.critic_issues = deterministic_critic(case, memo, bundle)
    gated = groundedness_gate(memo)
    if memo.author == "offline_composer":
        return gated
    gated, rejected = quality_gate(gated)
    if rejected:
        # The model's draft did not meet the bar (common with small local models). The underwriter
        # gets the deterministic memo rather than a thin or repetitive one, and is told why.
        log.info("memo_draft_rejected", extra={"case_id": case.case_id, "detail": rejected})
        fallback = compose_offline(case)
        fallback.critic_issues = deterministic_critic(case, fallback, bundle)
        fallback.open_questions.append(f"The {memo.author} model's draft was not used because {rejected}; this memo "
                                       "was composed deterministically from the same evidence.")
        return groundedness_gate(fallback)
    return gated
