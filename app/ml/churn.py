"""Customer Churn Prediction: a Random Forest Classifier trained on Pardz's completed rentals.

Six behavioural features per customer (proposal, Table 5, without total_amount_spent: WheelSync no longer
records rental prices, and on the history the amount did not improve the model - AUC 0.776 without, 0.771 with):
    days_since_last_rental, total_rentals_completed, avg_days_between_rentals, late_return_rate,
    preferred_service_type, customer_tenure_days

Training labels (180-day inactivity window): the history is cut at several evaluation dates T. For each
customer with at least two completed rentals before T, the features are computed from the rentals before T
and the label is "churned" when the customer did not start another rental within the 180 days after T.
Only dates whose 180-day window is fully inside the history are used, so every label is observed, not guessed.

The split is stratified 80/20 and the 5-fold cross-validation is stratified too; both keep all snapshots of
one customer on the same side, so the test score is measured on customers the model has never seen.
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np

CHURN_WINDOW_DAYS = 180
SNAPSHOT_STEP_DAYS = 30
MIN_RENTALS = 2  # cold start: fewer completed rentals = "New Customer - Not Yet Rated"
RECALL_TARGET = 0.75  # proposal: recall of 75% or higher on the Churned class
# Proposal's starting point was 200 trees, depth 8, class weighting. On Pardz's history depth 8 overfit
# (cross-validated recall 0.49), so the trees are kept shallower and every leaf needs 10 customers.
MODEL_PARAMS = {"n_estimators": 200, "max_depth": 4, "min_samples_leaf": 10, "class_weight": "balanced", "random_state": 42}

FEATURES = [
    "days_since_last_rental", "total_rentals_completed", "avg_days_between_rentals", "late_return_rate",
    "preferred_service_type", "customer_tenure_days",
]
SERVICE_CODES = {"self_drive": 0, "with_driver": 1, "pickup_dropoff": 2}

MODEL_DIR = Path(__file__).resolve().parent
MODEL_FILE = MODEL_DIR / "churn_model.joblib"
REPORT_FILE = MODEL_DIR / "churn_model.json"


@dataclass
class Rental:
    """One completed rental, as the model needs it."""
    customer_id: str
    start: date
    returned: date  # actual return date (or the agreed end date when no return was recorded)
    late: bool
    service_type: str


def features_for(rentals: list[Rental], as_of: date) -> dict | None:
    """The six features from a customer's completed rentals up to `as_of`; None below the cold-start minimum."""
    # Only rentals already returned by `as_of`: a rental still out has no return date or late flag yet.
    done = sorted((r for r in rentals if r.returned < as_of), key=lambda r: r.start)
    if len(done) < MIN_RENTALS:
        return None
    gaps = [(b.start - a.start).days for a, b in zip(done, done[1:])]
    services: dict[str, int] = {}
    for r in done:
        services[r.service_type] = services.get(r.service_type, 0) + 1
    preferred = max(services, key=services.get)
    return {
        "days_since_last_rental": max(0, (as_of - max(r.returned for r in done)).days),
        "total_rentals_completed": len(done),
        "avg_days_between_rentals": round(sum(gaps) / len(gaps), 1),
        "late_return_rate": round(sum(r.late for r in done) / len(done), 3),
        "preferred_service_type": preferred,
        "customer_tenure_days": (as_of - done[0].start).days,
    }


def vector(features: dict) -> list[float]:
    return [
        float(SERVICE_CODES.get(features[name], 0)) if name == "preferred_service_type" else float(features[name])
        for name in FEATURES
    ]


def build_training_set(rentals: list[Rental]):
    """Rows (X), labels (y: 1 = churned), and the customer of each row (for grouped splitting)."""
    by_customer: dict[str, list[Rental]] = {}
    for r in rentals:
        by_customer.setdefault(r.customer_id, []).append(r)
    first = min(r.start for r in rentals)
    last = max(r.start for r in rentals)
    X, y, groups = [], [], []
    cut = first + timedelta(days=SNAPSHOT_STEP_DAYS * 3)
    while cut + timedelta(days=CHURN_WINDOW_DAYS) <= last:
        window_end = cut + timedelta(days=CHURN_WINDOW_DAYS)
        for customer, own in by_customer.items():
            if any(r.start < cut <= r.returned for r in own):
                continue  # mid-rental at the cut: clearly active, not a churn example either way
            features = features_for(own, cut)
            if features is None:
                continue
            returned = any(cut <= r.start < window_end for r in own)
            X.append(vector(features))
            y.append(0 if returned else 1)
            groups.append(customer)
        cut += timedelta(days=SNAPSHOT_STEP_DAYS)
    return np.array(X), np.array(y), np.array(groups)


def train(rentals: list[Rental]) -> dict:
    """Train, evaluate and save the model. Returns the evaluation report (also saved next to the model)."""
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
    from sklearn.model_selection import StratifiedGroupKFold, cross_val_predict

    X, y, groups = build_training_set(rentals)
    if len(X) < 20 or len(set(y)) < 2:
        raise ValueError("Not enough rental history to train the churn model (need customers who both returned and churned).")

    # 80/20 stratified split, grouped by customer: one of five stratified folds is the test set.
    train_idx, test_idx = next(StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42).split(X, y, groups))

    # 5-fold stratified cross-validation on the training part: every training row gets a probability from
    # a model that did not see it. The risk threshold is chosen from these: the highest one whose recall
    # still reaches the target (recall is the primary metric, so missing a churning suki costs most).
    oof = cross_val_predict(
        RandomForestClassifier(**MODEL_PARAMS), X[train_idx], y[train_idx], groups=groups[train_idx],
        cv=StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=7), method="predict_proba",
    )[:, 1]
    candidates = [t / 100 for t in range(20, 61)]
    reaching = [t for t in candidates if recall_score(y[train_idx], (oof >= t).astype(int)) >= RECALL_TARGET]
    threshold = max(reaching) if reaching else 0.5

    def scores(truth, proba):
        predicted = (proba >= threshold).astype(int)
        return {
            "accuracy": accuracy_score(truth, predicted),
            "precision": precision_score(truth, predicted, zero_division=0),
            "recall": recall_score(truth, predicted, zero_division=0),
            "f1": f1_score(truth, predicted, zero_division=0),
            "auc_roc": roc_auc_score(truth, proba),
        }

    # How often each label is right, and what really happened per risk band (cross-validated, so honest).
    risky = oof >= threshold
    label_check = {
        "at_risk_correct": round(float(y[train_idx][risky].mean()), 3) if risky.any() else None,
        "likely_correct": round(float(1 - y[train_idx][~risky].mean()), 3) if (~risky).any() else None,
        "risk_bands": [
            {"from": lo, "to": hi, "examples": int(band.sum()), "churned": round(float(y[train_idx][band].mean()), 3)}
            for lo, hi in [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
            if (band := (oof * 100 >= lo) & ((oof * 100 < hi) | (hi == 100))).any()
        ],
        # A one-feature rule for comparison: rank customers only by days since their last rental.
        "recency_only_auc": round(float(roc_auc_score(y[train_idx], X[train_idx][:, FEATURES.index("days_since_last_rental")])), 3),
    }

    model = RandomForestClassifier(**MODEL_PARAMS).fit(X[train_idx], y[train_idx])
    test = scores(y[test_idx], model.predict_proba(X[test_idx])[:, 1])
    cv = scores(y[train_idx], oof)
    # The served model learns from every row once the scores are measured.
    final = RandomForestClassifier(**MODEL_PARAMS).fit(X, y)
    report = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": "RandomForestClassifier",
        "params": MODEL_PARAMS,
        "features": FEATURES,
        "churn_window_days": CHURN_WINDOW_DAYS,
        "risk_threshold": threshold,
        "rentals": len(rentals),
        "customers": len({r.customer_id for r in rentals}),
        "training_rows": int(len(X)),
        "churned_rows": int(y.sum()),
        "train_rows": int(len(train_idx)),
        "test_rows": int(len(test_idx)),
        "test": {k: round(float(v), 3) for k, v in test.items()},
        "cross_validation": {k: round(float(v), 3) for k, v in cv.items()},
        "recall_target": RECALL_TARGET,
        "label_check": label_check,
        "feature_importance": {
            name: round(float(value), 3)
            for name, value in sorted(zip(FEATURES, final.feature_importances_), key=lambda item: -item[1])
        },
    }
    joblib.dump(final, MODEL_FILE)
    REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    _cache.clear()
    return report


_cache: dict = {}


def load_model():
    """The saved model; reloaded when the file changes (e.g. retrained by the script while the server runs)."""
    stamp = MODEL_FILE.stat().st_mtime_ns if MODEL_FILE.exists() else None
    if _cache.get("stamp") != stamp or "model" not in _cache:
        _cache["model"] = joblib.load(MODEL_FILE) if stamp else None
        _cache["stamp"] = stamp
    return _cache["model"]


def load_report() -> dict | None:
    return json.loads(REPORT_FILE.read_text(encoding="utf-8")) if REPORT_FILE.exists() else None


def risk_threshold() -> float:
    return (load_report() or {}).get("risk_threshold", 0.5)


def predict(features: dict) -> float:
    """Probability (0-1) that the customer churns."""
    model = load_model()
    if model is None:
        raise RuntimeError("The churn model has not been trained yet.")
    return float(model.predict_proba(np.array([vector(features)]))[0, 1])
