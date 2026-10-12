import enum
import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Index, Integer,
    Numeric, String, Text, Time, func, text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _enum(py_enum: type[enum.Enum], name: str) -> Enum:
    return Enum(py_enum, name=name, values_callable=lambda e: [m.value for m in e])


class UserRole(str, enum.Enum):
    admin = "admin"
    staff = "staff"
    customer = "customer"


class VehicleStatus(str, enum.Enum):
    available = "available"
    rented = "rented"
    maintenance = "maintenance"
    cleaning = "cleaning"  # returned; waiting for post-rental verification and cleaning


class BookingStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"
    rejected = "rejected"
    ongoing = "ongoing"
    completed = "completed"
    cancelled = "cancelled"


class InspectionType(str, enum.Enum):
    pre_rental = "pre_rental"
    post_rental = "post_rental"


class Profile(Base):
    """A user account (admin / staff / customer). Replaces auth.users + public.profiles."""

    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint("age IS NULL OR age BETWEEN 1 AND 120", name="profiles_age_check"),
        CheckConstraint(
            "verification_status IN ('unsubmitted','pending','verified','rejected')",
            name="profiles_verification_status_check",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    full_name: Mapped[str] = mapped_column(Text)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lower-case
    password_hash: Mapped[str] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    email_verification_token_hash: Mapped[str | None] = mapped_column(String(64))
    email_verification_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_reset_code_hash: Mapped[str | None] = mapped_column(String(64))
    password_reset_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_reset_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    age: Mapped[int | None] = mapped_column(Integer)
    address: Mapped[str | None] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), default=UserRole.customer)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    id_type: Mapped[str | None] = mapped_column(Text)
    id_number: Mapped[str | None] = mapped_column(Text)
    id_photo_path: Mapped[str | None] = mapped_column(Text)
    second_id_type: Mapped[str | None] = mapped_column(Text)
    second_id_number: Mapped[str | None] = mapped_column(Text)
    second_id_photo_path: Mapped[str | None] = mapped_column(Text)
    proof_of_address_path: Mapped[str | None] = mapped_column(Text)
    proof_of_income_path: Mapped[str | None] = mapped_column(Text)
    extra_document_1_path: Mapped[str | None] = mapped_column(Text)
    extra_document_2_path: Mapped[str | None] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(Text, default="unsubmitted", server_default="unsubmitted")
    verification_notes: Mapped[str | None] = mapped_column(Text)
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Vehicle(Base):
    __tablename__ = "vehicles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    make: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    year: Mapped[int] = mapped_column(Integer)
    plate_number: Mapped[str] = mapped_column(Text, unique=True)
    category: Mapped[str] = mapped_column(Text, default="sedan", server_default="sedan")
    description: Mapped[str | None] = mapped_column(Text)
    daily_rate: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    price_per_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0")
    status: Mapped[VehicleStatus] = mapped_column(
        _enum(VehicleStatus, "vehicle_status"), default=VehicleStatus.available
    )
    image_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("distance_km >= 0", name="bookings_distance_km_check"),
        CheckConstraint("start_date <= end_date", name="bookings_dates_check"),
        # Database-level guarantee: two accepted/ongoing bookings can never overlap on one vehicle.
        ExcludeConstraint(
            ("vehicle_id", "="),
            (text("daterange(start_date, end_date, '[]')"), "&&"),
            where=text("status IN ('accepted','ongoing')"),
            using="gist",
            name="bookings_no_active_overlap",
        ),
        Index("ix_bookings_customer", "customer_id"),
        Index("ix_bookings_staff", "assigned_staff_id"),
        Index("ix_bookings_status_start", "status", "start_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    vehicle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vehicles.id"))
    assigned_staff_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    pickup_time: Mapped[time | None] = mapped_column(Time)
    return_time: Mapped[time | None] = mapped_column(Time)
    pickup_location: Mapped[str | None] = mapped_column(Text)
    # Where the customer is travelling, e.g. Cagayan de Oro -> Bukidnon.
    destination_from: Mapped[str | None] = mapped_column(Text)
    destination_to: Mapped[str | None] = mapped_column(Text)
    service_type: Mapped[str] = mapped_column(Text, default="self_drive", server_default="self_drive")
    distance_km: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0")
    actual_return_date: Mapped[date | None] = mapped_column(Date)
    late_return: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    status: Mapped[BookingStatus] = mapped_column(
        _enum(BookingStatus, "booking_status"), default=BookingStatus.pending
    )
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # After the return: who cleans the vehicle and when they finished.
    cleaning_staff_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    cleaning_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the 2-hour / 30-minute return reminders went out (each is sent once).
    return_reminder_2h_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    return_reminder_30m_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped[Profile] = relationship(foreign_keys=[customer_id])
    vehicle: Mapped[Vehicle] = relationship()
    assigned_staff: Mapped[Profile | None] = relationship(foreign_keys=[assigned_staff_id])
    inspections: Mapped[list["Inspection"]] = relationship(
        back_populates="booking", order_by="Inspection.created_at", cascade="all, delete-orphan"
    )


class Inspection(Base):
    __tablename__ = "inspections"
    __table_args__ = (CheckConstraint("damage_fee >= 0", name="inspections_damage_fee_check"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"))
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id"))
    type: Mapped[InspectionType] = mapped_column(_enum(InspectionType, "inspection_type"))
    checklist: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    damage_notes: Mapped[str | None] = mapped_column(Text)
    damage_fee: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0, server_default="0")
    odometer_reading: Mapped[int | None] = mapped_column(Integer)
    fuel_level: Mapped[str | None] = mapped_column(Text)
    photos: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    # From the staff app's paper-style form: tool status per item, marks on the car diagram, sign-off.
    tools: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    damage_marks: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    renter_name: Mapped[str | None] = mapped_column(Text)
    renter_signed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    staff_name: Mapped[str | None] = mapped_column(Text)
    staff_signed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id"))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    booking: Mapped[Booking] = relationship(back_populates="inspections")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_user", "user_id", "is_read"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    # What the notification is about (booking_accepted, return_reminder_2h, ...), so apps can group and style it.
    kind: Mapped[str] = mapped_column(Text, default="general", server_default="general")
    booking_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bookings.id", ondelete="SET NULL"))
    data: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
