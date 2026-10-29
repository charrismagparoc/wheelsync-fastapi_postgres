from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create extensions + tables. Safe to run on every start."""
    from . import models  # noqa: F401  (register tables)

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS btree_gist")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS address text")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS email_verified boolean NOT NULL DEFAULT true")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS email_verification_token_hash varchar(64)")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS email_verification_expires_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS password_reset_code_hash varchar(64)")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS password_reset_expires_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS password_reset_attempts integer NOT NULL DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS extra_document_1_path text")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS extra_document_2_path text")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS rejection_reason text")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS destination_from text")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS destination_to text")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS tools jsonb NOT NULL DEFAULT '{}'::jsonb")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS damage_marks jsonb NOT NULL DEFAULT '[]'::jsonb")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS renter_name text")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS renter_signed boolean NOT NULL DEFAULT false")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS staff_name text")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS staff_signed boolean NOT NULL DEFAULT false")
        conn.exec_driver_sql("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS description text")
        conn.exec_driver_sql("ALTER TYPE vehicle_status ADD VALUE IF NOT EXISTS 'cleaning'")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cleaning_staff_id uuid REFERENCES profiles(id)")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS cleaning_done_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS verified_by uuid REFERENCES profiles(id)")
        conn.exec_driver_sql("ALTER TABLE inspections ADD COLUMN IF NOT EXISTS verified_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS return_reminder_2h_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS return_reminder_30m_at timestamp with time zone")
        conn.exec_driver_sql("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'general'")
        conn.exec_driver_sql("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS booking_id uuid REFERENCES bookings(id) ON DELETE SET NULL")
        conn.exec_driver_sql("ALTER TABLE notifications ADD COLUMN IF NOT EXISTS data jsonb NOT NULL DEFAULT '{}'::jsonb")
        conn.exec_driver_sql("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS destination_fee numeric(10,2) NOT NULL DEFAULT 0")
        conn.exec_driver_sql("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS auto_rented boolean NOT NULL DEFAULT false")
        conn.exec_driver_sql("ALTER TABLE vehicles ALTER COLUMN year DROP NOT NULL")
        # Staff may be added without an email; they sign in with their phone number.
        conn.exec_driver_sql("ALTER TABLE profiles ALTER COLUMN email DROP NOT NULL")
        # Added payments may keep the same return date (earlier the date had to move).
        conn.exec_driver_sql("ALTER TABLE transaction_extensions DROP CONSTRAINT IF EXISTS transaction_extension_dates_check")
        conn.exec_driver_sql(
            "ALTER TABLE transaction_extensions ADD CONSTRAINT transaction_extension_dates_check CHECK (previous_end_date <= new_end_date)"
        )
        # Destinations no longer have a "shown" switch: every destination the admin adds is offered to customers.
        conn.exec_driver_sql("UPDATE locations SET is_active = true WHERE is_active = false")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS is_imported boolean NOT NULL DEFAULT false")
        conn.exec_driver_sql("ALTER TABLE profiles ADD COLUMN IF NOT EXISTS email_verification_code_hash varchar(64)")
        # Blacklist records are identified by ID number: one record per ID, ignoring spaces, dashes and letter case.
        conn.exec_driver_sql("ALTER TABLE blacklist_records ADD COLUMN IF NOT EXISTS id_number text")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS blacklist_records_id_number_key ON blacklist_records "
            "(upper(regexp_replace(id_number, '[^A-Za-z0-9]', '', 'g'))) WHERE id_number IS NOT NULL"
        )
        # "SAN FRANCE" on the price sheet is San Francisco, Agusan del Sur (between Bayugan and Trento).
        conn.exec_driver_sql(
            "UPDATE locations SET name = 'San Francisco' WHERE name = 'San France' "
            "AND NOT EXISTS (SELECT 1 FROM locations WHERE name = 'San Francisco')"
        )
        for category in ("sedan", "suv", "van", "pickup"):
            conn.exec_driver_sql(f"ALTER TABLE locations ADD COLUMN IF NOT EXISTS fee_{category} numeric(10,2) NOT NULL DEFAULT 0")
        # An earlier version had one fee for every vehicle type: copy it to each type, then drop it.
        conn.exec_driver_sql(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'locations' AND column_name = 'fee') THEN "
            "UPDATE locations SET fee_sedan = fee, fee_suv = fee, fee_van = fee, fee_pickup = fee; "
            "ALTER TABLE locations DROP COLUMN fee; END IF; END $$"
        )
        # Older notifications were saved before they had a kind: classify them by their title.
        conn.exec_driver_sql(
            "UPDATE notifications SET kind = CASE title "
            "WHEN 'Booking approved' THEN 'booking_accepted' "
            "WHEN 'Booking needs changes' THEN 'booking_rejected' "
            "WHEN 'Booking request received' THEN 'booking_request' "
            "WHEN 'Booking status updated' THEN 'booking_status' "
            "WHEN 'Booking assigned' THEN 'task_assigned' "
            "WHEN 'Booking reassigned' THEN 'task_reassigned' "
            "WHEN 'Customer updated rejected booking' THEN 'booking_request' "
            "WHEN 'Requirements need correction' THEN 'requirements' "
            "WHEN 'Customer requirements updated' THEN 'requirements' "
            "ELSE kind END WHERE kind = 'general'"
        )
        conn.exec_driver_sql(
            "UPDATE notifications SET kind = CASE "
            "WHEN title LIKE '%%inspection done' OR title = 'Inspection verified' THEN 'inspection' "
            "WHEN title LIKE 'Cleaning%%' THEN 'cleaning' "
            "WHEN title = 'Vehicle returned' THEN 'vehicle_returned' "
            "ELSE kind END WHERE kind = 'general'"
        )

    from .services.company_data import seed_company_data

    with SessionLocal() as db:
        seed_company_data(db)
