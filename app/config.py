from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_JWT_SECRET = "change-me-to-a-long-random-string"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://wheelsync:wheelsync@localhost:5432/wheelsync"

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720
    environment: str = "development"

    cors_origins: str = "http://localhost:5173,http://localhost:5174"
    public_base_url: str = "http://localhost:8000"
    # Address used in activation-email links. Set it to an address phones can open (e.g. the PC's Wi-Fi IP);
    # empty means public_base_url.
    activation_base_url: str | None = None

    upload_dir: str = "./uploads"
    max_upload_mb: int = 10

    timezone: str = "Asia/Manila"
    # How often the server checks for 2-hour / 30-minute return reminders. 0 turns the check off.
    reminder_interval_seconds: int = 60
    # Load Pardz's past rental history (app/data/rental_history.csv) into a new database. Loaded once only.
    seed_rental_history: bool = True
    rental_address: str = "Zone 7, Kauswagan Highway, Cagayan de Oro City"

    resend_api_key: str | None = None
    # Brevo (brevo.com) sends over HTTPS, so it works where SMTP ports are blocked (e.g. Render's free plan).
    brevo_api_key: str | None = None
    email_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    allow_admin_signup: bool = True

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
