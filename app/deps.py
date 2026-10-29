import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import Profile, UserRole
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Profile:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        user_id = decode_access_token(creds.credentials)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    user = db.get(Profile, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    if user.role == UserRole.customer and user.is_blacklisted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is currently restricted. Please contact Pardz Car Rental.")
    return user


def get_optional_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Profile | None:
    if creds is None:
        return None
    return get_current_user(creds, db)


def require_roles(*roles: UserRole):
    def checker(user: Profile = Depends(get_current_user)) -> Profile:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to do that.")
        return user

    return checker


require_admin = require_roles(UserRole.admin)
require_customer = require_roles(UserRole.customer)
require_staff = require_roles(UserRole.staff)
require_admin_or_staff = require_roles(UserRole.admin, UserRole.staff)


def parse_uuid(value: str, what: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")
