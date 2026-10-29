from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from html import escape
from urllib.parse import urlsplit, urlunsplit
import smtplib
import socket
import ssl

import logging

import httpx
from sqlalchemy import func, select

from ..config import get_settings
from ..models import Booking, Profile

logger = logging.getLogger(__name__)


class EmailNotConfigured(Exception):
    pass


class EmailDeliveryFailed(Exception):
    pass


def _is_imported_customer(recipient: str) -> bool:
    """Past customers from the company's rental records: their addresses were never confirmed by them."""
    from ..database import SessionLocal

    with SessionLocal() as db:
        return bool(db.scalar(select(Profile.is_imported).where(func.lower(Profile.email) == recipient.strip().lower())))


def _deliver_email(recipient: str, subject: str, text: str, html: str) -> None:
    if _is_imported_customer(recipient):
        logger.info("Skipped e-mail %r to %s: imported past customer", subject, recipient)
        return
    settings = get_settings()
    smtp_configured = bool(settings.smtp_host and settings.smtp_from)
    brevo_configured = bool(settings.brevo_api_key and settings.email_from)
    resend_configured = bool(settings.resend_api_key and settings.email_from)
    if not smtp_configured and not brevo_configured and not resend_configured:
        raise EmailNotConfigured("Email is not configured. Set SMTP_HOST and SMTP_FROM (or BREVO_API_KEY or RESEND_API_KEY, plus EMAIL_FROM) on the API server.")

    if smtp_configured:
        message = EmailMessage()
        sender_address = parseaddr(settings.smtp_from)[1]
        message["From"] = settings.smtp_from
        message["To"] = recipient
        message["Reply-To"] = settings.smtp_from
        message["Subject"] = subject
        # Headers mail providers expect from a real mail client; missing ones raise spam scores.
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=sender_address.split("@")[-1] or None)
        message.set_content(text)
        message.add_alternative(_html_document(subject, html), subtype="html")
        transport = smtplib.SMTP_SSL if settings.smtp_ssl else smtplib.SMTP
        try:
            with transport(settings.smtp_host, settings.smtp_port, timeout=15) as client:
                if settings.smtp_starttls and not settings.smtp_ssl:
                    client.starttls(context=ssl.create_default_context())
                if settings.smtp_username:
                    # One PLAIN attempt: smtplib's login() retries with LOGIN after a rejection, and Gmail then
                    # hangs up, hiding the real "535 Username and Password not accepted" behind "connection closed".
                    client.ehlo_or_helo_if_needed()
                    client.user, client.password = settings.smtp_username, settings.smtp_password or ""
                    client.auth("PLAIN", client.auth_plain)
                client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailDeliveryFailed(
                f"The mail server rejected SMTP_USERNAME / SMTP_PASSWORD ({exc.smtp_code}). For Gmail, SMTP_PASSWORD must be "
                "an app password created on the same Google account as SMTP_USERNAME."
            ) from exc
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryFailed(f"Could not send email through SMTP: {exc}") from exc
        return

    if brevo_configured:
        sender_name, sender_address = parseaddr(settings.email_from)
        try:
            response = httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": settings.brevo_api_key, "Content-Type": "application/json", "Accept": "application/json"},
                json={
                    "sender": {"name": sender_name or sender_address, "email": sender_address},
                    "to": [{"email": recipient}],
                    "subject": subject,
                    "textContent": text,
                    "htmlContent": _html_document(subject, html),
                },
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise EmailDeliveryFailed(f"Could not reach the email provider: {exc}") from exc
        if response.status_code >= 400:
            raise EmailDeliveryFailed(f"Email provider rejected the message: {response.text}")
        return

    try:
        response = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.resend_api_key}", "Content-Type": "application/json"},
            json={"from": settings.email_from, "to": [recipient], "subject": subject, "text": text, "html": html},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise EmailDeliveryFailed(f"Could not reach the email provider: {exc}") from exc
    if response.status_code >= 400:
        raise EmailDeliveryFailed(f"Email provider rejected the message: {response.text}")


def _html_document(subject: str, body: str) -> str:
    """A complete, simple HTML email (a bare fragment looks machine-made to spam filters)."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width'><title>{escape(subject)}</title></head>"
        "<body style='margin:0;padding:24px;background:#f4f6fb;font-family:Arial,Helvetica,sans-serif;color:#1d2433'>"
        "<div style='max-width:520px;margin:0 auto;background:#ffffff;border-radius:10px;padding:28px;line-height:1.55'>"
        f"{body}"
        "<p style='margin-top:28px;font-size:12px;color:#667085'>Pardz Car Rental · Cagayan de Oro City</p>"
        "</div></body></html>"
    )


def send_notification_email(recipient: str, subject: str, text: str) -> str:
    paragraphs = "".join(f"<p>{escape(line)}</p>" for line in text.split("\n\n") if line)
    _deliver_email(recipient, subject, text, paragraphs)
    return recipient


def _reachable_base(base: str) -> str:
    """A link opened on a phone can't use "localhost" (that is the phone itself). When the API runs locally,
    use this computer's current Wi-Fi/LAN address instead, so the link opens on devices on the same network."""
    parsed = urlsplit(base)
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        return base
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))  # no packet is sent; this just picks the outgoing network interface
            lan_ip = probe.getsockname()[0]
    except OSError:
        return base
    return urlunsplit((parsed.scheme, f"{lan_ip}:{parsed.port}" if parsed.port else lan_ip, parsed.path, "", ""))


def send_verification_email(recipient: str, token: str, code: str = '') -> str:
    """Send the account activation link through the configured SMTP or Resend provider."""
    settings = get_settings()
    base = settings.activation_base_url or _reachable_base(settings.public_base_url)
    activation_url = f"{base.rstrip('/')}/auth/verify-email?token={token}"
    text = (
        "Hello,\n\n"
        "Thank you for signing up with Pardz Car Rental.\n\n"
        "Open this link to activate your account:\n"
        f"{activation_url}\n\n"
        "The link expires in 24 hours. If you did not create this account, you can ignore this email.\n\n"
        "Pardz Car Rental"
    )
    safe_url = escape(activation_url, quote=True)
    html = (
        "<p>Hello,</p>"
        "<p>Thank you for signing up with Pardz Car Rental. Click the button below to activate your account.</p>"
        "<p style='margin:26px 0'>"
        f"<a href='{safe_url}' style='display:inline-block;background:#1d5aa0;color:#ffffff;text-decoration:none;"
        "font-weight:bold;padding:14px 26px;border-radius:8px'>Activate my account</a></p>"
        "<p style='font-size:13px;color:#475467'>The link expires in 24 hours. "
        "If you did not create this account, you can ignore this email.</p>"
    )
    _deliver_email(recipient, "Activate your Pardz Car Rental account", text, html)
    return recipient


def send_password_reset_email(recipient: str, code: str) -> str:
    """Send the 6-digit password reset code through the configured SMTP or Resend provider."""
    text = (
        "We received a request to reset your WheelSync password.\n\n"
        f"Your password reset code is: {code}\n\n"
        "Enter this code on the sign-in page to choose a new password. It expires in 15 minutes.\n\n"
        "If you did not ask to reset your password, you can ignore this email."
    )
    html = (
        "<p>We received a request to reset your WheelSync password.</p>"
        "<p>Your password reset code is:</p>"
        f'<p style="font-size:28px;font-weight:bold;letter-spacing:6px">{escape(code)}</p>'
        "<p>Enter this code on the sign-in page to choose a new password. It expires in 15 minutes.</p>"
        "<p>If you did not ask to reset your password, you can ignore this email.</p>"
    )
    _deliver_email(recipient, "Your WheelSync password reset code", text, html)
    return recipient


def send_booking_approved_email(booking: Booking) -> str:
    """Send the booking approval email through configured SMTP or Resend."""
    settings = get_settings()
    customer = booking.customer
    if not customer.email:
        raise EmailDeliveryFailed("Customer email is missing")

    name = customer.full_name or "Customer"
    vehicle = f"{booking.vehicle.make or 'Vehicle'} {booking.vehicle.model or ''}".strip()
    pickup = f" at {booking.pickup_time.strftime('%H:%M')}" if booking.pickup_time else ""
    ret = f" at {booking.return_time.strftime('%H:%M')}" if booking.return_time else ""
    schedule = f"{booking.start_date}{pickup} to {booking.end_date}{ret}"
    address = settings.rental_address

    text = (
        f"Hello {name},\n\nYour booking requirements have been approved and verified.\n\n"
        f"Vehicle: {vehicle}\nSchedule: {schedule}\n\n"
        f"Please visit Pardz Car Rental at {address}. Submit one physical ID to our staff for vehicle release; "
        "it will be returned to you when the vehicle is returned.\n\nPardz Car Rental"
    )
    html = (
        f"<p>Hello {escape(name)},</p><p>Your booking requirements have been approved and verified.</p>"
        f"<p><strong>Vehicle:</strong> {escape(vehicle)}<br><strong>Schedule:</strong> {escape(schedule)}</p>"
        f"<p>Please visit Pardz Car Rental at <strong>{escape(address)}</strong>. Submit one physical ID to our staff "
        "for vehicle release; it will be returned to you when the vehicle is returned.</p><p>Pardz Car Rental</p>"
    )
    _deliver_email(customer.email, "Your Pardz Car Rental booking is approved", text, html)
    return customer.email