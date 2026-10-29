"""Load (or remove) the synthetic rental history used to test reports and the churn model.

    python -m scripts.import_synthetic                  # import db/synthetic_rentals_first542.csv
    python -m scripts.import_synthetic path/to/file.csv
    python -m scripts.import_synthetic --remove         # delete everything this script created

Synthetic customers are recognised by their bookings (notes start with "synthetic row") and get realistic e-mails
on EMAIL_DOMAIN (example.com is reserved and never delivers mail); every synthetic vehicle gets the description
SYNTHETIC_DESCRIPTION. That way the data can be removed again without touching real records. When the CSV has
customer_name and plate_number columns (rentals_ph.csv), those names and plates are used, one vehicle per plate. The vehicles are
put in maintenance so customers cannot book them. Re-running the import replaces the previous load.
"""
import csv
import random
import re
import secrets
import sys
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.models import Booking, BookingStatus, Profile, UserRole, Vehicle, VehicleStatus
from app.security import hash_password

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "db" / "synthetic_rentals_first542.csv"
SYNTHETIC_DOMAIN = "@synthetic.wheelsync.test"  # e-mails used by older imports
EMAIL_DOMAIN = "@example.com"
SYNTHETIC_DESCRIPTION = "Synthetic test vehicle (historical data only)."
VEHICLES = {
    # model: (make, category, typical daily rate)
    "Vios": ("Toyota", "sedan", 1650),
    "Innova": ("Toyota", "van", 2600),
    "Fortuner": ("Toyota", "suv", 3600),
    "Montero": ("Mitsubishi", "suv", 3600),
    "Hilux": ("Toyota", "pickup", 2600),
}
SERVICE_TYPES = {"self-drive": "self_drive", "with driver": "with_driver"}


def email_for(full_name: str, taken: set[str]) -> str:
    """A realistic, varied address from the name: 'Gerald R. Ramos' -> e.g. 'geraldramos', 'g.ramos', 'ramos.gerald88'.

    The pattern is picked from the name itself, so the same customer always gets the same address.
    """
    parts = [re.sub(r"[^a-z]", "", part.lower()) for part in full_name.split(" ")]
    first, *middle = [part for part in parts if part]
    # "Dela Cruz", "Del Rosario", "De Leon": everything after the middle initial is the surname
    initial = next((m for m in middle if len(m) == 1), None)
    surname = "".join(middle[middle.index(initial) + 1:] if initial else middle)
    pick = random.Random(full_name)
    year, short = str(pick.randint(1975, 2004)), f"{pick.randint(1, 99):02d}"
    patterns = [
        f"{first}.{surname}", f"{first}{surname}", f"{first}_{surname}", f"{first[0]}{surname}",
        f"{first[0]}.{surname}", f"{first}{surname[0]}", f"{surname}.{first}", f"{first}.{surname}{short}",
        f"{first}{surname}{year}", f"{first}{year[-2:]}", f"{first}.{initial or ''}{surname}",
        f"{surname}{first[0]}{short}", f"{first}_{year}", f"its{first}{surname[0]}",
    ]
    base = pick.choice(patterns)
    email, n = f"{base}{EMAIL_DOMAIN}", 2
    while email in taken:
        email, n = f"{base}{n}{EMAIL_DOMAIN}", n + 1
    return email


def remove_synthetic(db) -> None:
    synthetic_customers = select(Booking.customer_id).where(Booking.notes.like("synthetic row %"))
    real_customers = select(Booking.customer_id).where(~Booking.notes.like("synthetic row %") | Booking.notes.is_(None))
    customer_ids = [row for row in db.scalars(
        select(Profile.id).where(
            Profile.email.endswith(SYNTHETIC_DOMAIN)
            | (Profile.id.in_(synthetic_customers) & Profile.id.not_in(real_customers) & (Profile.role == UserRole.customer))
        )
    )]
    is_synthetic_vehicle = Vehicle.plate_number.startswith("SYN-") | (Vehicle.description == SYNTHETIC_DESCRIPTION)
    vehicle_ids = select(Vehicle.id).where(is_synthetic_vehicle)
    removed = db.execute(delete(Booking).where(Booking.customer_id.in_(customer_ids) | Booking.vehicle_id.in_(vehicle_ids))).rowcount
    db.execute(delete(Profile).where(Profile.id.in_(customer_ids)))
    db.execute(delete(Vehicle).where(is_synthetic_vehicle))
    print(f"Removed {removed} synthetic bookings and their customers/vehicles.")


def import_rows(db, rows: list[dict]) -> None:
    # One vehicle per plate when the CSV names plates, otherwise one per model.
    plate_of = lambda row: row.get("plate_number") or f"SYN-{row['vehicle_model'].upper()}"  # noqa: E731
    vehicles = {}
    for row in rows:
        plate = plate_of(row)
        if plate not in vehicles:
            make, category, rate = VEHICLES[row["vehicle_model"]]
            vehicles[plate] = Vehicle(
                make=make, model=row["vehicle_model"], year=2024, plate_number=plate, category=category,
                description=SYNTHETIC_DESCRIPTION, daily_rate=Decimal(rate),
                price_per_km=Decimal(0), status=VehicleStatus.maintenance,
            )
            db.add(vehicles[plate])

    password_hash = hash_password(secrets.token_urlsafe(24))  # nobody can sign in as a synthetic customer
    customers = {}
    names = {row["customer_id"]: row.get("customer_name") or f"Synthetic Customer {row['customer_id']}" for row in rows}
    taken = set(db.scalars(select(Profile.email)))
    for code in names:
        email = email_for(names[code], taken)
        taken.add(email)
        customer = Profile(
            id=uuid.uuid4(), full_name=names[code], email=email,
            password_hash=password_hash, role=UserRole.customer, email_verified=True,
        )
        db.add(customer)
        customers[code] = customer
    db.flush()

    for number, row in enumerate(rows, start=1):
        released = datetime.fromisoformat(row["release_datetime"])
        agreed_end = datetime.fromisoformat(row["agreed_end_datetime"])
        returned = datetime.fromisoformat(row["actual_return_datetime"])
        db.add(Booking(
            customer_id=customers[row["customer_id"]].id,
            vehicle_id=vehicles[plate_of(row)].id,
            start_date=released.date(), end_date=agreed_end.date(),
            pickup_time=released.time(), return_time=agreed_end.time(),
            service_type=SERVICE_TYPES[row["service_type"]],
            distance_km=Decimal(0),
            actual_return_date=returned.date(),
            # Same rule the app applies when staff complete a rental (complete_rental): late = returned after the end date.
            late_return=returned.date() > agreed_end.date(),
            status=BookingStatus(row["status"]),
            total_amount=Decimal(row["final_amount"]),
            notes=f"synthetic row {number}; segment={row['segment']}; daily_rate={row['daily_rate_php']}; "
                  f"actual_return={row['actual_return_datetime']}",
        ))
    print(f"Imported {len(rows)} bookings for {len(customers)} customers on {len(vehicles)} vehicles.")


def main() -> None:
    args = sys.argv[1:]
    init_db()
    with SessionLocal() as db:
        remove_synthetic(db)
        if args[:1] != ["--remove"]:
            path = Path(args[0]) if args else DEFAULT_CSV
            with path.open(newline="", encoding="utf-8") as handle:
                import_rows(db, list(csv.DictReader(handle)))
        db.commit()


if __name__ == "__main__":
    main()
