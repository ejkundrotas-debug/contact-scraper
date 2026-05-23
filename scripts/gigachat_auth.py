"""Get a temporary GigaChat access token.

Usage:
    export GIGACHAT_AUTH_KEY="base64-client-credentials-from-Sber"
    python scripts/gigachat_auth.py

The token usually expires quickly. Put the printed token into .env as
GIGACHAT_ACCESS_TOKEN=...

For production, implement automatic refresh in modules/router.py and configure
required TLS certificates according to the official GigaChat docs.
"""
from __future__ import annotations

import os
import sys
import uuid

import requests


def main() -> int:
    auth_key = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
    scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()
    if not auth_key:
        print("ERROR: set GIGACHAT_AUTH_KEY first", file=sys.stderr)
        return 2
    url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    headers = {
        "Authorization": f"Basic {auth_key}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    resp = requests.post(url, headers=headers, data={"scope": scope}, timeout=30)
    if resp.status_code >= 400:
        print(f"ERROR {resp.status_code}: {resp.text}", file=sys.stderr)
        return 1
    data = resp.json()
    token = data.get("access_token")
    if not token:
        print(f"ERROR: no access_token in response: {data}", file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
