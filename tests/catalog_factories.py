from habitquest_backend.catalog.models import Habit, Level, Release


def make_habit(
    habit_id="h",
    *,
    goals=None,
    min_age=6,
    max_age=13,
    anchor="bedtime",
    levels=None,
    tags=(),
    supports=None,
    variants=None,
    withdrawn=False,
):
    return Habit(
        id=habit_id,
        version=1,
        title=f"Title {habit_id}",
        instruction=f"Do {habit_id}.",
        min_age=min_age,
        max_age=max_age,
        routine_anchor=anchor,
        levels=tuple(levels or (Level("standard", 5, 1),)),
        goals=goals if goals is not None else {"g": 1.0},
        tags=frozenset(tags),
        variants=variants or {},
        supports=supports or {},
        withdrawn=withdrawn,
    )


def make_release(*habits):
    return Release(id=1, goals={}, concerns={}, habits={h.id: h for h in habits}, chapters={})
