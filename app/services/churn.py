"""Churn predictions for the Customers page: features from the database, probability from the Random Forest."""
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..ml import churn as ml
from ..models import Booking, BookingStatus, ChurnPrediction, Profile, UserRole

NOT_RATED = "New Customer - Not Yet Rated"
AT_RISK = "At Risk of Churn"
LIKELY = "Likely to Return"


def completed_rentals(db: Session) -> list[ml.Rental]:
    rows = db.execute(
        select(
            Booking.customer_id, Booking.start_date, Booking.end_date, Booking.actual_return_date,
            Booking.late_return, Booking.service_type,
        ).where(Booking.status == BookingStatus.completed)
    ).all()
    return [
        ml.Rental(
            customer_id=str(r.customer_id), start=r.start_date, returned=r.actual_return_date or r.end_date,
            late=bool(r.late_return), service_type=r.service_type or "self_drive",
        )
        for r in rows
    ]


def retrain(db: Session) -> dict:
    return ml.train(completed_rentals(db))


def predict_all(db: Session, today: date) -> list[dict]:
    """One result per customer, saved to churn_predictions (latest only)."""
    rentals = completed_rentals(db)
    if ml.load_model() is None:
        try:
            ml.train(rentals)  # first run on a new server: learn from the history already in the database
        except ValueError:
            pass  # too little history yet: everyone stays "Not Yet Rated" until the model can be trained
    trained = ml.load_model() is not None
    by_customer: dict[str, list[ml.Rental]] = {}
    for r in rentals:
        by_customer.setdefault(r.customer_id, []).append(r)

    threshold = ml.risk_threshold()
    results = []
    for customer_id in db.scalars(select(Profile.id).where(Profile.role == UserRole.customer)):
        own = by_customer.get(str(customer_id), [])
        features = ml.features_for(own, today)
        if features is None or not trained:
            results.append({"customer_id": customer_id, "label": NOT_RATED, "probability": None,
                            "valuable": False, "rentals": len(own), "features": None})
            continue
        probability = ml.predict(features)
        results.append({
            "customer_id": customer_id, "label": AT_RISK if probability >= threshold else LIKELY,
            "probability": round(probability, 4), "valuable": False, "rentals": len(own), "features": features,
        })

    # "Valuable - At Risk" (proposal, Table 9): at risk and in the top quarter by completed rentals (a frequent suki).
    rated = [r for r in results if r["features"]]
    if rated:
        rentals_cut = _quartile([r["features"]["total_rentals_completed"] for r in rated])
        for r in rated:
            r["valuable"] = r["label"] == AT_RISK and r["features"]["total_rentals_completed"] >= rentals_cut

    db.execute(delete(ChurnPrediction))
    db.add_all(
        ChurnPrediction(customer_id=r["customer_id"], churn_label=r["label"],
                        churn_probability=None if r["probability"] is None else Decimal(str(r["probability"])))
        for r in results
    )
    db.commit()
    return results


def _quartile(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.75))]
