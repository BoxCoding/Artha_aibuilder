"""Train and evaluate the learned risk model against the transparent scorecard.

    python -m evals.train_risk_model --n 1500

Writes app/risk/artifacts/ (model + metrics) and docs/MODEL_METRICS.md. Exits non-zero if the
learned model fails to beat the scorecard or if its probabilities are badly calibrated, so a bad
model cannot be shipped by accident.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.cashflow.categorise import apply_rules  # noqa: E402
from app.cashflow.features import compute_features  # noqa: E402
from app.risk.engine import score  # noqa: E402
from app.risk.ml_model import (ModelMetrics, MODEL_VERSION, expected_calibration_error, gini,  # noqa: E402
                               ks_statistic, save, to_vector, train)
from app.risk.outcomes import build_training_case, simulate_outcome  # noqa: E402
from app.synthetic.generator import random_population  # noqa: E402

GATES = {"min_gini_uplift": 0.01, "max_calibration_error": 0.06}


def build_dataset(n: int, seed: int) -> tuple[list[dict[str, float]], list[int], list[float]]:
    rng = random.Random(seed)
    rows: list[dict[str, float]] = []
    labels: list[int] = []
    scorecard_pd: list[float] = []
    for index, statement in enumerate(random_population(n, seed=seed)):
        if statement.labels.tampered or statement.labels.injection:
            continue  # credit behaviour only; forged documents are the integrity layer's problem
        case = build_training_case(statement, index)
        apply_rules(case.transactions)
        try:
            features, _ = compute_features(case)
        except ValueError:
            continue
        outcome = simulate_outcome(statement.profile, rng) if statement.profile else None
        if outcome is None:
            continue
        rows.append(features)
        labels.append(int(outcome.defaulted))
        scorecard_pd.append(score(features).pd)
    return rows, labels, scorecard_pd


def fit_scorecard_intercept(rows: list[dict[str, float]], labels: list[int]) -> tuple[float, float]:
    """Refit only the intercept, holding every weight fixed.

    The weights encode the directions a credit team is willing to defend; the intercept is what
    ties them to an observed base rate. Refitting it is standard scorecard development, and
    without it the scorecard and the learned model are not on the same probability scale, which
    would make any disagreement control fire on every case.
    """
    import math

    from app.risk.engine import load_scorecard

    card = load_scorecard()
    base = []
    for features in rows:
        logit = 0.0
        for name, spec in card["features"].items():
            x = min(max(features[name], spec["cap_low"]), spec["cap_high"])
            logit += spec["weight"] * x
        base.append(logit)

    best, best_loss = card["intercept"], float("inf")
    for step in range(-400, 200):
        intercept = step / 100
        loss = 0.0
        for logit, label in zip(base, labels):
            p = 1 / (1 + math.exp(-(intercept + logit)))
            p = min(max(p, 1e-6), 1 - 1e-6)
            loss -= label * math.log(p) + (1 - label) * math.log(1 - p)
        if loss < best_loss:
            best, best_loss = intercept, loss
    return best, best_loss / max(1, len(labels))


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the LedgerLens risk challenger model.")
    parser.add_argument("--n", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=20260926)
    args = parser.parse_args()

    rows, labels, _ = build_dataset(args.n, args.seed)
    if len(set(labels)) < 2:
        print("dataset has a single class; nothing to learn")
        return 1
    print(f"dataset: {len(rows)} cases, default rate {sum(labels) / len(labels):.3f}")

    intercept, logloss = fit_scorecard_intercept(rows, labels)
    print(f"scorecard intercept fitted to the base rate: {intercept} (log loss {logloss:.4f})")
    _write_intercept(intercept)
    scorecard_pd = [score(f).pd for f in rows]

    estimator, evaluation = train(rows, labels, seed=args.seed)

    # Score the scorecard on exactly the same held-out rows, so the comparison is honest.
    import numpy as np
    from sklearn.metrics import brier_score_loss, roc_auc_score

    X_test, y_test = evaluation.pop("_X_test"), evaluation.pop("_y_test")
    lookup = {tuple(to_vector(r)): pd for r, pd in zip(rows, scorecard_pd)}
    held_out_scorecard = [lookup[tuple(np.asarray(v, dtype=float).tolist())] for v in X_test]

    metrics = ModelMetrics(
        version=MODEL_VERSION,
        trained_on=evaluation["trained_on"], tested_on=evaluation["tested_on"],
        default_rate=round(evaluation["default_rate"], 4),
        ml_auc=round(evaluation["ml_auc"], 4), ml_gini=round(evaluation["ml_gini"], 4),
        ml_ks=round(evaluation["ml_ks"], 4), ml_brier=round(evaluation["ml_brier"], 5),
        scorecard_auc=round(float(roc_auc_score(y_test, held_out_scorecard)), 4),
        scorecard_gini=round(gini(y_test.tolist(), held_out_scorecard), 4),
        scorecard_ks=round(ks_statistic(y_test.tolist(), held_out_scorecard), 4),
        scorecard_brier=round(float(brier_score_loss(y_test, held_out_scorecard)), 5),
        gini_uplift=round(evaluation["ml_gini"] - gini(y_test.tolist(), held_out_scorecard), 4),
        calibration_error=round(evaluation["calibration_error"], 4),
    )
    save(estimator, metrics)
    write_report(metrics)
    print(json.dumps(metrics.as_dict(), indent=2))

    failures = []
    if metrics.gini_uplift < GATES["min_gini_uplift"]:
        failures.append(f"gini uplift {metrics.gini_uplift} below {GATES['min_gini_uplift']}")
    if metrics.calibration_error > GATES["max_calibration_error"]:
        failures.append(f"calibration error {metrics.calibration_error} above {GATES['max_calibration_error']}")
    if failures:
        print("GATE FAILURES: " + "; ".join(failures))
        return 1
    print("all model gates passed")
    return 0


def _write_intercept(intercept: float) -> None:
    """Persist the fitted intercept into the versioned scorecard config."""
    from app.risk.engine import CONFIG_DIR, load_policy, load_scorecard

    path = CONFIG_DIR / "scorecard_v1.yaml"
    text = path.read_text(encoding="utf-8")
    current = load_scorecard()["intercept"]
    text = text.replace(f"intercept: {current}", f"intercept: {intercept}", 1)
    path.write_text(text, encoding="utf-8")
    load_scorecard.cache_clear()
    load_policy.cache_clear()


def write_report(m: ModelMetrics) -> None:
    lines = [
        "# Learned risk model — metrics",
        "",
        f"Artifact `{m.version}`, trained on {m.trained_on} synthetic cases and evaluated on a held-out "
        f"{m.tested_on}. Simulated default rate {m.default_rate:.1%}.",
        "",
        "| Metric | Scorecard (decides) | Learned model (challenges) |",
        "| --- | --- | --- |",
        f"| AUC | {m.scorecard_auc} | {m.ml_auc} |",
        f"| Gini | {m.scorecard_gini} | {m.ml_gini} |",
        f"| KS | {m.scorecard_ks} | {m.ml_ks} |",
        f"| Brier score | {m.scorecard_brier} | {m.ml_brier} |",
        "",
        f"Gini uplift from the learned model: **{m.gini_uplift:+.4f}**. Expected calibration error "
        f"{m.calibration_error:.4f}.",
        "",
        "## How the two are used together",
        "",
        "The scorecard decides, because a borrower and a regulator are owed a reason they can follow, and its "
        "monotonic weights give one. The learned model runs beside it and does two jobs: it measures how much "
        "signal a linear view leaves on the table, and it acts as a control — when the two models disagree "
        "materially about a case, the case goes to a human rather than to whichever number is more convenient.",
        "",
        "## What these numbers are not",
        "",
        "Labels are simulated from each business's latent parameters, not observed from repayment, because no "
        "synthetic borrower has ever repaid anything. The simulation deliberately contains one interaction a "
        "linear model cannot express — volatility only bites when the balance buffer is thin — so the uplift "
        "above is a measurement of that specific non-linearity under controlled conditions. It says the "
        "training, calibration, gating and fallback machinery works. It says nothing about real-world "
        "discrimination, and the model must be refitted on observed defaults, then tested for disparate impact "
        "by segment, before it goes anywhere near a lending decision.",
    ]
    path = Path(__file__).resolve().parents[1] / "docs" / "MODEL_METRICS.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
