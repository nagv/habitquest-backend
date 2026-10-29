from catalog_factories import make_habit

from habitquest_backend.catalog.models import Chapter, Release
from habitquest_backend.catalog.revalidation import stale_references


def test_stale_references_reports_each_problem_in_order():
    release = Release(
        id=2,
        goals={},
        concerns={},
        habits={"kept": make_habit("kept"), "gone": make_habit("gone", withdrawn=True)},
        chapters={("space", "g"): Chapter("space-g", 2, "space", "g", "Title")},
    )
    problems = stale_references(
        release, [("kept", 1), ("gone", 1), ("missing", 1), ("kept", 3)], [("space-g", 1), ("space-g", 2)]
    )
    assert problems == [
        "gone: withdrawn",
        "missing: not_in_release",
        "kept: version_superseded",
        "space-g: chapter_changed",
    ]
    assert stale_references(release, [("kept", 1)], [("space-g", 2)]) == []
