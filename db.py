"""
Database layer for the TSGC Customer & Machine Registry.

Talks to Postgres (Supabase) using psycopg2, no ORM. Two tables:
  customers(id, full_name, phone, email, business_name, ...)
  machines(id, customer_id, ...trello + job details..., trello_card_id, trello_card_url)

A customer is matched by phone first, then email, so the same person
submitting multiple forms accumulates machines under one customer record
instead of creating duplicates.
"""

import os
import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(DATABASE_URL)


def _clean(value):
    """Treat empty string as NULL so matching/searching isn't fooled by ''."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def upsert_customer(full_name, phone, email, business_name):
    """
    Find an existing customer by phone, then by email. If found, fill in
    any newly-provided fields and update them. If not found, create one.
    Returns the customer's id (str).
    """
    phone = _clean(phone)
    email = _clean(email)
    full_name = _clean(full_name) or "Unknown"
    business_name = _clean(business_name)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            existing = None
            if phone:
                cur.execute("select * from customers where phone = %s", (phone,))
                existing = cur.fetchone()
            if not existing and email:
                cur.execute("select * from customers where email = %s", (email,))
                existing = cur.fetchone()

            if existing:
                new_phone = phone or existing["phone"]
                new_email = email or existing["email"]
                new_business = business_name or existing["business_name"]
                new_name = full_name if full_name != "Unknown" else existing["full_name"]
                cur.execute(
                    """
                    update customers
                    set full_name = %s, phone = %s, email = %s,
                        business_name = %s, updated_at = now()
                    where id = %s
                    """,
                    (new_name, new_phone, new_email, new_business, existing["id"]),
                )
                conn.commit()
                return str(existing["id"])

            cur.execute(
                """
                insert into customers (full_name, phone, email, business_name)
                values (%s, %s, %s, %s)
                returning id
                """,
                (full_name, phone, email, business_name),
            )
            new_id = cur.fetchone()["id"]
            conn.commit()
            return str(new_id)
    finally:
        conn.close()


def insert_machine(customer_id, fields):
    """
    fields is a dict with keys matching the machines table columns
    (form_type, machine, model, serial_number, symptoms, hire_date,
    return_date, hire_charge, security_deposit, accessories,
    trello_card_id, trello_card_url, trello_list_id).
    Returns the new machine row's id (str).
    """
    columns = [
        "form_type", "machine", "model", "serial_number", "symptoms",
        "hire_date", "return_date", "hire_charge", "security_deposit",
        "accessories", "trello_card_id", "trello_card_url", "trello_list_id",
    ]
    values = [fields.get(c) for c in columns]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            placeholders = ", ".join(["%s"] * len(columns))
            col_list = ", ".join(columns)
            cur.execute(
                f"""
                insert into machines (customer_id, {col_list})
                values (%s, {placeholders})
                returning id
                """,
                [customer_id] + values,
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            return str(new_id)
    finally:
        conn.close()


def search_customers(query, limit=50):
    """
    Search customers by name, phone, email, or business name, and also
    match on machine/model/serial so a serial number lookup finds the
    right customer. Returns a list of dicts, each with a `machine_count`.
    """
    query = f"%{query.strip()}%"
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select c.*, count(m.id) as machine_count
                from customers c
                left join machines m on m.customer_id = c.id
                where c.full_name ilike %s
                   or c.phone ilike %s
                   or c.email ilike %s
                   or c.business_name ilike %s
                   or exists (
                        select 1 from machines m2
                        where m2.customer_id = c.id
                          and (m2.machine ilike %s or m2.model ilike %s or m2.serial_number ilike %s)
                   )
                group by c.id
                order by c.full_name asc
                limit %s
                """,
                (query, query, query, query, query, query, query, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def get_customer_with_machines(customer_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select * from customers where id = %s", (customer_id,))
            customer = cur.fetchone()
            if not customer:
                return None
            cur.execute(
                "select * from machines where customer_id = %s order by created_at desc",
                (customer_id,),
            )
            machines = cur.fetchall()
            customer = dict(customer)
            customer["machines"] = machines
            return customer
    finally:
        conn.close()



def list_all_customers(limit=500):
    """
    Every customer, alphabetical by name, each with a machine_count —
    used to show the full list below the search box before anyone types.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select c.*, count(m.id) as machine_count
                from customers c
                left join machines m on m.customer_id = c.id
                group by c.id
                order by c.full_name asc
                limit %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()
