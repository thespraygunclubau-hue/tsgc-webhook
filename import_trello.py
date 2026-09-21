"""
One-off / re-runnable backfill: pulls existing cards from your Trello
board(s) and loads them into the registry database, so historical
customers show up in search alongside new submissions.

Reads structured fields from the card description when present (cards
created by the webhook, with lines like "Machine:"), and falls back to
parsing the card TITLE (e.g. "Drop-Off — Daniel DPM | Wagner PS3.25")
for older cards created by hand in Trello that don't have that format.

Safe to re-run: each card is upserted the same way the live
/trello-webhook sync does it (matched by trello_card_id, customer
matched by phone/email), so running it again just refreshes rows
rather than duplicating or skipping them.

Usage:
    python import_trello.py <BOARD_ID> [<BOARD_ID_2> ...]

Also reachable via the app's /admin/import-trello?board_id=...&secret=...
endpoint, which calls import_board() directly.
"""

import os
import sys
import requests

import trello_sync

TRELLO_KEY = os.environ.get("TRELLO_KEY")
TRELLO_TOKEN = os.environ.get("TRELLO_TOKEN")


def fetch_cards(board_id):
    url = f"https://api.trello.com/1/boards/{board_id}/cards"
    params = {
        "key": TRELLO_KEY,
        "token": TRELLO_TOKEN,
        "fields": "id,name,desc,shortUrl,idList,closed",
    }
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def import_board(board_id):
    print(f"\nFetching cards from board {board_id}...")
    cards = fetch_cards(board_id)
    print(f"Found {len(cards)} cards.")

    synced, errors = 0, 0

    for card in cards:
        card_id = card["id"]
        try:
            trello_sync.sync_card(card_id, card=card)
            synced += 1
        except Exception as e:
            errors += 1
            print(f"  ERROR on card {card_id} ({card.get('name')}): {e}")

    print(f"Board {board_id}: {synced} synced, {errors} errors.")


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
