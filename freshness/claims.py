"""
Step 1 for `staleguard verify`: find what to check in an LLM's answer.

Two kinds of claims, two methods:
  - code blocks (```python ...```): the same ast extraction as for edits
    (freshness/extract.py). No LLM, exact.
  - prose: a cheap model lists the time-sensitive claims (versions, dates,
    prices, "latest X", deprecations ...). Its output is not trusted:
      * pydantic validates the JSON shape;
      * every claim must carry a `quote` that appears word for word in the
        answer — a claim the model invented is dropped (and counted);
      * the search query is cut to one line of VERIFY_QUERY_MAX_CHARS.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from agent import lookups
from agent.llm import LLM, LLMError
from agent.policy import BudgetExceeded, BudgetGuard
from freshness.extract import ApiUse, extract_api_uses
from freshness.textutil import contains, first_json_object, normalize

_FENCE = re.compile(lookups.CODE_FENCE_PATTERN, re.DOTALL)


class ProseClaim(BaseModel):
    quote: str
    claim: str
    topic: str = ""
    search_query: str = ""


class _ExtractReply(BaseModel):
    claims: list[ProseClaim] = []


@dataclass(slots=True)
class ExtractedClaims:
    code_uses: list[ApiUse] = field(default_factory=list)
    prose: list[ProseClaim] = field(default_factory=list)
    dropped: int = 0          # claims whose quote is not in the answer
    error: str = ""           # extraction failed (shown, not fatal)


def code_blocks(text: str) -> list[str]:
    return [m.strip() for m in _FENCE.findall(text)]


def extract_code_uses(text: str) -> list[ApiUse]:
    seen: dict[str, ApiUse] = {}
    for block in code_blocks(text):
        for use in extract_api_uses(block):
            seen.setdefault(use.symbol, use)
    return list(seen.values())


def prose_only(text: str) -> str:
    """The answer with code blocks removed (they are checked separately)."""
    return _FENCE.sub(" ", text)


def extract_claims(text: str, llm: LLM | None, *, model: str,
                   fallbacks: Sequence[str] | None = None,
                   budget: BudgetGuard | None = None) -> ExtractedClaims:
    text = text[:lookups.VERIFY_MAX_INPUT_CHARS]
    out = ExtractedClaims(code_uses=extract_code_uses(text))
    prose = prose_only(text).strip()
    if not prose or llm is None:
        return out
    messages = [{"role": "system", "content": lookups.VERIFY_EXTRACT_PROMPT},
                {"role": "user", "content": prose}]
    try:
        if budget is not None:
            budget.check(model, fallbacks or (), messages)
        result = llm.chat(messages, model=model, fallbacks=fallbacks, temperature=0.0,
                          max_tokens=lookups.VERIFY_EXTRACT_MAX_TOKENS)
    except (LLMError, BudgetExceeded) as exc:
        out.error = f"claim extraction failed: {exc}"
        return out
    if budget is not None:
        budget.record(result)
    out.prose, out.dropped, out.error = parse_claims(result.content, prose)
    return out


def parse_claims(raw: str, source_text: str) -> tuple[list[ProseClaim], int, str]:
    """(claims whose quote is really in source_text, dropped count, error)."""
    try:
        reply = _ExtractReply.model_validate(first_json_object(raw))
    except (ValueError, ValidationError) as exc:
        return [], 0, f"claim extractor returned invalid JSON ({exc.__class__.__name__})"
    kept, dropped, seen = [], 0, set()
    for c in reply.claims:
        quote = c.quote.strip()
        if not contains(source_text, quote):
            dropped += 1
            continue
        if normalize(c.claim) in seen:
            continue
        seen.add(normalize(c.claim))
        query = " ".join((c.search_query or c.claim).split())[:lookups.VERIFY_QUERY_MAX_CHARS]
        kept.append(c.model_copy(update={"quote": quote, "search_query": query,
                                         "topic": c.topic.strip().lower()}))
    return kept, dropped, ""
