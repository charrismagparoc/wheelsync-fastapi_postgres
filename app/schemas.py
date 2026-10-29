import uuid
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, computed_field, field_serializer, field_validator

from .models import BookingStatus, InspectionType, UserRole, VehicleStatus
from .security import inspection_photo_url


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ------------------------------------------------------------------ auth / profiles
class SignupIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    password: str = Field(min_length=6, max_length=128)
    role: Literal["customer", "admin"] = "customer"

    @field_validator("full_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Full name is required")
        return v


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=320)  # an email, or a staff member's phone number
    password: str
    # Which app is signing in; the server rejects accounts of another role.
    portal: Literal["admin", "customer", "staff"] | None = None


class EmailIn(BaseModel):
    email: EmailStr


class ActivationCodeIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class ResetPasswordIn(BaseModel):
    email: EmailStr
    code: str = Field(pattern=r"^\d{6}$")
    new_password: str = Field(min_length=6, max_length=128)


class ProfileOut(ORM):
    id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    age: int | None
    address: str | None
    role: UserRole
    is_blacklisted: bool
    id_type: str | None
    id_number: str | None
    id_photo_path: str | None
    second_id_type: str | None
    second_id_number: str | None
    second_id_photo_path: str | None
    proof_of_address_path: str | None
    proof_of_income_path: str | None
    extra_document_1_path: str | None
    extra_document_2_path: str | None
    verification_status: str
    verification_notes: str | None
    verified_at: datetime | None
    created_at: datetime

    # Never send where a private document is stored; the apps only need to know whether it was submitted.
    # The admin opens the file itself through the short-lived signed links of /admin/customers/{id}/documents.
    @field_serializer(
        "id_photo_path", "second_id_photo_path", "proof_of_address_path", "proof_of_income_path",
        "extra_document_1_path", "extra_document_2_path",
    )
    def _hide_storage_path(self, value: str | None) -> str | None:
        return "submitted" if value else None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: ProfileOut


class AgeIn(BaseModel):
    age: int = Field(ge=1, le=120)


class StaffCreateIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    email: EmailStr | None = None  # optional: staff can sign in with their phone instead
    phone: str = Field(min_length=7, max_length=50)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_email_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v
    password: str = Field(min_length=6, max_length=128)


class StaffUpdateIn(BaseModel):
    """Admin edits a staff account; only the fields sent are changed. A new password replaces the old one."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=7, max_length=50)
    password: str | None = Field(default=None, min_length=6, max_length=128)

    @field_validator("email", "password", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v


class StaffOut(ORM):
    id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    created_at: datetime


class BlacklistIn(BaseModel):
    is_blacklisted: bool


class BlacklistRecordIn(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    id_number: str | None = Field(default=None, max_length=100)
    area: str | None = Field(default=None, max_length=50)
    source: str | None = Field(default=None, max_length=50)
    reason: str = Field(min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("full_name", "reason")
    @classmethod
    def _required_text(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("This field is required")
        return v

    @field_validator("id_number")
    @classmethod
    def _optional_id(cls, v: str | None) -> str | None:
        return (v or "").strip() or None

    @field_validator("area", "source", "notes")
    @classmethod
    def _optional_text(cls, v: str | None) -> str | None:
        return (v or "").strip() or None


class BlacklistRecordOut(ORM):
    id: uuid.UUID
    full_name: str
    id_number: str | None
    area: str | None
    source: str | None
    reason: str
    notes: str | None
    created_at: datetime


class BlacklistMatchOut(BaseModel):
    """A customer account whose ID number (or, if not, name) matches a record on Pardz's blacklist."""

    customer_id: uuid.UUID
    matched_by: Literal["id_number", "name"]
    record: BlacklistRecordOut


class VerificationReviewIn(BaseModel):
    status: Literal["verified", "rejected"]
    reason: str | None = Field(default=None, max_length=2000)


# ------------------------------------------------------------------ vehicles
Category = Literal["sedan", "suv", "van", "pickup"]


class VehicleCreate(BaseModel):
    make: str = Field(min_length=1, max_length=100)
    model: str = Field(default="", max_length=100)
    year: int | None = Field(default=None, ge=1900, le=2100)
    plate_number: str = Field(min_length=1, max_length=30)
    category: Category = "sedan"
    description: str | None = Field(default=None, max_length=4000)
    daily_rate: float = Field(default=0, ge=0)  # no longer used: prices are agreed at the office
    price_per_km: float = Field(default=0, ge=0)
    image_url: str | None = None
    status: VehicleStatus = VehicleStatus.available


class VehicleUpdate(BaseModel):
    make: str | None = Field(default=None, min_length=1, max_length=100)
    model: str | None = Field(default=None, max_length=100)
    year: int | None = Field(default=None, ge=1900, le=2100)
    plate_number: str | None = Field(default=None, min_length=1, max_length=30)
    category: Category | None = None
    description: str | None = Field(default=None, max_length=4000)
    daily_rate: float | None = Field(default=None, ge=0)
    price_per_km: float | None = Field(default=None, ge=0)
    image_url: str | None = None
    status: VehicleStatus | None = None


class VehicleAdminOut(ORM):
    id: uuid.UUID
    make: str
    model: str
    year: int | None
    plate_number: str
    category: str
    description: str | None
    daily_rate: float
    price_per_km: float
    status: VehicleStatus
    image_url: str | None
    created_at: datetime


class VehicleCustomerOut(ORM):
    """Customers see the car's name, type, photo and details only: no price, plate number or year."""

    id: uuid.UUID
    make: str
    model: str
    category: str
    description: str | None
    status: VehicleStatus
    image_url: str | None
    created_at: datetime


class UnavailableDateOut(BaseModel):
    vehicle_id: uuid.UUID
    start_date: date
    end_date: date


# ------------------------------------------------------------------ bookings
ServiceType = Literal["self_drive", "with_driver", "pickup_dropoff"]


class BookingCreate(BaseModel):
    vehicle_id: uuid.UUID
    start_date: date
    end_date: date
    pickup_time: time | None = None
    return_time: time | None = None
    pickup_location: str | None = Field(default=None, max_length=500)
    destination_from: str | None = Field(default=None, max_length=200)
    destination_to: str | None = Field(default=None, max_length=200)
    service_type: ServiceType = "self_drive"
    distance_km: float = Field(default=0, ge=0)
    notes: str | None = Field(default=None, max_length=2000)


class AcceptIn(BaseModel):
    staff_id: uuid.UUID


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Enter a reason so the customer knows what to correct.")
        return value


class DamageMark(BaseModel):
    zone: str = Field(max_length=40)
    zone_label: str | None = Field(default=None, max_length=60)
    code: str = Field(max_length=20)
    label: str | None = Field(default=None, max_length=60)


class InspectionCreate(BaseModel):
    type: InspectionType
    checklist: dict = Field(default_factory=dict)
    odometer_reading: int | None = Field(default=None, ge=0)
    fuel_level: str | None = Field(default=None, max_length=20)
    damage_notes: str | None = Field(default=None, max_length=4000)
    damage_fee: float = Field(default=0, ge=0)
    tools: dict[str, Literal["present", "missing", "damaged"] | None] = Field(default_factory=dict)
    damage_marks: list[DamageMark] = Field(default_factory=list, max_length=200)
    renter_name: str | None = Field(default=None, max_length=200)
    renter_signed: bool = False
    staff_name: str | None = Field(default=None, max_length=200)
    staff_signed: bool = False
    # paths returned by POST /bookings/{id}/inspection-photos
    photos: list[str] = Field(default_factory=list, max_length=20)


class InspectionOut(ORM):
    id: uuid.UUID
    booking_id: uuid.UUID
    staff_id: uuid.UUID
    type: InspectionType
    checklist: dict
    damage_notes: str | None
    damage_fee: float
    odometer_reading: int | None
    fuel_level: str | None
    tools: dict
    damage_marks: list
    renter_name: str | None
    renter_signed: bool
    staff_name: str | None
    staff_signed: bool
    photos: list[str]
    verified_by: uuid.UUID | None
    verified_at: datetime | None
    created_at: datetime

    @computed_field
    @property
    def photo_urls(self) -> list[str]:
        return [inspection_photo_url(path) for path in self.photos]


class _VehicleBrief(ORM):
    make: str
    model: str
    plate_number: str


class _VehicleCustomerBrief(ORM):
    make: str
    model: str


class _CustomerAdminBrief(ORM):
    id: uuid.UUID
    full_name: str
    email: str
    phone: str | None
    age: int | None
    address: str | None
    id_type: str | None
    id_number: str | None
    second_id_type: str | None
    second_id_number: str | None
    verification_status: str
    is_blacklisted: bool


class _CustomerStaffBrief(ORM):
    full_name: str
    phone: str | None


# NOTE: the nested keys are called `profiles` / `vehicles` on purpose. They keep the exact
# JSON shape the three front-ends were already written against, so no UI code had to change.
class BookingBase(ORM):
    id: uuid.UUID
    customer_id: uuid.UUID
    vehicle_id: uuid.UUID
    assigned_staff_id: uuid.UUID | None
    start_date: date
    end_date: date
    pickup_time: time | None
    return_time: time | None
    pickup_location: str | None
    destination_from: str | None
    destination_to: str | None
    service_type: str
    distance_km: float
    status: BookingStatus
    notes: str | None
    rejection_reason: str | None
    created_at: datetime
    vehicles: _VehicleBrief = Field(validation_alias="vehicle")


class BookingAdminOut(BookingBase):
    actual_return_date: date | None
    late_return: bool
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    cleaning_staff_id: uuid.UUID | None
    cleaning_done_at: datetime | None
    profiles: _CustomerAdminBrief = Field(validation_alias="customer")
    inspections: list[InspectionOut]


class BookingStaffOut(BookingBase):
    actual_return_date: date | None
    cleaning_staff_id: uuid.UUID | None
    cleaning_done_at: datetime | None
    profiles: _CustomerStaffBrief = Field(validation_alias="customer")
    inspections: list[InspectionOut]


class BookingCustomerOut(BookingBase):
    vehicles: _VehicleCustomerBrief = Field(validation_alias="vehicle")  # no plate number for customers


class BookingAdminEdit(BaseModel):
    """The admin corrects a booking, e.g. the customer changed plans at the office."""

    vehicle_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    pickup_time: time | None = None
    return_time: time | None = None
    pickup_location: str | None = Field(default=None, max_length=500)
    destination_to: str | None = Field(default=None, max_length=200)
    service_type: ServiceType | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CustomerAdminEdit(BaseModel):
    """The admin corrects a customer's details and ID information."""

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    age: int | None = Field(default=None, ge=1, le=120)
    address: str | None = Field(default=None, max_length=500)
    id_type: str | None = Field(default=None, max_length=100)
    id_number: str | None = Field(default=None, max_length=100)
    second_id_type: str | None = Field(default=None, max_length=100)
    second_id_number: str | None = Field(default=None, max_length=100)


# ------------------------------------------------------------------ notifications
class NotificationOut(ORM):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    body: str | None
    kind: str
    booking_id: uuid.UUID | None
    data: dict
    is_read: bool
    created_at: datetime


# ------------------------------------------------------------------ locations (destination price list)
class LocationIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sort_order: int = Field(default=0, ge=0, le=100_000)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Enter the destination name")
        return v


class LocationOut(ORM):
    id: uuid.UUID
    name: str
    sort_order: int


class LocationPublicOut(ORM):
    """What the customer app gets: names only, like the rest of its pricing-free views."""

    name: str


# ------------------------------------------------------------------ walk-in transactions
PaymentMethod = Literal["cash", "gcash", "bank_transfer", "card", "other"]


class WalkInIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=200)
    customer_phone: str | None = Field(default=None, max_length=50)
    vehicle_id: uuid.UUID | None = None
    vehicle_label: str | None = Field(default=None, max_length=200)
    start_date: date
    end_date: date
    destination: str | None = Field(default=None, max_length=200)
    service_type: ServiceType = "self_drive"
    amount: float = Field(ge=0, le=10_000_000)
    payment_method: PaymentMethod = "cash"
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("customer_name")
    @classmethod
    def _strip_customer(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Enter the customer's name")
        return v


class ExtensionIn(BaseModel):
    """An added payment; new_end_date only when the customer also keeps the car longer."""

    new_end_date: date | None = None
    amount: float = Field(gt=0, le=10_000_000)
    payment_method: PaymentMethod = "cash"
    notes: str | None = Field(default=None, max_length=2000)


class ExtensionOut(ORM):
    id: uuid.UUID
    previous_end_date: date
    new_end_date: date
    amount: float
    payment_method: str
    paid_on: date
    notes: str | None
    created_at: datetime


class WalkInOut(ORM):
    id: uuid.UUID
    customer_name: str
    customer_phone: str | None
    vehicle_id: uuid.UUID | None
    vehicle_label: str
    start_date: date
    end_date: date
    destination: str | None
    service_type: str
    amount: float
    payment_method: str
    notes: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    extensions: list[ExtensionOut] = []

    @computed_field
    @property
    def total_paid(self) -> float:
        """First payment plus every extension payment."""
        return round(self.amount + sum(extension.amount for extension in self.extensions), 2)


# ------------------------------------------------------------------ audit log
class AuditLogOut(ORM):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_name: str
    actor_role: str
    action: str
    entity: str | None
    entity_id: str | None
    summary: str
    created_at: datetime
