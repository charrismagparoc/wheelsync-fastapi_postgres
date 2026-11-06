import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import Response

from ..security import decode_document_token
from ..services import storage

router = APIRouter(prefix="/files", tags=["files"])


def _send(bucket: str, path: str, missing: str, cache: str) -> Response:
    found = storage.read_file(bucket, path)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, missing)
    data, content_type = found
    return Response(data, media_type=content_type, headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"})


@router.get("/vehicle-images/{path:path}")
def vehicle_image(path: str):
    """Public: vehicle photos are shown to every customer."""
    return _send(storage.VEHICLE_BUCKET, path, "Image not found", "public, max-age=86400")


@router.get("/inspection-photos/{path:path}")
def inspection_photo(path: str, token: str = Query(...)):
    """Private: the signed link comes with the inspection in the admin API responses."""
    try:
        allowed_path = decode_document_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    if allowed_path != f"{storage.INSPECTION_BUCKET}/{path}":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    return _send(storage.INSPECTION_BUCKET, path, "Photo not found", "private, max-age=3600")


@router.get("/customer-documents/{path:path}")
def customer_document(path: str, token: str = Query(...)):
    """Private: needs a short-lived signed token minted by GET /admin/customers/{id}/documents."""
    try:
        allowed_path = decode_document_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    if allowed_path != path:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This link is invalid or has expired.")
    return _send(storage.DOCUMENT_BUCKET, path, "Document not found", "private, no-store")
