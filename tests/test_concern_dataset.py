"""Labelled recall test for the fake_mapping keyword heuristic.

Each case carries a min_recall threshold. The test asserts that the
heuristic surfaces at least that fraction of the expected goals.
Cases with min_recall=0 are unmatched-phrase cases: the heuristic is
allowed to return no goals, and unmatched_phrases should be non-empty.

Set LIVE_LOCAL=1 to run the same cases through the local Ollama model
and report its recall separately (never fails the build).
"""

import json
import os
import pathlib
import sys

import pytest

from habitquest_backend.planning import fake_mapping

DATASET = pathlib.Path(__file__).parent / "data" / "concerns.jsonl"


def load_cases():
    cases = []
    with DATASET.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fake_mapping_recall(case):
    result = fake_mapping(case["concerns"])
    found = {g["goal_id"] for g in result["goals"]}
    expected = set(case["expected_goals"])
    min_recall = case["min_recall"]

    if min_recall == 0:
        # unmatched-phrase case: unmatched_phrases must be non-empty
        assert result["unmatched_phrases"], (
            f"[{case['id']}] Expected unmatched_phrases for: {case['concerns']!r}"
        )
        return

    if not expected:
        return

    matched = found & expected
    recall = len(matched) / len(expected)
    assert recall >= min_recall, (
        f"[{case['id']}] recall {recall:.2f} < {min_recall}: "
        f"matched={matched}, missed={expected - found}, "
        f"concerns={case['concerns']!r}"
    )


def _live_local_report():
    """Optional: run all cases through Ollama and print a recall summary."""
    try:
        from langchain_community.chat_models import ChatOllama
        from langchain_core.messages import HumanMessage, SystemMessage
        import re

        model = ChatOllama(model="llama3.2", temperature=0, num_predict=512)
        goals_catalog = "bedtime, morning, meals, responsibility"
        prompt_tmpl = (
            f"Map these parental concerns to goal IDs from: [{goals_catalog}]. "
            "Return JSON {{goals:[{{goal_id,evidence_span}}],unmatched_phrases:[]}}. "
            "Concerns: {concerns}"
        )

        hits = misses = 0
        for case in CASES:
            if not case["expected_goals"]:
                continue
            msg = model.invoke([HumanMessage(content=prompt_tmpl.format(concerns=case["concerns"]))])
            text = msg.content
            m = re.search(r'\[.*?\]', text, re.DOTALL)
            if not m:
                misses += len(case["expected_goals"])
                continue
            try:
                found = {g["goal_id"] for g in json.loads(m.group(0))}
            except Exception:
                misses += len(case["expected_goals"])
                continue
            expected = set(case["expected_goals"])
            hits += len(found & expected)
            misses += len(expected - found)

        total = hits + misses
        print(f"\nOllama live recall: {hits}/{total} = {hits/total:.1%}" if total else "\nNo cases", file=sys.stderr)
    except Exception as exc:
        print(f"\nLIVE_LOCAL recall skipped: {exc}", file=sys.stderr)


if os.environ.get("LIVE_LOCAL") == "1":
    _live_local_report()
