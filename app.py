from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")
DROPOFF_TEMPLATE_ID = os.environ.get("DROPOFF_TEMPLATE_ID")
HIRE_TEMPLATE_ID = os.environ.get("HIRE_TEMPLATE_ID")
DROPOFF_LIST_ID = os.environ.get("DROPOFF_LIST_ID")
HIRE_LIST_ID = os.environ.get("HIRE_LIST_ID")

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    print("INCOMING DATA:", data)

    custom = data.get("customData", {})  # everything GHL sends lives in here

    form_type = custom.get("form_type", "dropoff")
    customer_name = custom.get("full_name", "Unknown")
    phone = custom.get("phone", "")
    email = custom.get("email", "")
    business_name = custom.get("business_nam", "")
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

    update_params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "desc": description
    }
    update_response = requests.put(f"https://api.trello.com/1/cards/{card_id}", params=update_params)
    print("TRELLO UPDATE STATUS:", update_response.status_code, update_response.text)

    return jsonify({"status": "ok", "card_id": card_id}), 200

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "alive"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
