import uuid
import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, extract, func, or_, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import parse_uuid, require_admin
from ..models import AuditLog, BlacklistRecord, Booking, BookingStatus, Profile, TransactionExtension, UserRole, Vehicle, WalkInTransaction
from ..schemas import AuditLogOut, BlacklistIn, BlacklistMatchOut, BlacklistRecordIn, BlacklistRecordOut, CustomerAdminEdit, ProfileOut, StaffCreateIn, StaffOut, StaffUpdateIn, VerificationReviewIn
from ..security import create_document_token, hash_password
from ..services import audit
from ..services import bookings as booking_svc
from ..services import notifications as notes
from ..services import storage
from ..services import churn as churn_svc
from ..ml import churn as churn_ml
from ..services.blacklist import blacklist_matches, record_with_id
from ..services.phones import find_staff_by_phone, phone_digits
from ..services.email import EmailDeliveryFailed, EmailNotConfigured, send_notification_email
from ..services.synthetic import is_real_vehicle, on_real_vehicle

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ customers
@router.get("/customers", response_model=list[ProfileOut])
def list_customers(blacklisted: bool | None = None, db: Session = Depends(get_db)):
    stmt = select(Profile).where(Profile.role == UserRole.customer).order_by(Profile.created_at.desc())
    if blacklisted is not None:
        stmt = stmt.where(Profile.is_blacklisted.is_(blacklisted))
    return db.scalars(stmt).all()


@router.get("/customers/blacklist-matches", response_model=list[BlacklistMatchOut])
def customer_blacklist_matches(db: Session = Depends(get_db)):
    """Customer accounts whose ID number or name matches someone on Pardz's blacklist."""
    customers = db.scalars(select(Profile).where(Profile.role == UserRole.customer)).all()
    return [
        {"customer_id": customer_id, "record": record, "matched_by": matched_by}
        for customer_id, (record, matched_by) in blacklist_matches(db, customers).items()
    ]


# ------------------------------------------------------------------ blacklist records (Pardz's list)
@router.get("/blacklist-records", response_model=list[BlacklistRecordOut])
def list_blacklist_records(db: Session = Depends(get_db)):
    return db.scalars(select(BlacklistRecord).order_by(func.lower(BlacklistRecord.full_name))).all()


def _describe_record(record: BlacklistRecord) -> str:
    id_part = f", ID {record.id_number}" if record.id_number else ""
    return f"{record.full_name} ({record.reason}{id_part})"


def _assert_unique_id(db: Session, data: BlacklistRecordIn, exclude=None) -> None:
    other = record_with_id(db, data.id_number, exclude=exclude)
    if other is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"ID number {data.id_number} is already on the blacklist ({other.full_name}).")


@router.post("/blacklist-records", response_model=BlacklistRecordOut, status_code=status.HTTP_201_CREATED)
def create_blacklist_record(data: BlacklistRecordIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    # New records need an ID number; only the records loaded from Pardz's printed list may be without one.
    if not data.id_number:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter the person's ID number.")
    _assert_unique_id(db, data)
    record = BlacklistRecord(**data.model_dump())
    db.add(record)
    db.commit()
    audit.record(db, admin, "blacklist_record.create", f"Added to blacklist records: {_describe_record(record)}", entity="blacklist_record", entity_id=record.id)
    return record


def _record_or_404(db: Session, record_id: str) -> BlacklistRecord:
    record = db.get(BlacklistRecord, parse_uuid(record_id, "Blacklist record"))
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Blacklist record not found")
    return record


@router.patch("/blacklist-records/{record_id}", response_model=BlacklistRecordOut)
def edit_blacklist_record(record_id: str, data: BlacklistRecordIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    record = _record_or_404(db, record_id)
    before = _describe_record(record)
    if record.id_number and not data.id_number:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The ID number can't be removed from this record.")
    _assert_unique_id(db, data, exclude=record.id)
    for key, value in data.model_dump().items():
        setattr(record, key, value)
    db.commit()
    audit.record(db, admin, "blacklist_record.update", f"Edited blacklist record {before} -> {_describe_record(record)}", entity="blacklist_record", entity_id=record.id)
    return record


@router.delete("/blacklist-records/{record_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_blacklist_record(record_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    record = _record_or_404(db, record_id)
    summary = _describe_record(record)
    db.delete(record)
    db.commit()
    audit.record(db, admin, "blacklist_record.delete", f"Removed from blacklist records: {summary}", entity="blacklist_record", entity_id=record_id)


@router.get("/churn/predictions")
def churn_predictions(db: Session = Depends(get_db)):
    """Random Forest churn result of every customer (label, probability, the seven features)."""
    return churn_svc.predict_all(db, booking_svc.today_local())


@router.get("/churn/model")
def churn_model_report():
    """How the churn model was trained and how well it scored (shown in the "How churn is computed" panel)."""
    return churn_ml.load_report()


@router.post("/churn/retrain")
def retrain_churn_model(admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """Retrain the Random Forest on all completed rentals now in WheelSync."""
    try:
        report = churn_svc.retrain(db)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))
    audit.record(db, admin, "churn.retrain", f"Retrained the churn model on {report['rentals']} rentals (test recall {report['test']['recall']:.0%})", entity="churn_model")
    return report


@router.get("/customers/booking-history")
def booking_history(db: Session = Depends(get_db)):
    """Slim booking rows used by the churn model on the Customers page."""
    rows = db.execute(
        select(
            Booking.customer_id, Booking.start_date, Booking.end_date, Booking.actual_return_date,
            Booking.late_return, Booking.service_type, Booking.total_amount, Booking.status,
        )
    ).all()
    return [
        {
            "customer_id": r.customer_id, "start_date": r.start_date, "end_date": r.end_date,
            "actual_return_date": r.actual_return_date, "late_return": r.late_return,
            "service_type": r.service_type, "total_amount": float(r.total_amount) if r.total_amount is not None else None,
            "status": r.status.value,
        }
        for r in rows
    ]


def _customer_or_404(db: Session, customer_id: str) -> Profile:
    customer = db.get(Profile, parse_uuid(customer_id, "Customer"))
    if customer is None or customer.role != UserRole.customer:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Customer not found")
    return customer


@router.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(customer_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """Delete a customer and their booking history for good (also past customers from the company records:
    they are loaded only once, so they don't come back after a restart or deploy)."""
    customer = _customer_or_404(db, customer_id)
    active = [BookingStatus.pending, BookingStatus.accepted, BookingStatus.ongoing]
    if db.scalar(select(func.count(Booking.id)).where(Booking.customer_id == customer.id, Booking.status.in_(active))):
        raise HTTPException(status.HTTP_409_CONFLICT, "This customer has a pending, accepted or ongoing booking. Finish or cancel it before deleting the customer.")
    rentals = db.scalar(select(func.count(Booking.id)).where(Booking.customer_id == customer.id))
    summary = f"Deleted customer {customer.full_name} ({customer.email or customer.phone or 'no contact'}) and {rentals} booking(s)"
    db.execute(delete(Profile).where(Profile.id == customer.id))  # bookings, inspections and notifications cascade
    db.commit()
    audit.record(db, admin, "customer.delete", summary, entity="customer", entity_id=customer_id)


@router.patch("/customers/{customer_id}", response_model=ProfileOut)
def edit_customer(customer_id: str, data: CustomerAdminEdit, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """Correct a customer's details or ID information (e.g. they brought a different ID to the office)."""
    customer = _customer_or_404(db, customer_id)
    changed = []
    for key, value in data.model_dump(exclude_unset=True).items():
        value = value.strip() or None if isinstance(value, str) else value
        if key == "full_name" and not value:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "The customer's name can't be empty.")
        if value != getattr(customer, key):
            changed.append(f"{key.replace('_', ' ')} {getattr(customer, key) or '-'} -> {value or '-'}")
            setattr(customer, key, value)
    db.commit()
    if changed:
        audit.record(db, admin, "customer.edit", f"Edited customer {customer.full_name}: " + "; ".join(changed), entity="customer", entity_id=customer.id)
    return customer


@router.patch("/customers/{customer_id}/blacklist", response_model=ProfileOut)
def set_blacklist(customer_id: str, data: BlacklistIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    customer = _customer_or_404(db, customer_id)
    customer.is_blacklisted = data.is_blacklisted
    db.commit()
    audit.record(
        db, admin, "customer.blacklist" if data.is_blacklisted else "customer.unblacklist",
        f"{'Blacklisted' if data.is_blacklisted else 'Removed from blacklist'}: {customer.full_name} ({customer.email})",
        entity="customer", entity_id=customer.id,
    )
    return customer


@router.post("/customers/{customer_id}/verification", response_model=ProfileOut)
def review_verification(customer_id: str, data: VerificationReviewIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    customer = _customer_or_404(db, customer_id)
    reason = (data.reason or "").strip()

    if data.status == "rejected" and not reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a reason so the customer knows what to correct.")
    if data.status == "verified" and not all(
        [customer.id_photo_path, customer.second_id_photo_path, customer.proof_of_address_path, customer.proof_of_income_path]
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "All four documents are required before verification.")

    customer.verification_status = data.status
    customer.verification_notes = reason if data.status == "rejected" else None
    customer.verified_by = admin.id
    customer.verified_at = datetime.now(timezone.utc)
    if data.status == "rejected":
        notes.add_notification(
            db, customer.id, "Requirements need correction",
            f"Your submitted requirements were not verified. Reason: {reason} Please update your documents and resubmit them for review.",
            kind="requirements",
        )
    db.commit()
    if data.status == "rejected":
        notes.publish_after_commit(False, [customer.id])
        try:
            send_notification_email(
                customer.email,
                "Your customer requirements need changes",
                f"Your submitted requirements were not verified. Reason: {reason} Please update your documents and resubmit them for review.",
            )
        except (EmailNotConfigured, EmailDeliveryFailed) as exc:
            logger.warning("Verification rejection email was not sent: %s", exc)
    audit.record(
        db, admin, f"customer.{data.status}",
        f"{'Verified' if data.status == 'verified' else 'Rejected'} requirements of {customer.full_name}"
        + (f". Reason: {reason}" if data.status == "rejected" else ""),
        entity="customer", entity_id=customer.id,
    )
    return customer


@router.get("/customers/{customer_id}/documents")
def customer_documents(customer_id: str, db: Session = Depends(get_db)):
    """Five-minute signed links to the customer's private documents (empty string = not submitted)."""
    customer = _customer_or_404(db, customer_id)
    base = get_settings().public_base_url.rstrip("/")

    def link(path: str | None) -> str:
        if not path:
            return ""
        return f"{base}/files/{storage.DOCUMENT_BUCKET}/{path}?token={create_document_token(path)}"

    return {
        "primaryId": link(customer.id_photo_path),
        "secondId": link(customer.second_id_photo_path),
        "address": link(customer.proof_of_address_path),
        "income": link(customer.proof_of_income_path),
        "extra1": link(customer.extra_document_1_path),
        "extra2": link(customer.extra_document_2_path),
    }


# ------------------------------------------------------------------ staff
@router.get("/staff", response_model=list[StaffOut])
def list_staff(db: Session = Depends(get_db)):
    return db.scalars(select(Profile).where(Profile.role == UserRole.staff).order_by(Profile.created_at.desc())).all()


@router.post("/staff", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
def create_staff(data: StaffCreateIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    email = data.email.strip().lower() if data.email else None
    if email and db.scalar(select(Profile.id).where(Profile.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email address has already been registered")
    if len(phone_digits(data.phone)) < 7:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid phone number.")
    if find_staff_by_phone(db, data.phone):
        raise HTTPException(status.HTTP_409_CONFLICT, "Another staff member already uses this phone number.")
    staff = Profile(
        full_name=data.full_name.strip(),
        email=email,
        phone=data.phone.strip(),
        password_hash=hash_password(data.password),
        role=UserRole.staff,
        created_by=admin.id,
    )
    db.add(staff)
    db.commit()
    audit.record(db, admin, "staff.create", f"Created staff account {staff.full_name} ({staff.email or staff.phone})", entity="staff", entity_id=staff.id)
    return staff


@router.patch("/staff/{staff_id}", response_model=StaffOut)
def edit_staff(staff_id: str, data: StaffUpdateIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """Change a staff member's name, phone, email, or set a new password for them."""
    staff = db.get(Profile, parse_uuid(staff_id, "Staff member"))
    if staff is None or staff.role != UserRole.staff:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Staff member not found")
    fields = data.model_dump(exclude_unset=True)
    changed = []
    if "full_name" in fields and fields["full_name"].strip() != staff.full_name:
        changed.append(f"name {staff.full_name} -> {fields['full_name'].strip()}")
        staff.full_name = fields["full_name"].strip()
    if "phone" in fields and phone_digits(fields["phone"]) != phone_digits(staff.phone):
        if len(phone_digits(fields["phone"])) < 7:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter a valid phone number.")
        other = find_staff_by_phone(db, fields["phone"])
        if other is not None and other.id != staff.id:
            raise HTTPException(status.HTTP_409_CONFLICT, "Another staff member already uses this phone number.")
        changed.append(f"phone {staff.phone or '-'} -> {fields['phone'].strip()}")
        staff.phone = fields["phone"].strip()
    if "email" in fields:
        email = fields["email"].strip().lower() if fields["email"] else None
        if email != staff.email:
            if email and db.scalar(select(Profile.id).where(Profile.email == email, Profile.id != staff.id)):
                raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email address has already been registered")
            changed.append(f"email {staff.email or '-'} -> {email or '-'}")
            staff.email = email
    if fields.get("password"):
        staff.password_hash = hash_password(fields["password"])
        changed.append("password changed")
    db.commit()
    if changed:
        audit.record(db, admin, "staff.edit", f"Edited staff account {staff.full_name}: " + "; ".join(changed), entity="staff", entity_id=staff.id)
    return staff


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
def dashboard(year: int | None = Query(default=None, ge=2000, le=2100), db: Session = Depends(get_db)):
    year = year or booking_svc.today_local().year

    today = booking_svc.today_local()
    real = on_real_vehicle()
    pending = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == BookingStatus.pending, real)) or 0
    vehicles = db.scalar(select(func.count()).select_from(Vehicle).where(is_real_vehicle())) or 0
    customers = db.scalar(select(func.count()).select_from(Profile).where(Profile.role == UserRole.customer)) or 0

    # New sign-ups per period, by the local (Manila) calendar. Imported past customers only count in the total:
    # their account date is the day the history was loaded, not when they registered.
    zone = ZoneInfo(get_settings().timezone)

    def signed_up_since(start: date) -> int:
        since = datetime.combine(start, time.min, tzinfo=zone)
        return db.scalar(
            select(func.count()).select_from(Profile)
            .where(Profile.role == UserRole.customer, Profile.is_imported.is_(False), Profile.created_at >= since)
        ) or 0

    # Current renters: customers with a car out right now (ongoing bookings + walk-ins recorded for today).
    ongoing = db.scalars(
        select(Booking.customer_id).where(Booking.status == BookingStatus.ongoing, real).distinct()
    ).all()
    walk_ins = db.scalars(
        select(WalkInTransaction.customer_name)
        .where(WalkInTransaction.start_date <= today, WalkInTransaction.end_date >= today)
        .distinct()
    ).all()
    current_renters = len(ongoing) + len(walk_ins)

    customers_breakdown = {
        "today": signed_up_since(today),
        "week": signed_up_since(today - timedelta(days=today.weekday())),
        "month": signed_up_since(today.replace(day=1)),
        "total": customers,
    }

    # Revenue = money recorded on the Transactions page (prices are agreed at the office, not online):
    # the first payment on the release day, each extension payment on the day it was paid.
    def revenue_since(start=None) -> float:
        recorded = select(func.coalesce(func.sum(WalkInTransaction.amount), 0))
        extensions = select(func.coalesce(func.sum(TransactionExtension.amount), 0))
        if start is not None:
            recorded = recorded.where(WalkInTransaction.start_date >= start, WalkInTransaction.start_date <= today)
            extensions = extensions.where(TransactionExtension.paid_on >= start, TransactionExtension.paid_on <= today)
        return float(sum((db.scalar(query) or Decimal(0)) for query in (recorded, extensions)))

    revenue = {
        "today": revenue_since(today),
        "month": revenue_since(today.replace(day=1)),
        "year": revenue_since(today.replace(month=1, day=1)),
        "total": revenue_since(),
    }

    month_expr = extract("month", Booking.start_date)
    per_month = dict(
        db.execute(
            select(month_expr, func.count())
            .where(
                Booking.status.in_((BookingStatus.accepted, BookingStatus.ongoing, BookingStatus.completed)),
                extract("year", Booking.start_date) == year,
                real,
            )
            .group_by(month_expr)
        ).all()
    )
    walk_in_month = extract("month", WalkInTransaction.start_date)
    walk_ins_per_month = dict(
        db.execute(
            select(walk_in_month, func.count())
            .where(extract("year", WalkInTransaction.start_date) == year)
            .group_by(walk_in_month)
        ).all()
    )
    monthly = [
        {"month": m, "count": int(per_month.get(m, 0)) + int(walk_ins_per_month.get(m, 0))}
        for m in range(1, 13)
    ]

    recent_rows = db.scalars(booking_svc.booking_query().where(real).order_by(Booking.created_at.desc()).limit(6)).all()
    recent = [
        {
            "id": b.id, "start_date": b.start_date, "end_date": b.end_date,
            "pickup_time": b.pickup_time, "return_time": b.return_time, "status": b.status.value,
            "profiles": {"full_name": b.customer.full_name},
            "vehicles": {"make": b.vehicle.make, "model": b.vehicle.model},
        }
        for b in recent_rows
    ]
    return {
        "year": year,
        "pending": pending,
        "vehicles": vehicles,
        "customers": customers,
        "customers_breakdown": customers_breakdown,
        "current_renters": current_renters,
        "revenue": revenue["total"],
        "revenue_breakdown": revenue,
        "monthly_rentals": monthly,
        "recent": recent,
    }


# ------------------------------------------------------------------ fleet calendar
@router.get("/calendar")
def fleet_calendar(start: date, end: date, db: Session = Depends(get_db)):
    """Which car is taken on which days between `start` and `end`, for the dashboard calendar.

    pending = a booking request waiting for the admin; rented = an accepted, ongoing or completed booking,
    or a rental recorded on the Transactions page.
    """
    if end < start or (end - start).days > 62:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a range of at most two months.")
    vehicles = db.scalars(select(Vehicle).where(is_real_vehicle()).order_by(Vehicle.make, Vehicle.model, Vehicle.plate_number)).all()
    ids = [v.id for v in vehicles]
    taken = (BookingStatus.pending, BookingStatus.accepted, BookingStatus.ongoing, BookingStatus.completed)
    bookings = db.scalars(
        booking_svc.booking_query().where(
            Booking.vehicle_id.in_(ids), Booking.status.in_(taken), Booking.start_date <= end, Booking.end_date >= start,
        )
    ).all()
    recorded = db.scalars(
        select(WalkInTransaction).where(
            WalkInTransaction.vehicle_id.in_(ids), WalkInTransaction.start_date <= end, WalkInTransaction.end_date >= start,
        )
    ).all()
    entries = [
        {
            "vehicle_id": b.vehicle_id, "start_date": b.start_date, "end_date": b.end_date,
            "kind": "pending" if b.status == BookingStatus.pending else "rented", "status": b.status.value,
            "customer": b.customer.full_name, "booking_id": b.id, "transaction_id": None,
            "phone": b.customer.phone, "email": b.customer.email, "verification": b.customer.verification_status,
            "pickup_time": b.pickup_time, "return_time": b.return_time, "service_type": b.service_type,
            "destination": b.destination_to, "pickup_location": b.pickup_location,
            "staff": b.assigned_staff.full_name if b.assigned_staff else None,
            "actual_return_date": b.actual_return_date, "late_return": b.late_return, "notes": b.notes,
            "requested_at": b.created_at,
        }
        for b in bookings
    ] + [
        {
            "vehicle_id": t.vehicle_id, "start_date": t.start_date, "end_date": t.end_date,
            "kind": "rented", "status": "recorded", "customer": t.customer_name, "booking_id": None, "transaction_id": t.id,
            "phone": t.customer_phone, "email": None, "verification": None, "pickup_time": None, "return_time": None,
            "service_type": t.service_type, "destination": t.destination, "pickup_location": None, "staff": None,
            "actual_return_date": None, "late_return": False, "notes": t.notes, "requested_at": t.created_at,
            "amount": float(t.amount), "payment_method": t.payment_method,
        }
        for t in recorded
    ]
    return {
        "vehicles": [
            {
                "id": v.id, "name": v.name, "plate_number": v.plate_number, "status": v.status.value, "year": v.year,
                "category": v.category, "description": v.description, "image_url": v.image_url,
            }
            for v in vehicles
        ],
        "entries": entries,
    }


# ------------------------------------------------------------------ audit log
@router.get("/audit-logs", response_model=list[AuditLogOut])
def audit_logs(
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(AuditLog.summary.ilike(like), AuditLog.actor_name.ilike(like), AuditLog.action.ilike(like)))
    return db.scalars(stmt).all()
