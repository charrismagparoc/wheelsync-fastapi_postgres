# WheelSync API (FastAPI + PostgreSQL)

The backend of WheelSync for **Pardz Car Rental**. The admin console, the customer website and the staff mobile app
all talk to this API over REST plus one WebSocket for live updates. It is the only part of the system that reads and
writes the PostgreSQL database. It also sends emails, generates return reminders, and runs the
**Customer Churn Prediction** model (Random Forest).

System overview and the full flow: see [`../README.md`](../README.md).

## Contents

- [Folder structure](#folder-structure)
- [Run it (Windows, this PC)](#run-it-windows-this-pc)
- [Run it with Docker](#run-it-with-docker)
- [Configuration (.env)](#configuration-env)
- [Data model](#data-model)
- [Booking lifecycle and rules](#booking-lifecycle-and-rules)
- [Company data loaded once](#company-data-loaded-once)
- [Customer Churn Prediction](#customer-churn-prediction)
- [Endpoints](#endpoints)
- [Scripts](#scripts)
- [Tests](#tests)
- [Deployment notes](#deployment-notes)

## Folder structure

```
wheelsync-fastapi_postgres-main/
├── app/
│   ├── main.py              FastAPI app: routers, CORS, /health, WebSocket /ws, reminder loop on start-up
│   ├── config.py            Settings read from .env (database, JWT, SMTP, timezone, seeding…)
│   ├── database.py          Engine + session; init_db(): creates tables, small migrations, loads company data
│   ├── models.py            SQLAlchemy tables (see Data model)
│   ├── schemas.py           Pydantic request / response shapes and validation
│   ├── security.py          bcrypt passwords, JWT tokens, signed links for documents and photos
│   ├── deps.py              Current user + role guards (require_admin, require_staff…)
│   ├── realtime.py          WebSocket hub: pings apps when bookings / notifications change
│   ├── routers/             HTTP endpoints, one file per area
│   │   ├── auth.py            sign-up, email activation, login (email or staff phone), password reset
│   │   ├── me.py              customer age + requirement uploads
│   │   ├── vehicles.py        fleet, photos (any image type, converted to JPEG), unavailable dates
│   │   ├── bookings.py        booking lifecycle, inspections, cleaning, print pack, returns due
│   │   ├── admin.py           customers, blacklist, churn, staff, dashboard, audit log
│   │   ├── transactions.py    walk-in rental records and extra payments
│   │   ├── locations.py       destinations (price sheet)
│   │   ├── notifications.py   in-app notifications
│   │   ├── public.py          rental agreement terms for the customer site
│   │   └── files.py           serves vehicle images, inspection photos, customer documents
│   ├── services/            Business logic used by the routers
│   │   ├── bookings.py        booking rules, overlap check, status changes, inspections, cleaning
│   │   ├── reminders.py       2-hour and 30-minute return reminders (background loop)
│   │   ├── notifications.py   creates notifications and publishes live updates
│   │   ├── email.py           SMTP (Gmail) / Resend emails; never emails imported past customers
│   │   ├── blacklist.py       Pardz's printed blacklist + name matching against customer accounts
│   │   ├── churn.py           reads completed rentals, serves and stores churn predictions
│   │   ├── company_data.py    one-time loads: destinations, blacklist pages, rental history
│   │   ├── locations.py       default destination list
│   │   ├── agreement.py       rental agreement content (print pack)
│   │   ├── fleet.py           vehicle availability helpers
│   │   ├── storage.py         saving uploads under UPLOAD_DIR
│   │   ├── phones.py          phone-number matching for staff sign-in
│   │   ├── audit.py           audit-log entries
│   │   └── synthetic.py       hides the cars that only exist in the rental history from the fleet
│   ├── ml/
│   │   ├── churn.py           Random Forest: features, training, evaluation, prediction
│   │   ├── churn_model.joblib the trained model (shipped with the app)
│   │   └── churn_model.json   its evaluation report (shown in the admin's "How churn is computed")
│   └── data/
│       └── rental_history.csv Pardz's 542 past rentals (186 customers), loaded once into a new database
├── db/
│   ├── schema.sql           SQL for creating the database by hand (optional; the API creates tables itself)
│   └── synthetic_rentals_first542.csv  original source file of the rental history
├── scripts/
│   ├── create_admin.py      create the first admin from the command line
│   ├── import_rental_history.py  reload or remove the past rental history by hand
│   └── train_churn_model.py      retrain the churn model on the completed rentals in the database
├── tests/
│   ├── conftest.py          test setup (separate test database, emails captured)
│   └── test_api.py          40 end-to-end API tests
├── uploads/                 saved files (vehicle photos, customer documents, inspection photos) — back this up
├── requirements.txt         runtime packages
├── requirements-dev.txt     test packages
├── .env.example             every setting with comments (copy to .env)
├── start_db.bat             starts the local PostgreSQL (Windows)
├── Dockerfile, docker-compose.yml   container setup (API + PostgreSQL)
└── README.md
```

## Run it (Windows, this PC)

This PC uses a portable PostgreSQL in `%USERPROFILE%\pgsql`, with its data in `%USERPROFILE%\wheelsync-pgdata` on port 5432.

```bat
:: 1. start the database (once after each restart of the PC)
start_db.bat

:: 2. start the API
.venv\Scripts\uvicorn.exe app.main:app --reload --host 0.0.0.0 --port 8000
```

API: <http://localhost:8000> · interactive docs: <http://localhost:8000/docs>.
On start-up the API creates or updates the tables and loads the company data once (see below).

**First time on a new computer**

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      :: then edit DATABASE_URL, JWT_SECRET and the SMTP settings
```

You need PostgreSQL 13+ with the `btree_gist` extension; the API runs `CREATE EXTENSION IF NOT EXISTS btree_gist` itself.

**First admin:** open the admin console and use **Create the admin account**, which works only while no admin exists.
Or run `python -m scripts.create_admin "Full Name" admin@email.com "password"`.

## Run it with Docker

```bash
cp .env.example .env     # set JWT_SECRET, PUBLIC_BASE_URL, CORS_ORIGINS
docker compose up --build
```

## Configuration (.env)

All settings are in `.env.example` with comments. The important ones:

| Setting | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection |
| `JWT_SECRET` | Signing key for sign-in tokens. Required in production; the API refuses to start with the default when `ENVIRONMENT=production` |
| `CORS_ORIGINS` | Addresses of the web apps (e.g. `http://localhost:5173,http://localhost:5174`) |
| `PUBLIC_BASE_URL` | Address browsers use to reach this API (used in image and document links) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_STARTTLS`, `SMTP_SSL` | Email sending |
| `TIMEZONE`, `RENTAL_ADDRESS` | Asia/Manila; address printed on documents |
| `REMINDER_INTERVAL_SECONDS` | How often reminders are checked (default 60; 0 = off) |
| `SEED_RENTAL_HISTORY` | Load the past rental history into a new database (default true) |
| `ALLOW_ADMIN_SIGNUP` | Allow creating the first admin from the web (only while none exists) |

**Email with Gmail:**
1. Turn on 2-Step Verification on the Pardz Google account.
2. Create an **App password** under Security → App passwords.
3. Set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USERNAME=pardzcarrental@gmail.com`, `SMTP_PASSWORD=<app password>`,
   `SMTP_FROM=Pardz Car Rental <pardzcarrental@gmail.com>`, `SMTP_STARTTLS=true`, `SMTP_SSL=false`.

Without email set up, new accounts cannot activate themselves. The sign-in page then says the activation email could not be sent.

## Data model

| Table | Holds |
|---|---|
| `profiles` | All accounts: admin, staff, customer. Includes requirements, verification status, blacklist flag, and `is_imported` for past customers loaded from the rental history |
| `vehicles` | Fleet: brand, model, plate, category, rates, photo, status (available / rented / maintenance / cleaning) |
| `bookings` | Rental requests and their lifecycle: dates, times, service, destination, assigned staff, actual return, late return |
| `inspections` | Pre / post-rental inspections: tools checklist, damage marks, photos, odometer, fuel, fee, signatures, admin verification |
| `notifications` | In-app notifications per user (booking decisions, reminders, requirements) |
| `locations` | Destinations from the price sheet, with per-vehicle-type fees |
| `blacklist_records` | Pardz's blacklist: name, area (CDO/CEB/DVO), source (RN/FB), reason |
| `churn_predictions` | Latest churn label and probability per customer |
| `walk_in_transactions`, `transaction_extensions` | Rentals recorded by the admin, and extra payments |
| `audit_logs` | Who did what (append-only) |
| `seed_runs` | Which one-time company data loads already ran |

Tables are created on start-up. Small changes to existing databases are applied by `init_db()` in `app/database.py`.

## Booking lifecycle and rules

```
pending ──accept (+ staff)──► accepted ──start──► ongoing ──complete──► completed
   │                             │
   └──reject (reason)──► rejected ──customer edits & resubmits──► pending
```

- **New booking:**
  - cannot start in the past;
  - the end date must not be before the start date;
  - pickup/drop-off service needs a location;
  - the vehicle must not be under maintenance or still being cleaned.
- **No double booking:** an accepted or ongoing booking blocks the same vehicle on overlapping dates. This is checked in
  Python and by a PostgreSQL `EXCLUDE` constraint. Rentals recorded on Transactions also block the car.
- **Blacklist:** a blocked account cannot sign in or book. Name matches with Pardz's blacklist are flagged to the admin.
- **Accept** requires the customer's requirements to be verified.
- **Start / complete:** only the assigned staff member can start or complete a rental. Completing it needs a post-rental
  inspection, and a return after the end date sets `late_return`.
- **Back to the fleet:** a returned car becomes available only after the admin verifies the post-rental inspection and cleaning is marked done.
- **Return reminders:** 2 hours and 30 minutes before a rental is due back, sent once each. The customer gets an in-app
  notification and an email; for pickup/drop-off the message asks them to have the car ready for collection.
  Every admin and the assigned staff get an in-app notification with the customer's name and number.

## Company data loaded once

`app/services/company_data.py` runs on start-up and loads each item only once per database (recorded in `seed_runs`):

| Load | Content |
|---|---|
| `destinations` | 47 destinations of the price sheet (CDO … Dapitan, Camiguin) |
| `blacklist_page_1`, `blacklist_page_2` | Pardz's printed blacklist, 96 names |
| `rental_history` | `app/data/rental_history.csv`: 542 completed rentals of 186 past customers |

Deleting a customer, blacklist entry or destination later is permanent; restarts and redeploys do not bring it back.

About the past customers:
- they are verified (their IDs were checked in person) and cannot sign in;
- **no email is ever sent to them**: their addresses came with the records and were never confirmed by them;
- the cars of the history are hidden from the fleet.

## Customer Churn Prediction

`app/ml/churn.py` — **Random Forest Classifier** (scikit-learn).

- **Features (6):**
  - days since last rental;
  - completed rentals;
  - average days between rentals;
  - late-return rate;
  - preferred service type;
  - customer tenure.

  Rental price is not used: WheelSync no longer records prices.
- **Labels (180-day rule):** the history is cut at several past dates. A customer with 2+ returned rentals at that date
  is *churned* if they did not start another rental within the next 180 days. Customers in the middle of a rental at the cut are skipped.
- **Training:**
  - 200 trees, maximum depth 4, at least 10 customers per leaf, balanced class weights;
  - stratified 80/20 split and stratified 5-fold cross-validation, both grouped by customer.
- **Threshold:** chosen on the cross-validated training predictions as the highest cut-off that still reaches **recall ≥ 75%**
  on churned customers (currently 40%).
- **Output per customer:**
  - **At Risk of Churn** or **Likely to Return**, with a probability;
  - **Valuable – At Risk** for at-risk customers in the top 25% by completed rentals;
  - **New Customer – Not Yet Rated** below 2 completed rentals.
- **Retrain** after new rentals are completed: admin **Customers → ⓘ → Retrain model**, or `python -m scripts.train_churn_model`.
  The running API picks up a new model file automatically.

Latest scores (see `app/ml/churn_model.json`): test recall 80%, cross-validated recall 75%, AUC-ROC 0.76 to 0.79.

## Endpoints

Everything needs `Authorization: Bearer <token>` except sign-up, login, email activation, password reset, `/health`,
public destinations and the rental agreement terms. The full list with request shapes is at `/docs`.

| Area | Endpoints | Who |
|---|---|---|
| Auth | `POST /auth/signup`, `GET/POST /auth/verify-email`, `POST /auth/resend-verification`, `POST /auth/forgot-password`, `POST /auth/reset-password`, `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | public / signed in |
| Customer profile | `PUT /me/age`, `POST /me/verification` (ID details + 4 documents) | customer |
| Vehicles | `GET /vehicles`, `GET /vehicles/unavailable-dates`, `POST /vehicles`, `PATCH`/`DELETE /vehicles/{id}`, `POST /vehicles/images` | read: all; change: admin |
| Bookings | `GET /bookings`, `POST /bookings`, `GET`/`PATCH /bookings/{id}`, `PUT /bookings/{id}/resubmit` | role-scoped |
| Booking decisions | `POST /bookings/{id}/accept`, `/reject`, `/assign`, `/send-approval-email`, `GET /bookings/{id}/print-pack` | admin |
| Rental work | `POST /bookings/{id}/start`, `/complete`, `/inspections`, `/inspection-photos`, `/cleaning-done`; `GET /bookings/{id}/inspections` | assigned staff |
| Inspections & cleaning | `GET /bookings/inspection-queue`, `GET /bookings/returns-due`, `POST /bookings/{id}/inspections/{iid}/verify`, `POST /bookings/{id}/cleaner` | admin |
| Customers | `GET /admin/customers`, `PATCH`/`DELETE /admin/customers/{id}`, `PATCH …/{id}/blacklist`, `POST …/{id}/verification`, `GET …/{id}/documents`, `GET /admin/customers/booking-history`, `GET /admin/customers/blacklist-matches` | admin |
| Blacklist records | `GET`/`POST /admin/blacklist-records`, `PATCH`/`DELETE /admin/blacklist-records/{id}` | admin |
| Churn | `GET /admin/churn/predictions`, `GET /admin/churn/model`, `POST /admin/churn/retrain` | admin |
| Staff | `GET`/`POST /admin/staff`, `PATCH /admin/staff/{id}` (edit, reset password) | admin |
| Dashboard & audit | `GET /admin/dashboard`, `GET /admin/audit-logs` | admin |
| Transactions | `GET`/`POST /admin/transactions`, `PUT`/`DELETE /admin/transactions/{id}`, `POST …/{id}/extensions`, `DELETE …/{id}/extensions/{eid}` | admin |
| Destinations | `GET /public/locations`, `GET`/`POST /admin/locations`, `PUT`/`DELETE /admin/locations/{id}` | public / admin |
| Notifications | `GET /notifications`, `GET /notifications/unread-count`, `POST /notifications/{id}/read` | signed in |
| Files | `GET /files/vehicle-images/…`, `/files/inspection-photos/…`, `/files/customer-documents/…` (signed links) | — |
| Other | `GET /public/rental-agreement`, `GET /health`, `WS /ws?token=…` | — |

Bookings come back with nested `profiles` (customer) and `vehicles`. What a role sees inside `profiles` differs:
the admin sees ID details, staff see name and phone, customers see no customer block.

## Scripts

Run from this folder with the virtual environment, e.g. `.venv\Scripts\python.exe -m scripts.train_churn_model`.

| Command | Does |
|---|---|
| `python -m scripts.create_admin "Name" email password` | Creates an admin account |
| `python -m scripts.train_churn_model` | Retrains the churn model and prints its scores |
| `python -m scripts.import_rental_history` | Reloads `app/data/rental_history.csv` (replaces the previous load) |
| `python -m scripts.import_rental_history --remove` | Removes the imported past customers, their bookings and hidden cars |

## Tests

```bat
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
```

The suite has 40 tests and runs against a real PostgreSQL database named `wheelsync_test`. It covers:
- sign-up, activation, login and roles;
- the full booking lifecycle and overlap protection;
- inspections, cleaning and reminders;
- the blacklist, deleting customers, and the one-time data loads;
- emails;
- churn training and prediction.

**It drops and recreates all tables in that database — never point it at real data.**

## Deployment notes

- **Single worker:** run one uvicorn worker, because the live-update hub is in-process.
- **Files:** they live on disk under `UPLOAD_DIR`. Back it up together with the database.
- **Sessions:** 12-hour tokens with no refresh token; users sign in again after they expire.
- **Production:** set `ENVIRONMENT=production`, a strong `JWT_SECRET`, the public HTTPS address in `PUBLIC_BASE_URL`,
  and the web apps' addresses in `CORS_ORIGINS`.
