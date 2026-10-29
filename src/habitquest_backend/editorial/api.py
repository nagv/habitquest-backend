from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg import errors
from pydantic import BaseModel

from habitquest_backend.auth import verify_token
from habitquest_backend.catalog.releases import diff_items, validate_release
from habitquest_backend.catalog.repository import load_release
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction
from habitquest_backend.editorial import checks
from habitquest_backend.editorial.schemas import (
    ChapterDraft,
    ConcernDraft,
    GoalDraft,
    HabitDraft,
    Reason,
    ReleaseBuild,
)

KindName = Literal["habits", "goals", "concerns", "chapters"]
EntityId = Annotated[str, Path(pattern=r"^[a-z][a-z0-9-]*$", max_length=60)]


@dataclass(frozen=True)
class Kind:
    entity: str
    table: str
    id_column: str
    columns: tuple[str, ...]
    view: str
    body: type[BaseModel]
    findings: Callable[[dict, set[str]], list[str]]


KINDS: dict[str, Kind] = {
    "habits": Kind(
        "habit",
        "habit_versions",
        "habit_id",
        ("title", "instruction", "min_age", "max_age", "routine_anchor", "safety_notes"),
        "habit_version_details",
        HabitDraft,
        checks.habit_findings,
    ),
    "goals": Kind("goal", "goals", "id", ("title", "description"), "goals", GoalDraft, checks.goal_findings),
    "concerns": Kind(
        "concern",
        "concerns",
        "id",
        ("label", "synonyms"),
        "concern_version_details",
        ConcernDraft,
        checks.concern_findings,
    ),
    "chapters": Kind(
        "chapter",
        "chapter_templates",
        "id",
        ("world", "goal_id", "title"),
        "chapter_templates",
        ChapterDraft,
        checks.chapter_findings,
    ),
}

# The lifecycle functions refuse with these SQLSTATEs; the API reports the same refusal.
REFUSALS = {"42501": 403, "P0002": 404, "55000": 409, "22023": 422}


@contextmanager
def refusals() -> Iterator[None]:
    try:
        yield
    except (
        errors.InsufficientPrivilege,
        errors.NoDataFound,
        errors.ObjectNotInPrerequisiteState,
        errors.InvalidParameterValue,
    ) as exc:
        raise HTTPException(REFUSALS[exc.sqlstate or ""], exc.diag.message_primary) from exc


def write_details(conn, kind: Kind, entity_id: str, version: int, body: BaseModel) -> None:
    data = body.model_dump()
    key = (entity_id, version)
    if kind.entity == "concern":
        conn.execute("DELETE FROM hq.concern_goals WHERE concern_id=%s AND concern_version=%s", key)
        for goal, weight in data["goals"].items():
            conn.execute("INSERT INTO hq.concern_goals VALUES(%s,%s,%s,%s)", (*key, goal, weight))
    if kind.entity != "habit":
        return
    for table in ("habit_levels", "habit_goals", "habit_prereqs", "habit_tags", "habit_variants"):
        conn.execute(f"DELETE FROM hq.{table} WHERE habit_id=%s AND version=%s", key)
    for level in data["levels"]:
        conn.execute(
            "INSERT INTO hq.habit_levels VALUES(%s,%s,%s,%s,%s,%s)",
            (*key, level["level"], level["minutes"], level["points"], level["instruction"]),
        )
    for goal, strength in data["goals"].items():
        conn.execute("INSERT INTO hq.habit_goals VALUES(%s,%s,%s,%s)", (*key, goal, strength))
    for other in data["prepares"]:
        conn.execute("INSERT INTO hq.habit_prereqs VALUES(%s,%s,%s,'prepares')", (*key, other))
    for tag in data["tags"]:
        conn.execute("INSERT INTO hq.habit_tags VALUES(%s,%s,%s)", (*key, tag))
    for interest, variant in data["variants"].items():
        conn.execute(
            "INSERT INTO hq.habit_variants VALUES(%s,%s,%s,%s,%s)",
            (*key, interest, variant["title"], variant["instruction"]),
        )


def version_view(conn, kind: Kind, entity_id: str, version: int) -> dict:
    record = one(
        conn,
        f"SELECT * FROM hq.{kind.view} WHERE {kind.id_column}=%s AND version=%s",
        (entity_id, version),
    )
    audit = conn.execute(
        "SELECT from_status,to_status,actor,reason,at FROM hq.catalog_audit "
        "WHERE entity=%s AND entity_id=%s AND version=%s ORDER BY id",
        (kind.entity, entity_id, version),
    ).fetchall()
    return {**record, "audit": audit}


def lock_draft(conn, kind: Kind, entity_id: str, version: int) -> None:
    row = one(
        conn,
        f"SELECT status FROM hq.{kind.table} WHERE {kind.id_column}=%s AND version=%s FOR UPDATE",
        (entity_id, version),
    )
    if row["status"] != "draft":
        raise HTTPException(409, "Only drafts can be edited or submitted")


def release_items(conn, release_id: int) -> dict[tuple[str, str], int]:
    rows = conn.execute(
        "SELECT entity,entity_id,version FROM hq.release_items WHERE release_id=%s", (release_id,)
    ).fetchall()
    return {(row["entity"], row["entity_id"]): row["version"] for row in rows}


def router(settings: Settings) -> APIRouter:
    api = APIRouter(prefix="/v1/editor", tags=["editor"])
    bearer = HTTPBearer(auto_error=False)

    def editor(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> UUID:
        if credentials is None:
            raise HTTPException(401, "Editor authentication required")
        return verify_token(credentials.credentials, settings, hq_role="editor")

    Editor = Annotated[UUID, Depends(editor)]

    def register(plural: str, kind: Kind) -> None:
        body_type = kind.body

        @api.post(f"/{plural}/{{entity_id}}/versions", status_code=201, name=f"create_{kind.entity}_draft")
        def create_draft(entity_id: EntityId, body: body_type, user: Editor):  # type: ignore[valid-type]
            """Start a new entity, or a new draft version of an existing one."""
            with transaction(settings, editor=user) as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,2))", (f"{kind.entity}:{entity_id}",)
                )
                versions = conn.execute(
                    f"SELECT version,status FROM hq.{kind.table} WHERE {kind.id_column}=%s", (entity_id,)
                ).fetchall()
                if any(row["status"] in ("draft", "in_review") for row in versions):
                    raise HTTPException(409, "An open draft already exists for this entity")
                version = max((row["version"] for row in versions), default=0) + 1
                if kind.entity == "habit":
                    conn.execute("INSERT INTO hq.habits VALUES(%s) ON CONFLICT DO NOTHING", (entity_id,))
                data = cast(BaseModel, body).model_dump()
                columns = ",".join((kind.id_column, "version", *kind.columns))
                placeholders = ",".join(["%s"] * (len(kind.columns) + 2))
                conn.execute(
                    f"INSERT INTO hq.{kind.table}({columns}) VALUES({placeholders})",
                    (entity_id, version, *(data[c] for c in kind.columns)),
                )
                write_details(conn, kind, entity_id, version, body)
                return version_view(conn, kind, entity_id, version)

        @api.put(f"/{plural}/{{entity_id}}/versions/{{version}}", name=f"edit_{kind.entity}_draft")
        def edit_draft(entity_id: EntityId, version: int, body: body_type, user: Editor):  # type: ignore[valid-type]
            with transaction(settings, editor=user) as conn:
                lock_draft(conn, kind, entity_id, version)
                data = cast(BaseModel, body).model_dump()
                assignments = ",".join(f"{c}=%s" for c in kind.columns)
                conn.execute(
                    f"UPDATE hq.{kind.table} SET {assignments} WHERE {kind.id_column}=%s AND version=%s",
                    (*(data[c] for c in kind.columns), entity_id, version),
                )
                write_details(conn, kind, entity_id, version, body)
                return version_view(conn, kind, entity_id, version)

    for plural, kind in KINDS.items():
        register(plural, kind)

    @api.get("/{kind_name}/{entity_id}/versions/{version}")
    def get_version(kind_name: KindName, entity_id: EntityId, version: int, user: Editor):
        with transaction(settings, editor=user) as conn:
            return version_view(conn, KINDS[kind_name], entity_id, version)

    @api.post("/{kind_name}/{entity_id}/versions/{version}/submit")
    def submit(kind_name: KindName, entity_id: EntityId, version: int, user: Editor):
        kind = KINDS[kind_name]
        with transaction(settings, editor=user) as conn:
            lock_draft(conn, kind, entity_id, version)
            record = version_view(conn, kind, entity_id, version)
            known_goals = {row["id"] for row in conn.execute("SELECT DISTINCT id FROM hq.goals")}
            findings = kind.findings(record, known_goals)
            if findings:
                raise HTTPException(422, {"message": "Draft failed submit checks", "reasons": findings})
            conn.execute("SELECT hq.transition_version(%s,%s,%s,'submit')", (kind.entity, entity_id, version))
            return version_view(conn, kind, entity_id, version)

    @api.post("/{kind_name}/{entity_id}/versions/{version}/approve")
    def approve(kind_name: KindName, entity_id: EntityId, version: int, user: Editor):
        kind = KINDS[kind_name]
        with transaction(settings, editor=user) as conn, refusals():
            conn.execute(
                "SELECT hq.transition_version(%s,%s,%s,'approve')", (kind.entity, entity_id, version)
            )
            return version_view(conn, kind, entity_id, version)

    @api.post("/{kind_name}/{entity_id}/versions/{version}/reject")
    def reject(kind_name: KindName, entity_id: EntityId, version: int, body: Reason, user: Editor):
        kind = KINDS[kind_name]
        with transaction(settings, editor=user) as conn, refusals():
            conn.execute(
                "SELECT hq.transition_version(%s,%s,%s,'reject',%s)",
                (kind.entity, entity_id, version, body.reason),
            )
            return version_view(conn, kind, entity_id, version)

    @api.post("/habits/{entity_id}/withdraw")
    def withdraw(entity_id: EntityId, body: Reason, user: Editor):
        """Emergency withdrawal: immediate, permanent, and flags confirmed plans for parent review."""
        with transaction(settings, editor=user) as conn, refusals():
            flagged = one(conn, "SELECT hq.withdraw_habit(%s,%s) AS n", (entity_id, body.reason))["n"]
            return {"habit_id": entity_id, "flagged_plans": flagged}

    @api.get("/releases")
    def releases(user: Editor):
        with transaction(settings, editor=user) as conn:
            return conn.execute("SELECT * FROM hq.catalog_releases ORDER BY id DESC").fetchall()

    @api.post("/releases", status_code=201)
    def build(body: ReleaseBuild, user: Editor):
        with transaction(settings, editor=user) as conn, refusals():
            release_id = one(
                conn, "SELECT hq.build_release(%s,%s) AS id", (body.notes, body.source_release_id)
            )["id"]
            return one(conn, "SELECT * FROM hq.catalog_releases WHERE id=%s", (release_id,))

    @api.get("/releases/{release_id}/diff")
    def diff(release_id: int, user: Editor):
        with transaction(settings, editor=user) as conn:
            one(conn, "SELECT id FROM hq.catalog_releases WHERE id=%s", (release_id,))
            base = one(conn, "SELECT id FROM hq.current_release")["id"]
            return {
                "release_id": release_id,
                "base_release_id": base,
                "changes": diff_items(release_items(conn, release_id), release_items(conn, base)),
            }

    @api.get("/releases/{release_id}/validate")
    def validate(release_id: int, user: Editor):
        with transaction(settings, editor=user) as conn:
            return validate_release(load_release(conn, release_id, candidate=True))

    @api.post("/releases/{release_id}/publish")
    def publish(release_id: int, user: Editor):
        """A different editor from the builder publishes; validation errors block publication."""
        with transaction(settings, editor=user) as conn, refusals():
            report = validate_release(load_release(conn, release_id, candidate=True))
            if report["errors"]:
                raise HTTPException(422, {"message": "Release failed validation", **report})
            conn.execute("SELECT hq.publish_release(%s)", (release_id,))
            return {**one(conn, "SELECT * FROM hq.catalog_releases WHERE id=%s", (release_id,)), **report}

    @api.get("/catalog-requests")
    def catalog_requests(user: Editor):
        """Anonymised unmatched concern phrases, most frequent first."""
        with transaction(settings, editor=user) as conn:
            return conn.execute(
                "SELECT normalized_phrase,count,first_seen,last_seen FROM hq.catalog_requests "
                "ORDER BY count DESC,last_seen DESC LIMIT 200"
            ).fetchall()

    return api
