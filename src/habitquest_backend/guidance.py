"""Parenting guidance the coach searches and cites: BM25 over `guidance.json`, one chunk per `## ` section.

Lexical search keeps this deterministic, with no text sent to an embedding service. A small synonym map
bridges how parents describe problems ("iPad", "meltdown") and how the guidance is written.
"""

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

K1 = 1.2
B = 0.75
STOP = set(
    "a an and are as at be but by can do does for from has have he her his how i if in into is it its me my of on"
    " or our she so that the their them they this to too us was we when with won't wont doesn't dont don't just"
    " very really child kid kids son daughter".split()
)
SYNONYMS = {
    "tv": ["screen"],
    "tablet": ["screen"],
    "ipad": ["screen"],
    "phone": ["screen"],
    "game": ["screen"],
    "video": ["screen"],
    "youtube": ["screen"],
    "device": ["screen"],
    "meltdown": ["tantrum", "feelings"],
    "tantrum": ["feelings", "calm"],
    "angry": ["feelings", "calm"],
    "upset": ["feelings", "calm"],
    "cry": ["feelings"],
    "scream": ["tantrum", "calm"],
    "bed": ["sleep", "bedtime"],
    "night": ["sleep", "bedtime"],
    "tired": ["sleep"],
    "late": ["sleep", "bedtime"],
    "wake": ["morning"],
    "picky": ["tasting", "food"],
    "vegetable": ["veggie", "tasting"],
    "eat": ["food", "meal"],
    "dinner": ["meal"],
    "lunch": ["meal"],
    "messy": ["tidy", "chores"],
    "mess": ["tidy", "chores"],
    "room": ["tidy"],
    "help": ["chores"],
    "lazy": ["active", "motivation"],
    "bored": ["play", "active"],
    "outside": ["active", "play"],
    "exercise": ["active"],
    "sport": ["active"],
    "rude": ["kindness", "manners"],
    "fight": ["siblings", "kindness"],
    "share": ["kindness"],
    "brother": ["siblings"],
    "sister": ["siblings"],
    "teeth": ["tooth", "brush"],
    "wash": ["hygiene"],
    "school": ["homework"],
    "focus": ["homework"],
    "procrastinate": ["homework", "start"],
    "reward": ["motivation"],
    "bribe": ["motivation", "reward"],
    "read": ["book"],
}
ENDINGS = ["ations", "ation", "ities", "ity", "ness", "ings", "ing", "ies", "ate", "ed", "es", "e", "s", "y"]


def stem(word: str) -> str:
    """A light suffix stripper, run twice so "tidying" -> "tidy" -> "tid" matches "tidy" -> "tid"."""
    for _ in range(2):
        ending = next((e for e in ENDINGS if len(word) > len(e) + 2 and word.endswith(e)), None)
        if ending is None:
            break
        word = word[: -len(ending)]
    return word


def tokenize(text: str) -> list[str]:
    """Lowercase, split, drop stop words, and strip common endings so "brushing" matches "brush"."""
    words = (re.sub(r"'s$", "", w).replace("'", "") for w in re.split(r"[^a-z0-9']+", text.lower()))
    return [stem(w) for w in words if len(w) > 1 and w not in STOP]


SYNONYM_STEMS = {stem(word): [stem(s) for s in synonyms] for word, synonyms in SYNONYMS.items()}


@dataclass
class Chunk:
    ref: str  # "G1": a short, stable citation reference
    title: str
    text: str
    min_age: int
    max_age: int


def load_chunks() -> list[Chunk]:
    documents = json.loads((Path(__file__).parent / "guidance.json").read_text(encoding="utf-8"))
    sections = [
        (document, section.strip())
        for document in documents
        for section in re.split(r"^## ", document["body"], flags=re.MULTILINE)
        if section.strip()
    ]
    chunks = []
    for number, (document, section) in enumerate(sections, start=1):
        heading, _, body = section.partition("\n")
        low, high = document["ages"]
        chunks.append(Chunk(f"G{number}", f"{document['title']}: {heading.strip()}", body.strip(), low, high))
    return chunks


CHUNKS = load_chunks()
DOCUMENTS = [Counter(tokenize(f"{c.title} {c.title} {c.text}")) for c in CHUNKS]
AVERAGE_LENGTH = sum(sum(d.values()) for d in DOCUMENTS) / len(DOCUMENTS)
FREQUENCY = Counter(term for document in DOCUMENTS for term in document)


def score(terms: set[str], document: Counter) -> float:
    total = 0.0
    length = sum(document.values())
    for term in terms & document.keys():
        count = document[term]
        rarity = math.log(1 + (len(DOCUMENTS) - FREQUENCY[term] + 0.5) / (FREQUENCY[term] + 0.5))
        total += rarity * count * (K1 + 1) / (count + K1 * (1 - B + B * length / AVERAGE_LENGTH))
    return total


def search_guidance(query: str, age: int, limit: int = 3) -> list[Chunk]:
    """The passages for this age that best match the query, best first."""
    tokens = tokenize(query)
    terms = set(tokens) | {synonym for t in tokens for synonym in SYNONYM_STEMS.get(t, [])}
    ranked = sorted(zip(CHUNKS, DOCUMENTS, strict=True), key=lambda pair: -score(terms, pair[1]))
    found = [c for c, document in ranked if score(terms, document) > 0 and c.min_age <= age <= c.max_age]
    return found[:limit]
