import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse

from ..security import decode_document_token
from ..services import storage

router = APIRouter(prefix="/files", tags=["files"])


@router.get("/vehicle-images/{path:path}")
def vehicle_image(path: str):
    """Public: vehicle photos are shown to every customer."""
    file = storage.safe_path(storage.VEHICLE_BUCKET, path)
    if not file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Image not found")
    return FileResponse(file, headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})


@router.get("/inspection-photos/{path:path}")
def inspection_photo(path: str, token: str = Query(...)):
    """Private: the signed link comes with the inspection in the admin API responses."""
    try:
        allowed_path = decode_document_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    if allowed_path != f"{storage.INSPECTION_BUCKET}/{path}":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    file = storage.safe_path(storage.INSPECTION_BUCKET, path)
    if not file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Photo not found")
    return FileResponse(file, headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.get("/customer-documents/{path:path}")
def customer_document(path: str, token: str = Query(...)):
    """Private: needs a short-lived signed token minted by GET /admin/customers/{id}/documents."""
    try:
        allowed_path = decode_document_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    if allowed_path != path:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    file = storage.safe_path(storage.DOCUMENT_BUCKET, path)
    if not file.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found")
    return FileResponse(file, headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
