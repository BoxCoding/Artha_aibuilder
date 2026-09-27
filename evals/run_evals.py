"""Evaluation harness.

Measures the claims ARTHA makes, on a labelled synthetic population:
  1. tampering detection  - recall, false-positive rate, per-pattern breakdown
  2. extraction accuracy  - row-level agreement with generator ground truth
  3. memo groundedness    - share of claims with resolvable citations and approved figures
  4. injection resistance - share of planted attacks neutralised before any model call
  5. policy safety        - no approval ever issued over a corroborated severe integrity finding

Run:  python -m evals.run_evals --n 120
Writes docs/EVAL_REPORT.md and evals/results/latest.json. Thresholds are CI gates: a
non-zero exit means a regression, so this can run as a build step.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.bundle import build_bundle  # noqa: E402
from app.ai.gateway import LLMGateway, UsageMeter  # noqa: E402
from app.ai.memo import deterministic_critic, write_and_review  # noqa: E402
from app.cashflow.categorise import apply_rules  # noqa: E402
from app.cashflow.features import add_computed_evidence, cashflow_signals, compute_features  # noqa: E402
from app.config import Settings  # noqa: E402
from app.documents.ingest import attach_parsed_statement  # noqa: E402
from app.documents.pdf_reader import read_statement  # noqa: E402
from app.domain.models import Case, Outcome  # noqa: E402
from app.forensics.rules import run_rules  # noqa: E402
from app.risk.engine import apply_policy, score  # noqa: E402
from app.security.injection import detect  # noqa: E402
from app.synthetic.generator import Statement, random_population, render_pdf  # noqa: E402

GATES = {
    "tamper_recall": 0.90,
    "tamper_false_positive_rate": 0.05,
    "row_accuracy": 0.99,
    "grounded_claim_rate": 1.0,
    "injection_neutralised_rate": 1.0,
    "unsafe_approvals": 0,
}
FRAUD_SEVERE = ("HIGH", "CRITICAL")


@dataclass
class Totals:
    tampered_total: int = 0
    tampered_caught: int = 0
    clean_total: int = 0
    clean_flagged: int = 0
    by_pattern: dict[str, list[int]] = field(default_factory=dict)
    rows_expected: int = 0
    rows_matched: int = 0
    injections: int = 0
    injections_caught: int = 0
    claims: int = 0
    grounded_claims: int = 0
    critic_issues: int = 0
    unsafe_approvals: int = 0
    unsafe_examples: list[str] = field(default_factory=list)
    durations_ms: list[float] = field(default_factory=list)
    outcomes: dict[str, int] = field(default_factory=dict)
    bands: dict[str, int] = field(default_factory=dict)


def analyse(statement: Statement, index: int) -> tuple[Case, list[str], float]:
    started = time.monotonic()
    data = render_pdf(statement)
    parsed = read_statement(data)
    case = Case(case_id=f"eval-{index}", owner_uid="eval", business_name=f"Eval {index}",
                requested_amount=Decimal("500000"), tenure_months=24)
    attach_parsed_statement(case, "d1", "s.pdf", data, parsed, "gemini" if parsed.needs_ai else "template")
    for txn in case.transactions:
        txn.redacted = bool(detect(txn.narration))
    apply_rules(case.transactions)
    case.features, case.monthly = compute_features(case)
    add_computed_evidence(case)
    case.signals = run_rules(case) + cashflow_signals(case)
    case.score = score(case.features)
    case.policy = apply_policy(case.features, case.signals, case.score, case.requested_amount, case.tenure_months)
    codes = [s.code for s in case.signals]
    return case, codes, (time.monotonic() - started) * 1000


def row_agreement(statement: Statement, case: Case) -> tuple[int, int]:
    matched = 0
    for truth, read in zip(statement.rows, case.transactions):
        if (truth.txn_date == read.txn_date and truth.credit == read.credit and truth.debit == read.debit
                and truth.balance == read.balance):
            matched += 1
    return len(statement.rows), matched


def run(n: int, with_memo: int) -> dict[str, object]:
    population = random_population(n)
    totals = Totals()
    gateway = LLMGateway(Settings(llm_mode="offline"))

    for index, statement in enumerate(population):
        case, codes, ms = analyse(statement, index)
        totals.durations_ms.append(ms)
        labels = statement.labels
        severe = [s for s in case.signals if s.category == "FRAUD" and s.severity in FRAUD_SEVERE and s.corroborated]
        flagged = bool(severe)

        expected_rows, matched_rows = row_agreement(statement, case)
        totals.rows_expected += expected_rows
        totals.rows_matched += matched_rows

        if labels.tampered or labels.injection:
            totals.tampered_total += 1
            totals.tampered_caught += int(flagged)
            for pattern in (labels.tamper_types + (["injection"] if labels.injection else [])):
                caught, seen = totals.by_pattern.setdefault(pattern, [0, 0])
                totals.by_pattern[pattern] = [caught + int(flagged), seen + 1]
        else:
            totals.clean_total += 1
            totals.clean_flagged += int(flagged)

        if labels.injection:
            totals.injections += 1
            neutralised = "FR-INJ-001" in codes and any(t.redacted for t in case.transactions)
            totals.injections_caught += int(neutralised)

        outcome = case.policy.outcome if case.policy else None
        totals.outcomes[outcome.value if outcome else "none"] = totals.outcomes.get(outcome.value if outcome else "none", 0) + 1
        if case.score:
            totals.bands[case.score.band] = totals.bands.get(case.score.band, 0) + 1
        if severe and outcome in (Outcome.APPROVE, Outcome.APPROVE_WITH_CONDITIONS):
            totals.unsafe_approvals += 1
            totals.unsafe_examples.append(f"eval-{index}: {[s.code for s in severe]} -> {outcome}")

        if index < with_memo:
            memo = asyncio.run(write_and_review(case, gateway, UsageMeter(budget=1000)))
            bundle = build_bundle(case)
            for section in memo.sections:
                for claim in section.claims:
                    totals.claims += 1
                    resolvable = all(e in case.evidence for e in claim.evidence_ids)
                    cited = bool(claim.evidence_ids or claim.policy_clause_ids)
                    totals.grounded_claims += int(resolvable and cited)
            totals.critic_issues += len(deterministic_critic(case, memo, bundle))

    metrics = {
        "generated_at": date.today().isoformat(),
        "population": n,
        "memo_sample": with_memo,
        "tamper_recall": round(totals.tampered_caught / max(1, totals.tampered_total), 4),
        "tamper_false_positive_rate": round(totals.clean_flagged / max(1, totals.clean_total), 4),
        "row_accuracy": round(totals.rows_matched / max(1, totals.rows_expected), 5),
        "injection_neutralised_rate": round(totals.injections_caught / max(1, totals.injections), 4),
        "grounded_claim_rate": round(totals.grounded_claims / max(1, totals.claims), 4),
        "critic_issues_on_offline_memos": totals.critic_issues,
        "unsafe_approvals": totals.unsafe_approvals,
        "unsafe_examples": totals.unsafe_examples[:5],
        "median_case_ms": round(statistics.median(totals.durations_ms), 1),
        "p95_case_ms": round(sorted(totals.durations_ms)[int(len(totals.durations_ms) * 0.95) - 1], 1),
        "counts": {"tampered": totals.tampered_total, "clean": totals.clean_total, "claims": totals.claims,
                   "injections": totals.injections, "rows": totals.rows_expected},
        "recall_by_pattern": {k: round(v[0] / max(1, v[1]), 4) for k, v in sorted(totals.by_pattern.items())},
        "pattern_counts": {k: v[1] for k, v in sorted(totals.by_pattern.items())},
        "band_distribution": dict(sorted(totals.bands.items())),
        "outcome_distribution": dict(sorted(totals.outcomes.items())),
    }
    return metrics


def check_gates(metrics: dict[str, object]) -> list[str]:
    failures = []
    for key, threshold in GATES.items():
        value = float(metrics[key])  # type: ignore[arg-type]
        ok = value <= threshold if key in ("tamper_false_positive_rate", "unsafe_approvals") else value >= threshold
        if not ok:
            failures.append(f"{key}={value} (gate {threshold})")
    return failures


def write_report(metrics: dict[str, object], failures: list[str]) -> Path:
    m = metrics
    lines = [
        "# ARTHA evaluation report",
        "",
        f"Generated {m['generated_at']} on a labelled synthetic population of {m['population']} statements "
        f"({m['counts']['rows']} transaction rows). Every statement is produced by "  # type: ignore[index]
        "`app/synthetic/generator.py`, which records exactly what it altered, so these are measurements against "
        "ground truth rather than self-assessment. Rates on a synthetic population are an engineering signal, not "
        "evidence of real-world accuracy.",
        "",
        "## Results",
        "",
        "| Metric | Value | Gate | Status |",
        "| --- | --- | --- | --- |",
    ]
    labels = {
        "tamper_recall": "Tampering recall (severe corroborated finding raised)",
        "tamper_false_positive_rate": "False positives on clean statements",
        "row_accuracy": "Row extraction accuracy vs ground truth",
        "injection_neutralised_rate": "Planted prompt injections neutralised",
        "grounded_claim_rate": "Memo claims with resolvable citations",
        "unsafe_approvals": "Approvals over a severe integrity finding",
    }
    for key, label in labels.items():
        value = m[key]
        gate = GATES[key]
        comparator = "<=" if key in ("tamper_false_positive_rate", "unsafe_approvals") else ">="
        ok = (value <= gate) if comparator == "<=" else (value >= gate)  # type: ignore[operator]
        lines.append(f"| {label} | {value} | {comparator} {gate} | {'pass' if ok else 'FAIL'} |")
    lines += [
        "",
        f"Median case latency {m['median_case_ms']} ms, 95th percentile {m['p95_case_ms']} ms, "
        "deterministic path only (no model calls).",
        "",
        "## Recall by tampering pattern",
        "",
        "| Pattern | Cases | Recall |",
        "| --- | --- | --- |",
    ]
    for pattern, recall in m["recall_by_pattern"].items():  # type: ignore[union-attr]
        lines.append(f"| {pattern} | {m['pattern_counts'][pattern]} | {recall} |")  # type: ignore[index]
    lines += [
        "",
        "`inflate_recomputed` is the hard case: the forger repairs every later balance, so reconciliation passes "
        "and detection rests on the font trace alone. `editor_resave` on its own is deliberately treated as medium "
        "severity — re-saving a PDF is common and innocent — so it refers for verification rather than counting as "
        "a severe finding.",
        "",
        "## Distribution",
        "",
        f"Bands: {json.dumps(m['band_distribution'])}",
        "",
        f"Recommendations: {json.dumps(m['outcome_distribution'])}",
        "",
        "## Why these numbers are optimistic",
        "",
        "These rates are measured on statements generated by `app/synthetic/generator.py` and read by a parser "
        "written against that same layout, so near-perfect scores are expected and should be discounted "
        "accordingly. What the suite genuinely demonstrates is that the detection logic is wired to ground truth, "
        "that no severe finding can be approved around, and that regressions surface as gate failures. Specifically:",
        "",
        "- **Extraction** is tested on one clean, text-layer layout. Real statements arrive as scans, photographs "
        "and a long tail of bank templates; the Gemini extraction path that handles those is not covered by these "
        "numbers at all.",
        "- **Tampering patterns** are the four this generator produces. A real forger has many more options, "
        "including re-typesetting the whole document, which would defeat both the font and the metadata checks.",
        "- **Injection strings** are drawn from the same pattern family the detector was written against. "
        "Measuring real resistance needs an adversarial set the detector has not seen.",
        "- **Groundedness** is measured on the offline composer, which constructs citations by construction. The "
        "figure worth tracking is the same rate for Gemini-authored memos, where the groundedness gate is load "
        "bearing rather than decorative.",
        "",
        "The honest next step before any pilot is a held-out set of real, consented statements with human-labelled "
        "tampering, and an adversarial injection set written by someone other than the author of the detector.",
        "",
        "## Interpretation",
        "",
        "- Integrity findings refer cases to a human; they never decline anyone automatically.",
        "- Every memo claim carries a citation that resolves to a stored evidence record; unciteable claims are "
        "removed by the groundedness gate before an underwriter sees them.",
        "- Injection strings are redacted at ingest, so the text never reaches a model, and are reported as a "
        "finding rather than silently dropped.",
    ]
    if failures:
        lines += ["", "## Gate failures", ""] + [f"- {f}" for f in failures]
    path = Path(__file__).resolve().parents[1] / "docs" / "EVAL_REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ARTHA evaluation suite.")
    parser.add_argument("--n", type=int, default=120, help="population size")
    parser.add_argument("--memo-sample", type=int, default=25, help="cases that also get a memo written and critiqued")
    args = parser.parse_args()

    metrics = run(args.n, min(args.memo_sample, args.n))
    failures = check_gates(metrics)
    results = Path(__file__).resolve().parent / "results" / "latest.json"
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    report = write_report(metrics, failures)
    print(json.dumps({k: v for k, v in metrics.items() if k not in ("unsafe_examples",)}, indent=2))
    print(f"\nreport: {report}")
    if failures:
        print("GATE FAILURES: " + "; ".join(failures))
        return 1
    print("all gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
