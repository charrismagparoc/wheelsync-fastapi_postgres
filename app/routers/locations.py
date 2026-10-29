from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import parse_uuid, require_admin
from ..models import Location, Profile
from ..schemas import LocationIn, LocationOut, LocationPublicOut
from ..services import audit

router = APIRouter(tags=["locations"])
NAME_TAKEN = "This destination is already on the list."


def _describe(location: Location) -> str:
    return location.name


def _apply(location: Location, data: LocationIn) -> None:
    location.name, location.sort_order, location.is_active = data.name, data.sort_order, True


@router.get("/public/locations", response_model=list[LocationPublicOut])
def public_locations(db: Session = Depends(get_db)):
    """Destination dropdown of the customer app: every destination the admin added, in list order."""
    return db.scalars(select(Location).order_by(Location.sort_order, Location.name)).all()


@router.get("/admin/locations", response_model=list[LocationOut], dependencies=[Depends(require_admin)])
def list_locations(db: Session = Depends(get_db)):
    return db.scalars(select(Location).order_by(Location.sort_order, Location.name)).all()


@router.post("/admin/locations", response_model=LocationOut, status_code=status.HTTP_201_CREATED)
def create_location(data: LocationIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    location = Location()
    _apply(location, data)
    db.add(location)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, NAME_TAKEN)
    audit.record(db, admin, "location.create", f"Added destination {_describe(location)}", entity="location", entity_id=location.id)
    return location


@router.put("/admin/locations/{location_id}", response_model=LocationOut)
def update_location(location_id: str, data: LocationIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    location = db.get(Location, parse_uuid(location_id, "Location"))
    if location is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Location not found")
    before = _describe(location)
    _apply(location, data)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, NAME_TAKEN)
    audit.record(db, admin, "location.update", f"Renamed destination {before} -> {_describe(location)}", entity="location", entity_id=location.id)
    return location


@router.delete("/admin/locations/{location_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_location(location_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    location = db.get(Location, parse_uuid(location_id, "Location"))
    if location is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Location not found")
    name = location.name
    db.delete(location)  # bookings keep the destination name and the fee they were priced with
    db.commit()
    audit.record(db, admin, "location.delete", f"Removed destination {name}", entity="location", entity_id=location_id)
