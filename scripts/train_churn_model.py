"""Train (or retrain) the Customer Churn Prediction model on the completed rentals in the database.

    python -m scripts.train_churn_model

Saves app/ml/churn_model.joblib and the evaluation report app/ml/churn_model.json.
"""
import json

from app.database import SessionLocal, init_db
from app.services.churn import retrain


def main() -> None:
    init_db()
    with SessionLocal() as db:
        report = retrain(db)
    print(json.dumps({k: report[k] for k in ("training_rows", "churned_rows", "test", "cross_validation", "feature_importance")}, indent=2))


if __name__ == "__main__":
    main()
