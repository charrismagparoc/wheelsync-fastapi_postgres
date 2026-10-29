"""Keeps a car's status in step with recorded transactions, and stops the same car being rented twice.

A recorded transaction (rental entered by the admin) marks its car Rented from the release date to the
return date, then Available again. Online bookings already do this themselves when the staff starts and
completes the rental, so a car with an ongoing online booking is left alone.
"""
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Booking, BookingStatus, Vehicle, VehicleStatus, WalkInTransaction
from ..realtime import hub
from .bookings import today_local

TAKEN_MESSAGE = "This vehicle is already rented for part of these dates."


def transaction_clash(db: Session, vehicle_id: uuid.UUID, start: date, end: date, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(WalkInTransaction.id).where(
        WalkInTransaction.vehicle_id == vehicle_id,
        WalkInTransaction.start_date <= end,
        WalkInTransaction.end_date >= start,
    )
    if exclude_id is not None:
        stmt = stmt.where(WalkInTransaction.id != exclude_id)
    return db.execute(stmt.limit(1)).first() is not None


def booking_clash(db: Session, vehicle_id: uuid.UUID, start: date, end: date) -> bool:
    return db.execute(
        select(Booking.id).where(
            Booking.vehicle_id == vehicle_id,
            Booking.status.in_((BookingStatus.accepted, BookingStatus.ongoing)),
            Booking.start_date <= end,
            Booking.end_date >= start,
        ).limit(1)
    ).first() is not None


def assert_vehicle_free(db: Session, vehicle_id: uuid.UUID | None, start: date, end: date, exclude_transaction: uuid.UUID | None = None) -> None:
    """For a transaction on a fleet car: no other transaction and no accepted/ongoing booking on those dates."""
    if vehicle_id is None:
        return
    if transaction_clash(db, vehicle_id, start, end, exclude_transaction) or booking_clash(db, vehicle_id, start, end):
        raise HTTPException(status.HTTP_409_CONFLICT, TAKEN_MESSAGE)


def sync_rented_status(db: Session) -> int:
    """Rented while a recorded transaction is running today; Available again once it is over. Returns how many cars changed."""
    today = today_local()
    renting = set(db.scalars(select(WalkInTransaction.vehicle_id).where(
        WalkInTransaction.vehicle_id.is_not(None), WalkInTransaction.start_date <= today, WalkInTransaction.end_date >= today,
    )))
    on_online_rental = set(db.scalars(select(Booking.vehicle_id).where(Booking.status == BookingStatus.ongoing)))

    changed = 0
    if renting:
        for vehicle in db.scalars(select(Vehicle).where(Vehicle.id.in_(renting), Vehicle.status == VehicleStatus.available)):
            vehicle.status = VehicleStatus.rented
            vehicle.auto_rented = True
            changed += 1
    # Only cars this function made Rented go back (rental over, edited to other dates, or deleted).
    # A status the admin picked by hand is never overridden.
    for vehicle in db.scalars(select(Vehicle).where(Vehicle.auto_rented.is_(True))):
        if vehicle.status != VehicleStatus.rented:
            vehicle.auto_rented = False  # someone changed it meanwhile; stop managing it
            changed += 1
        elif vehicle.id not in renting:
            vehicle.auto_rented = False
            if vehicle.id not in on_online_rental:
                vehicle.status = VehicleStatus.available
            changed += 1
    if changed:
        db.commit()
        hub.publish("bookings")
    return changed
