import uuid
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import extract, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import parse_uuid, require_admin
from ..models import Booking, BookingStatus, Profile, UserRole, Vehicle
from ..schemas import BlacklistIn, ProfileOut, StaffCreateIn, StaffOut, VerificationReviewIn
from ..security import create_document_token, hash_password
from ..services import bookings as booking_svc
from ..services import notifications as notes
from ..services import storage
from ..services.email import EmailDeliveryFailed, EmailNotConfigured, send_notification_email

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])
logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ customers
@router.get("/customers", response_model=list[ProfileOut])
def list_customers(blacklisted: bool | None = None, db: Session = Depends(get_db)):
    stmt = select(Profile).where(Profile.role == UserRole.customer).order_by(Profile.created_at.desc())
    if blacklisted is not None:
        stmt = stmt.where(Profile.is_blacklisted.is_(blacklisted))
    return db.scalars(stmt).all()


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


@router.patch("/customers/{customer_id}/blacklist", response_model=ProfileOut)
def set_blacklist(customer_id: str, data: BlacklistIn, db: Session = Depends(get_db)):
    customer = _customer_or_404(db, customer_id)
    customer.is_blacklisted = data.is_blacklisted
    db.commit()
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
    email = data.email.strip().lower()
    if db.scalar(select(Profile.id).where(Profile.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email address has already been registered")
    staff = Profile(
        full_name=data.full_name.strip(),
        email=email,
        phone=(data.phone or "").strip() or None,
        password_hash=hash_password(data.password),
        role=UserRole.staff,
        created_by=admin.id,
    )
    db.add(staff)
    db.commit()
    return staff


# ------------------------------------------------------------------ dashboard
@router.get("/dashboard")
def dashboard(year: int | None = Query(default=None, ge=2000, le=2100), db: Session = Depends(get_db)):
    year = year or booking_svc.today_local().year

    pending = db.scalar(select(func.count()).select_from(Booking).where(Booking.status == BookingStatus.pending)) or 0
    vehicles = db.scalar(select(func.count()).select_from(Vehicle)) or 0
    customers = db.scalar(select(func.count()).select_from(Profile).where(Profile.role == UserRole.customer)) or 0
    revenue = db.scalar(select(func.coalesce(func.sum(Booking.total_amount), 0)).where(Booking.status == BookingStatus.completed)) or Decimal(0)

    month_expr = extract("month", Booking.start_date)
    per_month = dict(
        db.execute(
            select(month_expr, func.count())
            .where(
                Booking.status.in_((BookingStatus.accepted, BookingStatus.ongoing, BookingStatus.completed)),
                extract("year", Booking.start_date) == year,
            )
            .group_by(month_expr)
        ).all()
    )
    monthly = [{"month": m, "count": int(per_month.get(m, 0))} for m in range(1, 13)]

    recent_rows = db.scalars(booking_svc.booking_query().order_by(Booking.created_at.desc()).limit(6)).all()
    recent = [
        {
            "id": b.id, "start_date": b.start_date, "end_date": b.end_date,
            "pickup_time": b.pickup_time, "return_time": b.return_time, "status": b.status.value,
            "total_amount": float(b.total_amount) if b.total_amount is not None else None,
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
        "revenue": float(revenue),
        "monthly_rentals": monthly,
        "recent": recent,
    }
