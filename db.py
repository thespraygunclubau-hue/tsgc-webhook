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


_schema_ready = False


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg2.connect(DATABASE_URL)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn):
    """
    Small additions the app needs on top of schema.sql, created
    automatically the first time the app connects — nothing to run in
    Supabase by hand. Safe to run repeatedly.

      deleted_cards        Trello cards whose entries were deleted in the
                           app, so the Trello sync doesn't bring them back.
      customers.edited_at  set when a customer / entry is edited in the
      machines.edited_at   app, so the Trello sync doesn't overwrite it.
      customers.address1 / city / state / postal_code   customer address.
    """
    global _schema_ready
    if _schema_ready:
        return
    try:
        _run_schema_additions(conn)
        _schema_ready = True
    except Exception as e:
        # Never let this block the app (or the webhook) — log and retry
        # on the next connection.
        conn.rollback()
        print("SCHEMA SETUP ERROR:", repr(e))


def _run_schema_additions(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            create table if not exists deleted_cards (
                trello_card_id text primary key,
                deleted_at timestamptz not null default now()
            );
            alter table customers add column if not exists edited_at timestamptz;
            alter table machines add column if not exists edited_at timestamptz;
            alter table customers add column if not exists address1 text;
            alter table customers add column if not exists city text;
            alter table customers add column if not exists state text;
            alter table customers add column if not exists postal_code text;
            create table if not exists ghl_pull_status (
                id int primary key,
                data jsonb not null,
                updated_at timestamptz not null default now()
            );
            """
        )
    conn.commit()


def _clean(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


# --------------------------------------------------------------------
# Templates + app-only deletes
# --------------------------------------------------------------------

def is_template_name(name):
    """Trello template cards (e.g. "TEMPLATE - Drop Off") must never show
    up as customers."""
    return (name or "").strip().lower().startswith("template")


def is_card_deleted(trello_card_id):
    if not trello_card_id:
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 from deleted_cards where trello_card_id = %s", (trello_card_id,))
            found = cur.fetchone() is not None
            conn.commit()
            return found
    finally:
        conn.close()


def _remember_deleted_cards(cur, card_ids):
    for cid in card_ids:
        if cid:
            cur.execute(
                "insert into deleted_cards (trello_card_id) values (%s) on conflict do nothing",
                (cid,),
            )


def delete_machine(machine_id):
    """Delete one service entry from the app only (Trello is not touched).
    Returns the customer_id it belonged to, or None if not found."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "delete from machines where id = %s returning customer_id, trello_card_id",
                (machine_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            customer_id, card_id = row
            _remember_deleted_cards(cur, [card_id])
            conn.commit()
            return str(customer_id)
    finally:
        conn.close()


def delete_customer(customer_id):
    """Delete a customer and all their entries from the app only
    (Trello is not touched). Returns True if a customer was deleted."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select trello_card_id from machines where customer_id = %s", (customer_id,))
            card_ids = [r[0] for r in cur.fetchall()]
            _remember_deleted_cards(cur, card_ids)
            # machines rows go with it via "on delete cascade"
            cur.execute("delete from customers where id = %s", (customer_id,))
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
    finally:
        conn.close()


ADDRESS_FIELDS = ["address1", "city", "state", "postal_code"]


def _clean_address(address):
    """address: dict with any of address1 / city / state / postal_code.
    Returns a cleaned dict, or None if every part is blank."""
    if not address:
        return None
    cleaned = {k: _clean(address.get(k)) for k in ADDRESS_FIELDS}
    return cleaned if any(cleaned.values()) else None


def upsert_customer(full_name, phone, email, business_name, address=None):
    phone = _clean(phone)
    email = _clean(email)
    full_name = _clean(full_name) or "Unknown"
    business_name = _clean(business_name)
    address = _clean_address(address)

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
                has_address = any(existing.get(k) for k in ADDRESS_FIELDS)
                if existing.get("edited_at"):
                    # Edited by staff in the app — keep their version and
                    # only fill in anything that's still blank.
                    new_phone = existing["phone"] or phone
                    new_email = existing["email"] or email
                    new_business = existing["business_name"] or business_name
                    new_name = existing["full_name"]
                    use_address = address if (address and not has_address) else None
                else:
                    new_phone = phone or existing["phone"]
                    new_email = email or existing["email"]
                    new_business = business_name or existing["business_name"]
                    new_name = full_name if full_name != "Unknown" else existing["full_name"]
                    # A newly submitted address replaces the old one as a set.
                    use_address = address

                addr = use_address or {k: existing.get(k) for k in ADDRESS_FIELDS}
                cur.execute(
                    """
                    update customers
                    set full_name = %s, phone = %s, email = %s, business_name = %s,
                        address1 = %s, city = %s, state = %s, postal_code = %s,
                        updated_at = now()
                    where id = %s
                    """,
                    (new_name, new_phone, new_email, new_business,
                     addr["address1"], addr["city"], addr["state"], addr["postal_code"],
                     existing["id"]),
                )
                conn.commit()
                return str(existing["id"])

            addr = address or {k: None for k in ADDRESS_FIELDS}
            cur.execute(
                """
                insert into customers (full_name, phone, email, business_name,
                                       address1, city, state, postal_code)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (full_name, phone, email, business_name,
                 addr["address1"], addr["city"], addr["state"], addr["postal_code"]),
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
            if existing and existing.get("edited_at"):
                # Edited by staff in the app — only refresh the Trello
                # link/list, leave the details and customer as they are.
                cur.execute(
                    """
                    update machines
                    set trello_card_url = coalesce(%s, trello_card_url),
                        trello_list_id = coalesce(%s, trello_list_id)
                    where trello_card_id = %s
                    returning id
                    """,
                    [fields.get("trello_card_url"), fields.get("trello_list_id"), trello_card_id],
                )
            elif existing:
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
                where c.full_name not ilike 'template%%' and (
                   c.full_name ilike %s
                   or c.phone ilike %s
                   or c.email ilike %s
                   or c.business_name ilike %s
                   or concat_ws(' ', c.address1, c.city, c.state, c.postal_code) ilike %s
                   or exists (
                        select 1 from machines m2
                        where m2.customer_id = c.id
                          and (m2.machine ilike %s or m2.model ilike %s or m2.serial_number ilike %s)
                   ))
                group by c.id
                order by c.full_name asc
                limit %s
                """,
                (query, query, query, query, query, query, query, query, limit),
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
                where c.full_name not ilike 'template%%'
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


def list_board_customers(limit=5000):
    """
    Everything the board needs in one query: each customer with their
    service count, latest job type and date, and a lowercase blob of all
    their machine / model / serial values so the live search box can
    match on those too. Alphabetical by name. Templates hidden.
    """
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select c.id, c.full_name, c.phone, c.email, c.business_name, c.created_at,
                       c.address1, c.city, c.state, c.postal_code,
                       count(m.id) as service_count,
                       count(m.id) filter (where m.form_type = 'hire') as hire_count,
                       count(m.id) filter (where m.form_type is distinct from 'hire') as dropoff_count,
                       max(m.created_at) as last_service_at,
                       (select m3.form_type from machines m3
                         where m3.customer_id = c.id
                         order by m3.created_at desc limit 1) as latest_type,
                       (select m4.machine from machines m4
                         where m4.customer_id = c.id
                         order by m4.created_at desc limit 1) as latest_machine,
                       lower(coalesce(string_agg(
                           concat_ws(' ', m.machine, m.model, m.serial_number), ' '), '')) as machine_text
                from customers c
                left join machines m on m.customer_id = c.id
                where c.full_name not ilike 'template%%'
                group by c.id
                order by lower(c.full_name) asc, c.created_at asc
                limit %s
                """,
                (limit,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------
# Editing (app only — Trello cards are not changed)
# --------------------------------------------------------------------

class DuplicateContact(ValueError):
    """Raised when an edit would give a customer a phone/email that
    another customer already has."""


def get_customer(customer_id):
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("select * from customers where id = %s", (customer_id,))
            return cur.fetchone()
    finally:
        conn.close()


def update_customer(customer_id, full_name, phone, email, business_name, address=None):
    addr = {k: _clean((address or {}).get(k)) for k in ADDRESS_FIELDS}
    full_name = _clean(full_name)
    if not full_name:
        raise ValueError("Name can't be empty.")
    phone, email, business_name = _clean(phone), _clean(email), _clean(business_name)

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for col, val in (("phone", phone), ("email", email)):
                if val:
                    cur.execute(
                        f"select full_name from customers where {col} = %s and id <> %s",
                        (val, customer_id),
                    )
                    clash = cur.fetchone()
                    if clash:
                        raise DuplicateContact(
                            f"That {col} already belongs to {clash['full_name']}."
                        )
            cur.execute(
                """
                update customers
                set full_name = %s, phone = %s, email = %s, business_name = %s,
                    address1 = %s, city = %s, state = %s, postal_code = %s,
                    updated_at = now(), edited_at = now()
                where id = %s
                """,
                (full_name, phone, email, business_name,
                 addr["address1"], addr["city"], addr["state"], addr["postal_code"],
                 customer_id),
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated
    finally:
        conn.close()


EDITABLE_MACHINE_FIELDS = [
    "form_type", "machine", "model", "serial_number", "symptoms",
    "hire_date", "return_date", "hire_charge", "security_deposit", "accessories",
]


def update_machine(machine_id, fields):
    """Returns the customer_id the entry belongs to, or None if not found."""
    values = [_clean(fields.get(c)) for c in EDITABLE_MACHINE_FIELDS]
    ft_index = EDITABLE_MACHINE_FIELDS.index("form_type")
    values[ft_index] = "hire" if values[ft_index] == "hire" else "dropoff"

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            set_clause = ", ".join(f"{c} = %s" for c in EDITABLE_MACHINE_FIELDS)
            cur.execute(
                f"""
                update machines set {set_clause}, edited_at = now()
                where id = %s
                returning customer_id
                """,
                values + [machine_id],
            )
            row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else None
    finally:
        conn.close()


# --------------------------------------------------------------------
# Address backfill from GHL
# --------------------------------------------------------------------

def list_customers_missing_address():
    """Customers with no address at all (and an email or phone to look
    them up by). Templates skipped."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                select id, full_name, phone, email from customers
                where full_name not ilike 'template%%'
                  and coalesce(address1, '') = '' and coalesce(city, '') = ''
                  and coalesce(state, '') = '' and coalesce(postal_code, '') = ''
                  and (coalesce(phone, '') <> '' or coalesce(email, '') <> '')
                order by full_name
                """
            )
            return cur.fetchall()
    finally:
        conn.close()


def count_customers_missing_address():
    return len(list_customers_missing_address())


def fill_address(customer_id, address):
    """Sets the address only if the customer still has none — never
    overwrites an address someone has since typed in."""
    addr = _clean_address(address)
    if not addr:
        return False
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                update customers
                set address1 = %s, city = %s, state = %s, postal_code = %s, updated_at = now()
                where id = %s
                  and coalesce(address1, '') = '' and coalesce(city, '') = ''
                  and coalesce(state, '') = '' and coalesce(postal_code, '') = ''
                """,
                (addr["address1"], addr["city"], addr["state"], addr["postal_code"], customer_id),
            )
            done = cur.rowcount > 0
            conn.commit()
            return done
    finally:
        conn.close()


def get_pull_status():
    """Returns (status dict or None, seconds since last update or None)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select data, extract(epoch from now() - updated_at) from ghl_pull_status where id = 1"
            )
            row = cur.fetchone()
            conn.commit()
            return (row[0], float(row[1])) if row else (None, None)
    finally:
        conn.close()


def save_pull_status(data):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into ghl_pull_status (id, data, updated_at) values (1, %s, now())
                on conflict (id) do update set data = excluded.data, updated_at = now()
                """,
                (psycopg2.extras.Json(data),),
            )
            conn.commit()
    finally:
        conn.close()
