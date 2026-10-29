from datetime import date, timedelta
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import and_, exists, not_, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, parse_uuid, require_admin, require_admin_or_staff, require_customer, require_staff
from ..config import get_settings
from ..models import Booking, BookingStatus, Inspection, InspectionType, Profile, UserRole
from ..schemas import (
    AcceptIn, BookingAdminEdit, BookingAdminOut, BookingCustomerOut, BookingCreate, BookingStaffOut, RejectIn,
    InspectionCreate, InspectionOut,
)
from ..services import bookings as svc
from ..services import reminders
from ..services.email import (
    EmailDeliveryFailed, EmailNotConfigured, send_booking_approved_email, send_notification_email,
)
from ..security import create_document_token, inspection_photo_url
from ..services import audit, storage
from ..services.agreement import RENTAL_AGREEMENT_TERMS
from ..services.synthetic import on_real_vehicle

router = APIRouter(prefix="/bookings", tags=["bookings"])
logger = logging.getLogger(__name__)

STAFF_VISIBLE = (BookingStatus.accepted, BookingStatus.ongoing, BookingStatus.completed)
SORTS = {
    "created_desc": lambda: Booking.created_at.desc(),
    "start_asc": lambda: Booking.start_date.asc(),
    "start_desc": lambda: Booking.start_date.desc(),
}


def serialize(booking: Booking, user: Profile):
    if user.role == UserRole.admin:
        return BookingAdminOut.model_validate(booking)
    if user.role == UserRole.staff:
        return BookingStaffOut.model_validate(booking)
    return BookingCustomerOut.model_validate(booking)


def _describe(booking: Booking) -> str:
    return (
        f"{booking.customer.full_name} - {booking.vehicle.name} ({booking.vehicle.plate_number}), "
        f"{booking.start_date} to {booking.end_date}"
    )


def _staff_name(db: Session, staff_id) -> str:
    staff = db.get(Profile, staff_id) if staff_id else None
    return staff.full_name if staff else "nobody"


def _parse_statuses(raw: str | None) -> list[BookingStatus] | None:
    if not raw or raw == "all":
        return None
    try:
        return [BookingStatus(s.strip()) for s in raw.split(",") if s.strip()]
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Unknown booking status filter.")


@router.get("/returns-due")
def returns_due(
    hours: int = Query(default=24, ge=1, le=168),
    admin: Profile = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Ongoing rentals due back within `hours` (and overdue ones), with the customer's name and number to call."""
    return reminders.returns_due(db, timedelta(hours=hours))


@router.get("/inspection-queue", response_model=list[BookingAdminOut])
def inspection_queue(
    view: str = Query(default="open", pattern="^(open|done)$"),
    admin: Profile = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin Inspections page. open = rentals still being inspected, or returned vehicles waiting for
    verification/cleaning; done = returned vehicles that are verified, cleaned and back on the fleet."""
    has_post = exists().where(Inspection.booking_id == Booking.id, Inspection.type == InspectionType.post_rental)
    has_unverified = exists().where(Inspection.booking_id == Booking.id, Inspection.verified_at.is_(None))
    returned = and_(Booking.status == BookingStatus.completed, has_post)
    if view == "open":
        stmt = svc.booking_query().where(or_(
            Booking.status.in_((BookingStatus.accepted, BookingStatus.ongoing)),
            and_(returned, or_(Booking.cleaning_done_at.is_(None), has_unverified)),
        )).order_by(Booking.start_date, Booking.id)
    else:
        stmt = svc.booking_query().where(returned, Booking.cleaning_done_at.is_not(None), not_(has_unverified)) \
            .order_by(Booking.cleaning_done_at.desc(), Booking.id).limit(100)
    return db.scalars(stmt).all()


@router.get("")
def list_bookings(
    status_filter: str | None = Query(default=None, alias="status", description="all | one status | comma-separated statuses"),
    start_from: date | None = None,
    start_to: date | None = None,
    sort: str = "created_desc",
    limit: int | None = Query(default=None, ge=1, le=1000),
    user: Profile = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Role-scoped: admin = every booking, customer = own bookings, staff = bookings assigned to them."""
    stmt = svc.booking_query()
    if user.role == UserRole.admin:
        stmt = stmt.where(on_real_vehicle())  # synthetic test rentals stay out of the admin's records
    if user.role == UserRole.customer:
        stmt = stmt.where(Booking.customer_id == user.id)
    elif user.role == UserRole.staff:
        stmt = stmt.where(
            Booking.status.in_(STAFF_VISIBLE),
            or_(Booking.assigned_staff_id == user.id, and_(Booking.status == BookingStatus.completed, Booking.cleaning_staff_id == user.id)),
        )

    wanted = _parse_statuses(status_filter)
    if wanted:
        stmt = stmt.where(Booking.status.in_(wanted))
    if start_from:
        stmt = stmt.where(Booking.start_date >= start_from)
    if start_to:
        stmt = stmt.where(Booking.start_date <= start_to)
    if sort not in SORTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"sort must be one of {', '.join(SORTS)}")
    stmt = stmt.order_by(SORTS[sort](), Booking.id)
    if limit:
        stmt = stmt.limit(limit)
    return [serialize(b, user) for b in db.scalars(stmt).all()]


@router.post("", response_model=BookingCustomerOut, status_code=status.HTTP_201_CREATED)
def create_booking(data: BookingCreate, user: Profile = Depends(require_customer), db: Session = Depends(get_db)):
    return svc.create_booking(db, user, data)


@router.put("/{booking_id}/resubmit", response_model=BookingCustomerOut)
def resubmit_rejected_booking(
    booking_id: str,
    data: BookingCreate,
    user: Profile = Depends(require_customer),
    db: Session = Depends(get_db),
):
    return svc.resubmit_rejected_booking(db, parse_uuid(booking_id, "Booking"), user, data)


@router.get("/{booking_id}")
def get_booking(booking_id: str, user: Profile = Depends(get_current_user), db: Session = Depends(get_db)):
    booking = svc.get_booking_or_404(db, parse_uuid(booking_id, "Booking"))
    allowed = (
        user.role == UserRole.admin
        or (user.role == UserRole.customer and booking.customer_id == user.id)
        or (user.role == UserRole.staff and user.id in (booking.assigned_staff_id, booking.cleaning_staff_id))
    )
    if not allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return serialize(booking, user)


# ---- admin decisions
@router.patch("/{booking_id}", response_model=BookingAdminOut)
def edit_booking(booking_id: str, data: BookingAdminEdit, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking, changed = svc.admin_edit_booking(db, parse_uuid(booking_id, "Booking"), admin, data)
    if changed:
        audit.record(db, admin, "booking.edit", f"Edited booking of {_describe(booking)}: " + "; ".join(changed), entity="booking", entity_id=booking.id)
    return booking


@router.post("/{booking_id}/accept", response_model=BookingAdminOut)
def accept(booking_id: str, data: AcceptIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.accept_booking(db, parse_uuid(booking_id, "Booking"), data.staff_id, admin)
    audit.record(
        db, admin, "booking.accept",
        f"Accepted booking of {_describe(booking)}; assigned to {_staff_name(db, booking.assigned_staff_id)}",
        entity="booking", entity_id=booking.id,
    )
    return booking


@router.post("/{booking_id}/assign", response_model=BookingAdminOut)
def reassign(booking_id: str, data: AcceptIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.reassign_staff(db, parse_uuid(booking_id, "Booking"), data.staff_id, admin)
    audit.record(
        db, admin, "booking.reassign",
        f"Reassigned booking of {_describe(booking)} to {_staff_name(db, booking.assigned_staff_id)}",
        entity="booking", entity_id=booking.id,
    )
    return booking


@router.post("/{booking_id}/reject", response_model=BookingAdminOut)
def reject(booking_id: str, data: RejectIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.reject_booking(db, parse_uuid(booking_id, "Booking"), admin, data.reason)
    audit.record(
        db, admin, "booking.reject", f"Rejected booking of {_describe(booking)}. Reason: {booking.rejection_reason}",
        entity="booking", entity_id=booking.id,
    )
    try:
        send_notification_email(
            booking.customer.email,
            "Your Pardz Car Rental booking needs changes",
            f"Your booking was rejected. Reason: {booking.rejection_reason} Please edit and resubmit the booking in My bookings for another review.",
        )
    except (EmailNotConfigured, EmailDeliveryFailed) as exc:
        logger.warning("Booking rejection email was not sent: %s", exc)
    return booking


@router.post("/{booking_id}/send-approval-email")
def send_approval_email(booking_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.get_booking_or_404(db, parse_uuid(booking_id, "Booking"))
    if booking.status != BookingStatus.accepted:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only accepted bookings can receive an approval email")
    try:
        sent_to = send_booking_approved_email(booking)
    except EmailNotConfigured as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))
    except EmailDeliveryFailed as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc))
    audit.record(
        db, admin, "booking.approval_email", f"Sent approval email for booking of {_describe(booking)} to {sent_to}",
        entity="booking", entity_id=booking.id,
    )
    return {"sent": True, "email": sent_to}


# ---- staff workflow
@router.post("/{booking_id}/start", response_model=BookingStaffOut)
def start(booking_id: str, user: Profile = Depends(require_admin_or_staff), db: Session = Depends(get_db)):
    booking = svc.start_rental(db, parse_uuid(booking_id, "Booking"), user)
    audit.record(db, user, "booking.start", f"Released vehicle for booking of {_describe(booking)}", entity="booking", entity_id=booking.id)
    return booking


@router.post("/{booking_id}/complete", response_model=BookingStaffOut)
def complete(booking_id: str, user: Profile = Depends(require_admin_or_staff), db: Session = Depends(get_db)):
    booking = svc.complete_rental(db, parse_uuid(booking_id, "Booking"), user)
    audit.record(db, user, "booking.complete", f"Vehicle returned for booking of {_describe(booking)}", entity="booking", entity_id=booking.id)
    return booking


@router.post("/{booking_id}/inspections", response_model=InspectionOut, status_code=status.HTTP_201_CREATED)
def create_inspection(booking_id: str, data: InspectionCreate, staff: Profile = Depends(require_staff), db: Session = Depends(get_db)):
    return svc.add_inspection(db, parse_uuid(booking_id, "Booking"), staff, data)


@router.post("/{booking_id}/inspections/{inspection_id}/verify", response_model=BookingAdminOut)
def verify_inspection(booking_id: str, inspection_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.verify_inspection(db, parse_uuid(booking_id, "Booking"), parse_uuid(inspection_id, "Inspection"), admin)
    audit.record(db, admin, "inspection.verify", f"Verified an inspection of booking of {_describe(booking)}", entity="booking", entity_id=booking.id)
    return booking


@router.post("/{booking_id}/cleaner", response_model=BookingAdminOut)
def assign_cleaner(booking_id: str, data: AcceptIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.assign_cleaner(db, parse_uuid(booking_id, "Booking"), data.staff_id, admin)
    audit.record(
        db, admin, "cleaning.assign",
        f"Assigned cleaning of {booking.vehicle.plate_number} to {_staff_name(db, booking.cleaning_staff_id)}",
        entity="booking", entity_id=booking.id,
    )
    return booking


@router.post("/{booking_id}/cleaning-done")
def cleaning_done(booking_id: str, user: Profile = Depends(require_admin_or_staff), db: Session = Depends(get_db)):
    booking = svc.mark_cleaning_done(db, parse_uuid(booking_id, "Booking"), user)
    audit.record(db, user, "cleaning.done", f"Marked {booking.vehicle.plate_number} as cleaned", entity="booking", entity_id=booking.id)
    return serialize(booking, user)


@router.post("/{booking_id}/inspection-photos", status_code=status.HTTP_201_CREATED)
def upload_inspection_photo(booking_id: str, file: UploadFile = File(...), staff: Profile = Depends(require_staff), db: Session = Depends(get_db)):
    """Upload one damage photo; send the returned path in the inspection's `photos` list."""
    return {"path": svc.save_inspection_photo(db, parse_uuid(booking_id, "Booking"), staff, file)}


@router.get("/{booking_id}/inspections", response_model=list[InspectionOut])
def list_inspections(booking_id: str, user: Profile = Depends(require_admin_or_staff), db: Session = Depends(get_db)):
    booking = svc.get_booking_or_404(db, parse_uuid(booking_id, "Booking"))
    if user.role == UserRole.staff and booking.assigned_staff_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking.inspections


@router.get("/{booking_id}/print-pack")
def booking_print_pack(booking_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = svc.get_booking_or_404(db, parse_uuid(booking_id, "Booking"))
    customer = booking.customer
    vehicle = booking.vehicle
    base = get_settings().public_base_url.rstrip("/")


    def document_link(path: str | None) -> str:
        if not path:
            return ""
        token = create_document_token(path)
        return f"{base}/files/{storage.DOCUMENT_BUCKET}/{path}?token={token}"

    return {
        "booking": {
            "id": str(booking.id), "start_date": booking.start_date, "end_date": booking.end_date,
            "pickup_time": booking.pickup_time, "return_time": booking.return_time,
            "pickup_location": booking.pickup_location, "service_type": booking.service_type,
            "destination_from": booking.destination_from, "destination_to": booking.destination_to,
            "distance_km": float(booking.distance_km), "status": booking.status.value,
        },
        "customer": {
            "full_name": customer.full_name, "email": customer.email, "phone": customer.phone,
            "age": customer.age, "address": customer.address, "id_type": customer.id_type,
            "id_number": customer.id_number, "second_id_type": customer.second_id_type,
            "second_id_number": customer.second_id_number,
        },
        "vehicle": {
            "make": vehicle.make, "model": vehicle.model, "year": vehicle.year,
            "plate_number": vehicle.plate_number, "category": vehicle.category,
        },
        "agreement_terms": RENTAL_AGREEMENT_TERMS,
        "documents": {
            "Primary ID": document_link(customer.id_photo_path),
            "Second ID": document_link(customer.second_id_photo_path),
            "Proof of address": document_link(customer.proof_of_address_path),
            "Proof of income": document_link(customer.proof_of_income_path),
            "Additional document 1": document_link(customer.extra_document_1_path),
            "Additional document 2": document_link(customer.extra_document_2_path),
        },
        "inspections": [
            {
                "type": inspection.type.value, "checklist": inspection.checklist,
                "damage_notes": inspection.damage_notes, "damage_fee": float(inspection.damage_fee or 0),
                "odometer_reading": inspection.odometer_reading, "fuel_level": inspection.fuel_level,
                "tools": inspection.tools, "damage_marks": inspection.damage_marks,
                "renter_name": inspection.renter_name, "renter_signed": inspection.renter_signed,
                "staff_name": inspection.staff_name, "staff_signed": inspection.staff_signed,
                "photo_urls": [inspection_photo_url(path) for path in inspection.photos or []],
                "created_at": inspection.created_at,
            }
            for inspection in booking.inspections
        ],
    }
