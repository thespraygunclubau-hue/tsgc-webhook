"""
One-off backfill: pulls existing cards from your Trello board(s) and
loads them into the registry database, so historical customers show up
in search alongside new submissions.

Reads structured fields from the card description when present (cards
created by the webhook, with lines like "Machine:"), and falls back to
parsing the card TITLE (e.g. "Drop-Off — Daniel DPM | Wagner PS3.25")
for older cards created by hand in Trello that don't have that format.

Safe to re-run: it upserts customers the same way the webhook does
(matched by phone/email), and skips a machine entry if a row with the
same trello_card_id already exists.

Usage:
    python import_trello.py <BOARD_ID> [<BOARD_ID_2> ...]
"""

import os
import re
import sys
import requests

import db

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")

FIELD_PATTERNS = {
    "full_name": r"👤 Customer:\s*(.*)",
    "phone": r"📱 Phone:\s*(.*)",
    "email": r"📧 Email:\s*(.*)",
    "business_name": r"🏢 Business:\s*(.*)",
    "machine": r"🔧 Machine:\s*(.*)",
    "model": r"📋 Model:\s*(.*)",
    "serial_number": r"🔢 Serial:\s*(.*)",
    "symptoms": r"⚠️ Issue:\s*(.*)",
    "accessories": r"📦 Accessories:\s*(.*)",
    "hire_date": r"📅 Hire Date:\s*(.*)",
    "return_date": r"📅 Return Date:\s*(.*)",
    "hire_charge": r"💰 Hire Charge:\s*(.*)",
    "security_deposit": r"🔒 Security Deposit:\s*(.*)",
}

CARD_TITLE_RE = re.compile(r"^\s*(?:Drop-Off|Hire)\s*[—-]\s*(?P<customer>.+?)\s*\|\s*(?P<machine>.+?)\s*$")


def parse_description(desc):
    fields = {}
    for key, pattern in FIELD_PATTERNS.items():
        m = re.search(pattern, desc or "")
        fields[key] = m.group(1).strip() if m else ""
    return fields


def parse_card_title(name):
    m = CARD_TITLE_RE.match(name or "")
    if m:
        return m.group("customer").strip(), m.group("machine").strip()
    return None, None


def fetch_cards(board_id):
    url = f"https://api.trello.com/1/boards/{board_id}/cards"
    params = {"key": TRELLO_KEY, "token": TRELLO_TOKEN, "fields": "id,name,desc,shortUrl,idList"}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def machine_already_imported(trello_card_id):
    conn = db.get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 from machines where trello_card_id = %s", (trello_card_id,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def import_board(board_id):
    print(f"\nFetching cards from board {board_id}...")
    cards = fetch_cards(board_id)
    print(f"Found {len(cards)} cards.")

    imported, skipped, errors = 0, 0, 0

    for card in cards:
        card_id = card["id"]
        try:
            if machine_already_imported(card_id):
                skipped += 1
                continue

            fields = parse_description(card.get("desc", ""))
            title_customer, title_machine = parse_card_title(card.get("name", ""))

            full_name = fields.get("full_name") or title_customer or card.get("name") or "Unknown"
            machine_value = fields.get("machine") or title_machine or ""

            form_type = "hire" if (fields.get("hire_date") or fields.get("hire_charge")) else "dropoff"

            customer_id = db.upsert_customer(full_name, fields.get("phone"), fields.get("email"), fields.get("business_name"))

            machine_fields = {}
            machine_fields["form_type"] = form_type
            machine_fields["machine"] = machine_value
            machine_fields["model"] = fields.get("model")
            machine_fields["serial_number"] = fields.get("serial_number")
            machine_fields["symptoms"] = fields.get("symptoms")
            machine_fields["hire_date"] = fields.get("hire_date")
            machine_fields["return_date"] = fields.get("return_date")
            machine_fields["hire_charge"] = fields.get("hire_charge")
            machine_fields["security_deposit"] = fields.get("security_deposit")
            machine_fields["accessories"] = fields.get("accessories")
            machine_fields["trello_card_id"] = card_id
            machine_fields["trello_card_url"] = card.get("shortUrl")
            machine_fields["trello_list_id"] = card.get("idList")

            db.insert_machine(customer_id, machine_fields)
            imported += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR on card {card_id} ({card.get('name')}): {e}")

    print(f"Board {board_id}: {imported} imported, {skipped} already present, {errors} errors.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if not (TRELLO_KEY and TRELLO_TOKEN and os.environ.get("DATABASE_URL")):
        print("Missing TRELLO_KEY, TRELLO_TOKEN, or DATABASE_URL in your environment.")
        sys.exit(1)

    for board_id in sys.argv[1:]:
        import_board(board_id)

    print("\nDone.")
