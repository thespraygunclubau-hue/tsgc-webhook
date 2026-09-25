from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import requests
import os
import secrets
import io
import contextlib
import hmac
import hashlib
import base64

import db
import import_trello
import trello_sync
import prefill

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
# Stops other websites from submitting forms (e.g. delete) using a
# logged-in staff member's session.
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("RENDER") is not None


@app.context_processor
def _template_helpers():
    return {"prefill_url": prefill.build_url}

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
DROPOFF_TEMPLATE_ID = os.environ.get("DROPOFF_TEMPLATE_ID")
HIRE_TEMPLATE_ID = os.environ.get("HIRE_TEMPLATE_ID")
DROPOFF_LIST_ID = os.environ.get("DROPOFF_LIST_ID")
HIRE_LIST_ID = os.environ.get("HIRE_LIST_ID")

REGISTRY_PASSWORD = os.environ.get("REGISTRY_PASSWORD")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")

# For verifying that /trello-webhook requests really came from Trello.
# TRELLO_API_SECRET is your Power-Up's API secret (trello.com/power-ups/admin).
# TRELLO_WEBHOOK_URL must be exactly the callbackURL the webhook was
# registered with. If either is unset, verification is skipped — the
# endpoint still works, just unauthenticated.
TRELLO_API_SECRET = os.environ.get("TRELLO_API_SECRET")
TRELLO_WEBHOOK_URL = os.environ.get("TRELLO_WEBHOOK_URL")


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("INCOMING DATA:", data)

    custom = data.get("customData", {})

    form_type = custom.get("form_type", "dropoff")
    customer_name = custom.get("full_name", "Unknown")
    phone = custom.get("phone", "")
    email = custom.get("email", "")
    business_name = custom.get("business_name", "") or custom.get("business_nam", "")
    machine = custom.get("machine", "")
    model = custom.get("model", "")
    serial_number = custom.get("serial_number", "")
    symptoms = custom.get("symptoms", "")

    hire_date = custom.get("hire_date", "")
    return_date = custom.get("return_date", "")
    hire_charge = custom.get("hire_charge", "")
    security_deposit = custom.get("security_deposit", "")
    accessories = custom.get("accessories", "")

    if form_type == "hire":
        description = f"""👤 Customer: {customer_name}
📱 Phone: {phone}
📧 Email: {email}
🏢 Business: {business_name}
🔧 Machine: {machine}
📋 Model: {model}
🔢 Serial: {serial_number}
📦 Accessories: {accessories}
📅 Hire Date: {hire_date}
📅 Return Date: {return_date}
💰 Hire Charge: {hire_charge}
🔒 Security Deposit: {security_deposit}"""
        template_id = HIRE_TEMPLATE_ID
        list_id = HIRE_LIST_ID
        card_name = f"Hire — {customer_name} | {machine} {model}"
    else:
        description = f"""👤 Customer: {customer_name}
📱 Phone: {phone}
📧 Email: {email}
🏢 Business: {business_name}
🔧 Machine: {machine}
📋 Model: {model}
🔢 Serial: {serial_number}
⚠️ Issue: {symptoms}"""
        template_id = DROPOFF_TEMPLATE_ID
        list_id = DROPOFF_LIST_ID
        card_name = f"Drop-Off — {customer_name} | {machine} {model}"

    if not template_id or not list_id:
        print("ERROR: Missing template_id or list_id — check env vars for", form_type)
        return jsonify({"status": "error", "detail": "Missing Trello template/list ID"}), 500

    create_params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "idCardSource": template_id,
        "idList": list_id,
        "name": card_name,
        "keepFromSource": "checklists"
    }
    create_response = requests.post("https://api.trello.com/1/cards", params=create_params)
    print("TRELLO CREATE STATUS:", create_response.status_code, create_response.text)

    if create_response.status_code != 200:
        return jsonify({"status": "error", "detail": create_response.text}), 500

    card = create_response.json()
    card_id = card.get("id")
    card_url = card.get("shortUrl") or card.get("url")

    update_params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "desc": description
    }
    update_response = requests.put(f"https://api.trello.com/1/cards/{card_id}", params=update_params)
    print("TRELLO UPDATE STATUS:", update_response.status_code, update_response.text)

    try:
        if db.is_template_name(customer_name):
            raise ValueError("template entry — not saved to registry")
        customer_id = db.upsert_customer(customer_name, phone, email, business_name)
        db.insert_machine(customer_id, {
            "form_type": form_type,
            "machine": machine,
            "model": model,
            "serial_number": serial_number,
            "symptoms": symptoms,
            "hire_date": hire_date,
            "return_date": return_date,
            "hire_charge": hire_charge,
            "security_deposit": security_deposit,
            "accessories": accessories,
            "trello_card_id": card_id,
            "trello_card_url": card_url,
            "trello_list_id": list_id,
        })
    except Exception as e:
        print("DB ERROR:", repr(e))

    return jsonify({"status": "ok", "card_id": card_id}), 200


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200


# --------------------------------------------------------------------
# Trello → database sync — real-time, for edits made directly in Trello
# --------------------------------------------------------------------

def _verify_trello_signature(req):
    if not TRELLO_API_SECRET or not TRELLO_WEBHOOK_URL:
        return True  # not configured yet — see setup_trello_webhook.py
    signature = req.headers.get("X-Trello-Webhook")
    if not signature:
        return False
    expected = base64.b64encode(
        hmac.new(
            TRELLO_API_SECRET.encode("utf-8"),
            req.get_data() + TRELLO_WEBHOOK_URL.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")
    return hmac.compare_digest(signature, expected)


@app.route("/trello-webhook", methods=["HEAD", "GET", "POST"])
def trello_webhook():
    # Trello sends a HEAD request the moment the webhook is registered,
    # just to confirm the URL is reachable — there's no body yet, and
    # nothing to process.
    if request.method in ("HEAD", "GET"):
        return "", 200

    if not _verify_trello_signature(request):
        print("TRELLO WEBHOOK: signature check failed — ignoring request")
        return jsonify({"status": "error", "detail": "invalid signature"}), 401

    payload = request.json or {}
    action = payload.get("action", {})
    action_type = action.get("type")
    card = (action.get("data") or {}).get("card") or {}
    card_id = card.get("id")

    if not card_id:
        return jsonify({"status": "ignored"}), 200

    try:
        if action_type == "deleteCard":
            deleted = trello_sync.delete_card(card_id)
            print("TRELLO WEBHOOK: deleteCard", card_id, "removed rows:", deleted)
        elif action_type in ("updateCard", "createCard", "copyCard"):
            trello_sync.sync_card(card_id)
            print("TRELLO WEBHOOK:", action_type, "synced card", card_id)
        # other action types (comments, checklist ticks, member changes,
        # etc.) don't affect registry fields, so they're ignored.
    except Exception as e:
        print("TRELLO WEBHOOK ERROR:", repr(e))
        # Still return 200: Trello auto-disables a webhook after enough
        # consecutive non-2xx responses, and a DB hiccup on our end
        # shouldn't cost us the whole webhook registration.

    return jsonify({"status": "ok"}), 200


def _logged_in():
    return session.get("logged_in") is True


@app.route("/login", methods=["GET", "POST"])
def login():
    if not REGISTRY_PASSWORD:
        return "REGISTRY_PASSWORD is not set on the server — ask an admin to set it.", 500

    error = None
    if request.method == "POST":
        if request.form.get("password") == REGISTRY_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("board"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/board", methods=["GET"])
def board():
    if not _logged_in():
        return redirect(url_for("login"))

    customers = []
    error = None
    try:
        customers = db.list_board_customers()
    except Exception as e:
        print("BOARD ERROR:", repr(e))
        error = "Couldn't load customers — check server logs."

    return render_template(
        "board.html",
        customers=customers,
        error=error,
        query=request.args.get("q", ""),
        open_id=request.args.get("open", ""),
        flash_msg=request.args.get("msg", ""),
        flash_err=request.args.get("err", ""),
    )


@app.route("/search", methods=["GET"])
def search():
    # Old address — keeps existing bookmarks working.
    q = request.args.get("q", "")
    return redirect(url_for("board", q=q) if q else url_for("board"))


@app.route("/customer/<customer_id>", methods=["GET"])
def customer_profile(customer_id):
    if not _logged_in():
        if request.args.get("partial"):
            return "Session expired — please log in again.", 401
        return redirect(url_for("login"))

    try:
        customer = db.get_customer_with_machines(customer_id)
    except Exception as e:
        print("PROFILE ERROR:", repr(e))
        return "Failed to load customer — check server logs.", 500

    if not customer:
        return "Customer not found.", 404

    if request.args.get("partial"):
        # Just the card contents, loaded into the pop-up on the board.
        return render_template("_customer_detail.html", customer=customer)
    return render_template("customer.html", customer=customer)


@app.route("/entry/<machine_id>/delete", methods=["POST"])
def delete_entry(machine_id):
    """Deletes one service entry from the app only — Trello is untouched."""
    if not _logged_in():
        return redirect(url_for("login"))
    try:
        customer_id = db.delete_machine(machine_id)
    except Exception as e:
        print("DELETE ENTRY ERROR:", repr(e))
        return "Delete failed — check server logs.", 500
    print("DELETED ENTRY", machine_id, "for customer", customer_id)
    if customer_id:
        return redirect(url_for("board", open=customer_id, msg="Entry deleted."))
    return redirect(url_for("board"))


@app.route("/customer/<customer_id>/edit", methods=["POST"])
def edit_customer(customer_id):
    """Edits a customer's details in the app only — Trello is untouched."""
    if not _logged_in():
        return redirect(url_for("login"))
    f = request.form
    try:
        db.update_customer(customer_id, f.get("full_name"), f.get("phone"),
                           f.get("email"), f.get("business_name"))
    except ValueError as e:  # includes DuplicateContact
        return redirect(url_for("board", open=customer_id, err=str(e)))
    except Exception as e:
        print("EDIT CUSTOMER ERROR:", repr(e))
        return redirect(url_for("board", open=customer_id, err="Save failed — check server logs."))
    return redirect(url_for("board", open=customer_id, msg="Customer details saved."))


@app.route("/entry/<machine_id>/edit", methods=["POST"])
def edit_entry(machine_id):
    """Edits one service entry in the app only — Trello is untouched."""
    if not _logged_in():
        return redirect(url_for("login"))
    try:
        customer_id = db.update_machine(machine_id, request.form)
    except Exception as e:
        print("EDIT ENTRY ERROR:", repr(e))
        return redirect(url_for("board", err="Save failed — check server logs."))
    if not customer_id:
        return redirect(url_for("board", err="That entry no longer exists."))
    return redirect(url_for("board", open=customer_id, msg="Entry saved."))


@app.route("/customer/<customer_id>/delete", methods=["POST"])
def delete_customer(customer_id):
    """Deletes a customer and all their entries from the app only."""
    if not _logged_in():
        return redirect(url_for("login"))
    try:
        db.delete_customer(customer_id)
    except Exception as e:
        print("DELETE CUSTOMER ERROR:", repr(e))
        return "Delete failed — check server logs.", 500
    print("DELETED CUSTOMER", customer_id)
    return redirect(url_for("board", msg="Customer deleted."))


@app.route("/admin/import-trello", methods=["GET"])
def admin_import_trello():
    if not ADMIN_SECRET or request.args.get("secret") != ADMIN_SECRET:
        return "Not authorized.", 403

    board_id = request.args.get("board_id")
    if not board_id:
        return "Missing ?board_id= parameter.", 400

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            import_trello.import_board(board_id)
    except Exception as e:
        return f"<pre>{buf.getvalue()}\n\nERROR: {e}</pre>", 500

    return f"<pre>{buf.getvalue()}</pre>"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
