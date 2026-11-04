"""
Tests for the freshness checker (freshness/*) and its hook in the approval
gate. Offline: a fake searcher plays Tavily and a fake LLM plays the judge.
"""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from openai.types.chat import ChatCompletionMessage

from agent import lookups
from agent.approval import PendingAction, Review
from agent.llm import LLMError, LLMResult, Usage
from agent.lookups import FreshnessStatus, Tool
from agent.tools import Repo, ToolExecutor, edit_file
from freshness.cache import VerdictCache
from freshness.checker import FreshnessChecker, local_modules
from freshness.extract import ApiUse, extract_api_uses, new_api_uses
from freshness.judge import Judge, Verdict, grade
from freshness.report import model_note
from freshness.research import ResearchError, Source, TavilySearcher, build_query
from freshness.versions import VersionInfo, distribution_for, find_version

STALE_OPENAI = '''import os
import openai

openai.api_key = os.environ["KEY"]
SECRET = "SECRET_TOKEN_123"

def ask(q):
    return openai.ChatCompletion.create(model="m", messages=[{"role": "user", "content": q}])
'''
QUOTE = "You tried to access openai.ChatCompletion, but this is no longer supported"
SRC = Source("https://github.com/openai/openai-python/issues/1",
             "v1 migration", f"Error: {QUOTE} in openai>=1.0.0.")
USE = ApiUse("openai", "openai.ChatCompletion.create", 8, "call")
VER = VersionInfo("openai", ">=1.40", "requirements.txt")


def judge_reply(status: str, quote: str = QUOTE, source: int | None = 1, fix: str = "") -> str:
    return json.dumps({"status": status, "evidence_quote": quote, "source": source,
                       "inference": "my reasoning", "suggested_fix": fix})


class FakeLLM:
    def __init__(self, *texts: str | Exception) -> None:
        self.texts = list(texts)
        self.sent: list[list[dict]] = []

    def chat(self, messages, **kwargs) -> LLMResult:
        self.sent.append(list(messages))
        nxt = self.texts.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return LLMResult(ChatCompletionMessage(role="assistant", content=nxt),
                         kwargs["model"], kwargs["model"], False, Usage(10, 5, 15, 0))


class FakeSearcher:
    def __init__(self, sources: list[Source] | Exception) -> None:
        self.sources = sources
        self.queries: list[str] = []

    def __call__(self, use: ApiUse, version: VersionInfo) -> list[Source]:
        self.queries.append(build_query(use, version))
        if isinstance(self.sources, Exception):
            raise self.sources
        return self.sources


# ── extract ──────────────────────────────────────────────────
def test_extract_resolves_imports_and_skips_stdlib() -> None:
    uses = {u.symbol: u.kind for u in extract_api_uses(STALE_OPENAI)}
    assert uses == {"openai.api_key": "attribute", "openai.ChatCompletion.create": "call"}


def test_extract_aliases_from_imports_and_local_modules() -> None:
    src = ("import numpy as np\nfrom requests import get as fetch\nimport mylib\n"
           "np.zeros(3)\nfetch('u')\nmylib.run()\n")
    found = {u.symbol for u in extract_api_uses(src, local_modules=frozenset({"mylib"}))}
    assert found == {"numpy.zeros", "requests.get"}


def test_extract_skips_base_classes_and_annotations_but_hints_methods() -> None:
    src = ("from pydantic import BaseModel, validator\n"
           "class U(BaseModel):\n    x: int\n"
           "    @validator('x')\n    def v(cls, x): return x\n"
           "def f(u: BaseModel) -> BaseModel:\n    return U.parse_obj({}).dict()\n")
    uses = {u.symbol: u.kind for u in extract_api_uses(src)}
    assert uses == {"pydantic.validator": "call",
                    "pydantic.BaseModel.parse_obj": "method (heuristic)",
                    "pydantic.BaseModel.dict": "method (heuristic)"}


def test_method_hints_need_the_library_import() -> None:
    assert extract_api_uses("import json\nd = {}\nd.copy()\nx.dict()\n") == []


def test_new_api_uses_only_reports_additions() -> None:
    old = "import openai\nopenai.api_key = 'k'\n"
    assert [u.symbol for u in new_api_uses(old, STALE_OPENAI)] == ["openai.ChatCompletion.create"]
    assert extract_api_uses("def broken(:\n") == []


# ── versions ─────────────────────────────────────────────────
def test_version_sources_in_priority_order(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\ndependencies = ["openai>=1.0"]\n')
    assert find_version(tmp_path, "openai") == VersionInfo("openai", ">=1.0", "pyproject.toml")
    (tmp_path / "requirements.txt").write_text("# pins\nOpenAI==1.50.0  # comment\n")
    assert find_version(tmp_path, "openai").version == "1.50.0"
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "openai"\nversion = "1.51.2"\n')
    assert find_version(tmp_path, "openai").source == "uv.lock"
    site = tmp_path / ".venv/lib/python3.12/site-packages/openai-1.52.0.dist-info"
    site.mkdir(parents=True)
    (site / "METADATA").write_text("Metadata-Version: 2.1\nName: openai\nVersion: 1.52.0\n")
    assert find_version(tmp_path, "openai") == VersionInfo("openai", "1.52.0", ".venv (installed)")


def test_unknown_version_and_name_mapping(tmp_path: Path) -> None:
    assert find_version(tmp_path, "openai").version is None
    assert distribution_for("yaml") == "PyYAML"
    assert distribution_for("google.generativeai.types") == "google-generativeai"
    (tmp_path / "requirements.txt").write_text("pyyaml==6.0\n")  # PEP 503: case-insensitive
    assert find_version(tmp_path, "yaml") == VersionInfo("PyYAML", "6.0", "requirements.txt")


# ── research ─────────────────────────────────────────────────
def test_query_contains_only_names() -> None:
    assert build_query(USE, VER) == ("openai >=1.40 openai.ChatCompletion.create "
                                     "deprecated removed replaced")
    with pytest.raises(ResearchError):
        build_query(ApiUse("x", 'x.f("SECRET")\nimport os', 1, "call"), VER)


def test_tavily_searcher_restricts_domains_and_clips() -> None:
    calls: list[dict] = []

    class Client:
        def __init__(self, key: str) -> None:
            assert key == "tvly-test"

        def search(self, query, **kw):
            calls.append({"query": query, **kw})
            return {"results": [{"url": "https://docs.pydantic.dev/x", "title": "t",
                                 "content": "a" * 5000}, {"title": "no url"}]}

    use = ApiUse("pydantic", "pydantic.validator", 1, "call")
    found = TavilySearcher("tvly-test", client_factory=Client)(use, VER)
    assert calls[0]["include_domains"] == ["docs.pydantic.dev"]
    assert len(found) == 1 and len(found[0].content) == lookups.FRESHNESS_SNIPPET_CHARS
    with pytest.raises(ResearchError):
        TavilySearcher("")


def test_tavily_failure_becomes_research_error() -> None:
    class Client:
        def __init__(self, key): ...
        def search(self, *a, **k):
            raise TimeoutError("slow")

    with pytest.raises(ResearchError, match="TimeoutError"):
        TavilySearcher("k", client_factory=Client)(USE, VER)


# ── judge: evidence vs inference is enforced in code ────────
def test_grade_changed_with_verbatim_quote() -> None:
    v = grade(USE, VER, [SRC], "Sure!\n" + judge_reply("changed", fix="client.chat.completions"))
    assert v.status is FreshnessStatus.CHANGED and v.evidence_verified
    assert v.source_url == SRC.url and v.evidence == QUOTE and v.inference == "my reasoning"


@pytest.mark.parametrize("reply, note", [
    (judge_reply("changed", quote="ChatCompletion was removed in 1.0"), "not found"),
    (judge_reply("changed", source=7), "no source we provided"),
    (judge_reply("supported", quote="short"), "not found"),
    ("I think it's deprecated.", "not valid JSON"),
    ('{"status": "maybe"}', "not valid JSON"),
])
def test_grade_downgrades_unbacked_verdicts(reply: str, note: str) -> None:
    v = grade(USE, VER, [SRC], reply)
    assert v.status is FreshnessStatus.UNVERIFIABLE and note in v.note
    assert v.evidence == "" and v.suggested_fix == ""


def test_judge_skips_llm_without_sources_and_survives_errors() -> None:
    llm = FakeLLM(LLMError("down"))
    j = Judge(llm, model="ollama/x")  # type: ignore[arg-type]
    assert j(USE, VER, []).status is FreshnessStatus.UNVERIFIABLE and llm.sent == []
    v = j(USE, VER, [SRC])
    assert v.status is FreshnessStatus.UNVERIFIABLE and "judge unavailable" in v.note


# ── cache ────────────────────────────────────────────────────
def test_cache_roundtrip_ttl_and_unsourced(tmp_path: Path) -> None:
    cache = VerdictCache(tmp_path / "c.sqlite3")
    sourced = grade(USE, VER, [SRC], judge_reply("changed"))
    cache.put("openai", ">=1.40", sourced)
    hit = cache.get("openai", ">=1.40", USE.symbol)
    assert hit is not None and hit.status is FreshnessStatus.CHANGED
    assert cache.get("openai", "2.0", USE.symbol) is None
    cache.put("openai", "2.0", grade(USE, VER, [SRC], "garbage"))  # unsourced: not stored
    assert cache.get("openai", "2.0", USE.symbol) is None
    stale = VerdictCache(tmp_path / "c.sqlite3", ttl_days=0)
    stale.ttl = timedelta(seconds=-1)
    assert stale.get("openai", ">=1.40", USE.symbol) is None


# ── checker + approval gate ──────────────────────────────────
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "requirements.txt").write_text("openai>=1.40\n")
    (tmp_path / "helpers.py").write_text("x = 1\n")
    return tmp_path


def make_checker(repo: Path, llm: FakeLLM, searcher: FakeSearcher, **kw) -> FreshnessChecker:
    return FreshnessChecker(repo, searcher=searcher,
                            judge=Judge(llm, model="ollama/x"),  # type: ignore[arg-type]
                            cache=VerdictCache.for_repo(repo), **kw)


def test_checker_end_to_end_and_cache_hit(repo: Path) -> None:
    llm = FakeLLM(judge_reply("changed"), *[judge_reply("unverifiable")] * 2)
    searcher = FakeSearcher([SRC])
    checker = make_checker(repo, llm, searcher)
    report = checker.check("", STALE_OPENAI)
    assert [v.symbol for v in report.changed] == ["openai.ChatCompletion.create"]
    assert report.changed[0].line == 8 and report.changed[0].version == ">=1.40"
    again = checker.check("", STALE_OPENAI)  # changed verdict cached, unverifiable re-asked
    assert again.cached == 1 and len(searcher.queries) == 3


def test_no_code_reaches_tavily_or_the_judge(repo: Path) -> None:
    llm, searcher = FakeLLM(judge_reply("changed"), judge_reply("changed")), FakeSearcher([SRC])
    make_checker(repo, llm, searcher).check("", STALE_OPENAI)
    sent = json.dumps(llm.sent) + " ".join(searcher.queries)
    assert "SECRET_TOKEN_123" not in sent and 'os.environ["KEY"]' not in sent


def test_cap_on_checks(repo: Path) -> None:
    src = "import numpy as np\n" + "".join(f"np.f{i}()\n" for i in range(5))
    llm = FakeLLM(*[judge_reply("unverifiable")] * 2)
    report = make_checker(repo, llm, FakeSearcher([SRC]), max_checks=2).check("", src)
    assert len(report.verdicts) == 2 and report.skipped == 3


def test_review_in_approval_gate_tells_the_model(repo: Path) -> None:
    llm = FakeLLM(judge_reply("changed"), judge_reply("unverifiable"))
    searcher = FakeSearcher([SRC])
    seen: list[PendingAction] = []

    def approver(action: PendingAction) -> bool:
        seen.append(action)
        return False

    executor = ToolExecutor(repo, approver, reviewer=make_checker(repo, llm, searcher))
    res = executor.execute(Tool.EDIT_FILE, {"path": "ai.py", "search": "",
                                            "replace": STALE_OPENAI})
    assert seen[0].review is not None  # the user saw the report before deciding
    assert res.approved is False and lookups.TOOL_DECLINED_MESSAGE in res.output
    assert lookups.FRESHNESS_MODEL_NOTE_HEADER in res.output and SRC.url in res.output


def test_review_skips_non_python_and_failing_reviewers(repo: Path) -> None:
    checker = make_checker(repo, FakeLLM(), FakeSearcher([SRC]))
    assert checker.review(edit_file(Repo(repo), "notes.md", "", "import openai\n")) is None

    def broken(action: PendingAction) -> Review:
        raise RuntimeError("bug")

    res = ToolExecutor(repo, lambda a: True, reviewer=broken).execute(
        Tool.EDIT_FILE, {"path": "a.py", "search": "", "replace": "x = 2\n"})
    assert res.ok and (repo / "a.py").exists()  # a reviewer bug never blocks the edit


def test_local_modules_and_model_note(repo: Path) -> None:
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text("")
    assert local_modules(repo) == {"helpers", "pkg"}
    v = Verdict(claim="c", symbol="a.b", library="a", version="1", status="supported")
    assert model_note([v]) == ""
