"""Pardz's own records, loaded into a new database once: the destination price list, the printed blacklist
and the past rental history (app/data/rental_history.csv).

Each load runs once per database and is remembered in seed_runs, so a deploy fills an empty database but
anything the admin deletes later stays deleted, even after a restart. A database that already holds the
data from before seed_runs existed is marked as loaded without adding anything again.
"""
import csv
import secrets
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import BlacklistRecord, Booking, BookingStatus, Location, Profile, SeedRun, UserRole, Vehicle, VehicleStatus
from ..security import hash_password
from .blacklist import DEFAULT_RECORDS, SECOND_PAGE_RECORDS, add_records
from .locations import DEFAULT_DESTINATIONS
from .synthetic import SYNTHETIC_DESCRIPTION

RENTAL_HISTORY_CSV = Path(__file__).resolve().parent.parent / "data" / "rental_history.csv"
HISTORY_NOTE = "synthetic row"  # bookings notes start with this; it marks imported history rows

# Cars in the history that are not in today's fleet: kept hidden (services/synthetic.py), in maintenance.
HISTORY_VEHICLES = {
    # model: (make, category, typical daily rate)
    "Vios": ("Toyota", "sedan", 1650),
    "Innova": ("Toyota", "van", 2600),
    "Fortuner": ("Toyota", "suv", 3600),
    "Montero": ("Mitsubishi", "suv", 3600),
    "Hilux": ("Toyota", "pickup", 2600),
}
SERVICE_TYPES = {"self-drive": "self_drive", "with driver": "with_driver"}


def _count(db: Session, query) -> int:
    return db.scalar(select(func.count()).select_from(query.subquery()))


def _seed_destinations(db: Session) -> None:
    if _count(db, select(Location.id)):
        return
    db.add_all(Location(name=name, sort_order=i) for i, name in enumerate(DEFAULT_DESTINATIONS))


def _seed_blacklist_page(rows: list[tuple]):
    def seed(db: Session) -> None:
        existing = set(db.scalars(select(BlacklistRecord.full_name)))
        add_records(db, [row for row in rows if row[2] not in existing])
    return seed


def _seed_blacklist_first_page(db: Session) -> None:
    if not _count(db, select(BlacklistRecord.id)):
        add_records(db, DEFAULT_RECORDS)


def _seed_rental_history(db: Session) -> None:
    if not get_settings().seed_rental_history:
        return
    if _count(db, select(Booking.id).where(Booking.notes.like(f"{HISTORY_NOTE} %"))):
        return
    with RENTAL_HISTORY_CSV.open(newline="", encoding="utf-8") as handle:
        import_rental_history(db, list(csv.DictReader(handle)))


SEEDS = [
    ("destinations", _seed_destinations),
    ("blacklist_page_1", _seed_blacklist_first_page),
    ("blacklist_page_2", _seed_blacklist_page(SECOND_PAGE_RECORDS)),
    ("rental_history", _seed_rental_history),
]


def seed_company_data(db: Session) -> None:
    done = set(db.scalars(select(SeedRun.key)))
    for key, seed in SEEDS:
        if key in done:
            continue
        if key == "rental_history" and not get_settings().seed_rental_history:
            continue  # tests skip the history; don't mark it as loaded
        seed(db)
        db.add(SeedRun(key=key))
        db.commit()


def import_rental_history(db: Session, rows: list[dict]) -> tuple[int, int]:
    """Past rentals as completed bookings of past customers, on hidden vehicles. Returns (bookings, customers)."""
    plate_of = lambda row: row.get("plate_number") or f"SYN-{row['vehicle_model'].upper()}"  # noqa: E731
    vehicles = {}
    for row in rows:
        plate = plate_of(row)
        if plate not in vehicles:
            make, category, rate = HISTORY_VEHICLES[row["vehicle_model"]]
            vehicles[plate] = db.scalar(select(Vehicle).where(Vehicle.plate_number == plate)) or Vehicle(
                make=make, model=row["vehicle_model"], year=2024, plate_number=plate, category=category,
                description=SYNTHETIC_DESCRIPTION, daily_rate=Decimal(rate),
                price_per_km=Decimal(0), status=VehicleStatus.maintenance,
            )
            db.add(vehicles[plate])

    password_hash = hash_password(secrets.token_urlsafe(24))  # past customers can't sign in with these accounts
    customers = {}
    for row in rows:
        code = row["customer_id"]
        if code in customers:
            continue
        email = (row.get("customer_email") or "").strip().lower() or None
        customer = db.scalar(select(Profile).where(func.lower(Profile.email) == email)) if email else None
        if customer is None:
            # Past renters showed their IDs at the office when they rented, so their requirements count as verified.
            customer = Profile(
                id=uuid.uuid4(), full_name=row.get("customer_name") or f"Customer {code}", email=email,
                phone=row.get("customer_phone") or None, password_hash=password_hash, role=UserRole.customer,
                email_verified=True, is_imported=True, verification_status="verified",
                verification_notes="ID checked in person at the office (past rental record).",
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
            notes=f"{HISTORY_NOTE} {number}; segment={row['segment']}; daily_rate={row['daily_rate_php']}; "
                  f"actual_return={row['actual_return_datetime']}",
        ))
    db.flush()
    return len(rows), len(customers)
