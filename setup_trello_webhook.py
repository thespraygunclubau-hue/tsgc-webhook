"""
Registers (or lists/deletes) a Trello webhook so that edits made
directly in Trello — editing a card's description, moving it between
lists, deleting it — get pushed to this app's /trello-webhook endpoint
and applied to the registry database in real time.

Run `create` once per board you want synced. Trello allows only one
webhook per (token, model, callbackURL) combination, so re-running
`create` for the same board/URL will just error clearly — use `list`
first if you're not sure one already exists.

Usage:
    python setup_trello_webhook.py create <BOARD_ID> <CALLBACK_URL>
    python setup_trello_webhook.py list
    python setup_trello_webhook.py delete <WEBHOOK_ID>

CALLBACK_URL is your deployed app's webhook URL, e.g.:
    https://your-app.onrender.com/trello-webhook

Requires TRELLO_KEY and TRELLO_TOKEN in your environment. The moment
you run `create`, Trello sends a HEAD request to CALLBACK_URL to
confirm it's reachable — make sure the app is already deployed with the
/trello-webhook route live before running this.

After creating the webhook, set TRELLO_WEBHOOK_URL on Render to the
same CALLBACK_URL you used here, and TRELLO_API_SECRET to your Power-Up
API secret (from https://trello.com/power-ups/admin — click your key,
"API key" tab, reveal "Secret"). Both are needed for the app to verify
that incoming webhook requests genuinely came from Trello; without
them, /trello-webhook still works but accepts requests unauthenticated.
"""

import os
import sys
import requests

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")


def create(board_id, callback_url):
    resp = requests.post(
        "https://api.trello.com/1/webhooks",
        params={
            "key": TRELLO_KEY,
            "token": TRELLO_TOKEN,
            "callbackURL": callback_url,
            "idModel": board_id,
            "description": "TSGC registry sync",
        },
    )
    print(resp.status_code, resp.text)
    if resp.status_code == 200:
        print("\nWebhook created. Trello will now POST to your app whenever")
        print("a card on this board is created, edited, moved, or deleted.")
        print("\nNext: set these on Render, then redeploy —")
        print(f"  TRELLO_WEBHOOK_URL = {callback_url}")
        print("  TRELLO_API_SECRET  = (from https://trello.com/power-ups/admin)")


def list_webhooks():
    resp = requests.get(
        f"https://api.trello.com/1/tokens/{TRELLO_TOKEN}/webhooks",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN},
    )
    resp.raise_for_status()
    hooks = resp.json()
    if not hooks:
        print("No webhooks registered for this token.")
        return
    for h in hooks:
        print(f"- id={h['id']}  active={h['active']}  model={h['idModel']}  url={h['callbackURL']}")


def delete(webhook_id):
    resp = requests.delete(
        f"https://api.trello.com/1/webhooks/{webhook_id}",
        params={"key": TRELLO_KEY, "token": TRELLO_TOKEN},
    )
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    if not (TRELLO_KEY and TRELLO_TOKEN):
        print("Missing TRELLO_KEY or TRELLO_TOKEN in your environment.")
        sys.exit(1)

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "create" and len(sys.argv) == 4:
        create(sys.argv[2], sys.argv[3])
    elif cmd == "list":
        list_webhooks()
    elif cmd == "delete" and len(sys.argv) == 3:
        delete(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)
