-- WheelSync PostgreSQL schema (generated from app/models.py).
-- The API creates all of this automatically on start-up; use this file only if you prefer to
-- provision the database manually:   psql "$DATABASE_URL" -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS btree_gist;

--
--
\restrict sBFYfZ8QZauiTseJEVidt45J1vWGUvQ4OgUZalyPxKdb1Ai0jiEp9ptp3ugzGLJ
--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;
--
-- Name: EXTENSION btree_gist; Type: COMMENT; Schema: -; Owner: -
--
COMMENT ON EXTENSION btree_gist IS 'support for indexing common datatypes in GiST';
--
-- Name: booking_status; Type: TYPE; Schema: public; Owner: -
--
CREATE TYPE public.booking_status AS ENUM (
    'pending',
    'accepted',
    'rejected',
    'ongoing',
    'completed',
    'cancelled'
);
--
-- Name: inspection_type; Type: TYPE; Schema: public; Owner: -
--
CREATE TYPE public.inspection_type AS ENUM (
    'pre_rental',
    'post_rental'
);
--
-- Name: user_role; Type: TYPE; Schema: public; Owner: -
--
CREATE TYPE public.user_role AS ENUM (
    'admin',
    'staff',
    'customer'
);
--
-- Name: vehicle_status; Type: TYPE; Schema: public; Owner: -
--
CREATE TYPE public.vehicle_status AS ENUM (
    'available',
    'rented',
    'maintenance',
    'cleaning'
);
--
-- Name: bookings; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.bookings (
    id uuid NOT NULL,
    customer_id uuid NOT NULL,
    vehicle_id uuid NOT NULL,
    assigned_staff_id uuid,
    start_date date NOT NULL,
    end_date date NOT NULL,
    pickup_time time without time zone,
    return_time time without time zone,
    pickup_location text,
    destination_from text,
    destination_to text,
    service_type text DEFAULT 'self_drive'::text NOT NULL,
    distance_km numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    actual_return_date date,
    late_return boolean DEFAULT false NOT NULL,
    status public.booking_status NOT NULL,
    total_amount numeric(10,2),
    notes text,
    rejection_reason text,
    decided_by uuid,
    decided_at timestamp with time zone,
    cleaning_staff_id uuid,
    cleaning_done_at timestamp with time zone,
    return_reminder_2h_at timestamp with time zone,
    return_reminder_30m_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT bookings_dates_check CHECK ((start_date <= end_date)),
    CONSTRAINT bookings_distance_km_check CHECK ((distance_km >= (0)::numeric))
);
--
-- Name: inspections; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.inspections (
    id uuid NOT NULL,
    booking_id uuid NOT NULL,
    staff_id uuid NOT NULL,
    type public.inspection_type NOT NULL,
    checklist jsonb DEFAULT '{}'::jsonb NOT NULL,
    damage_notes text,
    damage_fee numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    odometer_reading integer,
    fuel_level text,
    photos jsonb DEFAULT '[]'::jsonb NOT NULL,
    tools jsonb DEFAULT '{}'::jsonb NOT NULL,
    damage_marks jsonb DEFAULT '[]'::jsonb NOT NULL,
    renter_name text,
    renter_signed boolean DEFAULT false NOT NULL,
    staff_name text,
    staff_signed boolean DEFAULT false NOT NULL,
    verified_by uuid,
    verified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT inspections_damage_fee_check CHECK ((damage_fee >= (0)::numeric))
);
--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.notifications (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    title text NOT NULL,
    body text,
    kind text DEFAULT 'general'::text NOT NULL,
    booking_id uuid,
    data jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_read boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
--
-- Name: profiles; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.profiles (
    id uuid NOT NULL,
    full_name text NOT NULL,
    email character varying(320) NOT NULL,
    password_hash text NOT NULL,
    phone text,
    email_verified boolean DEFAULT true NOT NULL,
    email_verification_token_hash character varying(64),
    email_verification_expires_at timestamp with time zone,
    password_reset_code_hash character varying(64),
    password_reset_expires_at timestamp with time zone,
    password_reset_attempts integer DEFAULT 0 NOT NULL,
    age integer,
    address text,
    role public.user_role NOT NULL,
    is_blacklisted boolean DEFAULT false NOT NULL,
    id_type text,
    id_number text,
    id_photo_path text,
    second_id_type text,
    second_id_number text,
    second_id_photo_path text,
    proof_of_address_path text,
    proof_of_income_path text,
    extra_document_1_path text,
    extra_document_2_path text,
    verification_status text DEFAULT 'unsubmitted'::text NOT NULL,
    verification_notes text,
    verified_by uuid,
    verified_at timestamp with time zone,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT profiles_age_check CHECK (((age IS NULL) OR ((age >= 1) AND (age <= 120)))),
    CONSTRAINT profiles_verification_status_check CHECK ((verification_status = ANY (ARRAY['unsubmitted'::text, 'pending'::text, 'verified'::text, 'rejected'::text])))
);
--
-- Name: vehicles; Type: TABLE; Schema: public; Owner: -
--
CREATE TABLE public.vehicles (
    id uuid NOT NULL,
    make text NOT NULL,
    model text NOT NULL,
    year integer NOT NULL,
    plate_number text NOT NULL,
    category text DEFAULT 'sedan'::text NOT NULL,
    description text,
    daily_rate numeric(10,2) NOT NULL,
    price_per_km numeric(10,2) DEFAULT '0'::numeric NOT NULL,
    status public.vehicle_status NOT NULL,
    image_url text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);
--
-- Name: bookings bookings_no_active_overlap; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_no_active_overlap EXCLUDE USING gist (vehicle_id WITH =, daterange(start_date, end_date, '[]'::text) WITH &&) WHERE ((status = ANY (ARRAY['accepted'::public.booking_status, 'ongoing'::public.booking_status])));
--
-- Name: bookings bookings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_pkey PRIMARY KEY (id);
--
-- Name: inspections inspections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.inspections
    ADD CONSTRAINT inspections_pkey PRIMARY KEY (id);
--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);
--
-- Name: profiles profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_pkey PRIMARY KEY (id);
--
-- Name: vehicles vehicles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_pkey PRIMARY KEY (id);
--
-- Name: vehicles vehicles_plate_number_key; Type: CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.vehicles
    ADD CONSTRAINT vehicles_plate_number_key UNIQUE (plate_number);
--
-- Name: ix_bookings_customer; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_bookings_customer ON public.bookings USING btree (customer_id);
--
-- Name: ix_bookings_staff; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_bookings_staff ON public.bookings USING btree (assigned_staff_id);
--
-- Name: ix_bookings_status_start; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_bookings_status_start ON public.bookings USING btree (status, start_date);
--
-- Name: ix_notifications_user; Type: INDEX; Schema: public; Owner: -
--
CREATE INDEX ix_notifications_user ON public.notifications USING btree (user_id, is_read);
--
-- Name: ix_profiles_email; Type: INDEX; Schema: public; Owner: -
--
CREATE UNIQUE INDEX ix_profiles_email ON public.profiles USING btree (email);
--
-- Name: bookings bookings_assigned_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_assigned_staff_id_fkey FOREIGN KEY (assigned_staff_id) REFERENCES public.profiles(id);
--
-- Name: bookings bookings_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.profiles(id) ON DELETE CASCADE;
--
-- Name: bookings bookings_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.profiles(id);
--
-- Name: bookings bookings_cleaning_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_cleaning_staff_id_fkey FOREIGN KEY (cleaning_staff_id) REFERENCES public.profiles(id);
--
-- Name: notifications notifications_booking_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_booking_id_fkey FOREIGN KEY (booking_id) REFERENCES public.bookings(id) ON DELETE SET NULL;
--
-- Name: inspections inspections_verified_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.inspections
    ADD CONSTRAINT inspections_verified_by_fkey FOREIGN KEY (verified_by) REFERENCES public.profiles(id);
--
-- Name: bookings bookings_vehicle_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.bookings
    ADD CONSTRAINT bookings_vehicle_id_fkey FOREIGN KEY (vehicle_id) REFERENCES public.vehicles(id);
--
-- Name: inspections inspections_booking_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.inspections
    ADD CONSTRAINT inspections_booking_id_fkey FOREIGN KEY (booking_id) REFERENCES public.bookings(id) ON DELETE CASCADE;
--
-- Name: inspections inspections_staff_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.inspections
    ADD CONSTRAINT inspections_staff_id_fkey FOREIGN KEY (staff_id) REFERENCES public.profiles(id);
--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.profiles(id) ON DELETE CASCADE;
--
-- Name: profiles profiles_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.profiles(id);
--
-- Name: profiles profiles_verified_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--
ALTER TABLE ONLY public.profiles
    ADD CONSTRAINT profiles_verified_by_fkey FOREIGN KEY (verified_by) REFERENCES public.profiles(id);
--
--
\unrestrict sBFYfZ8QZauiTseJEVidt45J1vWGUvQ4OgUZalyPxKdb1Ai0jiEp9ptp3ugzGLJ
