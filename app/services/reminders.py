"""Return reminders: 2 hours and 30 minutes before a rental is due back.

Runs from a background loop (see app/main.py). Each reminder is sent once per booking: the
customer gets an in-app notification and an email, every admin and the staff in charge get an
in-app notification with the customer's name and mobile number so they can call.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import Booking, BookingStatus
from . import notifications as notes
from .email import EmailDeliveryFailed, EmailNotConfigured, send_notification_email

log = logging.getLogger("wheelsync.reminders")

# Used when a booking has no return time (older bookings): the rental is due by close of business.
DEFAULT_RETURN_TIME = time(17, 0)
SERVICE_LABELS = {"self_drive": "Self-drive", "with_driver": "With driver", "pickup_dropoff": "Pickup / drop-off"}


@dataclass(frozen=True)
class Reminder:
    key: str          # stored in Notification.kind as return_reminder_<key>
    lead: timedelta
    label: str        # "2 hours"
    column: str       # Booking column that records when it was sent


REMINDERS = (
    Reminder("2h", timedelta(hours=2), "2 hours", "return_reminder_2h_at"),
    Reminder("30m", timedelta(minutes=30), "30 minutes", "return_reminder_30m_at"),
)


def due_at(booking: Booking) -> datetime:
    """When the vehicle is due back, as an aware datetime in the rental's local timezone."""
    tz = ZoneInfo(get_settings().timezone)
    return datetime.combine(booking.end_date, booking.return_time or DEFAULT_RETURN_TIME, tzinfo=tz)


def _when(due: datetime) -> str:
    return due.strftime("%I:%M %p on %b %d, %Y").lstrip("0")


def _return_instructions(booking: Booking, due: datetime) -> str:
    when = _when(due)
    if booking.service_type == "with_driver":
        return f"Please finish your trip so our driver can bring the vehicle back to Pardz Car Rental by {when}."
    if booking.service_type == "pickup_dropoff":
        where = booking.pickup_location or "the agreed pickup location"
        return f"Please have the vehicle ready for our staff to collect at {where} by {when}."
    return f"Please return the vehicle to Pardz Car Rental at {get_settings().rental_address} by {when}."


def reminder_data(booking: Booking, due: datetime) -> dict:
    """Structured details the admin pop-up and cards show (name and number to call)."""
    customer, vehicle = booking.customer, booking.vehicle
    return {
        "booking_id": str(booking.id),
        "customer_name": customer.full_name,
        "customer_phone": customer.phone,
        "customer_email": customer.email,
        "vehicle": f"{vehicle.make} {vehicle.model}",
        "plate_number": vehicle.plate_number,
        "service_type": booking.service_type,
        "pickup_location": booking.pickup_location,
        "due_at": due.isoformat(),
    }


def _send(db: Session, booking: Booking, reminder: Reminder, due: datetime) -> tuple[list[uuid.UUID], tuple[str, str, str] | None]:
    customer, vehicle = booking.customer, booking.vehicle
    kind = f"return_reminder_{reminder.key}"
    data = reminder_data(booking, due)
    vehicle_label = f"{vehicle.make} {vehicle.model} ({vehicle.plate_number})"
    instructions = _return_instructions(booking, due)

    notes.add_notification(
        db, customer.id, f"Return reminder: {reminder.label} left",
        f"Your {vehicle_label} rental is due back in {reminder.label}. {instructions}",
        kind=kind, booking_id=booking.id, data=data,
    )
    staff_body = (
        f"{customer.full_name} · {customer.phone or 'no mobile number'} must return the {vehicle_label} "
        f"in {reminder.label} ({_when(due)}, {SERVICE_LABELS.get(booking.service_type, booking.service_type)}). "
        "Call the customer to remind them."
    )
    users = [customer.id]
    for admin_id in notes.admin_ids(db):
        notes.add_notification(db, admin_id, f"Vehicle due back in {reminder.label}", staff_body, kind=kind, booking_id=booking.id, data=data)
        users.append(admin_id)
    if booking.assigned_staff_id:
        notes.add_notification(db, booking.assigned_staff_id, f"Vehicle due back in {reminder.label}", staff_body, kind=kind, booking_id=booking.id, data=data)
        users.append(booking.assigned_staff_id)

    email = None
    if customer.email:
        email = (
            customer.email,
            f"Reminder: your rental is due back in {reminder.label}",
            f"Hello {customer.full_name},\n\n"
            f"This is a reminder from Pardz Car Rental. Your {vehicle_label} rental is due back in {reminder.label}.\n\n"
            f"{instructions}\n\n"
            "If you need more time, please contact us before the return time.\n\nPardz Car Rental",
        )
    return users, email


def send_due_return_reminders(db: Session, now: datetime | None = None) -> int:
    """Send every reminder that is due. Returns how many reminders were sent."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(ZoneInfo(get_settings().timezone)).date()
    stmt = (
        select(Booking)
        .where(Booking.status == BookingStatus.ongoing, Booking.end_date.between(today - timedelta(days=1), today + timedelta(days=1)))
        .options(selectinload(Booking.customer), selectinload(Booking.vehicle))
        .with_for_update(of=Booking, skip_locked=True)  # several server processes never send the same reminder twice
    )
    users: list[uuid.UUID] = []
    emails: list[tuple[str, str, str]] = []
    sent = 0
    for booking in db.scalars(stmt).all():
        due = due_at(booking)
        if now >= due:
            continue  # already due; reminders are only useful before the return time
        # Only the latest reminder that applies: a rental that starts 1 hour before its return gets just the 30-minute one.
        applicable = [r for r in REMINDERS if now >= due - r.lead]
        if not applicable:
            continue
        latest = min(applicable, key=lambda r: r.lead)
        if getattr(booking, latest.column) is not None:
            continue
        for reminder in applicable:
            if getattr(booking, reminder.column) is None:
                setattr(booking, reminder.column, now)
        new_users, email = _send(db, booking, latest, due)
        users += new_users
        if email:
            emails.append(email)
        sent += 1
    db.commit()
    if sent:
        notes.publish_after_commit(True, users)
    for recipient, subject, text in emails:
        try:
            send_notification_email(recipient, subject, text)
        except (EmailNotConfigured, EmailDeliveryFailed) as exc:
            log.warning("Return reminder email to %s was not sent: %s", recipient, exc)
    return sent


def returns_due(db: Session, within: timedelta, now: datetime | None = None) -> list[dict]:
    """Ongoing rentals due back within `within`, plus any that are already overdue (admin card)."""
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(
        select(Booking).where(Booking.status == BookingStatus.ongoing)
        .options(selectinload(Booking.customer), selectinload(Booking.vehicle), selectinload(Booking.assigned_staff))
    ).all()
    result = []
    for booking in rows:
        due = due_at(booking)
        if due - now > within:
            continue
        result.append({
            **reminder_data(booking, due),
            "minutes_left": int((due - now).total_seconds() // 60),
            "reminder_2h_sent_at": booking.return_reminder_2h_at,
            "reminder_30m_sent_at": booking.return_reminder_30m_at,
            "staff_name": booking.assigned_staff.full_name if booking.assigned_staff else None,
        })
    return sorted(result, key=lambda row: row["due_at"])
