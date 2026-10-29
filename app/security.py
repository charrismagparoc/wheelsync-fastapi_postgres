import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import get_settings


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))
    except ValueError:
        return False


def _encode(payload: dict, minutes: int) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    return jwt.encode({**payload, "iat": now, "exp": now + timedelta(minutes=minutes)}, s.jwt_secret, algorithm=s.jwt_algorithm)


def _decode(token: str, expected_type: str) -> dict:
    s = get_settings()
    payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def create_access_token(user_id: uuid.UUID) -> str:
    return _encode({"sub": str(user_id), "type": "access"}, get_settings().access_token_expire_minutes)


def decode_access_token(token: str) -> uuid.UUID:
    return uuid.UUID(_decode(token, "access")["sub"])


def create_document_token(path: str, minutes: int = 5) -> str:
    """Short-lived signed token that lets a browser <iframe> open one private document."""
    return _encode({"path": path, "type": "document"}, minutes)


def inspection_photo_url(path: str) -> str:
    """Signed link to one inspection photo; long enough for the admin to keep the Inspections page open."""
    token = create_document_token(f"inspection-photos/{path}", minutes=120)
    return f"{get_settings().public_base_url.rstrip('/')}/files/inspection-photos/{path}?token={token}"


def decode_document_token(token: str) -> str:
    return _decode(token, "document")["path"]
