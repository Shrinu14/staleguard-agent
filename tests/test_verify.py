"""
Tests for `staleguard verify` (Piece 9): claim extraction from free text,
claim research and judging, confidence, and the Verifier. Offline.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent import lookups
from agent.cli import _report_json
from agent.lookups import Confidence, FreshnessStatus
from freshness.cache import VerdictCache
from freshness.checker import FreshnessChecker
from freshness.claims import ProseClaim, code_blocks, extract_claims, parse_claims, prose_only
from freshness.judge import Judge, Verdict
from freshness.report import confidence
from freshness.research import Source, TavilySearcher, dedupe, is_official
from freshness.textutil import contains
from freshness.verify import Verifier, VerifyReport
from tests.test_freshness import FakeLLM, FakeSearcher, judge_reply

ANSWER = """Pydantic's latest version is 1.10. Python 3.8 is the newest release.

```python
import openai
openai.ChatCompletion.create(model="m", messages=[])
```
Use it like that."""

QUOTE = "@validator has been deprecated, and should be replaced with @field_validator"
DOCS = Source("https://docs.pydantic.dev/latest/migration", "Migration Guide",
              "`@validator` has been deprecated, and should be replaced with `@field_validator`.")
BLOG = Source("https://blog.example.com/python", "blog", "Python 3.14 is the latest release.")


def claims_json(*claims: dict) -> str:
    return "Here you go:\n" + json.dumps({"claims": list(claims)})


def claim(quote: str, text: str, topic: str = "pydantic", query: str = "q") -> dict:
    return {"quote": quote, "claim": text, "topic": topic, "search_query": query}


# ── text helpers ─────────────────────────────────────────────
def test_code_blocks_and_prose_split() -> None:
    blocks = code_blocks(ANSWER)
    assert len(blocks) == 1 and "ChatCompletion" in blocks[0]
    prose = prose_only(ANSWER)
    assert "ChatCompletion" not in prose and "Python 3.8" in prose


def test_contains_ignores_formatting_not_words() -> None:
    assert contains(DOCS.content, QUOTE)
    assert contains(DOCS.content, "`@VALIDATOR`   has been deprecated")
    assert not contains(DOCS.content, "@validator was removed")
    assert not contains(DOCS.content, "  ")


# ── claim extraction ─────────────────────────────────────────
def test_parse_claims_drops_invented_quotes_and_duplicates() -> None:
    raw = claims_json(
        claim("Pydantic's latest version is 1.10", "Pydantic's latest version is 1.10.",
              "Pydantic"),
        claim("Pydantic 3 is out", "Pydantic 3 is out."),            # not in the answer
        claim("latest version is 1.10", "pydantic's LATEST version is 1.10."),  # duplicate
        claim("Python 3.8 is the newest release", "Python 3.8 is the newest release.",
              "python", "x" * 500),
    )
    kept, dropped, err = parse_claims(raw, prose_only(ANSWER))
    assert err == "" and dropped == 1
    assert [c.claim for c in kept] == ["Pydantic's latest version is 1.10.",
                                       "Python 3.8 is the newest release."]
    assert kept[0].topic == "pydantic"
    assert len(kept[1].search_query) == lookups.VERIFY_QUERY_MAX_CHARS


def test_parse_claims_bad_json() -> None:
    assert parse_claims("no json here", "text")[2].startswith("claim extractor")


def test_extract_claims_sends_prose_only_and_survives_errors() -> None:
    from agent.llm import LLMError
    llm = FakeLLM(claims_json(claim("Python 3.8 is the newest release", "Py 3.8 newest.")))
    found = extract_claims(ANSWER, llm, model="ollama/x")  # type: ignore[arg-type]
    assert [u.symbol for u in found.code_uses] == ["openai.ChatCompletion.create"]
    assert len(found.prose) == 1
    assert "ChatCompletion" not in llm.sent[0][1]["content"]  # code goes through ast, not the LLM
    failed = extract_claims(ANSWER, FakeLLM(LLMError("down")), model="m")  # type: ignore[arg-type]
    assert "extraction failed" in failed.error and failed.code_uses  # code still checked


# ── research ─────────────────────────────────────────────────
def test_dedupe_same_page_and_same_snippet() -> None:
    a = Source("https://d.dev/m#one", "t", "same text")
    b = Source("https://d.dev/m#two", "t", "other")
    c = Source("https://e.dev/x", "t", "Same  TEXT")
    d = Source("https://f.dev/y", "t", "unique")
    assert dedupe([a, b, c, d]) == [a, d]


def test_search_claim_uses_claim_text_and_known_domains() -> None:
    calls: list[dict] = []

    class Client:
        def __init__(self, key): ...
        def search(self, query, **kw):
            calls.append({"query": query, **kw})
            return {"results": []}

    s = TavilySearcher("k", client_factory=Client)
    s.search_claim(ProseClaim(quote="q", claim="@validator is deprecated", topic="pydantic",
                              search_query="vague"))
    s.search_claim(ProseClaim(quote="q", claim="Python 3.8 is newest", topic="python"))
    assert calls[0]["query"] == "@validator is deprecated"
    assert calls[0]["include_domains"] == ["docs.pydantic.dev"]
    assert "include_domains" not in calls[1]  # unknown topic: open web


# ── confidence (computed, not self-reported) ─────────────────
@pytest.mark.parametrize("url, library, verified, expected", [
    ("https://docs.pydantic.dev/latest/migration", "pydantic", True, Confidence.HIGH),
    ("https://sub.docs.pydantic.dev/x", "pydantic", True, Confidence.HIGH),
    ("https://evil-docs.pydantic.dev.example.com/x", "pydantic", True, Confidence.MEDIUM),
    ("https://github.com/someone/repo", "python", True, Confidence.MEDIUM),  # unknown topic
    ("https://docs.pydantic.dev/x", "pydantic", False, Confidence.LOW),
])
def test_confidence(url: str, library: str, verified: bool, expected: Confidence) -> None:
    v = Verdict(claim="c", library=library, status=FreshnessStatus.SUPPORTED,
                source_url=url, evidence="e" if verified else "", evidence_verified=verified)
    assert confidence(v) is expected


def test_is_official_needs_a_known_library() -> None:
    assert is_official("https://github.com/openai/openai-python", "openai")
    assert not is_official("https://github.com/openai/openai-python", "")


# ── judge for prose claims ───────────────────────────────────
def test_judge_claim_gets_todays_date_and_enforces_quotes() -> None:
    llm = FakeLLM(judge_reply("supported", quote=QUOTE), judge_reply("supported", quote="made up"))
    j = Judge(llm, model="ollama/x")  # type: ignore[arg-type]
    pc = ProseClaim(quote="q", claim="@validator is deprecated in v2", topic="pydantic")
    v = j.judge_claim(pc, [DOCS])
    assert v.status is FreshnessStatus.SUPPORTED and v.kind == "prose"
    assert datetime.now(UTC).date().isoformat() in llm.sent[0][1]["content"]
    assert j.judge_claim(pc, [DOCS]).status is FreshnessStatus.UNVERIFIABLE
    assert j.judge_claim(pc, []).note == "no sources found" and len(llm.sent) == 2


# ── Verifier end to end ──────────────────────────────────────
class FakeClaimSearcher(FakeSearcher):
    def __init__(self, by_claim: dict[str, list[Source]], api_sources: list[Source]) -> None:
        super().__init__(api_sources)
        self.by_claim = by_claim
        self.claims: list[str] = []

    def search_claim(self, claim: ProseClaim) -> list[Source]:
        self.claims.append(claim.claim)
        return self.by_claim.get(claim.claim, [])


def make_verifier(tmp_path: Path, llm: FakeLLM, searcher: FakeClaimSearcher,
                  **kw) -> Verifier:
    judge = Judge(llm, model="ollama/x")  # type: ignore[arg-type]
    checker = FreshnessChecker(tmp_path, searcher=searcher, judge=judge,
                               cache=VerdictCache.for_repo(tmp_path))
    return Verifier(judge=judge, searcher=searcher, code_checker=checker,
                    prose_cache=VerdictCache(tmp_path / "prose.sqlite3"), **kw)


def test_verifier_checks_code_and_prose_and_caches(tmp_path: Path) -> None:
    api_src = Source("https://github.com/openai/x", "t",
                     "You tried to access openai.ChatCompletion, but this is no longer supported")
    searcher = FakeClaimSearcher({"Python 3.8 is newest.": [BLOG]}, [api_src])
    extract = claims_json(
        claim("Python 3.8 is the newest release", "Python 3.8 is newest.", "python"),
        claim("Pydantic's latest version is 1.10", "Pydantic latest is 1.10."),
        claim("Rust 2 is out", "Rust 2 is out."),  # invented: dropped
    )
    llm = FakeLLM(
        extract,
        judge_reply("changed", quote="You tried to access openai.ChatCompletion"),  # code block
        judge_reply("changed", quote="Python 3.14 is the latest release"),          # Python claim
    )
    report = make_verifier(tmp_path, llm, searcher).verify(ANSWER)
    by = {v.claim if v.kind == "prose" else v.symbol: v for v in report.verdicts}
    assert by["openai.ChatCompletion.create"].status is FreshnessStatus.CHANGED
    py = by["Python 3.8 is newest."]
    assert py.status is FreshnessStatus.CHANGED and confidence(py) is Confidence.MEDIUM
    assert by["Pydantic latest is 1.10."].note == "no sources found"  # no LLM call
    assert report.dropped == 1 and report.count(FreshnessStatus.CHANGED) == 2

    # second run: both changed verdicts come from the caches, only extraction is re-asked
    llm2 = FakeLLM(extract)
    again = make_verifier(tmp_path, llm2, searcher).verify(ANSWER)
    assert again.cached == 2 and len(llm2.sent) == 1
    cached_py = next(v for v in again.verdicts if v.claim == "Python 3.8 is newest.")
    assert cached_py.symbol == "" and "(cached)" in cached_py.note


def test_verifier_caps_prose_claims(tmp_path: Path) -> None:
    text = "A is 1. B is 2. C is 3."
    extract = claims_json(*[claim(q, q, "x") for q in ("A is 1", "B is 2", "C is 3")])
    searcher = FakeClaimSearcher({}, [])
    report = make_verifier(tmp_path, FakeLLM(extract), searcher, max_claims=2).verify(text)
    assert len(report.verdicts) == 2 and report.skipped == 1 and len(searcher.claims) == 2


def test_report_json_shape() -> None:
    v = Verdict(claim="c", status=FreshnessStatus.CHANGED, kind="prose", quote="q",
                source_url="https://blog.example.com", evidence="e", evidence_verified=True)
    data = _report_json(VerifyReport(verdicts=[v], dropped=1))
    assert data["verdicts"][0]["confidence"] == "medium"
    assert data["verdicts"][0]["kind"] == "prose" and data["dropped_claims"] == 1
    json.dumps(data)  # serializable
