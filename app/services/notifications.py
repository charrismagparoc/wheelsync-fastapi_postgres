import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Booking, BookingStatus, Inspection, InspectionType, Notification, Profile, UserRole
from ..realtime import hub


def add_notification(
    db: Session, user_id: uuid.UUID, title: str, body: str | None, *,
    kind: str = "general", booking_id: uuid.UUID | None = None, data: dict | None = None,
) -> Notification:
    n = Notification(user_id=user_id, title=title, body=body, kind=kind, booking_id=booking_id, data=data or {})
    db.add(n)
    return n


def notify_booking_created(db: Session, booking: Booking) -> list[uuid.UUID]:
    add_notification(
        db, booking.customer_id,
        "Booking request received",
        "Your booking request has been submitted for administrator review.",
        kind="booking_request", booking_id=booking.id,
    )
    return [booking.customer_id]


def notify_booking_status_change(db: Session, booking: Booking, rental_address: str) -> list[uuid.UUID]:
    """Customer notification for a status change (mirrors the old notify_booking_change trigger)."""
    kind = "booking_status"
    if booking.status == BookingStatus.accepted:
        kind = "booking_accepted"
        title = "Booking approved"
        body = (
            "Your booking requirements are approved and verified. "
            f"Please visit Pardz Car Rental at {rental_address}. "
            "Bring one physical ID for vehicle release; it will be returned to you when the vehicle is returned."
        )
    elif booking.status == BookingStatus.rejected and booking.rejection_reason:
        kind = "booking_rejected"
        title = "Booking needs changes"
        body = f"Your booking was rejected. Reason: {booking.rejection_reason} Edit and resubmit the booking in My bookings for another review."
    else:
        title = "Booking status updated"
        body = f"Your booking is now {booking.status.value.replace('_', ' ')}."
    add_notification(db, booking.customer_id, title, body, kind=kind, booking_id=booking.id)
    return [booking.customer_id]


def notify_staff_assigned(db: Session, booking: Booking) -> list[uuid.UUID]:
    if booking.assigned_staff_id is None:
        return []
    add_notification(
        db, booking.assigned_staff_id,
        "Booking assigned",
        "A booking has been assigned to you. Open My jobs for the inspection details.",
        kind="task_assigned", booking_id=booking.id,
    )
    return [booking.assigned_staff_id]


def notify_staff_unassigned(db: Session, booking: Booking, staff_id: uuid.UUID | None) -> list[uuid.UUID]:
    if staff_id is None:
        return []
    vehicle = booking.vehicle
    add_notification(
        db, staff_id,
        "Booking reassigned",
        f"The {vehicle.name} booking ({booking.start_date} to {booking.end_date}) "
        "was moved to another staff member. You no longer need to inspect it.",
        kind="task_reassigned", booking_id=booking.id,
    )
    return [staff_id]


def admin_ids(db: Session) -> list[uuid.UUID]:
    return list(db.scalars(select(Profile.id).where(Profile.role == UserRole.admin)))


def _vehicle_label(booking: Booking) -> str:
    vehicle = booking.vehicle
    return f"{vehicle.name} ({vehicle.plate_number})"


def notify_admins_inspection_done(db: Session, booking: Booking, inspection: Inspection, staff: Profile) -> list[uuid.UUID]:
    kind = "Pre-rental" if inspection.type == InspectionType.pre_rental else "Post-rental"
    admins = admin_ids(db)
    for admin_id in admins:
        add_notification(
            db, admin_id,
            f"{kind} inspection done",
            f"{staff.full_name} finished the {kind.lower()} inspection of the {_vehicle_label(booking)} "
            f"for {booking.customer.full_name}. Review and verify it in Inspections.",
            kind="inspection", booking_id=booking.id,
        )
    return admins


def notify_inspection_verified(db: Session, booking: Booking, inspection: Inspection) -> list[uuid.UUID]:
    kind = "pre-rental" if inspection.type == InspectionType.pre_rental else "post-rental"
    add_notification(
        db, inspection.staff_id,
        "Inspection verified",
        f"The admin verified your {kind} inspection of the {_vehicle_label(booking)}.",
        kind="inspection", booking_id=booking.id,
    )
    return [inspection.staff_id]


def notify_vehicle_returned(db: Session, booking: Booking) -> list[uuid.UUID]:
    admins = admin_ids(db)
    for admin_id in admins:
        add_notification(
            db, admin_id,
            "Vehicle returned",
            f"The {_vehicle_label(booking)} was returned by {booking.customer.full_name}. "
            "It stays off the fleet until the post-rental inspection is verified and cleaning is done.",
            kind="vehicle_returned", booking_id=booking.id,
        )
    return admins + notify_cleaning_assigned(db, booking)


def notify_cleaning_assigned(db: Session, booking: Booking) -> list[uuid.UUID]:
    if booking.cleaning_staff_id is None:
        return []
    add_notification(
        db, booking.cleaning_staff_id,
        "Cleaning check assigned",
        f"Make sure the {_vehicle_label(booking)} gets cleaned, then tap Mark cleaning done in Assigned tasks.",
        kind="cleaning", booking_id=booking.id,
    )
    return [booking.cleaning_staff_id]


def notify_cleaning_unassigned(db: Session, booking: Booking, staff_id: uuid.UUID | None) -> list[uuid.UUID]:
    if staff_id is None:
        return []
    add_notification(
        db, staff_id,
        "Cleaning check reassigned",
        f"Another staff member will now check the cleaning of the {_vehicle_label(booking)}.",
        kind="cleaning", booking_id=booking.id,
    )
    return [staff_id]


def notify_cleaning_done(db: Session, booking: Booking, cleaner: Profile, released: bool) -> list[uuid.UUID]:
    status_line = (
        "The vehicle is available for booking again."
        if released else "Verify the post-rental inspection to put the vehicle back on the fleet."
    )
    admins = admin_ids(db)
    for admin_id in admins:
        add_notification(db, admin_id, "Cleaning done", f"{cleaner.full_name} confirmed the {_vehicle_label(booking)} is clean. {status_line}", kind="cleaning", booking_id=booking.id)
    return admins


def publish_after_commit(booking_changed: bool, notified_users: list[uuid.UUID]) -> None:
    """Call AFTER db.commit() so clients that refetch immediately see the new rows."""
    if booking_changed:
        hub.publish("bookings")
    for uid in set(notified_users):
        hub.publish("notifications", uid)
