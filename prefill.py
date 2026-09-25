"""
Builds "Book Again" links: the GHL Drop-Off / Hire survey URL with the
customer's (and optionally the machine's) details added as URL
parameters, so GHL pre-fills those fields when the form opens.

GHL matches each URL parameter to a form field by that field's
"Query Key" (form builder -> click the field -> Query Key). Unknown
parameters are simply ignored by GHL, so each value is sent under a few
likely key names to cover the common setups.

If a field doesn't pre-fill, look up its real Query Key in GHL and set
the PREFILL_KEYS environment variable on Render, e.g.:

    {"machine": "machine_make", "serial_number": "serial_no"}

Any field listed there replaces the defaults below for that field
(use a comma-separated string to send several keys).
"""

import json
import os
from urllib.parse import urlencode

DROPOFF_FORM_URL = os.environ.get(
    "DROPOFF_FORM_URL",
    "https://api.leadconnectorhq.com/widget/survey/Jhya7srE0tWEulKMvoHa",
)
HIRE_FORM_URL = os.environ.get(
    "HIRE_FORM_URL",
    "https://api.leadconnectorhq.com/widget/survey/o0O2QgVcokxQmlXxDu73",
)

# our field -> GHL query key(s) to send it under
DEFAULT_KEYS = {
    "full_name":     ["full_name"],
    "first_name":    ["first_name"],
    "last_name":     ["last_name"],
    "phone":         ["phone"],
    "email":         ["email"],
    "business_name": ["organization", "company_name", "business_name"],
    "address1":      ["address", "address1"],
    "city":          ["city"],
    "state":         ["state"],
    "postal_code":   ["postal_code"],
    "machine":       ["machine"],
    "model":         ["model"],
    "serial_number": ["serial_number"],
}


def _load_keys():
    keys = {k: list(v) for k, v in DEFAULT_KEYS.items()}
    raw = os.environ.get("PREFILL_KEYS")
    if raw:
        try:
            overrides = json.loads(raw)
            for field, value in overrides.items():
                if isinstance(value, str):
                    value = [v.strip() for v in value.split(",") if v.strip()]
                keys[field] = list(value)
        except Exception as e:
            print("PREFILL_KEYS is not valid JSON — using defaults:", repr(e))
    return keys


KEYS = _load_keys()


def build_url(form_type, customer, machine=None):
    """
    form_type: 'dropoff' or 'hire'
    customer:  dict with full_name / phone / email / business_name and
               address1 / city / state / postal_code
    machine:   optional dict with machine / model / serial_number
               (given for "Book Again" on a specific service entry)
    """
    base = HIRE_FORM_URL if form_type == "hire" else DROPOFF_FORM_URL

    full_name = (customer.get("full_name") or "").strip()
    first, _, last = full_name.partition(" ")
    values = {
        "full_name": full_name,
        "first_name": first,
        "last_name": last.strip(),
        "phone": customer.get("phone") or "",
        "email": customer.get("email") or "",
        "business_name": customer.get("business_name") or "",
        "address1": customer.get("address1") or "",
        "city": customer.get("city") or "",
        "state": customer.get("state") or "",
        "postal_code": customer.get("postal_code") or "",
    }
    if machine:
        values["machine"] = machine.get("machine") or ""
        values["model"] = machine.get("model") or ""
        values["serial_number"] = machine.get("serial_number") or ""

    params = [("notrack", "true")]
    for field, value in values.items():
        if not value:
            continue
        for key in KEYS.get(field, []):
            params.append((key, value))

    return f"{base}?{urlencode(params)}"
