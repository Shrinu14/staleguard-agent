"""Small text helpers shared by the freshness modules."""

import json
from typing import Any


def first_json_object(text: str) -> dict[str, Any]:
    """The first JSON object in a model reply (models often add prose around it)."""
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no JSON object in reply")


def normalize(text: str) -> str:
    """Lowercase, whitespace collapsed: for 'is this quote really in that text?'."""
    return " ".join(text.lower().split())


# Markdown/typography that a model drops or changes when it copies a sentence.
# The WORDS must still match exactly; only this formatting is ignored.
_FORMATTING = str.maketrans({c: None for c in "`*_“”‘’\"'"})


def contains(haystack: str, quote: str) -> bool:
    """Is `quote` really in `haystack`? Case, whitespace and markdown ignored."""
    q = normalize(quote.translate(_FORMATTING))
    return bool(q) and q in normalize(haystack.translate(_FORMATTING))
