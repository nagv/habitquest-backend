"""Coach traces for LangSmith, with personal details removed before anything leaves the server.

A turn is collected in memory and sent only when it ends, so the outcome decides how much is kept: failed, retried
and no-match turns, plus a sample of the rest, keep redacted text. Other turns keep the structure alone. Only this
module traces: LangChain's automatic tracing would send everything as it is, so it is switched off here.
"""

import logging
import os
import random
import re
from collections.abc import Iterable, Iterator, Mapping
from functools import lru_cache
from typing import Any, cast

from langchain_core.tracers.run_collector import RunCollectorCallbackHandler
from langchain_core.tracers.schemas import Run
from langsmith import Client
from pydantic import BaseModel

from habitquest_backend.config import Settings

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

log = logging.getLogger(__name__)
PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "[email]"),
    (re.compile(r"(https?://|www\.)\S*[^\s.,;:!?)]", re.IGNORECASE), "[link]"),
    (re.compile(r"(?<![\w-])\+?\d[\d\s().-]{7,}\d(?![\w-])"), "[phone]"),
    (
        re.compile(
            r"\b\d+\s+(\w+\s+){1,3}(street|st|road|rd|avenue|ave|lane|ln|drive|dr|court|ct|close|way|place)\b",
            re.IGNORECASE,
        ),
        "[address]",
    ),
    (re.compile(r"\baged \d+", re.IGNORECASE), "aged [age]"),
]
KEPT_METADATA = {"langgraph_node", "ls_model_name", "ls_provider", "ls_temperature"}


def redact(text: str, names: Iterable[str]) -> str:
    """Removes details with a recognisable shape, then known names. Whole names and their single words both go."""
    for pattern, label in PATTERNS:
        text = pattern.sub(label, text)
    words = {word for name in names for word in [name, *name.split()] if len(word) > 1}
    for word in sorted(words, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(word)}\b", "[name]", text, flags=re.IGNORECASE)
    return text


def scrub(value: Any, names: Iterable[str], keep_text: bool) -> Any:
    """A copy with every string redacted, or replaced by its length. A value of an unknown type is hidden."""
    if isinstance(value, str):
        return redact(value, names) if keep_text else f"[{len(value)} characters]"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, BaseModel):  # LangChain messages
        return scrub(value.model_dump(), names, keep_text)
    if isinstance(value, Mapping):
        return {str(key): scrub(item, names, keep_text) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(item, names, keep_text) for item in value]
    return "[hidden]"


def steps(run: Run) -> Iterator[Run]:
    yield run
    for child in run.child_runs:
        yield from steps(child)


@lru_cache(maxsize=1)
def client(url: str, key: str) -> Client:
    return Client(api_url=url, api_key=key)


def start_trace(settings: Settings) -> RunCollectorCallbackHandler | None:
    return RunCollectorCallbackHandler() if settings.trace_coach else None


def keeps_text(settings: Settings, *, wanted: bool) -> bool:
    return wanted or random.random() < settings.trace_sample  # noqa: S311


def send_trace(
    settings: Settings,
    collector: RunCollectorCallbackHandler | None,
    names: Iterable[str],
    keep_text: bool,
    metadata: dict,
) -> None:
    """Sends a finished turn in LangSmith's background queue. Tracing never gets in the way of the reply, and a turn
    cut off by the deadline has nothing to send.
    """
    if collector is None or not collector.traced_runs:
        return
    try:
        assert settings.langsmith_api_key is not None  # noqa: S101 (checked in Settings)
        sender = client(settings.langsmith_endpoint, settings.langsmith_api_key.get_secret_value())
        names = list(names)
        for step in steps(collector.traced_runs[0]):
            found = step.extra.get("metadata", {})
            kept = {key: found[key] for key in KEPT_METADATA & found.keys()}
            sender.create_run(
                step.name,
                scrub(step.inputs, names, keep_text),
                cast(Any, step.run_type),
                project_name=settings.langsmith_project,
                id=step.id,
                trace_id=step.trace_id,
                dotted_order=step.dotted_order,
                parent_run_id=step.parent_run_id,
                start_time=step.start_time,
                end_time=step.end_time,
                outputs=scrub(step.outputs, names, keep_text),
                error=scrub(step.error, names, keep_text),
                extra={"metadata": kept | metadata if step.parent_run_id is None else kept},
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Coach trace was not sent (%s)", type(exc).__name__)
