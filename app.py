from flask import Flask, request, jsonify, render_template, redirect, url_for, session
import requests
import os
import secrets

import db

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
DROPOFF_TEMPLATE_ID = os.environ.get("DROPOFF_TEMPLATE_ID")
HIRE_TEMPLATE_ID = os.environ.get("HIRE_TEMPLATE_ID")
DROPOFF_LIST_ID = os.environ.get("DROPOFF_LIST_ID")
HIRE_LIST_ID = os.environ.get("HIRE_LIST_ID")

# Shared password for the internal search page. Set REGISTRY_PASSWORD in
# Render's env vars. If it's not set, the search page refuses to load
# rather than sitting open with no protection.
REGISTRY_PASSWORD = os.environ.get("REGISTRY_PASSWORD")


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("INCOMING DATA:", data)

    custom = data.get("customData", {})  # everything GHL sends lives in here

    form_type = custom.get("form_type", "dropoff")
    customer_name = custom.get("full_name", "Unknown")
    phone = custom.get("phone", "")
    email = custom.get("email", "")
    business_name = custom.get("business_name", "") or custom.get("business_nam", "")
    machine = custom.get("machine", "")
    model = custom.get("model", "")
    serial_number = custom.get("serial_number", "")
    symptoms = custom.get("symptoms", "")

    # Hire-specific fields (only populated once you add a matching
    # webhook action on the Hire form workflow — see note below)
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

    # --- Save to the registry database (customer + machine entry) -------
    # This runs after the Trello card is created so a DB hiccup never
    # blocks the card itself. If it fails, we log it and still return
    # success for the Trello part — check Render logs for "DB ERROR".
    try:
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
# Internal search UI — customer & machine registry
# --------------------------------------------------------------------

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
            return redirect(url_for("search"))
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/search", methods=["GET"])
def search():
    if not _logged_in():
        return redirect(url_for("login"))

    query = request.args.get("q", "").strip()
    results = []
    error = None
    if query:
        try:
            results = db.search_customers(query)
        except Exception as e:
            print("SEARCH ERROR:", repr(e))
            error = "Search failed — check server logs."

    return render_template("search.html", query=query, results=results, error=error)


@app.route("/customer/<customer_id>", methods=["GET"])
def customer_profile(customer_id):
    if not _logged_in():
        return redirect(url_for("login"))

    try:
        customer = db.get_customer_with_machines(customer_id)
    except Exception as e:
        print("PROFILE ERROR:", repr(e))
        return "Failed to load customer — check server logs.", 500

    if not customer:
        return "Customer not found.", 404

    return render_template("customer.html", customer=customer)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
