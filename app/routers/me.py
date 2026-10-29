import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_customer
from ..models import Profile, UserRole
from ..schemas import AgeIn, ProfileOut
from ..services import notifications as notes
from ..services.email import EmailDeliveryFailed, EmailNotConfigured, send_notification_email
from ..services import storage

router = APIRouter(prefix="/me", tags=["customer profile"])
logger = logging.getLogger(__name__)


@router.put("/age", response_model=ProfileOut)
def set_age(data: AgeIn, user: Profile = Depends(require_customer), db: Session = Depends(get_db)):
    user.age = data.age
    db.commit()
    return user


@router.post("/verification", response_model=ProfileOut)
def submit_requirements(
    id_type: str = Form(..., max_length=100),
    id_number: str = Form(..., max_length=100),
    second_id_type: str = Form(..., max_length=100),
    second_id_number: str = Form(..., max_length=100),
    address: str = Form(default="", max_length=500),
    first_id: UploadFile | None = File(default=None),
    second_id: UploadFile | None = File(default=None),
    proof_of_address: UploadFile | None = File(default=None),
    proof_of_income: UploadFile | None = File(default=None),
    extra_document_1: UploadFile | None = File(default=None),
    extra_document_2: UploadFile | None = File(default=None),
    user: Profile = Depends(require_customer),
    db: Session = Depends(get_db),
):
    """Submit or update ID details and documents for admin review."""
    slots = {
        "id_photo_path": first_id,
        "second_id_photo_path": second_id,
        "proof_of_address_path": proof_of_address,
        "proof_of_income_path": proof_of_income,
        "extra_document_1_path": extra_document_1,
        "extra_document_2_path": extra_document_2,
    }
    new_paths: dict[str, str] = {}
    try:
        for column, upload in slots.items():
            if upload is not None and upload.filename:
                new_paths[column] = storage.save_upload(storage.DOCUMENT_BUCKET, upload, subdir=str(user.id))

        required_slots = ("id_photo_path", "second_id_photo_path", "proof_of_address_path", "proof_of_income_path")
        missing = [c for c in required_slots if c not in new_paths and not getattr(user, c)]
        if missing or not address.strip() or not all(v.strip() for v in (id_type, id_number, second_id_type, second_id_number)):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add both ID details, a home address, and all four required documents.")
    except HTTPException:
        for path in new_paths.values():
            storage.delete_file(storage.DOCUMENT_BUCKET, path)
        raise

    for column, path in new_paths.items():
        storage.delete_file(storage.DOCUMENT_BUCKET, getattr(user, column))  # replaced document
        setattr(user, column, path)
    user.id_type, user.id_number = id_type.strip(), id_number.strip()
    user.second_id_type, user.second_id_number = second_id_type.strip(), second_id_number.strip()
    user.address = address.strip() or None
    user.verification_status = "pending"
    user.verification_notes = None
    admins = db.scalars(select(Profile).where(Profile.role == UserRole.admin)).all()
    for admin in admins:
        notes.add_notification(db, admin.id, "Customer requirements updated", f"{user.full_name} updated their customer requirements. Review the latest documents.", kind="requirements")
    db.commit()
    notes.publish_after_commit(False, [admin.id for admin in admins])
    for admin in admins:
        try:
            send_notification_email(
                admin.email,
                "WheelSync customer requirements updated",
                f"{user.full_name} submitted or updated customer requirements. Sign in to WheelSync Admin to review the latest documents.",
            )
        except (EmailNotConfigured, EmailDeliveryFailed) as exc:
            logger.warning("Customer requirements email was not sent to admin %s: %s", admin.id, exc)
    return user
