from habitquest_backend.catalog.models import Release


def stale_references(
    release: Release, habits: list[tuple[str, int]], chapters: list[tuple[str, int]]
) -> list[str]:
    """Reasons a proposal's habit and chapter versions are no longer usable in this release."""
    problems = []
    for habit_id, version in habits:
        current = release.habits.get(habit_id)
        if current is None:
            problems.append(f"{habit_id}: not_in_release")
        elif current.version != version:
            problems.append(f"{habit_id}: version_superseded")
        elif current.withdrawn:
            problems.append(f"{habit_id}: withdrawn")
    released = {(c.id, c.version) for c in release.chapters.values()}
    problems += [
        f"{chapter_id}: chapter_changed" for chapter_id, v in chapters if (chapter_id, v) not in released
    ]
    return problems
