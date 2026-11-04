"""
Step 5 of the freshness pipeline: a cheap judge model reads the sources and
returns a structured verdict. Its answer is not trusted as-is:

  1. pydantic validates the JSON shape (status must be one of three values).
  2. Code then enforces "evidence vs inference":
       - the cited source must be one of the sources we actually gave it,
         otherwise the verdict becomes `unverifiable`;
       - the evidence quote must really appear in that source (whitespace
         and case ignored). If not, the quote is dropped and flagged, and a
         `changed`/`supported` verdict without evidence becomes `unverifiable`.
     So `evidence` is always text from a real page; `inference` is always
     labelled as the model's own reasoning.
  3. No sources -> `unverifiable` without an LLM call (nothing to judge).

Two kinds of claim, same rules: an API use from code (Judge.__call__) and a
prose claim from `staleguard verify` (Judge.judge_claim, which also gives the
model today's date — "current" only means something relative to a date).
"""

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field, ValidationError

from agent import lookups
from agent.llm import LLM, LLMError
from agent.lookups import FreshnessStatus
from agent.policy import BudgetExceeded, BudgetGuard
from freshness.claims import ProseClaim
from freshness.extract import ApiUse
from freshness.research import Source
from freshness.textutil import contains, first_json_object
from freshness.versions import VersionInfo


class JudgeReply(BaseModel):
    """What the model must return (FRESHNESS_JUDGE_PROMPT)."""

    status: FreshnessStatus
    evidence_quote: str = ""
    source: int | None = None
    inference: str = ""
    suggested_fix: str = ""


class Verdict(BaseModel):
    """One checked claim (API use or prose), as shown and as cached."""

    claim: str
    symbol: str = ""               # API uses only
    library: str = ""              # library, or the topic of a prose claim
    version: str = ""
    status: FreshnessStatus
    evidence: str = ""             # verbatim from source_url, or empty
    source_url: str | None = None
    inference: str = ""            # the judge's own reasoning
    suggested_fix: str = ""
    evidence_verified: bool = False
    note: str = ""                 # why a verdict was downgraded, cache hits, errors
    line: int | None = Field(default=None, exclude=True)  # per edit, not cached
    kind: str = Field(default="", exclude=True)  # "call", ..., or "prose"
    quote: str = Field(default="", exclude=True)  # the words in the checked answer


def claim_text(use: ApiUse, version: VersionInfo) -> str:
    return f"`{use.symbol}` is current API in {version.distribution} {version.label}"


def base_for_use(use: ApiUse, version: VersionInfo) -> Verdict:
    return Verdict(claim=claim_text(use, version), symbol=use.symbol, library=use.library,
                   version=version.label, status=FreshnessStatus.UNVERIFIABLE,
                   line=use.line, kind=use.kind)


def base_for_claim(claim: ProseClaim) -> Verdict:
    return Verdict(claim=claim.claim, library=claim.topic,
                   status=FreshnessStatus.UNVERIFIABLE, kind="prose", quote=claim.quote)


def unverifiable(use: ApiUse, version: VersionInfo, note: str) -> Verdict:
    return base_for_use(use, version).model_copy(update={"note": note})


class Judge:
    def __init__(self, llm: LLM, *, model: str, fallbacks: Sequence[str] | None = None,
                 budget: BudgetGuard | None = None) -> None:
        self.llm, self.model, self.fallbacks, self.budget = llm, model, fallbacks, budget

    def __call__(self, use: ApiUse, version: VersionInfo, sources: list[Source]) -> Verdict:
        """Judge an API use from code."""
        return self._judge(base_for_use(use, version), lookups.FRESHNESS_JUDGE_PROMPT,
                           _judge_input(use, version, sources), sources,
                           "no documentation found on the allowed domains")

    def judge_claim(self, claim: ProseClaim, sources: list[Source]) -> Verdict:
        """Judge a prose claim from an LLM answer."""
        return self._judge(base_for_claim(claim), lookups.VERIFY_JUDGE_PROMPT,
                           _claim_input(claim, sources), sources, "no sources found")

    def _judge(self, base: Verdict, prompt: str, user: str, sources: list[Source],
               empty_note: str) -> Verdict:
        if not sources:
            return base.model_copy(update={"note": empty_note})
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": user}]
        try:
            if self.budget is not None:
                self.budget.check(self.model, self.fallbacks or (), messages)
            result = self.llm.chat(messages, model=self.model, fallbacks=self.fallbacks,
                                   temperature=0.0,
                                   max_tokens=lookups.FRESHNESS_JUDGE_MAX_TOKENS)
        except (LLMError, BudgetExceeded) as exc:
            return base.model_copy(update={"note": f"judge unavailable: {exc}"})
        if self.budget is not None:
            self.budget.record(result)
        return grade_reply(base, sources, result.content)


def grade(use: ApiUse, version: VersionInfo, sources: list[Source], raw: str) -> Verdict:
    """grade_reply for an API use (kept for callers and tests)."""
    return grade_reply(base_for_use(use, version), sources, raw)


def grade_reply(base: Verdict, sources: list[Source], raw: str) -> Verdict:
    """Parse the judge's text and enforce the evidence rules (pure, testable)."""
    try:
        reply = JudgeReply.model_validate(first_json_object(raw))
    except (ValueError, ValidationError) as exc:
        note = f"judge reply was not valid JSON ({exc.__class__.__name__})"
        return base.model_copy(update={"note": note})

    verdict = base.model_copy(update={"status": reply.status,
                                      "inference": reply.inference.strip(),
                                      "suggested_fix": reply.suggested_fix.strip()})
    if reply.status is FreshnessStatus.UNVERIFIABLE:
        return verdict

    index = (reply.source or 0) - 1
    if not 0 <= index < len(sources):
        return verdict.model_copy(update={
            "status": FreshnessStatus.UNVERIFIABLE, "suggested_fix": "",
            "note": "judge cited no source we provided"})
    source = sources[index]
    quote = reply.evidence_quote.strip()
    if len(quote) >= lookups.FRESHNESS_QUOTE_MIN_CHARS and contains(source.content, quote):
        return verdict.model_copy(update={"evidence": quote, "source_url": source.url,
                                          "evidence_verified": True})
    return verdict.model_copy(update={
        "status": FreshnessStatus.UNVERIFIABLE, "source_url": source.url, "suggested_fix": "",
        "note": "judge's quote was not found in the cited source"})


# ── helpers ──────────────────────────────────────────────────
def _judge_input(use: ApiUse, version: VersionInfo, sources: list[Source]) -> str:
    """Names only (no user code), plus the numbered sources."""
    lines = [f"Library: {version.distribution}",
             f"Version used by the repo: {version.label} (from {version.source})",
             f"API: {use.symbol}", "", "Sources:"]
    for i, s in enumerate(sources, 1):
        lines.append(f"[{i}] {s.title} — {s.url}\n{s.content}\n")
    return "\n".join(lines)


def _claim_input(claim: ProseClaim, sources: list[Source]) -> str:
    lines = [f"Today's date: {datetime.now(UTC).date().isoformat()}",
             f"Claim: {claim.claim}", f"Topic: {claim.topic or '-'}", "", "Sources:"]
    for i, s in enumerate(sources, 1):
        lines.append(f"[{i}] {s.title} — {s.url}\n{s.content}\n")
    return "\n".join(lines)
