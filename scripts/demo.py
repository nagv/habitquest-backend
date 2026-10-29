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

    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    child = call(
        "/v1/children",
        {
            "name": "Alex",
            "age_band": "6-9",
            "interests": ["space"],
            "preferred_world": "space",
            "timezone": "America/New_York",
            "routine_schedule": {
                "bedtime": {"cue": "20:00", "days": days},
                "after_school": {"cue": "16:00", "days": days[:5]},
            },
        },
    )
    record = call(
        f"/v1/children/{child['id']}/intakes",
        {
            "concerns": "Alex needs repeated reminders at bedtime, mornings are rushed, "
            "and plates and school things are left everywhere.",
            "available_minutes": 20,
        },
    )
    run = call(f"/v1/intakes/{record['id']}/plan-runs", key="plan-" + str(uuid.uuid4()))
    subprocess.run([sys.executable, "-m", "habitquest_backend.worker", "--once"], check=True)
    status = client.get(run["status_url"]).json()
    assert status["status"] == "ready", status
    proposal = client.get(f"/v1/proposals/{run['run_id']}").json()
    plan = call(
        f"/v1/proposals/{run['run_id']}/confirm",
        {"expected_plan_version": 0, "expected_proposal_version": proposal["version"]},
    )
    print(
        json.dumps(
            {
                "plan_id": plan["id"],
                "version": plan["version"],
                "goals": [(g["goal_id"], g["status"]) for g in plan["data"]["goals"]],
                "active_habits": [h["title"] for h in plan["data"]["active"]],
                "chapters": [c["title"] for c in plan["data"]["chapters"]],
                "questions": plan["data"]["questions"],
                "fallback": plan["data"]["fallback"],
            },
            indent=2,
        )
    )
