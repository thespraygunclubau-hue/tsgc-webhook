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
    if value is None:
        return None
    value = value.strip()
    return value or None


def upsert_customer(full_name, phone, email, business_name):
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


def get_machine_by_trello_card_id(trello_card_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select * from machines where trello_card_id = %s", (trello_card_id,))
            return cur.fetchone()
    finally:
        conn.close()


def upsert_machine_by_card(customer_id, fields):
    """
    Used by the Trello webhook sync (and now the backfill import too).
    Updates the machine row matching fields['trello_card_id'] if one
    already exists — re-linking it to customer_id, which may have
    changed if the card's phone/email was edited — otherwise inserts a
    new row (a card created by hand in Trello, never seen before).
    Returns the machine id (str).
    """
    trello_card_id = fields["trello_card_id"]
    existing = get_machine_by_trello_card_id(trello_card_id)

    columns = [
        "form_type", "machine", "model", "serial_number", "symptoms",
        "hire_date", "return_date", "hire_charge", "security_deposit",
        "accessories", "trello_card_url", "trello_list_id",
    ]
    values = [fields.get(c) for c in columns]

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            if existing:
                set_clause = ", ".join(f"{c} = %s" for c in columns)
                cur.execute(
                    f"""
                    update machines
                    set customer_id = %s, {set_clause}
                    where trello_card_id = %s
                    returning id
                    """,
                    [customer_id] + values + [trello_card_id],
                )
            else:
                all_columns = columns + ["trello_card_id"]
                placeholders = ", ".join(["%s"] * len(all_columns))
                col_list = ", ".join(all_columns)
                cur.execute(
                    f"""
                    insert into machines (customer_id, {col_list})
                    values (%s, {placeholders})
                    returning id
                    """,
                    [customer_id] + values + [trello_card_id],
                )
            new_id = cur.fetchone()[0]
            conn.commit()
            return str(new_id)
    finally:
        conn.close()


def delete_machine_by_trello_card_id(trello_card_id):
    """Returns the number of rows deleted (0 or 1)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("delete from machines where trello_card_id = %s", (trello_card_id,))
            deleted = cur.rowcount
            conn.commit()
            return deleted
    finally:
        conn.close()


def search_customers(query, limit=50):
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
