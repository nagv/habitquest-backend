"""Exercise separate API and worker processes using synthetic local-only data."""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
from dotenv import load_dotenv

from habitquest_backend.config import Settings

load_dotenv()
settings = Settings()
if os.getenv("LOCAL_DEMO_AUTH") != "true" or not settings.jwt_secret:
    raise SystemExit(
        "This demo uses native-local developer tokens only. For Supabase, sign in through local Auth and use /docs."
    )
now = datetime.now(timezone.utc)
token = jwt.encode(
    {
        "sub": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=10),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "role": "authenticated",
    },
    settings.jwt_secret.get_secret_value(),
    algorithm="HS256",
)
with httpx.Client(
    base_url="http://127.0.0.1:8000", headers={"Authorization": "Bearer " + token}, timeout=180
) as client:

    def call(path, body=None, key=None):
        r = client.post(path, json=body, headers={"Idempotency-Key": key} if key else None)
        r.raise_for_status()
        return r.json()

    child = call("/v1/children", {"name": "Alex", "age_band": "6-9"})
    data = {
        "concerns": "Bedtime is difficult, mornings rushed, and school bags and plates left everywhere.",
        "world": "space",
        "available_minutes": 10,
        "timezone": "America/New_York",
        "bedtime": "20:30",
        "morning": "07:30",
    }
    session = call(f"/v1/children/{child['id']}/onboarding", data)
    analysis = call(f"/v1/onboarding/{session['id']}/analyze-runs", key="analysis-" + str(uuid.uuid4()))
    subprocess.run([sys.executable, "-m", "habitquest_backend.worker", "--once"], check=True)
    suggestions = client.get(analysis["status_url"]).json()
    assert suggestions["status"] == "ready", suggestions
    call(
        f"/v1/onboarding/{session['id']}/confirm-goals",
        {"expected_version": 1, "goals": suggestions["analysis"]["goals"]},
    )
    run = call(f"/v1/onboarding/{session['id']}/plan-runs", key="plan-" + str(uuid.uuid4()))
    subprocess.run([sys.executable, "-m", "habitquest_backend.worker", "--once"], check=True)
    status = client.get(run["status_url"]).json()
    assert status["status"] == "ready", status
    plan = call(f"/v1/proposals/{run['run_id']}/confirm", {"expected_plan_version": 0})
    print(
        json.dumps(
            {
                "plan_id": plan["id"],
                "version": plan["version"],
                "goals": plan["data"]["goals"],
                "active_habits": plan["data"]["active_option_ids"],
                "chapters": [c["title"] for c in plan["data"]["chapters"]],
            },
            indent=2,
        )
    )
