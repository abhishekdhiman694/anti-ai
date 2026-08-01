"""Owner-only CLI for issuing/listing/revoking Meeting Copilot access
credentials. Run this on YOUR machine - it just calls the admin endpoints
on your deployed server, it doesn't need to run alongside the server itself.

Usage:
  python manage_tokens.py create <username> <password> <hours> [label]
  python manage_tokens.py list
  python manage_tokens.py revoke <username>

Reads the server URL and admin secret from environment variables (or a
.env file next to this script):
  SERVER_URL     e.g. https://your-app.onrender.com
  ADMIN_SECRET   the same value you set on the server
"""

import os
import sys
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_SECRET = os.environ.get("ADMIN_SECRET")


def _headers():
    if not ADMIN_SECRET:
        sys.exit("Set ADMIN_SECRET (env var or .env file next to this script).")
    return {"X-Admin-Secret": ADMIN_SECRET}


def create(username: str, password: str, hours: str, label: str = ""):
    resp = requests.post(
        f"{SERVER_URL}/admin/tokens",
        headers=_headers(),
        json={"username": username, "password": password, "hours": float(hours), "label": label},
    )
    resp.raise_for_status()
    data = resp.json()
    expires = datetime.fromtimestamp(data["expires_at"], tz=timezone.utc).astimezone()
    print(f"Created '{username}' - expires {expires:%Y-%m-%d %H:%M %Z}")
    print(f"Give them: username={username}  password={password}")


def list_tokens():
    resp = requests.get(f"{SERVER_URL}/admin/tokens", headers=_headers())
    resp.raise_for_status()
    tokens = resp.json()
    if not tokens:
        print("No tokens issued yet.")
        return
    now = datetime.now(tz=timezone.utc).timestamp()
    for username, entry in tokens.items():
        status = "REVOKED" if entry["revoked"] else ("EXPIRED" if now > entry["expires_at"] else "active")
        expires = datetime.fromtimestamp(entry["expires_at"], tz=timezone.utc).astimezone()
        label = f" ({entry['label']})" if entry["label"] else ""
        print(f"{username}{label}: {status}, expires {expires:%Y-%m-%d %H:%M %Z}")


def revoke(username: str):
    resp = requests.delete(f"{SERVER_URL}/admin/tokens/{username}", headers=_headers())
    resp.raise_for_status()
    print(f"Revoked '{username}'")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    command, rest = args[0], args[1:]
    if command == "create" and len(rest) >= 3:
        create(*rest)
    elif command == "list":
        list_tokens()
    elif command == "revoke" and len(rest) == 1:
        revoke(rest[0])
    else:
        print(__doc__)
        sys.exit(1)
