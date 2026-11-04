"""
`staleguard verify "<text>"`: the freshness pipeline for any LLM answer.

  1. claims  (freshness/claims.py)
       code blocks -> API uses via ast (no LLM)
       prose       -> time-sensitive claims via a cheap model; each must quote
                      the answer word for word, or it is dropped
  2. research  Tavily (API uses: names only, doc domains; prose: the claim's
               search query, doc domains when the topic is a known library)
  3. judge     same evidence rules as for edits; prose claims get today's date
  4. report    status + confidence (computed from the evidence) + source link,
               evidence and inference in separate columns

Code-block API uses go through FreshnessChecker (same cache as `check`,
versions from --repo if given). Prose verdicts use their own short-lived
cache in ~/.staleguard, keyed by the normalized claim text.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from agent import lookups
from agent.lookups import FreshnessStatus
from agent.policy import BudgetGuard
from freshness.cache import VerdictCache
from freshness.checker import FreshnessChecker
from freshness.claims import ExtractedClaims, ProseClaim, code_blocks, extract_claims
from freshness.judge import Judge, Verdict, base_for_claim
from freshness.research import ClaimSearcher, ResearchError
from freshness.textutil import normalize

_PROSE_CACHE_KEY = "prose"  # "distribution" column for prose claims in VerdictCache


@dataclass(slots=True)
class VerifyReport:
    verdicts: list[Verdict] = field(default_factory=list)
    dropped: int = 0        # extracted claims whose quote wasn't in the answer
    skipped: int = 0        # over the claim cap
    cached: int = 0
    notes: list[str] = field(default_factory=list)

    def count(self, status: FreshnessStatus) -> int:
        return sum(v.status is status for v in self.verdicts)


class Verifier:
    def __init__(self, *, judge: Judge, searcher: ClaimSearcher, code_checker: FreshnessChecker,
                 prose_cache: VerdictCache | None = None,
                 budget: BudgetGuard | None = None,
                 max_claims: int = lookups.VERIFY_MAX_CLAIMS,
                 on_progress: Callable[[str], None] | None = None) -> None:
        self.judge, self.searcher, self.code_checker = judge, searcher, code_checker
        self.prose_cache, self.budget, self.max_claims = prose_cache, budget, max_claims
        self.on_progress = on_progress or (lambda _msg: None)

    def verify(self, text: str) -> VerifyReport:
        report = VerifyReport()
        if len(text) > lookups.VERIFY_MAX_INPUT_CHARS:
            report.notes.append(f"answer cut to {lookups.VERIFY_MAX_INPUT_CHARS:,} characters")
        self.on_progress("finding time-sensitive claims…")
        found: ExtractedClaims = extract_claims(
            text, self.judge.llm, model=self.judge.model, fallbacks=self.judge.fallbacks,
            budget=self.budget)
        if found.error:
            report.notes.append(found.error)
        report.dropped = found.dropped

        for block in code_blocks(text[:lookups.VERIFY_MAX_INPUT_CHARS]):
            sub = self.code_checker.check("", block)
            report.verdicts += sub.verdicts
            report.cached += sub.cached
            report.skipped += sub.skipped

        report.skipped += max(0, len(found.prose) - self.max_claims)
        for claim in found.prose[: self.max_claims]:
            report.verdicts.append(self._check_claim(claim, report))
        return report

    def _check_claim(self, claim: ProseClaim, report: VerifyReport) -> Verdict:
        key = normalize(claim.claim)
        if self.prose_cache is not None:
            hit = self.prose_cache.get(_PROSE_CACHE_KEY, claim.topic, key)
            if hit is not None:
                report.cached += 1
                return hit.model_copy(update={"kind": "prose", "quote": claim.quote, "symbol": "",
                                              "note": (hit.note + " (cached)").strip()})
        self.on_progress(f"checking: {claim.claim}")
        try:
            sources = self.searcher.search_claim(claim)
        except ResearchError as exc:
            return base_for_claim(claim).model_copy(update={"note": str(exc)})
        verdict = self.judge.judge_claim(claim, sources)
        if self.prose_cache is not None:
            # cache under the normalized claim, so wording/case changes still hit
            self.prose_cache.put(_PROSE_CACHE_KEY, claim.topic,
                                 verdict.model_copy(update={"symbol": key}))
        return verdict
