"""Durable loopback/local worker. No API server process is required for execution."""

import argparse
import hashlib
import threading
import time
from uuid import uuid4

import psycopg
from fastapi import HTTPException
from langgraph.checkpoint.postgres import PostgresSaver
from langsmith import tracing_context
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from habitquest_backend.catalog.models import profile_from_row
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.planning import BudgetExceeded, generate
from habitquest_backend.service import current_version, validate_sources


def store_catalog_requests(conn, unmatched_phrases: list[str]) -> None:
    for phrase in unmatched_phrases:
        normalized = " ".join(phrase.lower().split())
        if not normalized:
            continue
        phrase_hash = hashlib.md5(normalized.encode()).hexdigest()
        conn.execute(
            "INSERT INTO hq.catalog_requests(phrase_hash,normalized_phrase) VALUES(%s,%s) "
            "ON CONFLICT(phrase_hash) DO UPDATE SET count=hq.catalog_requests.count+1,last_seen=now()",
            (phrase_hash, normalized),
        )


def claim(settings):
    with transaction(settings, worker=True) as conn:
        conn.execute(
            "UPDATE hq.jobs SET status='failed',error='deadline_exhausted',lease_token=NULL WHERE status IN ('queued','running') AND deadline<=now()"
        )
        conn.execute(
            "UPDATE hq.jobs SET status='failed',error='retry_exhausted' WHERE attempts>=%s AND (status='queued' OR (status='running' AND lease_until<=now()))",
            (settings.max_attempts,),
        )
        row = conn.execute(
            "SELECT id FROM hq.jobs WHERE kind='plan' AND attempts<%s AND ((status='queued' AND available_at<=now()) OR (status='running' AND lease_until<=now())) ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
            (settings.max_attempts,),
        ).fetchone()
        if row is None:
            return None
        lease = uuid4()
        return one(
            conn,
            "UPDATE hq.jobs SET status='running',attempts=attempts+1,lease_token=%s,lease_until=now()+make_interval(secs=>%s),resume_thread=checkpoint_thread,checkpoint_thread=id::text||':'||%s,deadline=coalesce(deadline,now()+make_interval(secs=>%s)) WHERE id=%s RETURNING *",
            (lease, settings.lease_seconds, str(lease), settings.max_run_seconds, row["id"]),
        )


def heartbeat(settings, job):
    with transaction(settings, worker=True) as conn:
        return (
            conn.execute(
                "UPDATE hq.jobs SET lease_until=now()+make_interval(secs=>%s) WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now() RETURNING id",
                (settings.lease_seconds, job["id"], job["lease_token"]),
            ).fetchone()
            is not None
        )


def renew_loop(settings, job, stop):
    while not stop.wait(settings.lease_seconds / 3):
        try:
            if not heartbeat(settings, job):
                return
        except psycopg.Error:
            return


def run_once(settings: Settings) -> bool:
    job = claim(settings)
    if job is None:
        return False
    stop = threading.Event()

    thread = threading.Thread(target=renew_loop, args=(settings, job, stop), daemon=True)
    thread.start()

    def record(kind, data):
        with transaction(settings, job["parent_id"], worker=True) as conn:
            conn.execute(
                "INSERT INTO hq.events(run_id,kind,data) VALUES(%s,%s,%s)", (job["id"], kind, Jsonb(data))
            )

    def charge(role, tokens):
        with transaction(settings, worker=True) as conn:
            row = conn.execute(
                "UPDATE hq.jobs SET model_calls=model_calls+1,reserved_tokens=reserved_tokens+%s WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now() AND deadline>now() AND model_calls<%s AND reserved_tokens+%s<=%s RETURNING model_calls",
                (
                    tokens,
                    job["id"],
                    job["lease_token"],
                    settings.max_model_calls,
                    tokens,
                    settings.max_run_tokens,
                ),
            ).fetchone()
            if row is None:
                raise BudgetExceeded("Budget exhausted, cancelled or lease lost")
        record(
            "model",
            {
                "role": role,
                "call": row["model_calls"],
                "reserved_tokens": tokens,
                "model_mode": settings.model_mode,
            },
        )

    try:
        with transaction(settings, job["parent_id"], worker=True) as conn:
            child = one(
                conn,
                "SELECT c.*,h.timezone FROM hq.children c JOIN hq.households h ON h.id=c.household_id WHERE c.id=%s",
                (job["child_id"],),
            )
            intake = one(conn, "SELECT * FROM hq.intakes WHERE id=%s", (job["intake_id"],))
            if intake["version"] != job["intake_version"]:
                raise ValueError("Intake changed")
            base = conn.execute(
                "SELECT data,release_id FROM hq.plans WHERE child_id=%s ORDER BY version DESC LIMIT 1",
                (child["id"],),
            ).fetchone()
            release = load_release(conn)
            policy = one(conn, "SELECT * FROM hq.policy WHERE name='onboarding'")
        store_feedback = bool(intake["data"].get("store_feedback"))
        context = {
            "concerns": intake["data"]["concerns"],
            "available_minutes": intake["data"]["available_minutes"],
            "world": child["preferred_world"],
            "timezone": child["timezone"],
            "age_band": child["age_band"],
            "interests": sorted(child["interests"]),
            "policy_version": policy["version"],
            "max_active_habits": policy["max_active_habits"],
            "profile_version": child["profile_version"],
            "base_plan_version": job["base_plan_version"],
        }
        with psycopg.connect(
            settings.database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
        ) as cp:
            cp.execute("SET ROLE hq_worker")
            cp.execute("SET search_path TO hq_checkpoint")
            with tracing_context(enabled=False):
                data = generate(
                    settings,
                    context,
                    profile_from_row(child),
                    release,
                    # Legacy plans (no release) carry nothing forward.
                    base["data"] if base and base["release_id"] is not None else {},
                    PostgresSaver(cp),
                    job["checkpoint_thread"],
                    charge,
                    record,
                    job["resume_thread"],
                    job["deadline"].timestamp(),
                )
        with transaction(settings, job["parent_id"], worker=True) as conn:
            one(
                conn,
                "SELECT id FROM hq.jobs WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>now() AND deadline>now() FOR UPDATE",
                (job["id"], job["lease_token"]),
            )
            child = one(conn, "SELECT profile_version FROM hq.children WHERE id=%s", (job["child_id"],))
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,1))", (str(job["child_id"]),))
            conn.execute("SELECT hq.lock_intake(%s)", (job["intake_id"],))
            intake = one(conn, "SELECT version FROM hq.intakes WHERE id=%s", (job["intake_id"],))
            if (
                intake["version"] != job["intake_version"]
                or current_version(conn, job["child_id"]) != job["base_plan_version"]
            ):
                raise ValueError("State changed while planning")
            if child["profile_version"] != data["profile_version"]:
                raise ValueError("Profile changed while planning")
            data["release_id"] = validate_sources(conn, data)
            conn.execute(
                "INSERT INTO hq.proposals(id,child_id,parent_id,base_plan_version,intake_version,release_id,data) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                (
                    job["id"],
                    job["child_id"],
                    job["parent_id"],
                    job["base_plan_version"],
                    job["intake_version"],
                    data["release_id"],
                    Jsonb(data),
                ),
            )
            if store_feedback and data.get("unmatched_phrases"):
                store_catalog_requests(conn, data["unmatched_phrases"])
            updated = conn.execute(
                "UPDATE hq.jobs SET status='ready',lease_token=NULL,lease_until=NULL,error=NULL WHERE id=%s AND deadline>clock_timestamp() RETURNING id",
                (job["id"],),
            ).fetchone()
            if updated is None:
                raise BudgetExceeded("Publication deadline lost")
    except (ValueError, HTTPException, BudgetExceeded) as exc:
        with transaction(settings, worker=True) as conn:
            conn.execute(
                "UPDATE hq.jobs SET status='failed',error=%s,lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s AND status='running'",
                (type(exc).__name__, job["id"], job["lease_token"]),
            )
    except Exception:
        # Provider / transient infrastructure failures are bounded by max_attempts.
        with transaction(settings, worker=True) as conn:
            conn.execute(
                "UPDATE hq.jobs SET status=CASE WHEN attempts>=%s THEN 'failed' ELSE 'queued' END,error='provider_or_infrastructure_failure',available_at=now()+make_interval(secs=>attempts*2),lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s AND status='running'",
                (settings.max_attempts, job["id"], job["lease_token"]),
            )
    finally:
        stop.set()
        thread.join(timeout=1)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    while True:
        worked = run_once(settings)
        if args.once:
            return
        if not worked:
            time.sleep(1)


if __name__ == "__main__":
    main()
