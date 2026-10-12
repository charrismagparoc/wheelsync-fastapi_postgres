import io
from datetime import date, datetime, timedelta, timezone

import pytest

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def d(offset: int) -> str:
    return (date.today() + timedelta(days=offset)).isoformat()


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup(client, email, role="customer", name="Test User", password="secret123", phone="0917"):
    response = client.post("/auth/signup", json={"full_name": name, "email": email, "password": password, "role": role, "phone": phone})
    if response.status_code == 201:
        token = client.app.state.verification_tokens[email.strip().lower()]
        activation = client.post("/auth/verify-email", data={"token": token})
        assert activation.status_code == 200, activation.text
    return response


def login(client, email, password="secret123", portal=None):
    body = {"email": email, "password": password}
    if portal:
        body["portal"] = portal
    return client.post("/auth/login", json=body)


def token_for(client, email, portal=None):
    r = login(client, email, portal=portal)
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture()
def world(client):
    """An admin, a staff member, a customer and one vehicle."""
    assert signup(client, "admin@x.com", role="admin", name="Admin").status_code == 201
    admin = token_for(client, "admin@x.com", "admin")
    r = client.post("/admin/staff", headers=auth(admin), json={"full_name": "Stan Staff", "email": "staff@x.com", "password": "secret123"})
    assert r.status_code == 201, r.text
    staff_id = r.json()["id"]
    staff = token_for(client, "staff@x.com", "staff")
    assert signup(client, "cust@x.com", name="Carla Customer").status_code == 201
    cust = token_for(client, "cust@x.com", "customer")
    r = client.post("/vehicles", headers=auth(admin), json={
        "make": "Toyota", "model": "Vios", "year": 2024, "plate_number": "CDO-1005",
        "category": "sedan", "description": "Air-conditioned sedan with four passenger seats.", "daily_rate": 1800, "price_per_km": 10,
    })
    assert r.status_code == 201, r.text
    return {"admin": admin, "staff": staff, "staff_id": staff_id, "cust": cust, "vehicle_id": r.json()["id"]}


def submit_requirements(client, token):
    files = {k: (f"{k}.png", io.BytesIO(PNG), "image/png") for k in ("first_id", "second_id", "proof_of_address", "proof_of_income")}
    files["extra_document_1"] = ("extra-1.png", io.BytesIO(PNG), "image/png")
    files["extra_document_2"] = ("extra-2.png", io.BytesIO(PNG), "image/png")
    return client.post(
        "/me/verification", headers=auth(token), files=files,
        data={"id_type": "Passport", "id_number": "P123", "second_id_type": "National ID", "second_id_number": "N456", "address": "Sample Street"},
    )


def customer_id(client, admin):
    return client.get("/admin/customers", headers=auth(admin)).json()[0]["id"]


def verify_customer(client, w):
    assert submit_requirements(client, w["cust"]).status_code == 200
    cid = customer_id(client, w["admin"])
    r = client.post(f"/admin/customers/{cid}/verification", headers=auth(w["admin"]), json={"status": "verified"})
    assert r.status_code == 200, r.text
    return cid


def make_booking(client, w, start=1, end=3, **extra):
    body = {"vehicle_id": w["vehicle_id"], "start_date": d(start), "end_date": d(end),
            "pickup_time": "09:00", "return_time": "17:00", "service_type": "self_drive", "distance_km": 20, **extra}
    return client.post("/bookings", headers=auth(w["cust"]), json=body)


# ------------------------------------------------------------------ auth
def test_signup_login_and_portals(client, world):
    assert signup(client, "cust@x.com").status_code == 409  # duplicate email
    assert login(client, "cust@x.com", password="wrong").status_code == 401
    assert login(client, "CUST@x.com").status_code == 200  # case-insensitive
    assert login(client, "cust@x.com", portal="admin").status_code == 403
    assert login(client, "staff@x.com", portal="customer").status_code == 403
    assert login(client, "admin@x.com", portal="staff").status_code == 403
    me = client.get("/auth/me", headers=auth(world["cust"]))
    assert me.status_code == 200 and me.json()["role"] == "customer" and "password_hash" not in me.json()


def test_signup_requires_email_activation_and_can_resend(client):
    response = client.post("/auth/signup", json={
        "full_name": "Pending User",
        "email": "pending@x.com",
        "password": "secret123",
        "role": "customer",
    })
    assert response.status_code == 201
    assert login(client, "pending@x.com").status_code == 403

    first_token = client.app.state.verification_tokens["pending@x.com"]
    assert client.post("/auth/resend-verification", json={"email": "PENDING@x.com"}).status_code == 200
    second_token = client.app.state.verification_tokens["pending@x.com"]
    assert second_token != first_token
    assert client.get("/auth/verify-email", params={"token": first_token}).status_code == 400  # replaced by the new link

    # one click on the link in the email activates the account
    page = client.get("/auth/verify-email", params={"token": second_token})
    assert page.status_code == 200 and "Your account is active" in page.text
    assert login(client, "pending@x.com").status_code == 200
    assert client.get("/auth/verify-email", params={"token": second_token}).status_code == 400


def test_forgot_and_reset_password(client, world):
    # Unknown emails get the same answer and no code.
    r = client.post("/auth/forgot-password", json={"email": "nobody@x.com"})
    assert r.status_code == 200
    assert "nobody@x.com" not in client.app.state.reset_codes

    r = client.post("/auth/forgot-password", json={"email": "Cust@X.com"})
    assert r.status_code == 200, r.text
    code = client.app.state.reset_codes["cust@x.com"]
    assert len(code) == 6 and code.isdigit()

    # A second request inside the cooldown keeps the first code.
    client.app.state.reset_codes.clear()
    assert client.post("/auth/forgot-password", json={"email": "cust@x.com"}).status_code == 200
    assert "cust@x.com" not in client.app.state.reset_codes

    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/auth/reset-password", json={"email": "cust@x.com", "code": wrong, "new_password": "newpass123"})
    assert r.status_code == 400
    r = client.post("/auth/reset-password", json={"email": "cust@x.com", "code": "12ab", "new_password": "newpass123"})
    assert r.status_code == 422
    r = client.post("/auth/reset-password", json={"email": "cust@x.com", "code": code, "new_password": "new"})
    assert r.status_code == 422

    r = client.post("/auth/reset-password", json={"email": "cust@x.com", "code": code, "new_password": "newpass123"})
    assert r.status_code == 200, r.text
    assert login(client, "cust@x.com").status_code == 401
    assert login(client, "cust@x.com", password="newpass123", portal="customer").status_code == 200

    # The code works only once.
    r = client.post("/auth/reset-password", json={"email": "cust@x.com", "code": code, "new_password": "other123"})
    assert r.status_code == 400


def test_reset_code_locks_after_too_many_attempts(client, world):
    from app.database import SessionLocal
    from app.models import Profile

    assert client.post("/auth/forgot-password", json={"email": "staff@x.com"}).status_code == 200
    code = client.app.state.reset_codes["staff@x.com"]
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        r = client.post("/auth/reset-password", json={"email": "staff@x.com", "code": wrong, "new_password": "newpass123"})
        assert r.status_code == 400
    r = client.post("/auth/reset-password", json={"email": "staff@x.com", "code": code, "new_password": "newpass123"})
    assert r.status_code == 400
    assert login(client, "staff@x.com", portal="staff").status_code == 200

    # Expired codes are refused too.
    assert client.post("/auth/forgot-password", json={"email": "admin@x.com"}).status_code == 200
    code = client.app.state.reset_codes["admin@x.com"]
    with SessionLocal() as db:
        user = db.query(Profile).filter_by(email="admin@x.com").one()
        user.password_reset_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    r = client.post("/auth/reset-password", json={"email": "admin@x.com", "code": code, "new_password": "newpass123"})
    assert r.status_code == 400


def test_forgot_password_email_not_configured(client, world, monkeypatch):
    from app.routers import auth as auth_router
    from app.services.email import EmailNotConfigured

    def fail(email, code):
        raise EmailNotConfigured("no email")

    monkeypatch.setattr(auth_router, "send_password_reset_email", fail)
    r = client.post("/auth/forgot-password", json={"email": "cust@x.com"})
    assert r.status_code == 503
    # The unsent code is discarded, so a retry is not blocked by the cooldown.
    monkeypatch.setattr(auth_router, "send_password_reset_email", lambda email, code: client.app.state.reset_codes.__setitem__(email, code))
    assert client.post("/auth/forgot-password", json={"email": "cust@x.com"}).status_code == 200
    assert "cust@x.com" in client.app.state.reset_codes


def test_admin_signup_closes_after_first_admin(client, world):
    assert signup(client, "second@x.com", role="admin").status_code == 403


def test_unauthenticated_and_bad_token(client):
    assert client.get("/bookings").status_code == 401
    assert client.get("/bookings", headers=auth("garbage")).status_code == 401


def test_role_guards(client, world):
    assert client.get("/admin/customers", headers=auth(world["cust"])).status_code == 403
    assert client.get("/admin/dashboard", headers=auth(world["staff"])).status_code == 403
    assert client.post("/vehicles", headers=auth(world["cust"]), json={}).status_code == 403
    assert client.post("/bookings", headers=auth(world["staff"]), json={}).status_code == 403


# ------------------------------------------------------------------ vehicles
def test_vehicle_views_and_rules(client, world):
    admin_view = client.get("/vehicles", headers=auth(world["admin"])).json()
    assert admin_view[0]["daily_rate"] == 1800
    assert admin_view[0]["description"] == "Air-conditioned sedan with four passenger seats."
    cust_view = client.get("/vehicles", headers=auth(world["cust"])).json()
    assert "daily_rate" not in cust_view[0] and cust_view[0]["plate_number"] == "CDO-1005"
    public_view = client.get("/vehicles")
    assert public_view.status_code == 200
    assert public_view.json()[0]["description"] == "Air-conditioned sedan with four passenger seats."
    assert "daily_rate" not in public_view.json()[0]
    assert client.get("/vehicles/unavailable-dates").status_code == 401
    assert client.post("/bookings", json={}).status_code == 401

    dup = client.post("/vehicles", headers=auth(world["admin"]), json={"make": "A", "model": "B", "year": 2020, "plate_number": "CDO-1005", "daily_rate": 1})
    assert dup.status_code == 409

    vid = world["vehicle_id"]
    assert client.patch(f"/vehicles/{vid}", headers=auth(world["admin"]), json={"status": "maintenance"}).status_code == 200
    assert client.get("/vehicles", headers=auth(world["cust"])).json() == []  # hidden from customers
    assert make_booking(client, world).status_code == 400  # maintenance blocks booking
    client.patch(f"/vehicles/{vid}", headers=auth(world["admin"]), json={"status": "available"})

    up = client.post("/vehicles/images", headers=auth(world["admin"]), files={"file": ("car.png", io.BytesIO(PNG), "image/png")})
    assert up.status_code == 200
    url = up.json()["image_url"]
    assert client.get(url.replace("http://testserver", "")).status_code == 200  # public
    assert client.post("/vehicles/images", headers=auth(world["admin"]), files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")}).status_code == 400


# ------------------------------------------------------------------ booking lifecycle
def test_full_booking_lifecycle(client, world):
    admin, staff, cust, vid = world["admin"], world["staff"], world["cust"], world["vehicle_id"]

    r = make_booking(client, world, start=1, end=3, status="accepted")  # client-supplied status is ignored
    assert r.status_code == 201, r.text
    booking = r.json()
    assert booking["status"] == "pending"
    assert booking["total_amount"] == 3 * (1800 + 20 * 10)  # 3 days * (rate + km * per-km)
    bid = booking["id"]

    # cannot accept until the customer is verified
    r = client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]})
    assert r.status_code == 400
    verify_customer(client, world)

    r = client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    assert client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]}).status_code == 409  # no longer pending

    # notifications for both people
    c_notes = client.get("/notifications", headers=auth(cust)).json()
    assert {n["title"] for n in c_notes} >= {"Booking request received", "Booking approved"}
    s_notes = client.get("/notifications", headers=auth(staff)).json()
    assert [n["title"] for n in s_notes] == ["Booking assigned"]
    assert client.get("/notifications/unread-count", headers=auth(staff)).json() == {"count": 1}
    assert client.post(f"/notifications/{s_notes[0]['id']}/read", headers=auth(staff)).status_code == 200
    assert client.get("/notifications/unread-count", headers=auth(staff)).json() == {"count": 0}
    assert client.post(f"/notifications/{s_notes[0]['id']}/read", headers=auth(cust)).status_code == 404  # not yours

    # dates now blocked for customers
    blocked = client.get("/vehicles/unavailable-dates", headers=auth(cust)).json()
    assert blocked == [{"vehicle_id": vid, "start_date": d(1), "end_date": d(3)}]

    # staff sees the job (with only name+phone of the customer)
    jobs = client.get("/bookings", headers=auth(staff)).json()
    assert len(jobs) == 1 and set(jobs[0]["profiles"]) == {"full_name", "phone"}

    # staff workflow
    assert client.post(f"/bookings/{bid}/complete", headers=auth(staff)).status_code == 409  # not ongoing yet
    assert client.post(f"/bookings/{bid}/start", headers=auth(staff)).status_code == 200
    assert next(v for v in client.get("/vehicles", headers=auth(admin)).json())["status"] == "rented"

    r = client.post(f"/bookings/{bid}/complete", headers=auth(staff))
    assert r.status_code == 409 and "post-rental inspection" in r.json()["detail"]

    pre = client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "pre_rental", "checklist": {"Tires": True}, "odometer_reading": 100, "fuel_level": "Full"})
    assert pre.status_code == 201
    assert client.post(f"/bookings/{bid}/complete", headers=auth(staff)).status_code == 409  # pre-rental doesn't count

    # Same shape the staff mobile app sends.
    mobile_details = {
        "tools": {"car_jack": "present", "spare_tire": "missing", "umbrella": None},
        "damage_marks": [{"zone": "door_lf", "zone_label": "Left front door", "code": "DS", "label": "Deep Scratch"}],
        "renter_name": "Carla Customer", "renter_signed": True, "staff_name": "Stan Staff", "staff_signed": True,
    }
    bad_tool = {**mobile_details, "tools": {"car_jack": "stolen"}}
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "post_rental", **bad_tool}).status_code == 422
    post = client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "post_rental", "damage_notes": "scratch", "damage_fee": 1500, **mobile_details})
    assert post.status_code == 201 and post.json()["damage_fee"] == 1500
    assert post.json()["tools"] == mobile_details["tools"] and post.json()["damage_marks"] == mobile_details["damage_marks"]
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "post_rental", "damage_fee": -5}).status_code == 422

    done = client.post(f"/bookings/{bid}/complete", headers=auth(staff))
    assert done.status_code == 200 and done.json()["status"] == "completed"
    # returned vehicles stay off the fleet until the inspection is verified and the car is cleaned
    assert next(v for v in client.get("/vehicles", headers=auth(admin)).json())["status"] == "cleaning"

    admin_row = client.get("/bookings?status=completed", headers=auth(admin)).json()[0]
    assert admin_row["actual_return_date"] == date.today().isoformat()
    assert admin_row["late_return"] is False  # returned before the end date
    assert admin_row["inspections"][1]["damage_fee"] == 1500
    assert admin_row["profiles"]["verification_status"] == "verified"
    print_pack = client.get(f"/bookings/{bid}/print-pack", headers=auth(admin))
    assert print_pack.status_code == 200
    assert print_pack.json()["customer"]["address"] == "Sample Street"
    assert print_pack.json()["documents"]["Additional document 2"]
    assert print_pack.json()["inspections"][0]["checklist"] == {"Tires": True}
    post_pack = print_pack.json()["inspections"][1]
    assert post_pack["damage_marks"][0]["zone_label"] == "Left front door"
    assert post_pack["tools"]["spare_tire"] == "missing"
    assert post_pack["renter_name"] == "Carla Customer" and post_pack["staff_signed"] is True
    assert print_pack.json()["agreement_terms"]["renter_terms"] == client.get("/public/rental-agreement").json()["renter_terms"]

    dash = client.get("/admin/dashboard", headers=auth(admin)).json()
    assert dash["revenue"] == 3 * 2000 and dash["vehicles"] == 1 and dash["customers"] == 1 and dash["pending"] == 0
    assert len(dash["monthly_rentals"]) == 12 and sum(m["count"] for m in dash["monthly_rentals"]) <= 1
    assert dash["recent"][0]["profiles"]["full_name"] == "Carla Customer"


def fleet_status(client, admin, vehicle_id):
    return next(v["status"] for v in client.get("/vehicles", headers=auth(admin)).json() if v["id"] == vehicle_id)


def test_return_to_fleet_needs_verification_and_cleaning(client, world):
    admin, staff, vid = world["admin"], world["staff"], world["vehicle_id"]
    r = client.post("/admin/staff", headers=auth(admin), json={"full_name": "Cleo Cleaner", "email": "cleaner@x.com", "password": "secret123"})
    cleaner_id = r.json()["id"]
    cleaner = token_for(client, "cleaner@x.com", "staff")
    bid = make_booking(client, world).json()["id"]
    verify_customer(client, world)
    client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]})
    assert [b["id"] for b in client.get("/bookings/inspection-queue", headers=auth(admin)).json()] == [bid]
    assert client.get("/bookings/inspection-queue", headers=auth(staff)).status_code == 403

    # the admin is told when the staff finishes an inspection, and can verify it
    pre = client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "pre_rental", "fuel_level": "Full"}).json()
    assert pre["verified_at"] is None
    assert client.get("/notifications", headers=auth(admin)).json()[0]["title"] == "Pre-rental inspection done"
    r = client.post(f"/bookings/{bid}/inspections/{pre['id']}/verify", headers=auth(admin))
    assert r.status_code == 200 and r.json()["inspections"][0]["verified_by"] is not None
    assert client.post(f"/bookings/{bid}/inspections/{pre['id']}/verify", headers=auth(admin)).status_code == 409
    assert client.get("/notifications", headers=auth(staff)).json()[0]["title"] == "Inspection verified"

    client.post(f"/bookings/{bid}/start", headers=auth(staff))
    assert client.post(f"/bookings/{bid}/cleaning-done", headers=auth(staff)).status_code == 409  # not returned yet
    post = client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "post_rental"}).json()
    done = client.post(f"/bookings/{bid}/complete", headers=auth(staff)).json()
    assert done["cleaning_staff_id"] == world["staff_id"]  # defaults to the inspecting staff
    assert fleet_status(client, admin, vid) == "cleaning"
    assert client.get("/vehicles", headers=auth(world["cust"])).json() == []  # hidden from customers
    assert make_booking(client, world, start=10, end=11).status_code == 400  # not bookable while being cleaned

    # hand the cleaning to a cleaner; only they (or the admin) can finish it
    r = client.post(f"/bookings/{bid}/cleaner", headers=auth(admin), json={"staff_id": cleaner_id})
    assert r.status_code == 200 and r.json()["cleaning_staff_id"] == cleaner_id
    assert client.get("/notifications", headers=auth(cleaner)).json()[0]["title"] == "Cleaning check assigned"
    assert bid in [b["id"] for b in client.get("/bookings", headers=auth(cleaner)).json()]
    assert client.post(f"/bookings/{bid}/cleaning-done", headers=auth(staff)).status_code == 403
    r = client.post(f"/bookings/{bid}/cleaning-done", headers=auth(cleaner))
    assert r.status_code == 200 and r.json()["cleaning_done_at"]
    assert client.get("/notifications", headers=auth(admin)).json()[0]["title"] == "Cleaning done"
    assert fleet_status(client, admin, vid) == "cleaning"  # post-rental not verified yet
    assert [b["id"] for b in client.get("/bookings/inspection-queue", headers=auth(admin)).json()] == [bid]

    client.post(f"/bookings/{bid}/inspections/{post['id']}/verify", headers=auth(admin))
    assert fleet_status(client, admin, vid) == "available"
    assert client.get("/bookings/inspection-queue", headers=auth(admin)).json() == []
    assert [b["id"] for b in client.get("/bookings/inspection-queue?view=done", headers=auth(admin)).json()] == [bid]


def test_inspection_damage_photos(client, world):
    admin, staff = world["admin"], world["staff"]
    bid = make_booking(client, world).json()["id"]
    verify_customer(client, world)
    client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]})

    def upload(token, name="dent.png", kind="image/png"):
        return client.post(f"/bookings/{bid}/inspection-photos", headers=auth(token), files={"file": (name, io.BytesIO(PNG), kind)})

    assert upload(world["cust"]).status_code == 403
    assert upload(staff, "x.pdf", "application/pdf").status_code == 400
    paths = [upload(staff).json()["path"] for _ in range(2)]
    assert all(path.startswith(f"{bid}/") for path in paths)

    body = {"type": "pre_rental", "photos": ["someone-else/x.png"]}
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json=body).status_code == 400
    r = client.post(f"/bookings/{bid}/inspections", headers=auth(staff), json={"type": "pre_rental", "photos": paths})
    assert r.status_code == 201 and r.json()["photos"] == paths

    # the admin sees signed links to the photos
    urls = client.get("/bookings/inspection-queue", headers=auth(admin)).json()[0]["inspections"][0]["photo_urls"]
    assert len(urls) == 2
    link = urls[0].split("8000", 1)[-1] if "8000" in urls[0] else urls[0]
    assert client.get(link).status_code == 200
    assert client.get(link.split("?")[0] + "?token=bad").status_code == 403
    pack = client.get(f"/bookings/{bid}/print-pack", headers=auth(admin)).json()
    assert len(pack["inspections"][0]["photo_urls"]) == 2


def test_return_reminders_two_hours_and_thirty_minutes(client, world, monkeypatch):
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from app.database import SessionLocal
    from app.services import reminders

    sent_emails = []
    monkeypatch.setattr(reminders, "send_notification_email", lambda to, subject, text: sent_emails.append((to, subject, text)))
    admin, staff, cust = world["admin"], world["staff"], world["cust"]
    bid = make_booking(client, world, start=0, end=1).json()["id"]
    verify_customer(client, world)
    client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]})
    # accept/reject notifications carry a kind so the apps can group them
    assert client.get("/notifications", headers=auth(cust)).json()[0]["kind"] == "booking_accepted"
    client.post(f"/bookings/{bid}/start", headers=auth(staff))

    due = datetime.combine(date.today() + timedelta(days=1), dtime(17, 0), tzinfo=ZoneInfo("Asia/Manila"))

    def run(at):
        with SessionLocal() as db:
            return reminders.send_due_return_reminders(db, now=at)

    assert run(due - timedelta(hours=3)) == 0
    assert run(due - timedelta(hours=1, minutes=59)) == 1
    assert run(due - timedelta(hours=1, minutes=30)) == 0  # sent once only
    customer_note = client.get("/notifications", headers=auth(cust)).json()[0]
    assert customer_note["kind"] == "return_reminder_2h" and "2 hours" in customer_note["title"]
    admin_note = client.get("/notifications", headers=auth(admin)).json()[0]
    assert admin_note["kind"] == "return_reminder_2h"
    assert admin_note["data"]["customer_name"] == "Carla Customer" and admin_note["data"]["customer_phone"] == "0917"
    assert client.get("/notifications", headers=auth(staff)).json()[0]["kind"] == "return_reminder_2h"
    assert len(sent_emails) == 1 and sent_emails[0][0] == "cust@x.com" and "2 hours" in sent_emails[0][1]

    assert run(due - timedelta(minutes=29)) == 1
    assert client.get("/notifications", headers=auth(cust)).json()[0]["kind"] == "return_reminder_30m"
    assert len(sent_emails) == 2 and "30 minutes" in sent_emails[1][1]
    assert run(due + timedelta(minutes=5)) == 0

    # the admin card lists the rental with the number to call
    rows = client.get("/bookings/returns-due?hours=72", headers=auth(admin)).json()
    assert [r["booking_id"] for r in rows] == [bid] and rows[0]["customer_phone"] == "0917"
    assert rows[0]["reminder_2h_sent_at"] and rows[0]["reminder_30m_sent_at"]
    assert client.get("/bookings/returns-due", headers=auth(staff)).status_code == 403


def test_late_start_only_sends_thirty_minute_reminder(client, world, monkeypatch):
    from datetime import time as dtime
    from zoneinfo import ZoneInfo

    from app.database import SessionLocal
    from app.services import reminders

    monkeypatch.setattr(reminders, "send_notification_email", lambda *args: None)
    bid = make_booking(client, world, start=0, end=0, pickup_time="08:00", return_time="17:00").json()["id"]
    verify_customer(client, world)
    client.post(f"/bookings/{bid}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]})
    client.post(f"/bookings/{bid}/start", headers=auth(world["staff"]))
    due = datetime.combine(date.today(), dtime(17, 0), tzinfo=ZoneInfo("Asia/Manila"))
    with SessionLocal() as db:
        assert reminders.send_due_return_reminders(db, now=due - timedelta(minutes=20)) == 1
    with SessionLocal() as db:
        assert reminders.send_due_return_reminders(db, now=due - timedelta(minutes=10)) == 0
    kinds = [n["kind"] for n in client.get("/notifications", headers=auth(world["cust"])).json()]
    assert "return_reminder_30m" in kinds and "return_reminder_2h" not in kinds


def test_admin_can_reassign_staff(client, world):
    admin = world["admin"]
    r = client.post("/admin/staff", headers=auth(admin), json={"full_name": "Second Staff", "email": "staff2@x.com", "password": "secret123"})
    assert r.status_code == 201, r.text
    second_id = r.json()["id"]
    second = token_for(client, "staff2@x.com", "staff")
    bid = make_booking(client, world).json()["id"]

    # pending bookings are assigned through accept, not reassign
    assert client.post(f"/bookings/{bid}/assign", headers=auth(admin), json={"staff_id": second_id}).status_code == 409
    verify_customer(client, world)
    assert client.post(f"/bookings/{bid}/accept", headers=auth(admin), json={"staff_id": world["staff_id"]}).status_code == 200

    assert client.post(f"/bookings/{bid}/assign", headers=auth(world["staff"]), json={"staff_id": second_id}).status_code == 403
    assert client.post(f"/bookings/{bid}/assign", headers=auth(admin), json={"staff_id": world["staff_id"]}).status_code == 400  # already assigned
    r = client.post(f"/bookings/{bid}/assign", headers=auth(admin), json={"staff_id": second_id})
    assert r.status_code == 200, r.text
    assert r.json()["assigned_staff_id"] == second_id

    # the job moves to the new staff member, and both are told
    assert [b["id"] for b in client.get("/bookings", headers=auth(second)).json()] == [bid]
    assert client.get("/bookings", headers=auth(world["staff"])).json() == []
    assert client.get("/notifications", headers=auth(second)).json()[0]["title"] == "Booking assigned"
    assert client.get("/notifications", headers=auth(world["staff"])).json()[0]["title"] == "Booking reassigned"
    insp = {"type": "pre_rental", "fuel_level": "Full"}
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(world["staff"]), json=insp).status_code == 403
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(second), json=insp).status_code == 201


def test_booking_destination(client, world):
    r = make_booking(client, world, destination_from=" Cagayan de Oro ", destination_to="Bukidnon")
    assert r.status_code == 201
    assert (r.json()["destination_from"], r.json()["destination_to"]) == ("Cagayan de Oro", "Bukidnon")
    row = client.get("/bookings", headers=auth(world["admin"])).json()[0]
    assert (row["destination_from"], row["destination_to"]) == ("Cagayan de Oro", "Bukidnon")


def test_overlap_protection(client, world):
    verify_customer(client, world)
    a = make_booking(client, world, start=5, end=8).json()["id"]
    b = make_booking(client, world, start=7, end=10).json()["id"]  # pending bookings may overlap...
    assert client.post(f"/bookings/{a}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]}).status_code == 200
    r = client.post(f"/bookings/{b}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]})  # ...but can't both be accepted
    assert r.status_code == 409 and "already reserved" in r.json()["detail"]
    assert client.get("/bookings?status=pending", headers=auth(world["admin"])).json()[0]["id"] == b  # b stays pending


def test_reject_and_validation(client, world):
    assert make_booking(client, world, start=-2, end=1).status_code == 400  # in the past
    assert make_booking(client, world, start=4, end=2).status_code == 400  # end before start
    assert make_booking(client, world, start=1, end=1, pickup_time="15:00", return_time="09:00").status_code == 400
    assert make_booking(client, world, service_type="pickup_dropoff").status_code == 400  # needs a location
    assert make_booking(client, world, distance_km=-1).status_code == 422
    bid = make_booking(client, world).json()["id"]
    assert client.post(f"/bookings/{bid}/reject", headers=auth(world["admin"])).status_code == 422
    r = client.post(f"/bookings/{bid}/reject", headers=auth(world["admin"]), json={"reason": "Please update the expired ID."})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert r.json()["rejection_reason"] == "Please update the expired ID."
    titles = [n["title"] for n in client.get("/notifications", headers=auth(world["cust"])).json()]
    assert "Booking needs changes" in titles
    assert any("expired ID" in n["body"] for n in client.get("/notifications", headers=auth(world["cust"])).json())


def test_customer_can_edit_and_resubmit_rejected_booking(client, world):
    verify_customer(client, world)  # bookings can only be accepted for verified customers
    booking = make_booking(client, world, start=5, end=7).json()
    booking_id = booking["id"]
    admin = world["admin"]
    customer = world["cust"]

    rejected = client.post(
        f"/bookings/{booking_id}/reject",
        headers=auth(admin),
        json={"reason": "Please change your rental dates and add a pickup location."},
    )
    assert rejected.status_code == 200

    resubmission = {
        "vehicle_id": world["vehicle_id"],
        "start_date": d(8),
        "end_date": d(9),
        "pickup_time": "10:00",
        "return_time": "16:00",
        "pickup_location": "Updated pickup point",
        "service_type": "pickup_dropoff",
        "distance_km": 12,
        "notes": "Updated request",
    }
    updated = client.put(f"/bookings/{booking_id}/resubmit", headers=auth(customer), json=resubmission)
    assert updated.status_code == 200, updated.text
    assert updated.json()["status"] == "pending"
    assert updated.json()["start_date"] == d(8)
    assert updated.json()["pickup_location"] == "Updated pickup point"
    assert updated.json()["rejection_reason"] is None

    pending = client.get("/bookings?status=pending", headers=auth(admin)).json()
    assert any(item["id"] == booking_id and item["notes"] == "Updated request" for item in pending)
    admin_notes = client.get("/notifications", headers=auth(admin)).json()
    assert any(item["title"] == "Customer updated rejected booking" and booking_id in item["body"] for item in admin_notes)

    rejected_again = client.post(
        f"/bookings/{booking_id}/reject",
        headers=auth(admin),
        json={"reason": "The new pickup location is outside the service area."},
    )
    assert rejected_again.status_code == 200
    assert rejected_again.json()["rejection_reason"] == "The new pickup location is outside the service area."
    customer_notes = client.get("/notifications", headers=auth(customer)).json()
    assert any("The new pickup location is outside the service area." in item["body"] for item in customer_notes)
    assert client.put(f"/bookings/{booking_id}/resubmit", headers=auth(admin), json=resubmission).status_code == 403

    accepted = client.post(
        f"/bookings/{booking_id}/accept",
        headers=auth(admin),
        json={"staff_id": world["staff_id"]},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "accepted"
    assert accepted.json()["rejection_reason"] is None


def test_blacklist_flow(client, world):
    cid = customer_id(client, world["admin"])
    r = client.patch(f"/admin/customers/{cid}/blacklist", headers=auth(world["admin"]), json={"is_blacklisted": True})
    assert r.status_code == 200
    assert login(client, "cust@x.com").status_code == 403
    assert client.get("/bookings", headers=auth(world["cust"])).status_code == 403  # existing token is cut off too
    assert [c["email"] for c in client.get("/admin/customers?blacklisted=true", headers=auth(world["admin"])).json()] == ["cust@x.com"]
    client.patch(f"/admin/customers/{cid}/blacklist", headers=auth(world["admin"]), json={"is_blacklisted": False})
    assert login(client, "cust@x.com").status_code == 200


def test_data_isolation(client, world):
    bid = make_booking(client, world).json()["id"]
    signup(client, "other@x.com", name="Other")
    other = token_for(client, "other@x.com")
    assert client.get("/bookings", headers=auth(other)).json() == []
    assert client.get(f"/bookings/{bid}", headers=auth(other)).status_code == 404
    assert client.get(f"/bookings/{bid}", headers=auth(world["cust"])).status_code == 200
    # unassigned staff can't touch it
    assert client.post(f"/bookings/{bid}/start", headers=auth(world["staff"])).status_code == 403
    assert client.post(f"/bookings/{bid}/inspections", headers=auth(world["staff"]), json={"type": "pre_rental"}).status_code == 403
    # customer response never leaks other people's fields
    assert "profiles" not in client.get(f"/bookings/{bid}", headers=auth(world["cust"])).json()


def test_delete_vehicle_rules(client, world):
    make_booking(client, world)
    r = client.delete(f"/vehicles/{world['vehicle_id']}", headers=auth(world["admin"]))
    assert r.status_code == 409 and "Maintenance" in r.json()["detail"]
    fresh = client.post("/vehicles", headers=auth(world["admin"]), json={"make": "Honda", "model": "City", "year": 2023, "plate_number": "NEW-1", "daily_rate": 1500}).json()["id"]
    assert client.delete(f"/vehicles/{fresh}", headers=auth(world["admin"])).status_code == 204


# ------------------------------------------------------------------ verification + documents
def test_verification_review_and_documents(client, world):
    cust, admin = world["cust"], world["admin"]
    # incomplete submission rejected
    r = client.post("/me/verification", headers=auth(cust), data={"id_type": "Passport", "id_number": "1", "second_id_type": "NID", "second_id_number": "2"})
    assert r.status_code == 400
    # bad file type rejected
    r = client.post("/me/verification", headers=auth(cust), data={"id_type": "P", "id_number": "1", "second_id_type": "N", "second_id_number": "2"},
                    files={"first_id": ("a.txt", io.BytesIO(b"hello"), "text/plain")})
    assert r.status_code == 400

    assert submit_requirements(client, cust).json()["verification_status"] == "pending"
    cid = customer_id(client, admin)
    admin_notifications = client.get("/notifications", headers=auth(admin)).json()
    assert any(
        item["title"] == "Customer requirements updated" and "Carla Customer" in item["body"]
        for item in admin_notifications
    )

    assert client.post(f"/admin/customers/{cid}/verification", headers=auth(admin), json={"status": "rejected"}).status_code == 400  # reason required
    r = client.post(f"/admin/customers/{cid}/verification", headers=auth(admin), json={"status": "rejected", "reason": "ID is blurry."})
    assert r.status_code == 200 and r.json()["verification_notes"] == "ID is blurry."
    assert any("ID is blurry." in n["body"] for n in client.get("/notifications", headers=auth(cust)).json())

    # re-submit keeps old docs when files are omitted
    r = client.post("/me/verification", headers=auth(cust), data={"id_type": "Passport", "id_number": "P9", "second_id_type": "NID", "second_id_number": "N9", "address": "Updated Street"})
    assert r.status_code == 200 and r.json()["verification_status"] == "pending" and r.json()["verification_notes"] is None
    r = client.post(
        f"/admin/customers/{cid}/verification",
        headers=auth(admin),
        json={"status": "rejected", "reason": "The updated proof of address is still unclear."},
    )
    assert r.status_code == 200 and r.json()["verification_notes"] == "The updated proof of address is still unclear."
    customer_notifications = client.get("/notifications", headers=auth(cust)).json()
    assert any("The updated proof of address is still unclear." in item["body"] for item in customer_notifications)
    r = submit_requirements(client, cust)
    assert r.status_code == 200 and r.json()["verification_status"] == "pending"
    assert r.json()["verification_notes"] is None

    docs = client.get(f"/admin/customers/{cid}/documents", headers=auth(admin)).json()
    assert all(docs[k] for k in ("primaryId", "secondId", "address", "income", "extra1", "extra2"))
    path = docs["primaryId"].replace("http://testserver", "")
    assert client.get(path).status_code == 200
    no_token = path.split("?")[0]
    assert client.get(no_token).status_code == 422
    other = docs["secondId"].split("?token=")[1]
    assert client.get(f"{no_token}?token={other}").status_code == 403  # token is bound to one path
    assert client.get(f"/files/customer-documents/../../etc/passwd?token={other}").status_code in (403, 404)

    assert client.post(f"/admin/customers/{cid}/verification", headers=auth(admin), json={"status": "verified"}).status_code == 200
    updated = submit_requirements(client, cust)
    assert updated.status_code == 200 and updated.json()["verification_status"] == "pending"
    assert updated.json()["address"] == "Sample Street"


def test_customer_age(client, world):
    assert client.put("/me/age", headers=auth(world["cust"]), json={"age": 0}).status_code == 422
    r = client.put("/me/age", headers=auth(world["cust"]), json={"age": 29})
    assert r.status_code == 200 and r.json()["age"] == 29


def test_staff_creation(client, world):
    dup = client.post("/admin/staff", headers=auth(world["admin"]), json={"full_name": "Dup", "email": "STAFF@x.com", "password": "secret123"})
    assert dup.status_code == 409
    assert [s["email"] for s in client.get("/admin/staff", headers=auth(world["admin"])).json()] == ["staff@x.com"]
    assert client.post("/admin/staff", headers=auth(world["staff"]), json={"full_name": "x", "email": "y@x.com", "password": "secret123"}).status_code == 403


def test_churn_history_and_reports_filters(client, world):
    make_booking(client, world, start=1, end=2)
    make_booking(client, world, start=20, end=21)
    hist = client.get("/admin/customers/booking-history", headers=auth(world["admin"])).json()
    assert len(hist) == 2 and {"customer_id", "late_return", "service_type", "total_amount", "status"} <= set(hist[0])
    rows = client.get(f"/bookings?status=all&start_from={d(10)}&start_to={d(30)}&sort=start_desc", headers=auth(world["admin"])).json()
    assert len(rows) == 1 and rows[0]["start_date"] == d(20)
    assert client.get("/bookings?status=bogus", headers=auth(world["admin"])).status_code == 422


def test_approval_email_not_configured(client, world):
    verify_customer(client, world)
    bid = make_booking(client, world).json()["id"]
    assert client.post(f"/bookings/{bid}/send-approval-email", headers=auth(world["admin"])).status_code == 409  # not accepted yet
    client.post(f"/bookings/{bid}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]})
    r = client.post(f"/bookings/{bid}/send-approval-email", headers=auth(world["admin"]))
    assert r.status_code == 503 and "RESEND_API_KEY" in r.json()["detail"]


def test_approval_email_sent(client, world, monkeypatch):
    import httpx
    from app.config import get_settings

    sent = {}

    class FakeResp:
        status_code = 200
        text = "ok"

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, json=json, headers=headers)
        return FakeResp()

    s = get_settings()
    monkeypatch.setattr(s, "resend_api_key", "re_test")
    monkeypatch.setattr(s, "email_from", "Pardz <no-reply@pardz.test>")
    monkeypatch.setattr(httpx, "post", fake_post)

    verify_customer(client, world)
    bid = make_booking(client, world).json()["id"]
    client.post(f"/bookings/{bid}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]})
    r = client.post(f"/bookings/{bid}/send-approval-email", headers=auth(world["admin"]))
    assert r.status_code == 200 and r.json() == {"sent": True, "email": "cust@x.com"}
    assert sent["json"]["to"] == ["cust@x.com"] and "Toyota Vios" in sent["json"]["text"]


def test_approval_email_uses_smtp(client, world, monkeypatch):
    from app.config import get_settings
    from app.services import email

    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.update(host=host, port=port, timeout=timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self, context):
            sent["starttls"] = context is not None

        def login(self, username, password):
            sent["credentials"] = (username, password)

        def send_message(self, message):
            sent["message"] = message

    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "mailer@example.test")
    monkeypatch.setattr(settings, "smtp_password", "test-password")
    monkeypatch.setattr(settings, "smtp_from", "Pardz <mailer@example.test>")
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(settings, "smtp_ssl", False)
    monkeypatch.setattr(settings, "resend_api_key", "")
    monkeypatch.setattr(settings, "email_from", "")
    monkeypatch.setattr(email.smtplib, "SMTP", FakeSMTP)

    email.send_verification_email("activate@x.com", "activation-token")
    message = sent["message"]
    assert message["Subject"] == "Activate your Pardz Car Rental account"
    assert message["Date"] and message["Message-ID"] and message["Reply-To"]
    assert "http://testserver/auth/verify-email?token=activation-token" in message.get_body(preferencelist=("plain",)).get_content()
    assert "Activate my account" in message.get_body(preferencelist=("html",)).get_content()

    verify_customer(client, world)
    bid = make_booking(client, world).json()["id"]
    client.post(f"/bookings/{bid}/accept", headers=auth(world["admin"]), json={"staff_id": world["staff_id"]})
    response = client.post(f"/bookings/{bid}/send-approval-email", headers=auth(world["admin"]))

    assert response.status_code == 200
    assert sent["host"] == "smtp.example.test" and sent["starttls"] is True
    assert sent["credentials"] == ("mailer@example.test", "test-password")
    assert sent["message"]["To"] == "cust@x.com"


# ------------------------------------------------------------------ realtime
def test_websocket_events(client, world):
    verify_customer(client, world)
    with client.websocket_connect(f"/ws?token={world['admin']}") as admin_ws, client.websocket_connect(f"/ws?token={world['cust']}") as cust_ws:
        bid = make_booking(client, world).json()["id"]
        assert admin_ws.receive_json() == {"table": "bookings"}
        assert cust_ws.receive_json() in ({"table": "bookings"}, {"table": "notifications"})
        client.post(f"/bookings/{bid}/reject", headers=auth(world["admin"]), json={"reason": "Missing details."})
        got = {admin_ws.receive_json()["table"]}
        assert "bookings" in got


def test_websocket_rejects_bad_token(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws?token=nope"):
            pass


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    agreement = client.get("/public/rental-agreement")
    assert agreement.status_code == 200
    assert len(agreement.json()["renter_terms"]) == 5
