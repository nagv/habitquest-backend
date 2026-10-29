"""Walk the parent and editor flows against a running API, with separate worker processes.

Synthetic, local-only data and native-local developer tokens. Start the API first; do not run a
continuously polling worker at the same time (each scenario runs `worker --once` itself).

    uv run python scripts/demo.py                       # basic: onboard a child, plan, confirm
    uv run python scripts/demo.py --scenario edits      # parent edits and the rules that refuse them
    uv run python scripts/demo.py --scenario second-round
    uv run python scripts/demo.py --scenario editor     # two-person review, release, withdrawal
    uv run python scripts/demo.py --scenario all
    uv run python scripts/demo.py --remote --base-url https://<service>.onrender.com

Expectations assume the seeded release 1 catalog and the fake model (MODEL_MODE=fake).
"""

import argparse
import json
import subprocess
import sys
import time
import uuid

import httpx
from dotenv import load_dotenv
from local_auth import EDITOR, REVIEWER, demo_settings, local_token

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PROFILE = {
    "name": "Alex",
    "age_band": "6-9",
    "interests": ["space"],
    "preferred_world": "space",
    "timezone": "America/New_York",
    "routine_schedule": {
        "bedtime": {"cue": "20:00", "days": DAYS},
        "after_school": {"cue": "16:00", "days": DAYS[:5]},
    },
}
CONCERNS = (
    "Alex needs repeated reminders at bedtime, mornings are rushed, "
    "and plates and school things are left everywhere."
)


class Session:
    """One identity against the API; every call states the status it expects."""

    def __init__(self, base_url: str, token: str, name: str):
        self.client = httpx.Client(
            base_url=base_url, headers={"Authorization": "Bearer " + token}, timeout=180
        )
        self.name = name

    def call(self, label, method, path, status, body=None, key=None):
        headers = {"Idempotency-Key": key} if key else None
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code != status:
            raise SystemExit(
                f"FAIL [{self.name}] {label}: expected {status}, got {response.status_code}\n{response.text}"
            )
        data = response.json()
        note = data.get("detail") if status >= 400 else None
        print(f"  ok [{self.name}] {label} -> {status}" + (f": {note}" if note else ""))
        return data


# Set by --remote: the server runs its own worker (e.g. Render with RUN_WORKER_IN_API), so poll instead.
REMOTE = False


def work():
    if not REMOTE:
        subprocess.run([sys.executable, "-m", "habitquest_backend.worker", "--once"], check=True)


def finished(parent, status_url, timeout=180):
    """Poll until the run leaves queued/running; a remote worker may be waking from sleep."""
    deadline = time.monotonic() + timeout
    while True:
        status = parent.client.get(status_url).json()
        if status["status"] not in ("queued", "running") or not REMOTE or time.monotonic() > deadline:
            return status
        time.sleep(2)


def plan(parent, child, concerns=CONCERNS, minutes=20):
    """Intake -> plan run -> worker -> ready proposal."""
    intake = parent.call(
        "save pain points",
        "POST",
        f"/v1/children/{child['id']}/intakes",
        201,
        {"concerns": concerns, "available_minutes": minutes},
    )
    run = parent.call(
        "start plan run", "POST", f"/v1/intakes/{intake['id']}/plan-runs", 202, key=str(uuid.uuid4())
    )
    work()
    status = finished(parent, run["status_url"])
    print(f"  ok [{parent.name}] plan run finished -> {status['status']}")
    if status["status"] != "ready":
        raise SystemExit(f"FAIL plan run did not finish: {status}")
    return parent.call("read proposal", "GET", f"/v1/proposals/{run['run_id']}", 200)


def edit(parent, proposal, label, change, status=200):
    body = {"edit": change, "expected_proposal_version": proposal["version"]}
    result = parent.call(label, "PATCH", f"/v1/proposals/{proposal['id']}", status, body)
    return result if status == 200 else proposal


def confirm(parent, proposal, plan_version):
    body = {"expected_plan_version": plan_version, "expected_proposal_version": proposal["version"]}
    return parent.call(
        f"confirm plan v{plan_version + 1}", "POST", f"/v1/proposals/{proposal['id']}/confirm", 200, body
    )


def summary(data):
    return {
        "goals": {g["goal_id"]: g["status"] for g in data["goals"]},
        "active": [f"{h['habit_id']} ({h['level']}, {h['minutes']} min, {h['cue']})" for h in data["active"]],
        "future": [h["habit_id"] for h in data["future"]],
        "chapters": [c["title"] for c in data["chapters"]],
        "questions": data["questions"],
        "fallback": data["fallback"],
    }


def show(title, data):
    print(
        f"  {title}:\n"
        + "\n".join("    " + line for line in json.dumps(summary(data), indent=2).splitlines())
    )


def basic(parent):
    """Onboard a child, describe pain points, review the suggested habits and confirm them."""
    child = parent.call("onboard child", "POST", "/v1/children", 201, PROFILE)
    proposal = plan(parent, child)
    show("proposal", proposal["data"])
    plan_record = confirm(parent, proposal, 0)
    print(
        f"  confirmed plan v{plan_record['version']}:", [h["habit_id"] for h in plan_record["data"]["active"]]
    )
    return child


def edits(parent):
    """Parent edits on the review screen; each one is re-checked and refusals name the rule."""
    child = parent.call("onboard child", "POST", "/v1/children", 201, PROFILE)
    proposal = plan(parent, child)
    bedtime = {"op": "change_schedule", "habit_id": "bedtime-prep", "cue": "19:45", "days": DAYS}
    proposal = edit(parent, proposal, "move bedtime-prep to 19:45", bedtime)
    edit(parent, proposal, "promote pack-bag (no evening cue)", promote("pack-bag"), 422)
    edit(parent, proposal, "change level to one the habit lacks", level("join-meal", "simple"), 422)
    edit(parent, proposal, "demote join-meal (meals would lose coverage)", demote("join-meal"), 422)
    proposal = edit(parent, proposal, "remove the meals goal", {"op": "remove_goal", "goal_id": "meals"})
    proposal = edit(parent, proposal, "now demote join-meal", demote("join-meal"))
    show("edited proposal", proposal["data"])
    confirm(parent, proposal, 0)


def second_round(parent):
    """Answer the morning cue question, describe a new pain point and rotate a habit to fit it."""
    child = basic(parent)
    profile = parent.call("read profile", "GET", f"/v1/children/{child['id']}", 200)
    morning = {**profile["routine_schedule"], "morning": {"cue": "07:00", "days": DAYS[:5]}}
    body = {**PROFILE, "routine_schedule": morning, "expected_profile_version": profile["profile_version"]}
    parent.call("add a morning cue", "PATCH", f"/v1/children/{child['id']}", 200, body)
    proposal = plan(parent, child, concerns="Mornings are still rushed before school.", minutes=30)
    show("second proposal (plan is full)", proposal["data"])
    edit(parent, proposal, "promote morning-ready while full", promote("morning-ready"), 422)
    proposal = edit(parent, proposal, "demote clear-plate", demote("clear-plate"))
    proposal = edit(parent, proposal, "promote morning-ready", promote("morning-ready"))
    plan_record = confirm(parent, proposal, 1)
    show("confirmed plan v2", plan_record["data"])
    goals = parent.call("goal history", "GET", f"/v1/children/{child['id']}/goals", 200)
    print("  goals:", {g["goal_id"]: g["status"] for g in goals})


def editor(author, reviewer, parent):
    """Two-person catalog review, a release, and an emergency withdrawal."""
    habit_id = "demo-" + uuid.uuid4().hex[:8]
    draft = {
        "title": "Brush your teeth",
        "instruction": "Brush for two minutes at the agreed cue.",
        "min_age": 6,
        "max_age": 13,
        "routine_anchor": "bedtime",
        "levels": [{"level": "standard", "minutes": 3, "points": 1}],
        # A weak link only, so the demo habit never displaces the seeded bedtime habit in later runs.
        "goals": {"bedtime": 0.5},
        "tags": ["access:short_instructions"],
    }
    path = f"/v1/editor/habits/{habit_id}/versions"
    author.call("editor token on editor route", "GET", "/v1/editor/releases", 200)
    parent.call("parent token on editor route", "GET", "/v1/editor/releases", 403)
    author.call("draft a habit", "POST", path, 201, draft)
    linted = {**draft, "instruction": "Brush your teeth or else you lose screen time."}
    author.call("edit the draft with coercive wording", "PUT", path + "/1", 200, linted)
    author.call("submit (lint finds coercive_wording)", "POST", path + "/1/submit", 422)
    author.call("fix the wording", "PUT", path + "/1", 200, draft)
    author.call("submit for review", "POST", path + "/1/submit", 200)
    queue = reviewer.call("review queue", "GET", "/v1/editor/habits?status=in_review", 200)
    print("  in review:", [f"{v['habit_id']} v{v['version']}" for v in queue])
    author.call("author approves own draft", "POST", path + "/1/approve", 403)
    reviewer.call("reviewer approves", "POST", path + "/1/approve", 200)
    release = author.call("build release", "POST", "/v1/editor/releases", 201, {"notes": "Demo: " + habit_id})
    rid = release["id"]
    changes = author.call("diff against current", "GET", f"/v1/editor/releases/{rid}/diff", 200)["changes"]
    print("  habits added:", changes["habit"]["added"])
    report = author.call("validate", "GET", f"/v1/editor/releases/{rid}/validate", 200)
    print("  validation:", report)
    author.call("builder publishes own release", "POST", f"/v1/editor/releases/{rid}/publish", 403)
    reviewer.call("reviewer publishes", "POST", f"/v1/editor/releases/{rid}/publish", 200)
    withdrawn = author.call(
        "withdraw the demo habit",
        "POST",
        f"/v1/editor/habits/{habit_id}/withdraw",
        200,
        {"reason": "Demo cleanup"},
    )
    print("  plans flagged for parent review:", withdrawn["flagged_plans"])
    requests = author.call("anonymised unmatched concerns", "GET", "/v1/editor/catalog-requests", 200)
    print("  catalog requests:", [(r["normalized_phrase"], r["count"]) for r in requests[:5]])


def promote(habit_id, name="standard"):
    return {"op": "promote_future", "habit_id": habit_id, "level": name}


def demote(habit_id):
    return {"op": "demote_active", "habit_id": habit_id}


def level(habit_id, name):
    return {"op": "change_level", "habit_id": habit_id, "level": name}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenario", choices=["basic", "edits", "second-round", "editor", "all"], default="basic"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--remote",
        action="store_true",
        help="the server runs its own worker (e.g. on Render); poll for results",
    )
    args = parser.parse_args()
    global REMOTE
    REMOTE = args.remote
    load_dotenv()
    settings = demo_settings()
    # A fresh parent per run keeps runs independent; editors keep stable ids for the audit trail.
    parent = Session(args.base_url, local_token(settings, uuid.uuid4(), minutes=30), "parent")
    author = Session(args.base_url, local_token(settings, EDITOR, editor=True, minutes=30), "editor")
    reviewer = Session(args.base_url, local_token(settings, REVIEWER, editor=True, minutes=30), "reviewer")
    scenarios = {
        "basic": (basic, (parent,)),
        "edits": (edits, (parent,)),
        "second-round": (second_round, (parent,)),
        "editor": (editor, (author, reviewer, parent)),
    }
    for name in scenarios if args.scenario == "all" else [args.scenario]:
        run, sessions = scenarios[name]
        print(f"\n== {name}: {run.__doc__}")
        run(*sessions)
    print("\nAll steps passed.")


if __name__ == "__main__":
    main()
