"""Deterministic submit checks. Findings are reason codes; any finding keeps the version a draft."""

import re

# Lexical lint for v6 section 9 prohibited content. Concern labels are parent descriptions of a
# problem, not suggested activities, so they are not linted.
PROHIBITED = {
    "punishment": r"\b(punish\w*|spank\w*|smack\w*|slap\w*|hit|beat|grounded|time-?outs?)\b",
    "secrecy": r"\b(secrets?|secretly|don'?t tell|without telling|keep (it|this) (quiet|between))\b",
    "dangerous_activity": r"\b(knife|knives|stove|oven|matches|lighter|bleach|chemicals?|ladder|roof|fire)\b",
    "weight_loss": r"\b(weight|diet\w*|calories?|skinny|fat)\b",
    "diagnosis_treatment": r"\b(adhd|autis\w*|diagnos\w*|disorders?|therapy|treatment|cure|symptoms?|medicat\w*|medicine)\b",
    "coercive_wording": r"\b(or else|no excuses|you have to|obey|forced?|if you don'?t)\b",
}


def lint(*texts: str) -> list[str]:
    return [
        code
        for code, pattern in PROHIBITED.items()
        if any(re.search(pattern, t, re.IGNORECASE) for t in texts)
    ]


def unknown_goals(goal_ids, known_goals: set[str]) -> list[str]:
    return [f"unknown_goal:{goal}" for goal in sorted(goal_ids) if goal not in known_goals]


def habit_findings(record: dict, known_goals: set[str]) -> list[str]:
    findings = []
    if not 1 <= len(record["levels"]) <= 3:
        findings.append("levels_count")
    if not 6 <= record["min_age"] <= record["max_age"] <= 13:
        findings.append("age_bounds")
    if not record["goals"]:
        findings.append("no_goal")
    texts = [
        record["title"],
        record["instruction"],
        *(level["instruction"] for level in record["levels"] if level["instruction"]),
        *(
            text
            for variant in record["variants"].values()
            for text in (variant["title"], variant["instruction"])
        ),
    ]
    return findings + unknown_goals(record["goals"], known_goals) + lint(*texts)


def goal_findings(record: dict, known_goals: set[str]) -> list[str]:
    return lint(record["title"], record["description"])


def concern_findings(record: dict, known_goals: set[str]) -> list[str]:
    return (["no_goal"] if not record["goals"] else []) + unknown_goals(record["goals"], known_goals)


def chapter_findings(record: dict, known_goals: set[str]) -> list[str]:
    return unknown_goals([record["goal_id"]], known_goals) + lint(record["title"])
