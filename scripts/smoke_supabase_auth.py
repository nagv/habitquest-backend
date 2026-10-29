"""Opt-in real local Supabase Auth check; requires local services already running."""

import json
import os
import subprocess
from uuid import uuid4

import httpx

from habitquest_backend.auth import verify_token
from habitquest_backend.config import Settings

status = subprocess.run(
    ["npx.cmd" if os.name == "nt" else "npx", "supabase", "status", "--output", "json"],
    check=True,
    capture_output=True,
    text=True,
)
data = json.loads(status.stdout)
with httpx.Client(base_url=data["API_URL"], headers={"apikey": data["ANON_KEY"]}, timeout=15) as client:
    response = client.post(
        "/auth/v1/signup",
        json={"email": f"local-{uuid4()}@habitquest.invalid", "password": "Synthetic-local-auth-check-2026!"},
    )
    response.raise_for_status()
    result = response.json()
    token = result.get("access_token")
    if not token:
        raise SystemExit("No access token: check local email confirmation settings.")
    parent = verify_token(token, Settings())
    assert str(parent) == result["user"]["id"]
print("Local Supabase signup and application JWT validation succeeded; no token printed.")
