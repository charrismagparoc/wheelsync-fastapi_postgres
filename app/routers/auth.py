from datetime import datetime, timedelta, timezone
import hashlib
import html
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..deps import get_current_user
from ..models import Profile, UserRole
from ..schemas import EmailIn, LoginIn, ProfileOut, ResetPasswordIn, SignupIn, TokenOut
from ..services.email import EmailDeliveryFailed, EmailNotConfigured, send_password_reset_email, send_verification_email
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
VERIFICATION_TOKEN_LIFETIME = timedelta(hours=24)
RESET_CODE_LIFETIME = timedelta(minutes=15)
RESET_CODE_RESEND_COOLDOWN = timedelta(seconds=60)
RESET_CODE_MAX_ATTEMPTS = 5

PORTAL_ROLE = {"admin": UserRole.admin, "customer": UserRole.customer, "staff": UserRole.staff}
PORTAL_MESSAGE = {
    "admin": "This account is not an admin account.",
    "customer": "This login is for customer accounts only.",
    "staff": "This app is for staff accounts only. Ask your admin for a staff login.",
}


def _new_verification_token() -> str:
    return secrets.token_urlsafe(32)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_activation(user: Profile) -> str:
    """New activation link token, valid 24 hours; it replaces any earlier link."""
    token = _new_verification_token()
    user.email_verification_token_hash = _token_hash(token)
    user.email_verification_expires_at = datetime.now(timezone.utc) + VERIFICATION_TOKEN_LIFETIME
    return token


def _send_activation_email(email: str, token: str) -> None:
    try:
        send_verification_email(email, token)
    except (EmailNotConfigured, EmailDeliveryFailed) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The account was saved, but its activation email could not be sent. Configure email and request another activation email.",
        ) from exc


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(data: SignupIn, db: Session = Depends(get_db)):
    """Customers can always self-register. An admin can self-register only while no admin exists yet.
    Staff accounts are created by an admin (POST /admin/staff)."""
    email = data.email.strip().lower()
    role = UserRole(data.role)

    if role == UserRole.admin:
        admin_count = db.scalar(select(func.count()).select_from(Profile).where(Profile.role == UserRole.admin))
        if not get_settings().allow_admin_signup or admin_count:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin sign-up is closed. Ask an existing admin to create your account.")

    if db.scalar(select(Profile.id).where(Profile.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "User already registered")

    user = Profile(
        full_name=data.full_name,
        email=email,
        phone=(data.phone or "").strip() or None,
        password_hash=hash_password(data.password),
        role=role,
        email_verified=False,
    )
    token = _issue_activation(user)
    db.add(user)
    db.commit()
    _send_activation_email(email, token)
    return {"detail": "Account created. Check your email and activate your account before signing in."}


def _activation_page(title: str, message: str, ok: bool) -> str:
    color = "#16a34a" if ok else "#e11d48"
    mark = "&#10003;" if ok else "!"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)} · Pardz Car Rental</title></head>"
        "<body style='margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
        "background:#eef2fb;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;color:#1d2433;padding:16px'>"
        "<main style='max-width:26rem;width:100%;background:#fff;border-radius:16px;padding:32px 28px;text-align:center;"
        "box-shadow:0 10px 30px rgba(18,60,115,.12)'>"
        f"<div style='width:64px;height:64px;margin:0 auto 18px;border-radius:50%;background:{color};color:#fff;"
        f"font-size:34px;line-height:64px;font-weight:700'>{mark}</div>"
        f"<h1 style='margin:0 0 10px;font-size:1.5rem'>{html.escape(title)}</h1>"
        f"<p style='margin:0;line-height:1.55;color:#475467'>{html.escape(message)}</p>"
        "<p style='margin:24px 0 0;font-size:.85rem;color:#98a2b3'>Pardz Car Rental</p>"
        "</main></body></html>"
    )


def _activate(db: Session, token: str) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    user = db.scalar(
        select(Profile).where(
            Profile.email_verification_token_hash == _token_hash(token),
            Profile.email_verification_expires_at > now,
            Profile.email_verified.is_(False),
        )
    )
    if user is None:
        return HTMLResponse(
            _activation_page(
                "Link expired or already used",
                "This activation link no longer works. If you already activated your account, just sign in. "
                "Otherwise, open the Pardz Car Rental sign-in page, enter your email and choose Resend activation email.",
                ok=False,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    db.commit()
    return HTMLResponse(_activation_page(
        "Your account is active",
        f"Thank you, {user.full_name}. Your Pardz Car Rental account is now activated. You can close this page and sign in.",
        ok=True,
    ))


@router.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
def verify_email_link(token: str, db: Session = Depends(get_db)):
    """The link in the activation email: one click activates the account."""
    return _activate(db, token)


@router.post("/verify-email", response_class=HTMLResponse)
def verify_email(token: str = Form(...), db: Session = Depends(get_db)):
    return _activate(db, token)


@router.post("/resend-verification")
def resend_verification(data: EmailIn, db: Session = Depends(get_db)):
    email = data.email.strip().lower()
    user = db.scalar(select(Profile).where(Profile.email == email))
    if user is None or user.email_verified:
        return {"detail": "If the account needs activation, an activation email will be sent."}

    token = _issue_activation(user)
    db.commit()
    _send_activation_email(email, token)
    return {"detail": "If the account needs activation, an activation email will be sent."}


def _reset_code_hash(user: Profile, code: str) -> str:
    # Salted with the account id so equal codes on different accounts hash differently.
    return hashlib.sha256(f"{user.id}:{code}".encode("utf-8")).hexdigest()


@router.post("/forgot-password")
def forgot_password(data: EmailIn, db: Session = Depends(get_db)):
    """Email a 6-digit code that lets the account owner choose a new password (POST /auth/reset-password)."""
    generic = {"detail": "If an account exists for this email, a password reset code has been sent."}
    email = data.email.strip().lower()
    user = db.scalar(select(Profile).where(Profile.email == email))
    if user is None:
        return generic

    now = datetime.now(timezone.utc)
    issued_at = user.password_reset_expires_at - RESET_CODE_LIFETIME if user.password_reset_expires_at else None
    if issued_at and now - issued_at < RESET_CODE_RESEND_COOLDOWN:
        return generic

    code = f"{secrets.randbelow(1_000_000):06d}"
    user.password_reset_code_hash = _reset_code_hash(user, code)
    user.password_reset_expires_at = now + RESET_CODE_LIFETIME
    user.password_reset_attempts = 0
    db.commit()
    try:
        send_password_reset_email(email, code)
    except (EmailNotConfigured, EmailDeliveryFailed) as exc:
        user.password_reset_code_hash = None
        user.password_reset_expires_at = None
        db.commit()
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The password reset email could not be sent. Please try again later or contact Pardz Car Rental.",
        ) from exc
    return generic


@router.post("/reset-password")
def reset_password(data: ResetPasswordIn, db: Session = Depends(get_db)):
    invalid = HTTPException(status.HTTP_400_BAD_REQUEST, "The reset code is invalid or has expired. Request a new code.")
    user = db.scalar(select(Profile).where(Profile.email == data.email.strip().lower()))
    if (
        user is None
        or user.password_reset_code_hash is None
        or user.password_reset_expires_at is None
        or user.password_reset_expires_at <= datetime.now(timezone.utc)
        or user.password_reset_attempts >= RESET_CODE_MAX_ATTEMPTS
    ):
        raise invalid

    if not secrets.compare_digest(user.password_reset_code_hash, _reset_code_hash(user, data.code)):
        user.password_reset_attempts += 1
        db.commit()
        raise invalid

    user.password_hash = hash_password(data.new_password)
    user.password_reset_code_hash = None
    user.password_reset_expires_at = None
    user.password_reset_attempts = 0
    # Receiving the code proves the person controls this email address.
    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    db.commit()
    return {"detail": "Your password has been changed. You can now sign in with your new password."}


@router.post("/login", response_model=TokenOut)
def login(data: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(Profile).where(Profile.email == data.email.strip().lower()))
    if user is None or not verify_password(data.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid login credentials")
    if data.portal and user.role != PORTAL_ROLE[data.portal]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, PORTAL_MESSAGE[data.portal])
    if not user.email_verified:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Please activate your email address before signing in.")
    if user.role == UserRole.customer and user.is_blacklisted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account is currently restricted. Please contact Pardz Car Rental.")
    return TokenOut(access_token=create_access_token(user.id), user=user)


@router.get("/me", response_model=ProfileOut)
def me(user: Profile = Depends(get_current_user)):
    return user
