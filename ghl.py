"""
Looks up customer addresses in GoHighLevel (GHL).

Needs two environment variables on Render:
  GHL_API_TOKEN     a Private Integration token with the "View Contacts"
                    (contacts.readonly) scope — GHL: Settings -> Private
                    Integrations -> Create new integration.
  GHL_LOCATION_ID   your sub-account's Location ID — it's the code after
                    /location/ in the GHL address bar.

If either is missing, lookups are skipped and nothing breaks.

Used by:
  - the "Pull addresses from GHL" button on the board (fills in every
    customer that has no address yet), and
  - the /webhook route, when a form submission arrives without an address.
"""

import os
import re
import threading
import time

import requests

import db

API_BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"

GHL_API_TOKEN = os.environ.get("GHL_API_TOKEN")
GHL_LOCATION_ID = os.environ.get("GHL_LOCATION_ID")


def configured():
    return bool(GHL_API_TOKEN and GHL_LOCATION_ID)


def _headers():
    token = GHL_API_TOKEN or ""
    if not token.lower().startswith("bearer "):
        token = f"Bearer {token}"
    return {"Authorization": token, "Version": API_VERSION, "Accept": "application/json"}


def _get(path, params):
    """GET with a simple retry when GHL says we're going too fast (429)."""
    for attempt in range(3):
        resp = requests.get(f"{API_BASE}{path}", headers=_headers(), params=params, timeout=15)
        if resp.status_code == 429:
            time.sleep(2 + attempt * 3)
            continue
        return resp
    return resp


def phone_variants(phone):
    """GHL stores phones in international format (+61412345678); the app
    stores them however the form sent them (0412 345 678). Try both."""
    if not phone:
        return []
    raw = phone.strip()
    digits = re.sub(r"[^\d+]", "", raw)
    variants = []
    if digits.startswith("+"):
        variants.append(digits)
    elif digits.startswith("61") and len(digits) >= 11:
        variants.append("+" + digits)
    elif digits.startswith("0") and len(digits) == 10:
        variants.append("+61" + digits[1:])      # Australian mobile / landline
    if digits and digits not in variants:
        variants.append(digits)
    return variants


def _address_from_contact(contact):
    if not contact:
        return None
    addr = {
        "address1": contact.get("address1") or contact.get("address") or "",
        "city": contact.get("city") or "",
        "state": contact.get("state") or "",
        "postal_code": contact.get("postalCode") or contact.get("postal_code") or "",
    }
    return addr if any(v.strip() for v in addr.values()) else None


def get_contact_by_id(contact_id):
    resp = _get(f"/contacts/{contact_id}", {})
    if resp.status_code != 200:
        return None
    return (resp.json() or {}).get("contact")


def find_contact(email=None, phone=None):
    """Exact match by email first, then by each phone format."""
    tries = []
    if email:
        tries.append({"email": email.strip()})
    for p in phone_variants(phone):
        tries.append({"number": p})

    for extra in tries:
        params = {"locationId": GHL_LOCATION_ID, **extra}
        resp = _get("/contacts/search/duplicate", params)
        if resp.status_code == 401 or resp.status_code == 403:
            raise PermissionError(
                f"GHL refused the request ({resp.status_code}) — check GHL_API_TOKEN "
                "has the View Contacts scope and GHL_LOCATION_ID is right."
            )
        if resp.status_code != 200:
            print("GHL LOOKUP", extra, "->", resp.status_code, resp.text[:200])
            continue
        contact = (resp.json() or {}).get("contact")
        if contact:
            return contact
        time.sleep(0.12)  # stay well under GHL's rate limit
    return None


def lookup_address(email=None, phone=None, contact_id=None):
    """Returns an address dict, or None if GHL has none / isn't set up."""
    if not configured():
        return None
    contact = get_contact_by_id(contact_id) if contact_id else None
    if not contact:
        contact = find_contact(email, phone)
    return _address_from_contact(contact)


# --------------------------------------------------------------------
# Bulk pull, run in the background so the page doesn't time out
# --------------------------------------------------------------------

_status_lock = threading.Lock()
_status = {"running": False, "total": 0, "done": 0, "filled": 0,
           "not_found": 0, "errors": 0, "message": "", "finished_at": None}


def status():
    with _status_lock:
        return dict(_status)


def _set(**kw):
    with _status_lock:
        _status.update(kw)


def start_pull():
    """Starts the bulk pull if it isn't already running. Returns
    (started: bool, message: str)."""
    if not configured():
        return False, "GHL isn't connected yet — add GHL_API_TOKEN and GHL_LOCATION_ID on Render."
    with _status_lock:
        if _status["running"]:
            return False, "Already pulling addresses — hang tight."
        _status.update(running=True, total=0, done=0, filled=0, not_found=0,
                       errors=0, message="Starting…", finished_at=None)
    threading.Thread(target=_pull_all, daemon=True).start()
    return True, "Pulling addresses from GHL…"


def _pull_all():
    try:
        customers = db.list_customers_missing_address()
        _set(total=len(customers), message="")
        for c in customers:
            try:
                addr = lookup_address(c.get("email"), c.get("phone"))
                if addr:
                    db.fill_address(c["id"], addr)
                    with _status_lock:
                        _status["filled"] += 1
                else:
                    with _status_lock:
                        _status["not_found"] += 1
            except PermissionError as e:
                _set(message=str(e))
                print("GHL PULL STOPPED:", e)
                return
            except Exception as e:
                print("GHL PULL ERROR for", c.get("full_name"), repr(e))
                with _status_lock:
                    _status["errors"] += 1
            with _status_lock:
                _status["done"] += 1
            time.sleep(0.12)
    except Exception as e:
        print("GHL PULL FAILED:", repr(e))
        _set(message="Pull failed — check server logs.")
    finally:
        _set(running=False, finished_at=time.time())
