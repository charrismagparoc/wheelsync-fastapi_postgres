from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import parse_uuid, require_admin
from ..models import Booking, BookingStatus, Profile
from ..services import audit
from ..services import notifications as notes

# A booking can be cancelled while it is still a request, or before the rental has started.
# Ongoing and completed rentals are not cancelled.
CANCELLABLE = (BookingStatus.pending, BookingStatus.rejected, BookingStatus.accepted)

router = APIRouter(prefix="/bookings", tags=["bookings"], dependencies=[Depends(require_admin)])


@router.post("/{booking_id}/cancel")
def cancel_booking(booking_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    booking = db.get(Booking, parse_uuid(booking_id, "Booking"))
    if booking is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    if booking.status not in CANCELLABLE:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only pending, rejected or accepted bookings can be cancelled. Ongoing and completed rentals can't be cancelled.",
        )

    customer_name = booking.customer.full_name
    vehicle = f"{booking.vehicle.make} {booking.vehicle.model}"
    dates = f"{booking.start_date} to {booking.end_date}"

    booking.status = BookingStatus.cancelled
    booking.decided_by = admin.id
    booking.decided_at = datetime.now(timezone.utc)

    notes.add_notification(
        db, booking.customer_id, "Booking cancelled",
        f"Your booking for {vehicle} ({dates}) was cancelled by Pardz Car Rental.",
        kind="booking_status", booking_id=booking.id,
    )
    notified = [booking.customer_id]
    if booking.assigned_staff_id:
        notes.add_notification(
            db, booking.assigned_staff_id, "Booking cancelled",
            f"The booking for {customer_name} ({vehicle}, {dates}) was cancelled. You no longer need to prepare it.",
            kind="booking_status", booking_id=booking.id,
        )
        notified.append(booking.assigned_staff_id)

    db.commit()
    notes.publish_after_commit(True, notified)
    audit.record(
        db, admin, "booking.cancel",
        f"Cancelled booking of {customer_name} ({vehicle}, {dates})",
        entity="booking", entity_id=booking.id,
    )
    return {"status": "cancelled"}
