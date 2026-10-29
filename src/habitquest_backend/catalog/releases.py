from habitquest_backend.catalog.eligibility import AGE_BANDS
from habitquest_backend.catalog.models import Release

WORLDS = ("space", "forest", "city")
ENTITIES = ("goal", "concern", "habit", "chapter")


def validate_release(release: Release) -> dict[str, list[str]]:
    """Errors block publication; warnings are for the publishing editor to weigh."""
    errors = [
        f"missing_chapter:{world}:{goal}"
        for goal in sorted(release.goals)
        for world in WORLDS
        if (world, goal) not in release.chapters
    ]
    errors += [
        f"chapter_unknown_goal:{chapter.id}:{chapter.goal_id}"
        for chapter in sorted(release.chapters.values(), key=lambda c: c.id)
        if chapter.goal_id not in release.goals
    ]
    for habit in sorted(release.habits.values(), key=lambda h: h.id):
        if habit.withdrawn:
            errors.append(f"habit_withdrawn:{habit.id}")
        if not habit.levels:
            errors.append(f"habit_without_levels:{habit.id}")
        errors += [
            f"habit_unknown_goal:{habit.id}:{goal}"
            for goal in sorted(habit.goals)
            if goal not in release.goals
        ]
        errors += [
            f"habit_unknown_prereq:{habit.id}:{other}"
            for other in sorted(habit.supports)
            if other not in release.habits
        ]
    for concern in sorted(release.concerns.values(), key=lambda c: c.id):
        if not concern.goals:
            errors.append(f"concern_without_goal:{concern.id}")
        errors += [
            f"concern_unknown_goal:{concern.id}:{goal}"
            for goal in sorted(concern.goals)
            if goal not in release.goals
        ]
    usable = [h for h in release.habits.values() if not h.withdrawn and h.levels]
    warnings = [
        f"no_habit_for_age_band:{goal}:{band}"
        for goal in sorted(release.goals)
        for band, (low, high) in AGE_BANDS.items()
        if not any(goal in h.goals and h.min_age <= low and h.max_age >= high for h in usable)
    ]
    return {"errors": errors, "warnings": warnings}


def diff_items(candidate: dict[tuple[str, str], int], base: dict[tuple[str, str], int]) -> dict[str, dict]:
    """Per-entity added, removed and re-versioned items of `candidate` relative to `base`."""
    result: dict[str, dict] = {entity: {"added": [], "removed": [], "changed": []} for entity in ENTITIES}
    for key in sorted(candidate.keys() | base.keys()):
        entity, entity_id = key
        if key not in base:
            result[entity]["added"].append({"id": entity_id, "version": candidate[key]})
        elif key not in candidate:
            result[entity]["removed"].append({"id": entity_id, "version": base[key]})
        elif candidate[key] != base[key]:
            result[entity]["changed"].append({"id": entity_id, "from": base[key], "to": candidate[key]})
    return result
