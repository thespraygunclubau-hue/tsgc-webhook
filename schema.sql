create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- One row per unique customer. Matched on phone OR email at write time,
-- so the same person submitting multiple forms doesn't create duplicates.
create table if not exists customers (
    id              uuid primary key default gen_random_uuid(),
    full_name       text not null,
    phone           text,
    email           text,
    business_name   text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

-- Lets you find a customer instantly by phone or email during a lookup,
-- and is also what the upsert logic in app.py relies on.
create unique index if not exists customers_phone_unique
    on customers (phone) where phone is not null and phone <> '';
create unique index if not exists customers_email_unique
    on customers (email) where email is not null and email <> '';

-- One row per machine/entry. A customer can have many.
create table if not exists machines (
    id                  uuid primary key default gen_random_uuid(),
    customer_id         uuid not null references customers(id) on delete cascade,
    form_type           text not null default 'dropoff',   -- 'dropoff' or 'hire'
    machine             text,
    model               text,
    serial_number       text,
    symptoms            text,
    hire_date           text,
    return_date         text,
    hire_charge         text,
    security_deposit    text,
    accessories         text,
    trello_card_id      text,
    trello_card_url     text,
    trello_list_id      text,
    created_at          timestamptz not null default now()
);

create index if not exists machines_customer_id_idx on machines (customer_id);
create index if not exists machines_serial_idx on machines (serial_number);
create index if not exists machines_trello_card_id_idx on machines (trello_card_id);

-- Simple search: name, phone, email, business, machine, model, serial —
-- all searchable from one box in the UI.
create index if not exists customers_name_trgm_idx on customers using gin (full_name gin_trgm_ops);
