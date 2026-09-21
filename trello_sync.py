"""
Shared logic for translating a Trello card's current state into registry
database rows. Used by:

  - app.py's /trello-webhook route — real-time sync whenever a card is
    created, edited, moved, or deleted directly in Trello.
  - import_trello.py — the one-off historical backfill.

Keeping this in one place means both paths parse card descriptions and
decide form_type the same way, so they can't drift out of sync with
each other.
"""

import os
import re
import requests

import db

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
HIRE_LIST_ID = os.environ.get("HIRE_LIST_ID")
DROPOFF_LIST_ID = os.environ.get("DROPOFF_LIST_ID")

# Maps the emoji-prefixed lines app.py writes into card descriptions back
# to field names. If a card's description doesn't match this format
# (e.g. it was written by hand in Trello), it still syncs with whatever
# fields ARE found, and the rest are left blank.
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


def fetch_card(card_id):
    """Fetch a single card's current state straight from Trello — used
    instead of trusting the webhook payload, which only includes the
    fields that changed, not the full card."""
    url = f"https://api.trello.com/1/cards/{card_id}"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "fields": "id,name,desc,shortUrl,idList,closed",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def form_type_for_list(list_id, fields):
    """Prefer the list the card sits in (authoritative — this is how a
    human moving a card between Drop-Off and Hire lists is reflected).
    Falls back to whether hire-only fields are present, for boards using
    lists other than the two configured ones."""
    if HIRE_LIST_ID and list_id == HIRE_LIST_ID:
        return "hire"
    if DROPOFF_LIST_ID and list_id == DROPOFF_LIST_ID:
        return "dropoff"
    return "hire" if (fields.get("hire_date") or fields.get("hire_charge")) else "dropoff"


def sync_card(card_id, card=None):
    """
    Pull a card's current state and write it into the registry: upserts
    the customer, then updates the existing machine row for this card,
    or inserts a new one if it wasn't in the DB yet (e.g. a card created
    by hand in Trello rather than through the GHL form).

    Pass `card` (a dict already fetched, e.g. during a bulk import) to
    skip the extra API call.

    Returns (customer_id, machine_id).
    """
    if card is None:
        card = fetch_card(card_id)

    fields = parse_description(card.get("desc", ""))
    full_name = fields.get("full_name") or card.get("name") or "Unknown"
    list_id = card.get("idList")
    form_type = form_type_for_list(list_id, fields)

    customer_id = db.upsert_customer(
        full_name,
        fields.get("phone"),
        fields.get("email"),
        fields.get("business_name"),
    )

    machine_fields = {
        "form_type": form_type,
        "machine": fields.get("machine"),
        "model": fields.get("model"),
        "serial_number": fields.get("serial_number"),
        "symptoms": fields.get("symptoms"),
        "hire_date": fields.get("hire_date"),
        "return_date": fields.get("return_date"),
        "hire_charge": fields.get("hire_charge"),
        "security_deposit": fields.get("security_deposit"),
        "accessories": fields.get("accessories"),
        "trello_card_id": card_id,
        "trello_card_url": card.get("shortUrl"),
        "trello_list_id": list_id,
    }

    machine_id = db.upsert_machine_by_card(customer_id, machine_fields)
    return customer_id, machine_id


def delete_card(card_id):
    """A card was permanently deleted in Trello — remove its machine row
    too. (Archiving a card is a different action — updateCard with
    closed=true — and is left alone; archived cards still sync normally.)"""
    return db.delete_machine_by_trello_card_id(card_id)
