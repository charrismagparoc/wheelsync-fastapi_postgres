"""Booking business rules.

These used to live in Postgres triggers (enforce_booking_rules, prevent_vehicle_booking_overlap,
require_post_rental_inspection_before_completion, sync_vehicle_status_with_booking,
notify_booking_change). They now live here, with the overlap rule additionally enforced by an
exclusion constraint in the database as a final safety net.
"""
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    Booking, BookingStatus, Inspection, InspectionType, Profile, UserRole, Vehicle, VehicleStatus,
)
from ..schemas import BookingCreate, InspectionCreate
from . import notifications as notes
from . import storage
from .blacklist import id_blacklist_record

OVERLAP_MESSAGE = "This vehicle is already reserved for part of the selected dates."
BLACKLISTED_ID_MESSAGE = (
    "Your ID number is on Pardz Car Rental's blacklist, so bookings are not available for this account. "
    "Please contact Pardz Car Rental."
)
ACTIVE = (BookingStatus.accepted, BookingStatus.ongoing)


def today_local() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def rental_days(start: date, end: date) -> int:
    return max(1, (end - start).days + 1)


def booking_query():
    return select(Booking).options(
        selectinload(Booking.customer), selectinload(Booking.vehicle), selectinload(Booking.inspections)
    )


def get_booking_or_404(db: Session, booking_id: uuid.UUID, *, lock: bool = False) -> Booking:
    stmt = booking_query().where(Booking.id == booking_id)
    if lock:
        stmt = stmt.with_for_update(of=Booking)
    booking = db.execute(stmt).scalar_one_or_none()
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    return booking


def _assert_no_overlap(db: Session, booking: Booking) -> None:
    clash = db.execute(
        select(Booking.id).where(
            Booking.vehicle_id == booking.vehicle_id,
            Booking.id != booking.id,
            Booking.status.in_(ACTIVE),
            Booking.start_date <= booking.end_date,
            Booking.end_date >= booking.start_date,
        ).limit(1)
    ).first()
    if clash or _recorded_rental_clash(db, booking.vehicle_id, booking.start_date, booking.end_date):
        raise HTTPException(status.HTTP_409_CONFLICT, OVERLAP_MESSAGE)


def _recorded_rental_clash(db: Session, vehicle_id: uuid.UUID, start: date, end: date) -> bool:
    """A rental the admin recorded on the Transactions page uses the car on these dates."""
    from .fleet import transaction_clash  # fleet imports this module

    return transaction_clash(db, vehicle_id, start, end)


def _commit_or_conflict(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if "bookings_no_active_overlap" in str(exc.orig):
            raise HTTPException(status.HTTP_409_CONFLICT, OVERLAP_MESSAGE)
        raise


def _validate_booking_request(
    db: Session,
    customer: Profile,
    data: BookingCreate,
    *,
    current_booking_id: uuid.UUID | None = None,
) -> Vehicle:
    if data.start_date > data.end_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The booking end date must be on or after the start date.")
    if data.start_date < today_local():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Bookings cannot start in the past.")
    if customer.is_blacklisted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This customer is currently restricted from making bookings.")
    if id_blacklist_record(db, customer) is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, BLACKLISTED_ID_MESSAGE)
    if data.start_date == data.end_date and data.pickup_time and data.return_time and data.return_time <= data.pickup_time:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Return time must be after pickup time for a same-day rental.")
    if data.service_type == "pickup_dropoff" and not (data.pickup_location or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A pickup / drop-off location is required for this service.")

    vehicle = db.get(Vehicle, data.vehicle_id)
    if vehicle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
    if vehicle.status == VehicleStatus.maintenance:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This vehicle is currently under maintenance.")
    if vehicle.status == VehicleStatus.cleaning:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This vehicle was just returned and is being inspected and cleaned.")
    # Dates already taken by an accepted or ongoing booking can't be requested again.
    clash_query = select(Booking.id).where(
        Booking.vehicle_id == vehicle.id,
        Booking.status.in_(ACTIVE),
        Booking.start_date <= data.end_date,
        Booking.end_date >= data.start_date,
    )
    if current_booking_id is not None:
        clash_query = clash_query.where(Booking.id != current_booking_id)
    if db.execute(clash_query.limit(1)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, OVERLAP_MESSAGE)
    if _recorded_rental_clash(db, vehicle.id, data.start_date, data.end_date):
        raise HTTPException(status.HTTP_409_CONFLICT, OVERLAP_MESSAGE)
    return vehicle


# ----------------------------------------------------------------- create
def create_booking(db: Session, customer: Profile, data: BookingCreate) -> Booking:
    vehicle = _validate_booking_request(db, customer, data)
    booking = Booking(
        customer_id=customer.id,
        vehicle_id=vehicle.id,
        start_date=data.start_date,
        end_date=data.end_date,
        pickup_time=data.pickup_time,
        return_time=data.return_time,
        pickup_location=(data.pickup_location or "").strip() or None,
        destination_from=(data.destination_from or "").strip() or None,
        destination_to=(data.destination_to or "").strip() or None,
        service_type=data.service_type,
        distance_km=Decimal(str(data.distance_km)),
        notes=data.notes,
        status=BookingStatus.pending,  # clients can never choose the initial status
    )
    db.add(booking)
    db.flush()
    users = notes.notify_booking_created(db, booking)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def resubmit_rejected_booking(
    db: Session,
    booking_id: uuid.UUID,
    customer: Profile,
    data: BookingCreate,
) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.customer_id != customer.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    if booking.status != BookingStatus.rejected:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only rejected bookings can be edited and resubmitted.")

    vehicle = _validate_booking_request(db, customer, data, current_booking_id=booking.id)
    booking.vehicle_id = vehicle.id
    booking.vehicle = vehicle
    booking.start_date = data.start_date
    booking.end_date = data.end_date
    booking.pickup_time = data.pickup_time
    booking.return_time = data.return_time
    booking.pickup_location = (data.pickup_location or "").strip() or None
    booking.destination_from = (data.destination_from or "").strip() or None
    booking.destination_to = (data.destination_to or "").strip() or None
    booking.service_type = data.service_type
    booking.distance_km = Decimal(str(data.distance_km))
    booking.notes = data.notes
    booking.status = BookingStatus.pending
    booking.rejection_reason = None
    booking.assigned_staff_id = None
    booking.decided_by = None
    booking.decided_at = None

    admins = db.scalars(select(Profile).where(Profile.role == UserRole.admin)).all()
    for admin in admins:
        notes.add_notification(
            db,
            admin.id,
            "Customer updated rejected booking",
            f"{customer.full_name} edited and resubmitted rejected booking {booking.id} for "
            f"{vehicle.name} ({data.start_date} to {data.end_date}). Review it in Pending bookings.",
            kind="booking_request", booking_id=booking.id,
        )
    db.commit()
    notes.publish_after_commit(True, [admin.id for admin in admins])
    return get_booking_or_404(db, booking.id)


# ----------------------------------------------------------------- admin decisions
def accept_booking(db: Session, booking_id: uuid.UUID, staff_id: uuid.UUID, admin: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status not in (BookingStatus.pending, BookingStatus.rejected):
        raise HTTPException(status.HTTP_409_CONFLICT, "This booking is no longer pending or rejected.")
    if booking.customer.verification_status != "verified":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Verify the customer's requirements before accepting this booking.")
    # The ID may have been added to the blacklist after the customer sent the request.
    record = id_blacklist_record(db, booking.customer)
    if record is not None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"This customer's ID number is on the blacklist ({record.full_name}, {record.reason}). The booking can't be accepted.",
        )
    staff = db.get(Profile, staff_id)
    if staff is None or staff.role != UserRole.staff:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a valid staff member to assign.")
    _assert_no_overlap(db, booking)

    booking.status = BookingStatus.accepted
    booking.rejection_reason = None  # an accepted booking no longer shows an earlier rejection
    booking.assigned_staff_id = staff.id
    booking.decided_by = admin.id
    booking.decided_at = datetime.now(timezone.utc)
    db.flush()
    users = notes.notify_booking_status_change(db, booking, get_settings().rental_address)
    users += notes.notify_staff_assigned(db, booking)
    _commit_or_conflict(db)
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def reassign_staff(db: Session, booking_id: uuid.UUID, staff_id: uuid.UUID, admin: Profile) -> Booking:
    """Hand an accepted or ongoing booking to another staff member (e.g. the assigned one is busy)."""
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status not in (BookingStatus.accepted, BookingStatus.ongoing):
        raise HTTPException(status.HTTP_409_CONFLICT, "Only accepted or ongoing bookings can be reassigned.")
    staff = db.get(Profile, staff_id)
    if staff is None or staff.role != UserRole.staff:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a valid staff member to assign.")
    if booking.assigned_staff_id == staff.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This staff member is already assigned to the booking.")

    previous_staff_id = booking.assigned_staff_id
    booking.assigned_staff_id = staff.id
    booking.decided_by = admin.id
    db.flush()
    users = notes.notify_staff_assigned(db, booking)
    users += notes.notify_staff_unassigned(db, booking, previous_staff_id)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def reject_booking(db: Session, booking_id: uuid.UUID, admin: Profile, reason: str) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status not in (BookingStatus.pending, BookingStatus.rejected):
        raise HTTPException(status.HTTP_409_CONFLICT, "This booking is no longer pending or rejected.")
    booking.status = BookingStatus.rejected
    booking.rejection_reason = reason.strip()
    booking.decided_by = admin.id
    booking.decided_at = datetime.now(timezone.utc)
    db.flush()
    users = notes.notify_booking_status_change(db, booking, get_settings().rental_address)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


# ----------------------------------------------------------------- staff workflow
def _assert_can_work_on(booking: Booking, user: Profile) -> None:
    if user.role == UserRole.admin:
        return
    if user.role == UserRole.staff and booking.assigned_staff_id == user.id:
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, "This booking is not assigned to you.")


def start_rental(db: Session, booking_id: uuid.UUID, user: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    _assert_can_work_on(booking, user)
    if booking.status != BookingStatus.accepted:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only accepted bookings can be started.")
    booking.status = BookingStatus.ongoing
    if booking.vehicle.status == VehicleStatus.available:
        booking.vehicle.status = VehicleStatus.rented
    db.flush()
    users = notes.notify_booking_status_change(db, booking, get_settings().rental_address)
    _commit_or_conflict(db)
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def complete_rental(db: Session, booking_id: uuid.UUID, user: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    _assert_can_work_on(booking, user)
    if booking.status != BookingStatus.ongoing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only ongoing rentals can be completed.")
    if not any(i.type == InspectionType.post_rental for i in booking.inspections):
        raise HTTPException(status.HTTP_409_CONFLICT, "A post-rental inspection must be completed before releasing the vehicle.")

    returned_on = today_local()
    booking.status = BookingStatus.completed
    booking.actual_return_date = returned_on
    booking.late_return = returned_on > booking.end_date
    if booking.vehicle.status == VehicleStatus.rented:
        booking.vehicle.status = VehicleStatus.cleaning
    if booking.cleaning_staff_id is None:
        booking.cleaning_staff_id = booking.assigned_staff_id  # the admin can hand cleaning to someone else
    db.flush()
    users = notes.notify_booking_status_change(db, booking, get_settings().rental_address)
    users += notes.notify_vehicle_returned(db, booking)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def _checked_photos(booking: Booking, photos: list[str]) -> list[str]:
    """Only accept photos that were uploaded for this booking."""
    for path in photos:
        if not path.startswith(f"{booking.id}/") or not storage.safe_path(storage.INSPECTION_BUCKET, path).is_file():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "One of the photos was not uploaded for this booking. Take it again.")
    return list(dict.fromkeys(photos))


def save_inspection_photo(db: Session, booking_id: uuid.UUID, staff: Profile, file) -> str:
    booking = get_booking_or_404(db, booking_id)
    if booking.assigned_staff_id != staff.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This booking is not assigned to you.")
    if booking.status not in (BookingStatus.accepted, BookingStatus.ongoing):
        raise HTTPException(status.HTTP_409_CONFLICT, "Inspections can only be recorded for accepted or ongoing bookings.")
    return storage.save_upload(storage.INSPECTION_BUCKET, file, subdir=str(booking.id), allowed=storage.IMAGE_TYPES)


def add_inspection(db: Session, booking_id: uuid.UUID, staff: Profile, data: InspectionCreate) -> Inspection:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.assigned_staff_id != staff.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This booking is not assigned to you.")
    if booking.status not in (BookingStatus.accepted, BookingStatus.ongoing):
        raise HTTPException(status.HTTP_409_CONFLICT, "Inspections can only be recorded for accepted or ongoing bookings.")
    inspection = Inspection(
        booking_id=booking.id,
        staff_id=staff.id,
        type=data.type,
        checklist=data.checklist,
        odometer_reading=data.odometer_reading,
        fuel_level=data.fuel_level,
        damage_notes=data.damage_notes,
        damage_fee=Decimal(str(data.damage_fee)),
        tools=data.tools,
        damage_marks=[mark.model_dump() for mark in data.damage_marks],
        renter_name=(data.renter_name or "").strip() or None,
        renter_signed=data.renter_signed,
        staff_name=(data.staff_name or "").strip() or None,
        staff_signed=data.staff_signed,
        photos=_checked_photos(booking, data.photos),
    )
    db.add(inspection)
    db.flush()
    users = notes.notify_admins_inspection_done(db, booking, inspection, staff)
    db.commit()
    notes.publish_after_commit(True, users)
    return inspection


# ----------------------------------------------------------------- return to fleet
def _release_vehicle_if_ready(booking: Booking) -> bool:
    """Put a returned vehicle back on the fleet once its post-rental inspection is verified and it is clean."""
    post = [i for i in booking.inspections if i.type == InspectionType.post_rental]
    ready = (
        booking.status == BookingStatus.completed
        and booking.cleaning_done_at is not None
        and post and all(i.verified_at is not None for i in post)
    )
    if ready and booking.vehicle.status == VehicleStatus.cleaning:
        booking.vehicle.status = VehicleStatus.available
        return True
    return False


def verify_inspection(db: Session, booking_id: uuid.UUID, inspection_id: uuid.UUID, admin: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    inspection = next((i for i in booking.inspections if i.id == inspection_id), None)
    if inspection is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inspection not found")
    if inspection.verified_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This inspection is already verified.")
    inspection.verified_by = admin.id
    inspection.verified_at = datetime.now(timezone.utc)
    _release_vehicle_if_ready(booking)
    db.flush()
    users = notes.notify_inspection_verified(db, booking, inspection)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def assign_cleaner(db: Session, booking_id: uuid.UUID, staff_id: uuid.UUID, admin: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status != BookingStatus.completed or booking.cleaning_done_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cleaning can only be assigned for a returned vehicle that is not cleaned yet.")
    staff = db.get(Profile, staff_id)
    if staff is None or staff.role != UserRole.staff:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a valid staff member to assign.")
    if booking.cleaning_staff_id == staff.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This staff member is already in charge of the cleaning.")
    previous_staff_id = booking.cleaning_staff_id
    booking.cleaning_staff_id = staff.id
    db.flush()
    users = notes.notify_cleaning_assigned(db, booking)
    users += notes.notify_cleaning_unassigned(db, booking, previous_staff_id)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


def mark_cleaning_done(db: Session, booking_id: uuid.UUID, user: Profile) -> Booking:
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status != BookingStatus.completed:
        raise HTTPException(status.HTTP_409_CONFLICT, "The vehicle has not been returned yet.")
    if user.role != UserRole.admin and booking.cleaning_staff_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This cleaning check is not assigned to you.")
    if booking.cleaning_done_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cleaning is already marked as done.")
    booking.cleaning_done_at = datetime.now(timezone.utc)
    released = _release_vehicle_if_ready(booking)
    db.flush()
    users = notes.notify_cleaning_done(db, booking, user, released)
    db.commit()
    notes.publish_after_commit(True, users)
    return get_booking_or_404(db, booking.id)


# ----------------------------------------------------------------- admin edits
EDITABLE_STATUSES = (BookingStatus.pending, BookingStatus.rejected, BookingStatus.accepted, BookingStatus.ongoing)


def admin_edit_booking(db: Session, booking_id: uuid.UUID, admin: Profile, data) -> tuple[Booking, list[str]]:
    """Change a booking's vehicle, schedule, destination or service. Returns the booking and what changed."""
    booking = get_booking_or_404(db, booking_id, lock=True)
    if booking.status not in EDITABLE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "Completed, cancelled or rejected-and-closed bookings can't be edited.")
    changes = data.model_dump(exclude_unset=True)
    changed: list[str] = []

    if "vehicle_id" in changes and changes["vehicle_id"] and changes["vehicle_id"] != booking.vehicle_id:
        vehicle = db.get(Vehicle, changes["vehicle_id"])
        if vehicle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
        if booking.status == BookingStatus.ongoing:
            raise HTTPException(status.HTTP_409_CONFLICT, "The car is already out. Change the vehicle only before the rental starts.")
        changed.append(f"vehicle {booking.vehicle.name} ({booking.vehicle.plate_number}) -> {vehicle.name} ({vehicle.plate_number})")
        booking.vehicle_id, booking.vehicle = vehicle.id, vehicle

    for key in ("start_date", "end_date", "pickup_time", "return_time", "service_type"):
        if key in changes and changes[key] is not None and changes[key] != getattr(booking, key):
            changed.append(f"{key.replace('_', ' ')} {getattr(booking, key)} -> {changes[key]}")
            setattr(booking, key, changes[key])
    for key in ("pickup_location", "destination_to", "notes"):
        if key in changes:
            value = (changes[key] or "").strip() or None
            if value != getattr(booking, key):
                changed.append(f"{key.replace('_', ' ')} {getattr(booking, key) or '-'} -> {value or '-'}")
                setattr(booking, key, value)

    if booking.start_date > booking.end_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The end date must be on or after the start date.")
    if booking.status in ACTIVE:
        _assert_no_overlap(db, booking)  # also checks rentals recorded on the Transactions page
    if not changed:
        return booking, changed

    notes.add_notification(
        db, booking.customer_id, "Booking updated",
        f"Pardz Car Rental updated your booking for {booking.vehicle.name} "
        f"({booking.start_date} to {booking.end_date}). Open My bookings to see the details.",
        kind="booking_status", booking_id=booking.id,
    )
    _commit_or_conflict(db)
    notes.publish_after_commit(True, [booking.customer_id])
    return get_booking_or_404(db, booking.id), changed
