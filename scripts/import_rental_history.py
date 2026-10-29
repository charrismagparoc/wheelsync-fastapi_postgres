"""Load (or remove) Pardz's past rental history by hand.

    python -m scripts.import_rental_history                  # (re)load app/data/rental_history.csv
    python -m scripts.import_rental_history path/to/file.csv
    python -m scripts.import_rental_history --remove         # delete the imported customers, bookings and hidden vehicles

A new database already gets the history once on startup (app/services/company_data.py); this script is only
for reloading it on purpose. Re-running replaces the previous load.
"""
import csv
import sys
from pathlib import Path

from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.models import Booking, Profile, UserRole, Vehicle
from app.services.company_data import HISTORY_NOTE, RENTAL_HISTORY_CSV, import_rental_history
from app.services.synthetic import is_synthetic_vehicle


def remove_history(db) -> None:
    history = Booking.notes.like(f"{HISTORY_NOTE} %")
    other_customers = select(Booking.customer_id).where(~history | Booking.notes.is_(None))
    customer_ids = list(db.scalars(select(Profile.id).where(
        Profile.role == UserRole.customer,
        Profile.is_imported | Profile.id.in_(select(Booking.customer_id).where(history)),
        Profile.id.not_in(other_customers),
    )))
    vehicle_ids = select(Vehicle.id).where(is_synthetic_vehicle())
    removed = db.execute(delete(Booking).where(history | Booking.vehicle_id.in_(vehicle_ids))).rowcount
    db.execute(delete(Profile).where(Profile.id.in_(customer_ids)))
    db.execute(delete(Vehicle).where(is_synthetic_vehicle()))
    print(f"Removed {removed} past bookings and {len(customer_ids)} past customers.")


def main() -> None:
    args = sys.argv[1:]
    init_db()
    with SessionLocal() as db:
        remove_history(db)
        if args[:1] != ["--remove"]:
            path = Path(args[0]) if args else RENTAL_HISTORY_CSV
            with path.open(newline="", encoding="utf-8") as handle:
                bookings, customers = import_rental_history(db, list(csv.DictReader(handle)))
            print(f"Imported {bookings} bookings for {customers} customers.")
        db.commit()


if __name__ == "__main__":
    main()
