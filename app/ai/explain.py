"""Borrower-safe explanation builder and conversational coach."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.ai.gateway import LLMGateway, LLMUnavailable, UsageMeter
from app.ai.schemas import CoachDraft, ExplanationDraft
from app.domain.formatting import emi, inr, pct
from app.domain.models import Case, Explanation, Outcome
from app.risk.engine import band_rate
from app.security.injection import detect

TEXTS_PATH = Path(__file__).resolve().parents[1] / "risk" / "config" / "reason_texts.yaml"
INTERNAL_TERMS = re.compile(r"\b(score|band|pd|probability of default|fraud|forensic|signal|FR-[A-Z]+|RC-[A-Z-]+|"
                            r"model|gemini|policy engine|tamper\w*|forg\w*)\b", re.I)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@lru_cache
def texts() -> dict[str, Any]:
    return yaml.safe_load(TEXTS_PATH.read_text(encoding="utf-8"))


def _approved_material(case: Case, lang: str) -> dict[str, Any]:
    t = texts()
    decision = case.decision
    assert decision and case.policy
    outcome = decision.final_outcome
    integrity = outcome == Outcome.REFER_INTEGRITY_REVIEW or (
        "RC-DOCUMENT-VERIFICATION" in case.policy.reason_codes
        and outcome not in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS))
    amount = decision.approved_amount or case.policy.recommended_amount
    tenure = case.policy.tenure_months
    outcome_key = "REFER_INTEGRITY_REVIEW" if integrity else outcome.value
    sentences = [t["outcomes"][outcome_key][lang].format(amount=inr(amount), tenure=tenure)]
    key_terms: list[str] = []
    if outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS):
        if amount < case.requested_amount:
            sentences.append(t["reduced_amount"][lang].format(requested=inr(case.requested_amount)))
        rate = case.policy.indicative_annual_rate or (band_rate(case.score.band, case.jurisdiction)
                                                      if case.score else None)
        if rate:
            key_terms.append(t["key_terms"][lang].format(emi=inr(emi(amount, rate, tenure)), tenure=tenure,
                                                         rate=f"{float(rate) * 100:.0f}"))
    if integrity:
        reason_codes = ["RC-DOCUMENT-VERIFICATION"]
        steps = [t["steps"]["RC-DOCUMENT-VERIFICATION"][lang]]
    else:
        reason_codes = [c for c in case.policy.reason_codes if c in t["reasons"]][:3]
        if outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS):
            reason_codes = []
        steps = []
        for step in case.improvement_plan:
            template = t["steps"].get(step.hint_key, {}).get(lang)
            if template:
                steps.append(template.format(target_pct=pct(step.target)))
        if not steps and outcome in (Outcome.DECLINE, Outcome.REFER):
            steps = [t["steps"][c][lang] for c in reason_codes if c in t["steps"] and "{" not in t["steps"][c][lang]][:2]
    return {
        "language": lang, "integrity_review": integrity, "outcome_sentences": sentences,
        "reason_texts": [t["reasons"][c][lang] for c in reason_codes], "step_texts": steps[:3],
        "key_terms": key_terms, "contact": t["contact"][lang],
    }


def _template_explanation(material: dict[str, Any]) -> Explanation:
    return Explanation(language=material["language"], outcome_sentence=" ".join(material["outcome_sentences"]),
                       reasons=material["reason_texts"], next_steps=material["step_texts"],
                       key_terms=material["key_terms"], contact=material["contact"],
                       integrity_review=material["integrity_review"], author="templates")


def _numbers_subset(text: str, source: str) -> bool:
    allowed = {n.replace(",", "") for n in NUMBER.findall(source)}
    return all(n.replace(",", "") in allowed for n in NUMBER.findall(text))


async def build_explanation(case: Case, lang: str, gateway: LLMGateway, meter: UsageMeter) -> Explanation:
    material = _approved_material(case, lang)
    fallback = _template_explanation(material)
    if not gateway.enabled:
        return fallback
    try:
        draft = await gateway.structured(prompt_id="explanation", schema=ExplanationDraft, meter=meter,
                                         contents=[json.dumps(material, ensure_ascii=False)])
    except LLMUnavailable:
        return fallback
    produced = " ".join([draft.outcome_sentence, *draft.reasons, *draft.next_steps, *draft.key_terms])
    source = json.dumps(material, ensure_ascii=False)
    if INTERNAL_TERMS.search(produced) or not _numbers_subset(produced, source):
        return fallback  # never show text that adds figures or leaks internal terms
    return Explanation(language=lang, outcome_sentence=draft.outcome_sentence, reasons=draft.reasons[:3],
                       next_steps=draft.next_steps[:3], key_terms=draft.key_terms[:2], contact=material["contact"],
                       integrity_review=material["integrity_review"], author=gateway.provider)


# --------------------------------------------------------------------------- coach

INTENTS: dict[str, tuple[str, ...]] = {
    "human": ("human", "person", "officer", "call", "talk to", "अधिकारी", "बात", "कॉल", "इंसान", "petugas", "telepon",
              "orang", "manusia", "hubungi"),
    "why": ("why", "reason", "kyon", "kyun", "क्यों", "कारण", "वजह", "kenapa", "mengapa", "alasan"),
    "improve": ("improve", "better", "next time", "increase", "what can i do", "सुधार", "बेहतर", "बढ़", "क्या करूँ",
                "tingkatkan", "perbaiki", "lebih baik", "apa yang bisa"),
    "terms": ("emi", "instalment", "installment", "interest", "rate", "amount", "how much", "कितना", "किश्त", "ब्याज",
              "राशि", "cicilan", "bunga", "berapa", "jumlah"),
    "distress": ("worried", "stress", "hardship", "can't pay", "cannot pay", "परेशान", "चिंता", "मुश्किल", "susah",
                 "khawatir", "sulit"),
}
SAFE_REDIRECT = {
    "en": "I can only help with your application, the reasons for the decision, and what to do next.",
    "hi": "मैं केवल आपके आवेदन, फ़ैसले के कारणों और आगे क्या करना है, इसमें मदद कर सकता हूँ।",
    "id": "Saya hanya dapat membantu soal pengajuan Anda, alasan keputusan, dan langkah selanjutnya.",
}
EMPATHY = {
    "en": "I understand this is stressful, and you do not have to work it out alone.",
    "hi": "मैं समझता हूँ कि यह चिंता की बात है, और आपको यह अकेले नहीं सुलझाना है।",
    "id": "Saya mengerti ini membuat khawatir, dan Anda tidak perlu menghadapinya sendiri.",
}
NONE_LEFT = {
    "en": "There is nothing more you need to do right now.",
    "hi": "अभी आपको और कुछ करने की ज़रूरत नहीं है।",
    "id": "Saat ini tidak ada lagi yang perlu Anda lakukan.",
}


def _intent(message: str) -> str:
    low = message.lower()
    for name in ("distress", "human", "why", "improve", "terms"):
        if any(k in low for k in INTENTS[name]):
            return name
    return "summary"


def coach_offline(expl: Explanation, message: str) -> CoachDraft:
    lang = expl.language
    if detect(message):
        return CoachDraft(reply=SAFE_REDIRECT[lang], offer_callback=False)
    intent = _intent(message)
    if intent == "distress":
        return CoachDraft(reply=f"{EMPATHY[lang]} {expl.contact}", offer_callback=True)
    if intent == "human":
        return CoachDraft(reply=expl.contact, offer_callback=True)
    if intent == "why":
        body = " ".join(expl.reasons) if expl.reasons else expl.outcome_sentence
        return CoachDraft(reply=body, offer_callback=False)
    if intent == "improve":
        body = " ".join(expl.next_steps) if expl.next_steps else NONE_LEFT[lang]
        return CoachDraft(reply=body, offer_callback=False)
    if intent == "terms":
        return CoachDraft(reply=" ".join(expl.key_terms) or expl.outcome_sentence, offer_callback=False)
    parts = [expl.outcome_sentence, *expl.reasons[:1], *expl.next_steps[:1]]
    return CoachDraft(reply=" ".join(parts), offer_callback=False)


GLOSSARY_QUESTION = re.compile(r"\b(what is|what's|what does|meaning|mean|explain|kya hai|kya hota|apa itu|apa arti)\b"
                               r"|\b(apr|kfs|emi|nach|cooling|prepay|penal|grievance|complain|udyam|gstin|cibil|"
                               r"aggregator|password)\b", re.I)


async def coach_reply(expl: Explanation, message: str, gateway: LLMGateway, meter: UsageMeter) -> tuple[CoachDraft, str]:
    if not detect(message) and GLOSSARY_QUESTION.search(message):
        from app.knowledge.search import search

        hits = search(message, audience="customer", k=1)
        if hits:
            return CoachDraft(reply=f"{hits[0]['title']}: {hits[0]['text']}", offer_callback=False), "glossary"
    if detect(message) or not gateway.enabled:
        return coach_offline(expl, message), "templates"
    payload = {"language": expl.language, "explanation": expl.model_dump(), "user_message": message[:500]}
    try:
        draft = await gateway.structured(prompt_id="coach", schema=CoachDraft, meter=meter,
                                         contents=[json.dumps(payload, ensure_ascii=False)])
    except LLMUnavailable:
        return coach_offline(expl, message), "templates"
    source = json.dumps(expl.model_dump(), ensure_ascii=False)
    if INTERNAL_TERMS.search(draft.reply) or not _numbers_subset(draft.reply, source) or len(draft.reply) > 900:
        return coach_offline(expl, message), "templates"
    return draft, "gemini"
