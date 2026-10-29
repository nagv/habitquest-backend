from typing import Any

from habitquest_backend.catalog.models import Chapter, Concern, Goal, Habit, Level, Release
from habitquest_backend.db import one


def load_release(conn: Any, release_id: int | None = None, *, candidate: bool = False) -> Release:
    """Load a published release through the read-only released_* views. Editors pass `candidate`
    to read any release, published or not, through the editor-only release_* views."""
    if release_id is None:
        release_id = one(conn, "SELECT id FROM hq.current_release")["id"]
    elif candidate:
        one(conn, "SELECT id FROM hq.catalog_releases WHERE id=%s", (release_id,))
    else:
        one(conn, "SELECT id FROM hq.published_releases WHERE id=%s", (release_id,))
    views = "release" if candidate else "released"
    params = (release_id,)
    goals = {
        row["goal_id"]: Goal(row["goal_id"], row["version"], row["title"], row["description"])
        for row in conn.execute(f"SELECT * FROM hq.{views}_goals WHERE release_id=%s", params)
    }
    concerns = {
        row["concern_id"]: Concern(
            row["concern_id"],
            row["version"],
            row["label"],
            tuple(row["synonyms"]),
            {goal: float(weight) for goal, weight in row["goals"].items()},
        )
        for row in conn.execute(f"SELECT * FROM hq.{views}_concerns WHERE release_id=%s", params)
    }
    habits = {
        row["habit_id"]: Habit(
            id=row["habit_id"],
            version=row["version"],
            title=row["title"],
            instruction=row["instruction"],
            min_age=row["min_age"],
            max_age=row["max_age"],
            routine_anchor=row["routine_anchor"],
            levels=tuple(Level(**level) for level in row["levels"]),
            goals={goal: float(strength) for goal, strength in row["goals"].items()},
            tags=frozenset(row["tags"]),
            variants={k: (v["title"], v["instruction"]) for k, v in row["variants"].items()},
            supports=dict(row["supports"]),
            withdrawn=row["withdrawn"],
        )
        for row in conn.execute(f"SELECT * FROM hq.{views}_habits WHERE release_id=%s", params)
    }
    chapters = {
        (row["world"], row["goal_id"]): Chapter(
            row["chapter_id"], row["version"], row["world"], row["goal_id"], row["title"]
        )
        for row in conn.execute(f"SELECT * FROM hq.{views}_chapters WHERE release_id=%s", params)
    }
    return Release(release_id, goals, concerns, habits, chapters)
