"""File storage in PostgreSQL (the stored_files table).

  vehicle-images/<name>              public
  customer-documents/<user>/<name>   private, only reachable through short-lived signed links
  inspection-photos/<booking>/<name> private, same signed links (damage photos from the staff app)

Files live in the database, so they last as long as the rest of the data. They used to be written to the server's
disk, which hosts like Render wipe on every restart and redeploy: the database still pointed to the files, but the
files were gone ("Document not found"). Files an older version saved on disk under UPLOAD_DIR are still read.

To move to S3/GCS later, only this module needs to change.
"""
import io
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import delete, insert, select

from ..config import get_settings
from ..database import engine
from ..models import StoredFile

VEHICLE_BUCKET = "vehicle-images"
DOCUMENT_BUCKET = "customer-documents"
INSPECTION_BUCKET = "inspection-photos"

IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
DOCUMENT_TYPES = {**IMAGE_TYPES, "application/pdf": "pdf"}
CONTENT_TYPES = {ext: mime for mime, ext in DOCUMENT_TYPES.items()}  # "jpg" -> "image/jpeg", "pdf" -> ...


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


def _content_type(relative: str) -> str:
    return CONTENT_TYPES.get(relative.rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _store(bucket: str, relative: str, data: bytes) -> str:
    safe_path(bucket, relative)  # same path rules as before: nothing may point outside its bucket
    with engine.begin() as conn:
        conn.execute(insert(StoredFile).values(bucket=bucket, path=relative, content_type=_content_type(relative), data=data))
    return relative


def read_file(bucket: str, relative: str) -> tuple[bytes, str] | None:
    """The file's bytes and content type, or None when the file does not exist."""
    target = safe_path(bucket, relative)
    with engine.connect() as conn:
        row = conn.execute(
            select(StoredFile.data, StoredFile.content_type).where(StoredFile.bucket == bucket, StoredFile.path == relative)
        ).first()
    if row is not None:
        return bytes(row.data), row.content_type
    if target.is_file():  # saved on disk by an older version
        return target.read_bytes(), _content_type(relative)
    return None


def file_exists(bucket: str, relative: str) -> bool:
    target = safe_path(bucket, relative)
    with engine.connect() as conn:
        found = conn.execute(select(StoredFile.path).where(StoredFile.bucket == bucket, StoredFile.path == relative)).first()
    return found is not None or target.is_file()


def sniff_type(data: bytes) -> str | None:
    """The real file type from the file's first bytes. The browser's Content-Type can be anything."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    return None


def save_upload(bucket: str, file: UploadFile, subdir: str = "", allowed: dict[str, str] | None = None) -> str:
    """Validate + store an upload. Returns the bucket-relative path."""
    allowed = allowed or DOCUMENT_TYPES
    kinds = ", ".join(sorted(set(allowed.values())))
    if (file.content_type or "").lower() not in allowed and file.content_type not in (None, "", "application/octet-stream"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Unsupported file type. Allowed: {kinds}.")

    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File is larger than {get_settings().max_upload_mb} MB.")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")
    # Store the file under the type its contents prove, so a renamed script or HTML page is never kept.
    ext = allowed.get(sniff_type(data) or "")
    if ext is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"This file is not a valid photo or document. Allowed: {kinds}.")

    relative = f"{subdir}/{uuid.uuid4()}.{ext}" if subdir else f"{uuid.uuid4()}.{ext}"
    return _store(bucket, relative, data)


def save_photo(bucket: str, file: UploadFile) -> str:
    """Store a photo of any image type. JPG, PNG, WEBP and GIF are kept as they are; anything else a phone or
    camera produces (HEIC from iPhones, BMP, TIFF, AVIF, ...) is converted to JPEG so every browser can show it."""
    limit = get_settings().max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Photo is larger than {get_settings().max_upload_mb} MB.")
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The uploaded file is empty.")
    ext = IMAGE_TYPES.get(sniff_type(data) or "")
    if ext is None:
        data, ext = _convert_to_jpeg(data), "jpg"
    return _store(bucket, f"{uuid.uuid4()}.{ext}", data)


def _convert_to_jpeg(data: bytes) -> bytes:
    from PIL import Image, ImageOps, UnidentifiedImageError  # imported here: only needed for unusual photo types

    try:
        from pillow_heif import register_heif_opener

        register_heif_opener()  # iPhone HEIC / HEIF photos
    except ImportError:
        pass
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")  # keep phone photos the right way up
            out = io.BytesIO()
            image.save(out, "JPEG", quality=88, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "This file is not a photo. Choose an image file.") from None


def delete_file(bucket: str, relative: str | None) -> None:
    if not relative:
        return
    try:
        target = safe_path(bucket, relative)
    except HTTPException:
        return
    with engine.begin() as conn:
        conn.execute(delete(StoredFile).where(StoredFile.bucket == bucket, StoredFile.path == relative))
    target.unlink(missing_ok=True)  # an older version may have saved it on disk
