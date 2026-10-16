"""Policy and knowledge retrieval: Vertex AI Search when configured, a local BM25 index otherwise.

Two audiences. Staff search the credit policy clauses, the reason-code guidance and the design docs
(model card, data card, responsible AI, threat model). Customers search only the glossary, so internal
policy thresholds never reach an applicant through search.

Every result carries its source (a clause ID, a glossary key or a document section), so an answer
built from it can be cited like a memo claim.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from app.config import get_settings

log = logging.getLogger("artha.knowledge")

ROOT = Path(__file__).resolve().parents[2]
DOCS = ("docs/RESPONSIBLE_AI.md", "docs/MODEL_CARD.md", "docs/DATA_CARD.md", "docs/THREAT_MODEL.md",
        "docs/ARCHITECTURE.md")
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "and", "or", "is", "are", "in", "for", "on", "what", "why", "how", "does",
         "do", "i", "my", "we", "our", "be", "it", "this", "that", "with", "by", "as", "at", "can", "if", "me"}


@dataclass(frozen=True)
class Passage:
    source: str
    title: str
    text: str
    audience: str  # "customer" (also visible to staff) or "staff"


SYNONYMS = {
    "two people": "second approver four eyes", "four eyes": "second approver", "maker checker": "second approver",
    "fake": "tampered edited forged", "forged": "tampered edited", "edited": "tampered editing",
    "bounce": "returned nach instalment", "bounced": "returned nach instalment", "sanction": "approval",
    "money laundering": "aml watchlist", "sanctions list": "watchlist aml", "interest": "rate apr",
    "loan size": "sizing amount", "how much": "sizing amount", "complaint": "grievance", "complain": "grievance complaint",
}
CLAUSE_TITLES = {
    "POL-INTEGRITY": "Integrity review for tampered or forged documents",
    "POL-META-VERIFY": "Verification when documents show editing traces",
    "POL-HISTORY": "Minimum statement history",
    "POL-BOUNCES": "Returned instalments (bounces)",
    "POL-BANDS": "Risk bands and outcomes",
    "POL-SIZING": "Loan sizing",
    "POL-MODEL-CHALLENGE": "When the two risk models disagree",
    "POL-FOUR-EYES": "Four-eyes approval for large loans",
    "POL-AML": "AML and watchlist screening",
    "POL-KFS": "Key Fact Statement before disbursal",
}


def _stem(w: str) -> str:
    for suffix in ("ings", "ing", "als", "al", "ers", "er", "es", "ed", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP]


def _expand(query: str) -> str:
    q = query.lower()
    return q + " " + " ".join(v for k, v in SYNONYMS.items() if k in q)


@lru_cache
def corpus() -> tuple[Passage, ...]:
    passages: list[Passage] = []
    glossary = yaml.safe_load((Path(__file__).with_name("glossary.yaml")).read_text(encoding="utf-8"))
    for key, entry in glossary.items():
        passages.append(Passage(f"glossary:{key}", entry["title"], entry["text"], "customer"))
    policy = yaml.safe_load((ROOT / "app/risk/config/policy_in.yaml").read_text(encoding="utf-8"))
    for clause, text in policy["clauses"].items():
        passages.append(Passage(clause, CLAUSE_TITLES.get(clause, f"Credit policy {clause}"), text, "staff"))
    passages.append(Passage("POL-SIZING-PARAMS", "Loan sizing parameters",
                            f"Amounts from {policy['sizing']['min_amount']} to {policy['sizing']['max_amount']} rupees; "
                            f"tenures {policy['sizing']['allowed_tenures']} months; indicative rates by band "
                            f"{policy['sizing']['indicative_annual_rate']}; four-eyes above {policy['four_eyes_above']}.",
                            "staff"))
    for rel in DOCS:
        path = ROOT / rel
        if not path.exists():
            continue
        section, lines = path.stem.replace("_", " ").title(), []
        for line in path.read_text(encoding="utf-8").splitlines() + ["## end"]:
            if line.startswith("#"):
                body = " ".join(" ".join(lines).split())
                if len(body) > 60:
                    passages.append(Passage(f"{rel}#{section[:40]}", section, body[:1200], "staff"))
                section, lines = line.lstrip("# ").strip(), []
            else:
                lines.append(line)
    return tuple(passages)


class LocalIndex:
    """Okapi BM25 over the corpus. Small enough to build at start-up in milliseconds."""

    def __init__(self, passages: tuple[Passage, ...], k1: float = 1.4, b: float = 0.75) -> None:
        self.passages = passages
        self.docs = [_terms(f"{p.title} {p.title} {p.text}") for p in passages]
        self.avg = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        self.df: dict[str, int] = {}
        for d in self.docs:
            for w in set(d):
                self.df[w] = self.df.get(w, 0) + 1
        self.k1, self.b = k1, b

    def search(self, query: str, audience: str, k: int = 5) -> list[tuple[Passage, float]]:
        q = _terms(_expand(query))
        n = len(self.docs)
        scored = []
        for p, d in zip(self.passages, self.docs):
            if audience == "customer" and p.audience != "customer":
                continue
            score = 0.0
            for w in q:
                tf = d.count(w)
                if not tf:
                    continue
                idf = math.log(1 + (n - self.df[w] + 0.5) / (self.df[w] + 0.5))
                score += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
            if score > 0:
                scored.append((p, score))
        return sorted(scored, key=lambda x: -x[1])[:k]


@lru_cache
def local_index() -> LocalIndex:
    return LocalIndex(corpus())


def vertex_search(query: str, k: int) -> list[dict[str, str]]:
    from app.gcp import get_gcp

    config = get_settings().vertex_search_serving_config
    body = {"query": query, "pageSize": k,
            "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}}}
    result = get_gcp().call("POST", f"https://discoveryengine.googleapis.com/v1/{config}:search", body)
    hits = []
    for r in result.get("results", []):
        data = r.get("document", {}).get("derivedStructData", {}) or r.get("document", {}).get("structData", {})
        snippets = data.get("snippets") or [{}]
        hits.append({"source": data.get("link") or r.get("id", "vertex"), "title": data.get("title", "Result"),
                     "text": snippets[0].get("snippet", "")[:1200], "engine": "vertex_ai_search"})
    return hits


def search(query: str, audience: str = "customer", k: int = 5) -> list[dict[str, str]]:
    """Top passages for a question. Staff queries use Vertex AI Search when configured; customer queries
    always use the local glossary so no internal document can reach an applicant."""
    query = query.strip()[:300]
    if not query:
        return []
    if audience == "staff" and get_settings().vertex_search_serving_config:
        try:
            hits = vertex_search(query, k)
            if hits:
                return hits
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the local index
            log.warning("vertex_search_failed", extra={"error_type": type(exc).__name__})
    return [{"source": p.source, "title": p.title, "text": p.text, "engine": "local_bm25", "score": round(s, 3)}
            for p, s in local_index().search(query, audience, k)]
