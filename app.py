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
    data = request.json
    form_type = data.get("form_type")
    customer_name = data.get("full_name", "Unknown")
    machine = data.get("machine", "Unknown Machine")

    if form_type == "dropoff":
        template_id = DROPOFF_TEMPLATE_ID
        list_id = DROPOFF_LIST_ID
        card_name = f"Drop-Off — {customer_name} | {machine}"
    else:
        template_id = HIRE_TEMPLATE_ID
        list_id = HIRE_LIST_ID
        card_name = f"Hire — {customer_name} | {machine}"

    url = "https://api.trello.com/1/cards"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "idCardSource": template_id,
        "idList": list_id,
        "name": card_name,
        "keepFromSource": "checklists"
    }
    response = requests.post(url, params=params)
    card = response.json()

    return jsonify({"status": "ok", "card_id": card.get("id")}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
