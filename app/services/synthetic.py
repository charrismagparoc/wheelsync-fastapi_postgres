"""Cars that only appear in Pardz's past rental history (services/company_data.py).

Past customers stay visible because the churn model learns from their rentals. The history's cars are not
part of today's fleet, so they and the rentals on them are hidden from the fleet, the customer app, the
dashboard and the bookings list; those pages only show the cars Pardz rents out now.
"""
from sqlalchemy import func, not_, or_, select

from ..models import Booking, Vehicle

SYNTHETIC_DESCRIPTION = "Synthetic test vehicle (historical data only)."


def is_synthetic_vehicle():
    return or_(Vehicle.plate_number.startswith("SYN-"), func.coalesce(Vehicle.description, "") == SYNTHETIC_DESCRIPTION)


def is_real_vehicle():
    return not_(is_synthetic_vehicle())


def on_real_vehicle():
    """Bookings filter: the booking's vehicle is not synthetic."""
    return Booking.vehicle_id.not_in(select(Vehicle.id).where(is_synthetic_vehicle()))
