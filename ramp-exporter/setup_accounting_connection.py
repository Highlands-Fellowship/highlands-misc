"""
One-time setup: registers an API-based accounting connection with Ramp.

This must be run once before --mark-synced will work for either card
transactions or reimbursements. If a Universal CSV connection already exists
in Ramp, this upgrades it in-place rather than creating a new one.

Usage:
  python setup_accounting_connection.py
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"
RAMP_CONNECTION_URL = "https://api.ramp.com/developer/v1/accounting/connection"


def main() -> None:
    client_id = os.getenv("RAMP_CLIENT_ID")
    client_secret = os.getenv("RAMP_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("ERROR: RAMP_CLIENT_ID and RAMP_CLIENT_SECRET must be set in .env")

    # Get token with accounting:write scope
    token_resp = requests.post(
        RAMP_TOKEN_URL,
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials", "scope": "accounting:write"},
        timeout=30,
    )
    token_resp.raise_for_status()
    token = token_resp.json()["access_token"]

    resp = requests.post(
        RAMP_CONNECTION_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"remote_provider_name": "Sage 50"},
        timeout=30,
    )

    if resp.ok:
        print("Accounting connection established. --mark-synced will now work.")
    else:
        sys.exit(
            f"ERROR {resp.status_code}:\n{resp.text}"
        )


if __name__ == "__main__":
    main()
