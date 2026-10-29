"""Send one test email with the SMTP settings in .env and print exactly what went wrong if it fails.

    python -m scripts.test_email someone@gmail.com
"""
import sys

from app.config import get_settings
from app.services import email
from app.services.email import EmailDeliveryFailed, EmailNotConfigured, send_notification_email


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m scripts.test_email recipient@example.com")
    email._is_imported_customer = lambda recipient: False  # test the mail server only; no database needed
    s = get_settings()
    print(f"SMTP host: {s.smtp_host or '(empty)'}:{s.smtp_port}  user: {s.smtp_username or '(empty)'}  "
          f"password: {'set, ' + str(len(s.smtp_password or '')) + ' characters' if s.smtp_password else '(empty)'}  "
          f"from: {s.smtp_from or '(empty)'}")
    try:
        send_notification_email(sys.argv[1], "WheelSync test email", "This is a test email from WheelSync. Email sending works.")
    except EmailNotConfigured as exc:
        sys.exit(f"Not configured: {exc}")
    except EmailDeliveryFailed as exc:
        sys.exit(f"Sending failed: {exc}")
    print(f"Sent. Check the inbox (and spam folder) of {sys.argv[1]}.")


if __name__ == "__main__":
    main()
