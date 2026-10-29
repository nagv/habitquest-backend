from uuid import UUID

import psycopg
import pytest
from conftest import CONFIRM, queued, token

from habitquest_backend import worker

AUTHOR = UUID("e0000000-0000-0000-0000-0000000000e1")
REVIEWER = UUID("e0000000-0000-0000-0000-0000000000e2")
HABIT = {
    "title": "Tidy the desk",
    "instruction": "Put pencils and papers in their agreed place.",
    "min_age": 6,
    "max_age": 13,
    "routine_anchor": "after_school",
    "levels": [{"level": "standard", "minutes": 5, "points": 1}],
    "goals": {"responsibility": 0.9},
    "prepares": ["pack-bag"],
    "tags": ["access:short_instructions", "interest:art"],
    "variants": {"art": {"title": "Studio reset", "instruction": "Put your art things back in their place."}},
}


class Editors:
    """One app, two editor identities and a parent, chosen per request."""

    def __init__(self, client, settings):
        self.client = client
        self.headers = {
            name: {"Authorization": "Bearer " + token(settings, user, app_metadata={"hq_role": "editor"})}
            for name, user in (("author", AUTHOR), ("reviewer", REVIEWER))
        }

    def __call__(self, who, method, path, **kwargs):
        return self.client.request(method, "/v1/editor" + path, headers=self.headers[who], **kwargs)


@pytest.fixture
def editors(client, settings):
    return Editors(client, settings)


def approved(editors, plural, entity_id, body):
    """Create, submit (author) and approve (reviewer) a version; return the approved record."""
    created = editors("author", "POST", f"/{plural}/{entity_id}/versions", json=body)
    assert created.status_code == 201, created.text
    version = created.json()["version"]
    path = f"/{plural}/{entity_id}/versions/{version}"
    assert editors("author", "POST", path + "/submit").status_code == 200
    response = editors("reviewer", "POST", path + "/approve")
    assert response.status_code == 200, response.text
    return response.json()


def test_editor_routes_require_editor_role(client, settings, editors):
    assert client.get("/v1/editor/releases", headers={"Authorization": ""}).status_code == 401
    # The parent client's token has no hq_role claim.
    assert client.get("/v1/editor/releases").status_code == 403
    other = token(settings, AUTHOR, app_metadata={"hq_role": "support"})
    assert client.get("/v1/editor/releases", headers={"Authorization": "Bearer " + other}).status_code == 403
    assert [r["id"] for r in editors("author", "GET", "/releases").json()] == [1]


def test_habit_lifecycle_two_person_review_and_audit(editors):
    created = editors("author", "POST", "/habits/tidy-desk/versions", json=HABIT)
    assert created.status_code == 201, created.text
    record = created.json()
    assert (record["version"], record["status"], record["created_by"]) == (1, "draft", str(AUTHOR))
    assert record["supports"] == {"pack-bag": "prepares"}
    assert record["variants"]["art"]["title"] == "Studio reset"
    path = "/habits/tidy-desk/versions/1"

    edited = editors(
        "author", "PUT", path, json={**HABIT, "levels": [{"level": "simple", "minutes": 2, "points": 1}]}
    )
    assert [level["level"] for level in edited.json()["levels"]] == ["simple"]
    assert editors("author", "POST", "/habits/tidy-desk/versions", json=HABIT).status_code == 409

    assert editors("author", "POST", path + "/submit").json()["status"] == "in_review"
    assert editors("author", "PUT", path, json=HABIT).status_code == 409
    assert editors("author", "POST", path + "/submit").status_code == 409
    assert editors("author", "POST", path + "/approve").status_code == 403

    record = editors("reviewer", "POST", path + "/approve").json()
    assert (record["status"], record["reviewed_by"]) == ("approved", str(REVIEWER))
    assert [(a["from_status"], a["to_status"]) for a in record["audit"]] == [
        ("draft", "in_review"),
        ("in_review", "approved"),
    ]
    assert editors("reviewer", "POST", path + "/approve").status_code == 409
    assert editors("author", "PUT", path, json=HABIT).status_code == 409

    second = editors("reviewer", "POST", "/habits/tidy-desk/versions", json=HABIT).json()
    assert (second["version"], second["created_by"]) == (2, str(REVIEWER))


def test_rejection_returns_draft_with_reason(editors):
    editors("author", "POST", "/goals/homework/versions", json={"title": "Start homework calmly"})
    path = "/goals/homework/versions/1"
    editors("author", "POST", path + "/submit")
    assert editors("author", "POST", path + "/reject", json={"reason": "Self review"}).status_code == 403
    record = editors("reviewer", "POST", path + "/reject", json={"reason": "Clarify the title"}).json()
    assert record["status"] == "draft"
    assert record["audit"][-1]["reason"] == "Clarify the title"
    assert editors("author", "PUT", path, json={"title": "Start homework with a plan"}).status_code == 200


def test_unknown_versions_and_entities(editors):
    assert editors("reviewer", "POST", "/goals/nope/versions/1/approve").status_code == 404
    assert editors("author", "GET", "/goals/nope/versions/1").status_code == 404
    assert editors("author", "PUT", "/goals/nope/versions/1", json={"title": "x"}).status_code == 404
    assert editors("author", "GET", "/widgets/x/versions/1").status_code == 422
    assert editors("author", "POST", "/goals/Bad_Id/versions", json={"title": "x"}).status_code == 422


@pytest.mark.parametrize(
    ("plural", "body", "reasons"),
    [
        (
            "habits",
            {**HABIT, "instruction": "Tidy up or else you will be punished.", "goals": {"nope": 1}},
            ["unknown_goal:nope", "punishment", "coercive_wording"],
        ),
        ("goals", {"title": "Lose weight before summer"}, ["weight_loss"]),
        ("concerns", {"label": "Messy desk", "goals": {"nope": 1}}, ["unknown_goal:nope"]),
        (
            "chapters",
            {"world": "space", "goal_id": "nope", "title": "Keep the secret base"},
            ["unknown_goal:nope", "secrecy"],
        ),
    ],
)
def test_submit_checks_keep_drafts(editors, plural, body, reasons):
    editors("author", "POST", f"/{plural}/flagged/versions", json=body)
    response = editors("author", "POST", f"/{plural}/flagged/versions/1/submit")
    assert response.status_code == 422
    assert response.json()["detail"]["reasons"] == reasons
    assert editors("author", "GET", f"/{plural}/flagged/versions/1").json()["status"] == "draft"


def test_release_build_diff_validate_and_publish(client, settings, editors):
    approved(editors, "goals", "homework", {"title": "Start homework calmly"})
    approved(editors, "concerns", "homework-fights", {"label": "Homework fights", "goals": {"homework": 1}})
    approved(editors, "habits", "tidy-desk", {**HABIT, "goals": {"homework": 1, "responsibility": 0.5}})
    approved(
        editors,
        "chapters",
        "space-homework",
        {"world": "space", "goal_id": "homework", "title": "Study Deck"},
    )

    built = editors("author", "POST", "/releases", json={"notes": "Homework"})
    assert built.status_code == 201
    release = built.json()
    assert (release["id"], release["built_by"], release["published_at"]) == (2, str(AUTHOR), None)

    changes = editors("author", "GET", "/releases/2/diff").json()
    assert changes["base_release_id"] == 1
    assert changes["changes"]["goal"]["added"] == [{"id": "homework", "version": 1}]
    assert [item["id"] for item in changes["changes"]["chapter"]["added"]] == ["space-homework"]
    assert changes["changes"]["habit"]["removed"] == []

    report = editors("author", "GET", "/releases/2/validate").json()
    assert report["errors"] == ["missing_chapter:forest:homework", "missing_chapter:city:homework"]
    refused = editors("reviewer", "POST", "/releases/2/publish")
    assert refused.status_code == 422
    assert refused.json()["detail"]["errors"] == report["errors"]

    for world in ("forest", "city"):
        approved(
            editors,
            "chapters",
            f"{world}-homework",
            {"world": world, "goal_id": "homework", "title": "Homework"},
        )
    assert editors("reviewer", "POST", "/releases", json={}).json()["id"] == 3
    assert editors("reviewer", "GET", "/releases/3/validate").json() == {"errors": [], "warnings": []}
    assert editors("reviewer", "POST", "/releases/3/publish").status_code == 403
    published = editors("author", "POST", "/releases/3/publish").json()
    assert (published["published_by"], published["is_current"]) == (str(AUTHOR), True)
    assert editors("author", "POST", "/releases/3/publish").status_code == 409
    assert editors("author", "POST", "/releases/99/publish").status_code == 404
    assert editors("author", "GET", "/releases/99/diff").status_code == 404

    # Parents plan against the newly published release.
    _, _, run = queued(client)
    worker.run_once(settings)
    assert client.get(f"/v1/proposals/{run}").json()["release_id"] == 3


def test_rollback_republishes_prior_items(editors):
    approved(editors, "goals", "homework", {"title": "Start homework calmly"})
    assert editors("author", "POST", "/releases", json={"source_release_id": 1}).json()["id"] == 2
    assert editors("author", "GET", "/releases/2/diff").json()["changes"]["goal"]["added"] == []
    assert editors("author", "POST", "/releases", json={"source_release_id": 2}).status_code == 404


def test_withdrawal_flags_plans_and_blocks_confirmation(client, settings, editors):
    _, _, first = queued(client, key="first")
    worker.run_once(settings)
    assert client.post(f"/v1/proposals/{first}/confirm", json=CONFIRM).status_code == 200
    plan = client.get(f"/v1/proposals/{first}").json()
    child_id = plan["child_id"]
    habit_id = plan["data"]["active"][0]["habit_id"]
    _, _, second = queued(client, key="second")
    worker.run_once(settings)

    response = editors("author", "POST", f"/habits/{habit_id}/withdraw", json={"reason": "Safety review"})
    assert response.json() == {"habit_id": habit_id, "flagged_plans": 1}
    flags = client.get(f"/v1/children/{child_id}/plan-flags").json()
    assert [(f["kind"], f["habit_id"]) for f in flags] == [("habit_withdrawn", habit_id)]
    confirm = client.post(f"/v1/proposals/{second}/confirm", json={**CONFIRM, "expected_plan_version": 1})
    assert confirm.status_code == 409

    assert (
        editors("author", "POST", f"/habits/{habit_id}/withdraw", json={"reason": "Again"}).status_code == 409
    )
    assert editors("author", "POST", "/habits/nope/withdraw", json={"reason": "x"}).status_code == 404
    record = editors("author", "GET", f"/habits/{habit_id}/versions/1").json()
    assert (record["withdrawn_reason"], record["audit"][-1]["to_status"]) == ("Safety review", "withdrawn")

    # New builds leave the habit out; a rollback copy skips the withdrawn version too.
    for body in ({}, {"source_release_id": 1}):
        release_id = editors("author", "POST", "/releases", json=body).json()["id"]
        removed = editors("author", "GET", f"/releases/{release_id}/diff").json()["changes"]["habit"][
            "removed"
        ]
        assert removed == [{"id": habit_id, "version": 1}]


def test_validate_reports_habit_withdrawn_after_build(editors):
    editors("author", "POST", "/releases", json={})
    editors("author", "POST", "/habits/pack-bag/withdraw", json={"reason": "Safety review"})
    report = editors("reviewer", "GET", "/releases/2/validate").json()
    assert report["errors"] == ["habit_withdrawn:pack-bag"]


def test_catalog_requests_listing(client, settings, editors):
    queued(client, concerns="dragon fire is a real problem here", store_feedback=True)
    worker.run_once(settings)
    rows = editors("author", "GET", "/catalog-requests").json()
    assert rows and all(set(row) == {"normalized_phrase", "count", "first_seen", "last_seen"} for row in rows)


def as_role(db, role, editor=None):
    db.execute(f"SET ROLE {role}")
    db.execute("SELECT set_config('app.editor_id',%s,false)", (str(editor or ""),))


@pytest.fixture
def raw(db):
    yield db
    db.execute("RESET ROLE")


@pytest.mark.parametrize("table", ["children", "households", "plans", "proposals", "intakes", "plan_flags"])
def test_editor_role_cannot_read_household_tables(raw, table):
    as_role(raw, "hq_editor", AUTHOR)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw.execute(f"SELECT 1 FROM hq.{table} LIMIT 0")


@pytest.mark.parametrize("role", ["hq_api", "hq_worker"])
@pytest.mark.parametrize("view", ["habit_version_details", "release_habits", "catalog_audit"])
def test_service_roles_cannot_read_drafts(raw, role, view):
    as_role(raw, role)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw.execute(f"SELECT 1 FROM hq.{view} LIMIT 0")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE hq.goals SET status='approved' WHERE id='bedtime'",
        "INSERT INTO hq.goals(id,version,title,created_by) VALUES('forged',1,'x','" + str(REVIEWER) + "')",
        "INSERT INTO hq.release_items VALUES(1,'goal','forged',1)",
        "UPDATE hq.catalog_releases SET is_current=false",
    ],
)
def test_editor_role_cannot_bypass_lifecycle(raw, statement):
    as_role(raw, "hq_editor", AUTHOR)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        raw.execute(statement)


def test_database_functions_enforce_two_person_rules(raw):
    as_role(raw, "hq_editor")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="Editor identity"):
        raw.execute("SELECT hq.build_release('x')")
    as_role(raw, "hq_editor", AUTHOR)
    raw.execute("INSERT INTO hq.goals(id,version,title) VALUES('homework',1,'Homework')")
    assert raw.execute("SELECT created_by FROM hq.goals WHERE id='homework'").fetchone() == (AUTHOR,)
    raw.execute("SELECT hq.transition_version('goal','homework',1,'submit')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="Reviewer must differ"):
        raw.execute("SELECT hq.transition_version('goal','homework',1,'approve')")
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        raw.execute("SELECT hq.transition_version('widget','homework',1,'approve')")
    as_role(raw, "hq_editor", REVIEWER)
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="reason"):
        raw.execute("SELECT hq.transition_version('goal','homework',1,'reject',' ')")
    with pytest.raises(psycopg.errors.InvalidParameterValue, match="reason"):
        raw.execute("SELECT hq.withdraw_habit('pack-bag','')")
    release = raw.execute("SELECT hq.build_release('x')").fetchone()[0]
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="Publisher must differ"):
        raw.execute("SELECT hq.publish_release(%s)", (release,))


def test_in_review_details_are_frozen(raw):
    as_role(raw, "hq_editor", AUTHOR)
    raw.execute("INSERT INTO hq.concerns(id,version,label) VALUES('desk',1,'Messy desk')")
    raw.execute("INSERT INTO hq.concern_goals VALUES('desk',1,'responsibility',1)")
    raw.execute("SELECT hq.transition_version('concern','desk',1,'submit')")
    raw.execute("INSERT INTO hq.habits VALUES('desk')")
    raw.execute(
        "INSERT INTO hq.habit_versions(habit_id,version,title,instruction,min_age,max_age,routine_anchor) "
        "VALUES('desk',1,'Desk','Tidy the desk.',6,13,'evening')"
    )
    raw.execute("INSERT INTO hq.habit_levels VALUES('desk',1,'standard',5,1,NULL)")
    raw.execute("SELECT hq.transition_version('habit','desk',1,'submit')")
    for statement in (
        "DELETE FROM hq.habit_levels WHERE habit_id='desk'",
        "INSERT INTO hq.habit_levels VALUES('desk',1,'simple',2,1,NULL)",
        "UPDATE hq.habit_versions SET title='x' WHERE habit_id='desk'",
        "DELETE FROM hq.concern_goals WHERE concern_id='desk'",
        "INSERT INTO hq.concern_goals VALUES('desk',1,'meals',1)",
        "UPDATE hq.concerns SET label='x' WHERE id='desk'",
    ):
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            raw.execute(statement)
