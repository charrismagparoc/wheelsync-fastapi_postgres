"""Phone numbers as staff type them ("0917 123 4567", "+63 917-123-4567") compared by their digits."""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Profile, UserRole


def phone_digits(phone: str | None) -> str:
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    if digits.startswith("63") and len(digits) == 12:  # +63 917... -> 0917...
        digits = "0" + digits[2:]
    return digits


def find_staff_by_phone(db: Session, phone: str) -> Profile | None:
    digits = phone_digits(phone)
    if len(digits) < 7:
        return None
    staff = db.scalars(select(Profile).where(Profile.role == UserRole.staff, func.coalesce(Profile.phone, "") != "")).all()
    return next((member for member in staff if phone_digits(member.phone) == digits), None)
