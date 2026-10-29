from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import parse_uuid, require_admin
from ..models import Profile, TransactionExtension, Vehicle, WalkInTransaction
from ..realtime import hub
from ..schemas import ExtensionIn, WalkInIn, WalkInOut
from ..services import audit
from ..services.bookings import today_local
from ..services.fleet import assert_vehicle_free, sync_rented_status

router = APIRouter(prefix="/admin/transactions", tags=["transactions"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[WalkInOut])
def list_transactions(
    start_from: date | None = None,
    start_to: date | None = None,
    q: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    stmt = select(WalkInTransaction).order_by(WalkInTransaction.start_date.desc(), WalkInTransaction.created_at.desc())
    if start_from:
        stmt = stmt.where(WalkInTransaction.start_date >= start_from)
    if start_to:
        stmt = stmt.where(WalkInTransaction.start_date <= start_to)
    if q and q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            WalkInTransaction.customer_name.ilike(like), WalkInTransaction.vehicle_label.ilike(like),
            WalkInTransaction.destination.ilike(like), WalkInTransaction.customer_phone.ilike(like),
        ))
    return db.scalars(stmt).all()


def _vehicle_label(db: Session, data: WalkInIn) -> tuple[Vehicle | None, str]:
    vehicle = None
    if data.vehicle_id:
        vehicle = db.get(Vehicle, data.vehicle_id)
        if vehicle is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Vehicle not found")
    label = f"{vehicle.name} ({vehicle.plate_number})" if vehicle else (data.vehicle_label or "").strip()
    if not label:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose the vehicle or type which vehicle was rented.")
    return vehicle, label


def _fill(transaction: WalkInTransaction, data: WalkInIn, vehicle: Vehicle | None, label: str) -> None:
    if data.start_date > data.end_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The return date must be on or after the release date.")
    transaction.customer_name = data.customer_name
    transaction.customer_phone = (data.customer_phone or "").strip() or None
    transaction.vehicle_id = vehicle.id if vehicle else None
    transaction.vehicle_label = label
    transaction.start_date, transaction.end_date = data.start_date, data.end_date
    transaction.destination = (data.destination or "").strip() or None
    transaction.service_type = data.service_type
    transaction.amount = Decimal(str(data.amount))
    transaction.payment_method = data.payment_method
    transaction.notes = (data.notes or "").strip() or None


def _get(db: Session, transaction_id: str) -> WalkInTransaction:
    transaction = db.get(WalkInTransaction, parse_uuid(transaction_id, "Transaction"))
    if transaction is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Transaction not found")
    return transaction


@router.post("", response_model=WalkInOut, status_code=status.HTTP_201_CREATED)
def create_transaction(data: WalkInIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    vehicle, label = _vehicle_label(db, data)
    assert_vehicle_free(db, vehicle.id if vehicle else None, data.start_date, data.end_date)
    transaction = WalkInTransaction(created_by=admin.id)
    _fill(transaction, data, vehicle, label)
    db.add(transaction)
    db.commit()
    sync_rented_status(db)
    hub.publish("bookings")  # the dashboard revenue and monthly chart include walk-ins
    audit.record(
        db, admin, "transaction.create",
        f"Recorded transaction: {transaction.customer_name}, {label}, {transaction.start_date} to "
        f"{transaction.end_date}, PHP {transaction.amount:,.2f}",
        entity="transaction", entity_id=transaction.id,
    )
    return transaction


@router.put("/{transaction_id}", response_model=WalkInOut)
def update_transaction(transaction_id: str, data: WalkInIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    transaction = _get(db, transaction_id)
    if transaction.extensions and data.end_date < transaction.extensions[-1].new_end_date:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "An added payment moved this return date. Undo that payment first to set an earlier return date.",
        )
    before = f"{transaction.customer_name}, {transaction.vehicle_label}, {transaction.start_date} to {transaction.end_date}, PHP {transaction.amount:,.2f}"
    vehicle, label = _vehicle_label(db, data)
    assert_vehicle_free(db, vehicle.id if vehicle else None, data.start_date, data.end_date, exclude_transaction=transaction.id)
    _fill(transaction, data, vehicle, label)
    db.commit()
    sync_rented_status(db)
    hub.publish("bookings")
    after = f"{transaction.customer_name}, {label}, {transaction.start_date} to {transaction.end_date}, PHP {transaction.amount:,.2f}"
    audit.record(db, admin, "transaction.update", f"Edited transaction: {before} -> {after}", entity="transaction", entity_id=transaction.id)
    return transaction


@router.post("/{transaction_id}/extensions", response_model=WalkInOut, status_code=status.HTTP_201_CREATED)
def add_payment(transaction_id: str, data: ExtensionIn, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """An added payment on this rental; optionally the customer also keeps the car longer (new return date)."""
    transaction = _get(db, transaction_id)
    new_end = data.new_end_date or transaction.end_date
    if new_end < transaction.end_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"The new return date can't be before the current one ({transaction.end_date}).")
    if new_end > transaction.end_date:
        assert_vehicle_free(db, transaction.vehicle_id, transaction.start_date, new_end, exclude_transaction=transaction.id)
    extension = TransactionExtension(
        previous_end_date=transaction.end_date,
        new_end_date=new_end,
        amount=Decimal(str(data.amount)),
        payment_method=data.payment_method,
        paid_on=today_local(),
        notes=(data.notes or "").strip() or None,
        created_by=admin.id,
    )
    transaction.extensions.append(extension)
    transaction.end_date = new_end
    db.commit()
    sync_rented_status(db)
    db.refresh(transaction)
    hub.publish("bookings")
    audit.record(
        db, admin, "transaction.payment",
        f"Added payment PHP {extension.amount:,.2f} to the rental of {transaction.customer_name} ({transaction.vehicle_label})"
        + (f"; return date moved from {extension.previous_end_date} to {extension.new_end_date}"
           if extension.new_end_date > extension.previous_end_date else ""),
        entity="transaction", entity_id=transaction.id,
    )
    return transaction


@router.delete("/{transaction_id}/extensions/{extension_id}", response_model=WalkInOut)
def undo_extension(transaction_id: str, extension_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    """Remove the latest added payment (entered by mistake); the return date goes back to what it was before it."""
    transaction = _get(db, transaction_id)
    if not transaction.extensions or str(transaction.extensions[-1].id) != extension_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "Only the latest added payment can be undone.")
    extension = transaction.extensions[-1]
    transaction.end_date = extension.previous_end_date
    transaction.extensions.remove(extension)
    db.commit()
    sync_rented_status(db)
    db.refresh(transaction)
    hub.publish("bookings")
    audit.record(
        db, admin, "transaction.payment_undo",
        f"Removed added payment PHP {extension.amount:,.2f} from the rental of {transaction.customer_name} "
        f"({transaction.vehicle_label}); return date {transaction.end_date}",
        entity="transaction", entity_id=transaction.id,
    )
    return transaction


@router.delete("/{transaction_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transaction(transaction_id: str, admin: Profile = Depends(require_admin), db: Session = Depends(get_db)):
    transaction = _get(db, transaction_id)
    total = transaction.amount + sum((extension.amount for extension in transaction.extensions), Decimal(0))
    summary = (
        f"Deleted transaction: {transaction.customer_name}, {transaction.vehicle_label}, "
        f"{transaction.start_date}, PHP {total:,.2f} paid in total"
    )
    db.delete(transaction)
    db.commit()
    sync_rented_status(db)
    hub.publish("bookings")
    audit.record(db, admin, "transaction.delete", summary, entity="transaction", entity_id=transaction_id)
