import os
import shutil
import tempfile

# Must be set BEFORE the app is imported.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://wheelsync:wheelsync@localhost:5432/wheelsync_test")
os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="wheelsync-test-uploads-")
os.environ["JWT_SECRET"] = "test-secret"
os.environ["RESEND_API_KEY"] = ""
os.environ["EMAIL_FROM"] = ""
os.environ["SMTP_HOST"] = ""
os.environ["SMTP_FROM"] = ""
os.environ["SMTP_USERNAME"] = ""
os.environ["SMTP_PASSWORD"] = ""
os.environ["PUBLIC_BASE_URL"] = "http://testserver"
os.environ["REMINDER_INTERVAL_SECONDS"] = "0"  # tests call the reminder check directly
os.environ["SEED_RENTAL_HISTORY"] = "0"  # 542 past rentals per test would be slow; one test loads them itself

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine, init_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    from app.routers import auth

    app.state.verification_tokens = {}
    app.state.activation_codes = {}
    monkeypatch.setattr(
        auth,
        "send_verification_email",
        lambda email, token, code='': (app.state.verification_tokens.__setitem__(email, token), app.state.activation_codes.__setitem__(email, code)),
    )
    app.state.reset_codes = {}
    monkeypatch.setattr(
        auth,
        "send_password_reset_email",
        lambda email, code: app.state.reset_codes.__setitem__(email, code),
    )
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(os.environ["UPLOAD_DIR"], ignore_errors=True)
