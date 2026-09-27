"""The learned risk model: a monotonically constrained, calibrated gradient-boosted classifier.

How it is used matters more than what it is. In regulated lending the model that *decides* has to
be explainable to a borrower, a credit committee and a regulator, so the transparent scorecard
keeps that job. The learned model runs alongside it as a challenger, and earns its place two ways:

1. **Ranking.** Gini and KS against the scorecard say whether the non-linearity is worth anything.
2. **Disagreement as a control.** When the two models materially disagree about a case, that case
   goes to a human instead of being decided by whichever number is prettier. A disagreement is
   information: it means the case sits where a linear view and a learned view part company.

Constraints, not hope, keep it sane: every feature carries a monotonic direction, so more bounced
instalments can never lower the predicted risk, whatever a correlation in the training sample
suggests. Probabilities are isotonically calibrated on held-out data, because a ranking model's
raw scores are not probabilities and policy thresholds are expressed in probabilities.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

log = logging.getLogger("artha.ml")

ARTIFACT_DIR = Path(__file__).parent / "artifacts"
MODEL_PATH = ARTIFACT_DIR / "risk_model.joblib"
METRICS_PATH = ARTIFACT_DIR / "risk_model_metrics.json"
MODEL_VERSION = "risk-gbm.v1"

# Feature order is part of the artifact contract; +1 means "higher value, higher risk".
FEATURES: tuple[tuple[str, int], ...] = (
    ("nach_returns_6m", 1),
    ("nach_returns_3m", 1),
    ("obligation_ratio", 1),
    ("inflow_cv", 1),
    ("low_balance_days_share", 1),
    ("cash_deposit_share", 1),
    ("abb_to_inflow", -1),
    ("inflow_trend", -1),
    ("statement_span_months", -1),
    ("digital_share", -1),
)
FEATURE_NAMES = tuple(name for name, _ in FEATURES)
MONOTONIC = tuple(direction for _, direction in FEATURES)

# Two models on the same calibrated scale still differ case by case. Only a gap of this many risk
# bands counts as a real disagreement worth a human's time.
DISAGREEMENT_BANDS = 2


@dataclass(frozen=True)
class ModelMetrics:
    version: str
    trained_on: int
    tested_on: int
    default_rate: float
    ml_auc: float
    ml_gini: float
    ml_ks: float
    ml_brier: float
    scorecard_auc: float
    scorecard_gini: float
    scorecard_ks: float
    scorecard_brier: float
    gini_uplift: float
    calibration_error: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def to_vector(features: dict[str, float]) -> list[float]:
    return [float(features.get(name, 0.0)) for name in FEATURE_NAMES]


def gini(y_true: list[int], scores: list[float]) -> float:
    from sklearn.metrics import roc_auc_score

    return 2 * roc_auc_score(y_true, scores) - 1


def ks_statistic(y_true: list[int], scores: list[float]) -> float:
    from sklearn.metrics import roc_curve

    fpr, tpr, _ = roc_curve(y_true, scores)
    return float(max(tpr - fpr))


def expected_calibration_error(y_true: list[int], probabilities: list[float], bins: int = 10) -> float:
    """Mean absolute gap between predicted and realised default rate, bucketed by prediction."""
    paired = sorted(zip(probabilities, y_true))
    if not paired:
        return 0.0
    size = max(1, len(paired) // bins)
    total, weighted = 0, 0.0
    for start in range(0, len(paired), size):
        chunk = paired[start:start + size]
        if not chunk:
            continue
        predicted = sum(p for p, _ in chunk) / len(chunk)
        realised = sum(y for _, y in chunk) / len(chunk)
        weighted += abs(predicted - realised) * len(chunk)
        total += len(chunk)
    return weighted / max(1, total)


class RiskModel:
    """Thin wrapper over the fitted pipeline, with a hard fail-safe: if the artifact is missing or
    a prediction misbehaves, callers fall back to the scorecard rather than guessing."""

    def __init__(self, estimator: Any, metrics: ModelMetrics) -> None:
        self._estimator = estimator
        self.metrics = metrics

    @property
    def version(self) -> str:
        return self.metrics.version

    def predict_pd(self, features: dict[str, float]) -> float:
        import numpy as np

        vector = np.asarray([to_vector(features)], dtype=float)
        probability = float(self._estimator.predict_proba(vector)[0][1])
        if not 0.0 <= probability <= 1.0:
            raise ValueError(f"model returned an out-of-range probability: {probability}")
        return probability


@lru_cache
def load_model() -> RiskModel | None:
    """Load the trained artifact, or return None so the system runs on the scorecard alone."""
    if not MODEL_PATH.exists() or not METRICS_PATH.exists():
        return None
    try:
        import joblib

        estimator = joblib.load(MODEL_PATH)
        metrics = ModelMetrics(**json.loads(METRICS_PATH.read_text(encoding="utf-8")))
        return RiskModel(estimator, metrics)
    except Exception as exc:  # noqa: BLE001 - a broken artifact must never take the service down
        log.warning("risk_model_load_failed", extra={"error_type": type(exc).__name__})
        return None


def train(rows: list[dict[str, float]], labels: list[int], seed: int = 20260926) -> tuple[Any, dict[str, Any]]:
    """Fit the constrained, calibrated model. Returns the estimator and a held-out evaluation."""
    import numpy as np
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import brier_score_loss, roc_auc_score
    from sklearn.model_selection import train_test_split

    X = np.asarray([to_vector(r) for r in rows], dtype=float)
    y = np.asarray(labels, dtype=int)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.3, random_state=seed, stratify=y)

    def make_base() -> HistGradientBoostingClassifier:
        return HistGradientBoostingClassifier(
            max_depth=3, max_iter=220, learning_rate=0.06, min_samples_leaf=25, l2_regularization=1.0,
            monotonic_cst=list(MONOTONIC), early_stopping=True, validation_fraction=0.2, random_state=seed)

    # The bands are probability thresholds, so the numbers must mean what they say, not merely rank
    # correctly. Isotonic is more flexible but needs data; sigmoid is more data-efficient. Choose
    # between them on an inner validation split - never on the test set, which would leak.
    X_fit, X_val, y_fit, y_val = train_test_split(X_train, y_train, test_size=0.25,
                                                  random_state=seed, stratify=y_train)
    chosen, best_error = "sigmoid", float("inf")
    for method in ("isotonic", "sigmoid"):
        trial = CalibratedClassifierCV(make_base(), method=method, cv=4)
        trial.fit(X_fit, y_fit)
        error = expected_calibration_error(y_val.tolist(), trial.predict_proba(X_val)[:, 1].tolist())
        log.info("calibration_candidate", extra={"method": method, "val_ece": error})
        if error < best_error:
            chosen, best_error = method, error

    estimator = CalibratedClassifierCV(make_base(), method=chosen, cv=4)
    estimator.fit(X_train, y_train)

    probabilities = estimator.predict_proba(X_test)[:, 1].tolist()
    evaluation = {
        "trained_on": int(len(y_train)), "tested_on": int(len(y_test)),
        "default_rate": float(y.mean()),
        "ml_auc": float(roc_auc_score(y_test, probabilities)),
        "ml_gini": gini(y_test.tolist(), probabilities),
        "ml_ks": ks_statistic(y_test.tolist(), probabilities),
        "ml_brier": float(brier_score_loss(y_test, probabilities)),
        "calibration_error": expected_calibration_error(y_test.tolist(), probabilities),
        "calibration_method": chosen,
        "_X_test": X_test, "_y_test": y_test,
    }
    return estimator, evaluation


def save(estimator: Any, metrics: ModelMetrics) -> None:
    import joblib

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(estimator, MODEL_PATH)
    METRICS_PATH.write_text(json.dumps(metrics.as_dict(), indent=2), encoding="utf-8")
    load_model.cache_clear()


def predict_remote(features: dict[str, float]) -> float | None:
    """Score on the Vertex AI endpoint when VERTEX_RISK_ENDPOINT is set; None to use the local artifact.

    The deployed model is the same versioned artifact (see deploy/README.md: upload risk_model.joblib to
    Vertex AI Model Registry with the scikit-learn prediction container). Any failure falls back to the
    local copy, so an endpoint outage never blocks a recommendation.
    """
    from app.config import get_settings

    endpoint = get_settings().vertex_risk_endpoint
    if not endpoint:
        return None
    from app.gcp import GcpError, get_gcp

    location = endpoint.split("/locations/")[1].split("/")[0]
    try:
        result = get_gcp().call("POST", f"https://{location}-aiplatform.googleapis.com/v1/{endpoint}:predict",
                                {"instances": [to_vector(features)]})
        prediction = result["predictions"][0]
        probability = float(prediction[-1] if isinstance(prediction, list) else prediction)
    except (GcpError, KeyError, IndexError, TypeError, ValueError):
        log.warning("vertex_endpoint_unavailable")
        return None
    return probability if 0.0 <= probability <= 1.0 else None
