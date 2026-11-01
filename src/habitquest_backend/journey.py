"""The world journey: Sparks walk a child along a path through six worlds, finding landmarks on the way.

A quest earns Sparks equal to its tier, at most `DAILY_SPARKS` a day, so harder quests move a child further and a long
plan can't outrun a short one. Sparks are never lost. Each world gives the child a new rank and level, so levels and
worlds are one ladder. Points are separate: they are what a child spends in the shop.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends

from habitquest_backend.auth import KID, signed_in
from habitquest_backend.config import Settings
from habitquest_backend.db import one, transaction

DAILY_SPARKS = 6
STAR_APPROVALS = (5, 15, 30)  # bronze, silver and gold
RARE_STAR_APPROVALS = (3, 8, 15)  # for quests a child does on fewer than RARE_DAYS days a week
RARE_DAYS = 3


def spot(key: str, name: str, emoji: str, sparks: int, x: int, y: int) -> dict:
    """A landmark at `sparks` lifetime Sparks; `x` and `y` are percent of the world's scene."""
    return {"id": key, "name": name, "emoji": emoji, "sparks": sparks, "x": x, "y": y}


WORLDS: list[dict[str, Any]] = [
    {
        "id": "camp",
        "name": "Starter Camp",
        "sparks": 0,
        "rank": "Camp Sprout",
        "tier": 1,
        "blurb": "A cosy campfire where every adventure begins.",
        "landmarks": [
            spot("lantern", "Glowing lantern", "🏮", 3, 20, 62),
            spot("map", "Treasure map", "🗺️", 10, 60, 82),
            spot("owl", "Sleepy owl", "🦉", 18, 80, 30),
            spot("tent", "Secret tent", "⛺", 25, 42, 42),
        ],
    },
    {
        "id": "mosswood",
        "name": "Mosswood Forest",
        "sparks": 30,
        "rank": "Forest Explorer",
        "tier": 2,
        "blurb": "Giant trees, glowing mushrooms and a hidden portal.",
        "landmarks": [
            spot("mushroom", "Glow mushroom", "🍄", 38, 18, 80),
            spot("stream", "Whispering stream", "💧", 48, 36, 62),
            spot("fox", "Friendly fox", "🦊", 58, 72, 78),
            spot("nest", "Bird's nest", "🪺", 68, 86, 24),
            spot("treehouse", "Hidden treehouse", "🌳", 78, 62, 26),
            spot("portal", "Purple portal", "🌀", 86, 50, 50),
        ],
    },
    {
        "id": "sky",
        "name": "Sky Islands",
        "sparks": 90,
        "rank": "Sky Adventurer",
        "tier": 3,
        "blurb": "Floating islands, waterfalls and a balloon airship.",
        "landmarks": [
            spot("kite", "Rainbow kite", "🪁", 100, 14, 20),
            spot("waterfall", "Cloud waterfall", "🌊", 112, 28, 66),
            spot("bridge", "Rainbow bridge", "🌈", 124, 44, 82),
            spot("castle", "Tiny castle", "🏰", 136, 52, 36),
            spot("balloon", "Balloon airship", "🎈", 148, 80, 22),
            spot("windmill", "Whirly windmill", "🌬️", 160, 88, 60),
            spot("kitten", "Cloud kitten", "🐱", 172, 66, 72),
        ],
    },
    {
        "id": "coral",
        "name": "Coral Cove",
        "sparks": 180,
        "rank": "Reef Hero",
        "tier": 3,
        "blurb": "Bright reefs, friendly turtles and a sunken ship.",
        "landmarks": [
            spot("shell", "Singing shell", "🐚", 192, 14, 74),
            spot("crab", "Dancing crab", "🦀", 206, 30, 86),
            spot("turtle", "Old sea turtle", "🐢", 220, 46, 62),
            spot("starfish", "Sleepy starfish", "⭐", 234, 64, 84),
            spot("dolphin", "Playful dolphin", "🐬", 248, 84, 66),
            spot("ship", "Sunken ship", "🚢", 262, 78, 38),
            spot("pearl", "Giant pearl", "🦪", 276, 50, 30),
            spot("lighthouse", "Reef lighthouse", "🗼", 290, 20, 26),
        ],
    },
    {
        "id": "crystal",
        "name": "Crystal Caves",
        "sparks": 300,
        "rank": "Cave Legend",
        "tier": 3,
        "blurb": "Glittering tunnels, gentle dragons and hidden gems.",
        "landmarks": [
            spot("gem", "Blue gem", "💎", 318, 12, 78),
            spot("bat", "Tiny bat", "🦇", 336, 26, 42),
            spot("lake", "Mirror lake", "🪞", 354, 42, 78),
            spot("minecart", "Rusty mine cart", "🛒", 372, 56, 54),
            spot("egg", "Dragon egg", "🥚", 390, 72, 80),
            spot("dragon", "Gentle dragon", "🐉", 408, 88, 56),
            spot("door", "Crystal door", "🚪", 424, 74, 26),
            spot("crown", "Cave crown", "👑", 440, 46, 22),
        ],
    },
    {
        "id": "star",
        "name": "Star Harbour",
        "sparks": 450,
        "rank": "Star Captain",
        "tier": 3,
        "blurb": "A harbour in the night sky, with new trails to chart.",
        "landmarks": [
            spot("dock", "Moonlit dock", "⚓", 468, 12, 80),
            spot("rocket", "Little rocket", "🚀", 486, 28, 56),
            spot("comet", "Friendly comet", "☄️", 504, 44, 76),
            spot("planet", "Ringed planet", "🪐", 522, 60, 58),
            spot("moon", "Smiling moon", "🌙", 540, 76, 78),
            spot("telescope", "Giant telescope", "🔭", 558, 88, 48),
            spot("compass", "Star compass", "🧭", 576, 68, 26),
            spot("wish", "Wishing star", "🌟", 594, 40, 24),
        ],
    },
]
LANDMARKS = {f"{w['id']}:{mark['id']}": mark for w in WORLDS for mark in w["landmarks"]}


def spark_totals(conn, child_id: UUID) -> tuple[int, int]:
    """Lifetime Sparks, and those a grown-up has yet to approve: the footprints that wait on the path."""
    row = conn.execute(
        "SELECT coalesce(sum(least(%(cap)s,approved)),0)::int AS sparks,"
        "coalesce(sum(least(%(cap)s,approved+waiting)-least(%(cap)s,approved)),0)::int AS pending,"
        "(SELECT journey_bonus FROM hq.child_extras WHERE child_id=%(child)s) AS bonus FROM ("
        "SELECT c.day,coalesce(sum(h.tier) FILTER (WHERE c.status='approved'),0) AS approved,"
        "coalesce(sum(h.tier) FILTER (WHERE c.status='submitted'),0) AS waiting"
        " FROM hq.completions c JOIN hq.child_habits ch ON ch.id=c.child_habit_id"
        " JOIN hq.habits h ON h.id=ch.habit_id WHERE ch.child_id=%(child)s GROUP BY c.day) days",
        {"cap": DAILY_SPARKS, "child": child_id},
    ).fetchone()
    return row["sparks"] + row["bonus"], row["pending"]


def world_at(sparks: int) -> tuple[int, dict]:
    """The child's level and the world they have reached."""
    return [(n, world) for n, world in enumerate(WORLDS, 1) if sparks >= world["sparks"]][-1]


def next_landmark(sparks: int) -> dict | None:
    for world in WORLDS:
        for mark in world["landmarks"]:
            if mark["sparks"] > sparks:
                return {**mark, "world": world["id"]}
    return None


def progress(conn, child_id: UUID) -> dict:
    """Where the child is on the journey. A parent's `tier_override` replaces the tier their world opens."""
    sparks, pending = spark_totals(conn, child_id)
    level, world = world_at(sparks)
    override = conn.execute("SELECT tier_override AS n FROM hq.children WHERE id=%s", (child_id,)).fetchone()[
        "n"
    ]
    following = [w["sparks"] for w in WORLDS if w["sparks"] > sparks]
    return {
        "sparks": sparks,
        "pending_sparks": pending,
        "level": level,
        "level_name": world["rank"],
        "world": world["id"],
        "unlocked_tier": override or world["tier"],
        "next_level_at": following[0] if following else None,
        "next_landmark": next_landmark(sparks),
    }


def star_targets(days_a_week: int) -> tuple[int, int, int]:
    return RARE_STAR_APPROVALS if days_a_week < RARE_DAYS else STAR_APPROVALS


def stars_for(approvals: int, targets: tuple[int, int, int] = STAR_APPROVALS) -> int:
    return sum(approvals >= needed for needed in targets)


def quest_stars(conn, child_id: UUID) -> list[dict]:
    """Every quest the child has been approved for, with its stars and the `targets` for each, most practised first."""
    rows = conn.execute(
        "SELECT ch.habit_id,h.kid_title,h.icon,count(*)::int AS approvals,max(cardinality(ch.days)) AS days"
        " FROM hq.completions c"
        " JOIN hq.child_habits ch ON ch.id=c.child_habit_id JOIN hq.habits h ON h.id=ch.habit_id"
        " WHERE ch.child_id=%s AND c.status='approved' GROUP BY ch.habit_id,h.kid_title,h.icon"
        " ORDER BY approvals DESC,h.kid_title",
        (child_id,),
    ).fetchall()
    for row in rows:
        row["targets"] = star_targets(row.pop("days"))
        row["stars"] = stars_for(row["approvals"], row["targets"])
    return rows


def journey_view(conn, child_id: UUID) -> dict:
    found = conn.execute("SELECT discoveries FROM hq.child_extras WHERE child_id=%s", (child_id,)).fetchone()[
        "discoveries"
    ]
    return {
        "progress": progress(conn, child_id),
        "worlds": WORLDS,
        "discoveries": found,
        "quest_stars": quest_stars(conn, child_id),
    }


def router(settings: Settings) -> APIRouter:
    api = APIRouter()
    User = Annotated[UUID, Depends(signed_in(settings))]
    Kid = Annotated[UUID, Depends(signed_in(settings, KID))]

    @api.get("/v1/kid/journey", tags=["Kid mode"], summary="The world journey")
    def kid_journey(kid: Kid) -> dict:
        """With a kid token: `progress`, all six `worlds` with their landmarks and the Sparks each needs, the
        `discoveries` the child has collected, and `quest_stars` for each quest they have practised.
        """
        with transaction(settings, kid=kid) as conn:
            return journey_view(conn, kid)

    @api.get("/v1/children/{child_id}/journey", tags=["Children"], summary="A child's journey")
    def child_journey(child_id: UUID, user: User) -> dict:
        """The same journey the child sees in kid mode. 404 unless the caller cares for the child."""
        with transaction(settings, user) as conn:
            one(conn, "SELECT id FROM hq.children WHERE id=%s", (child_id,))
            return journey_view(conn, child_id)

    return api
