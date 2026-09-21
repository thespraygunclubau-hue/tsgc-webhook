"""
One-off backfill: pulls existing cards from your Trello board(s) and
loads them into the registry database, so historical customers show up
in search alongside new submissions.

Safe to re-run: it upserts customers the same way the webhook does
(matched by phone/email), and skips a machine entry if a row with the
same trello_card_id already exists.

Usage:
    python import_trello.py <BOARD_ID> [<BOARD_ID_2> ...]

Where BOARD_ID is the ID (not the short link) of each Trello board that
holds Drop-Off and/or Hire cards — e.g. your main workflow board(s).
Get a board's ID by opening it in Trello, adding ".json" to the URL,
and reading the "id" field, or via https://api.trello.com/1/members/me/boards
with your key/token.

Requires the same env vars as app.py: TRELLO_KEY, TRELLO_TOKEN,
DATABASE_URL. Run it from your machine or a Render shell — it does not
touch the webhook.
"""

import os
import re
import sys
import requests

import db

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")

# Maps the emoji-prefixed lines app.py writes into card descriptions
# back to field names. If a card's description doesn't match this
# format (e.g. it was edited by hand in Trello), it's still imported
# with whatever fields ARE found, and the rest are left blank.
FIELD_PATTERNS = {
    "full_name":        r"👤 Customer:\s*(.*)",
    "phone":            r"📱 Phone:\s*(.*)",
    "email":            r"📧 Email:\s*(.*)",
    "business_name":    r"🏢 Business:\s*(.*)",
    "machine":          r"🔧 Machine:\s*(.*)",
    "model":            r"📋 Model:\s*(.*)",
    "serial_number":    r"🔢 Serial:\s*(.*)",
    "symptoms":         r"⚠️ Issue:\s*(.*)",
    "accessories":      r"📦 Accessories:\s*(.*)",
    "hire_date":        r"📅 Hire Date:\s*(.*)",
    "return_date":      r"📅 Return Date:\s*(.*)",
    "hire_charge":      r"💰 Hire Charge:\s*(.*)",
    "security_deposit": r"🔒 Security Deposit:\s*(.*)",
}


def parse_description(desc):
    fields = {}
    for key, pattern in FIELD_PATTERNS.items():
        m = re.search(pattern, desc or "")
        fields[key] = m.group(1).strip() if m else ""
    return fields


def fetch_cards(board_id):
    url = f"https://api.trello.com/1/boards/{board_id}/cards"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "fields": "id,name,desc,shortUrl,idList",
    }
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
            full_name = fields.get("full_name") or card.get("name") or "Unknown"
            form_type = "hire" if (fields.get("hire_date") or fields.get("hire_charge")) else "dropoff"

            customer_id = db.upsert_customer(
                full_name,
                fields.get("phone"),
                fields.get("email"),
                fields.get("business_name"),
            )
            db.insert_machine(customer_id, {
                "form_type": form_type,
                "machine": fields.get("machine"),
                "model": fields.get("model"),
                "serial_number": fields.get("serial_number"),
                "symptoms": fields.get("symptoms"),
                "hire_date": fields.get("hire_date"),
                "return_date": fields.get("return_date"),
                "hire_charge": fields.get("hire_charge"),
                "security_deposit": fields.get("security_deposit"),
