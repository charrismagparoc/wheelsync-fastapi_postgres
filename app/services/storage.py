"""Local-disk file storage.

  uploads/vehicle-images/<name>            public
  uploads/customer-documents/<user>/<name> private, only reachable through short-lived signed links
  uploads/inspection-photos/<booking>/<name> private, same signed links (damage photos from the staff app)

To move to S3/GCS later, only this module needs to change.
"""
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from ..config import get_settings

VEHICLE_BUCKET = "vehicle-images"
DOCUMENT_BUCKET = "customer-documents"
INSPECTION_BUCKET = "inspection-photos"

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
DOCUMENT_TYPES = {**IMAGE_TYPES, "application/pdf": "pdf"}


def _root() -> Path:
    root = Path(get_settings().upload_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_path(bucket: str, relative: str) -> Path:
    """Resolve a bucket-relative path and refuse anything that escapes the bucket."""
    base = (_root() / bucket).resolve()
    target = (base / relative).resolve()
    if base != target and base not in target.parents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid file path")
    return target


def save_upload(bucket: str, file: UploadFile, subdir: str = "", allowed: dict[str, str] | None = None) -> str:
    """Validate + store an upload. Returns the bucket-relative path."""
    allowed = allowed or DOCUMENT_TYPES
    ext = allowed.get((file.content_type or "").lower())
    if ext is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported file type. Allowed: {', '.join(sorted(set(allowed.values())))}.")

    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is larger than {get_settings().max_upload_mb} MB.")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")

    relative = f"{subdir}/{uuid.uuid4()}.{ext}" if subdir else f"{uuid.uuid4()}.{ext}"
    target = safe_path(bucket, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return relative


def delete_file(bucket: str, relative: str | None) -> None:
    if not relative:
        return
    try:
        safe_path(bucket, relative).unlink(missing_ok=True)
    except HTTPException:
        pass
