"""
Freshness benchmark: 20 snippets (bench/freshness/snippets.toml), half using a
stale API. For each, the real pipeline runs on the snippet's target API:
versions (from the snippet's requirement) -> Tavily -> judge -> verdict.
No cache, so every number is a fresh, live check.

Scoring (positive class = "stale"):
  true positive   stale snippet judged `changed`
  false positive  current snippet judged `changed`      (a false alarm)
  missed          stale snippet judged `unverifiable`
  wrong           stale snippet judged `supported`      (the harmful error)
precision = TP / (TP + FP), recall = TP / stale snippets.
"""

import json
import tempfile
import time
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agent import lookups
from freshness.extract import extract_api_uses
from freshness.judge import Judge, unverifiable
from freshness.report import confidence
from freshness.research import ResearchError, Searcher
from freshness.versions import find_version


@dataclass(frozen=True, slots=True)
class Snippet:
    id: str
    requirement: str
    target: str
    expected: str  # "changed" | "supported"
    code: str


@dataclass(slots=True)
class SnippetRecord:
    id: str
    target: str
    expected: str
    status: str
    confidence: str
    source_url: str | None
    evidence: str
    inference: str
    note: str
    sources: int
    seconds: float


def load_snippets(path: Path) -> list[Snippet]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [Snippet(s["id"], s["requirement"], s["target"], s["expected"], s["code"].strip())
            for s in data["snippet"]]


def run_snippet(snippet: Snippet, searcher: Searcher, judge: Judge) -> SnippetRecord:
    started = time.perf_counter()
    uses = {u.symbol: u for u in extract_api_uses(snippet.code)}
    use = uses.get(snippet.target)
    if use is None:
        raise ValueError(f"{snippet.id}: target {snippet.target} not found by extraction")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "requirements.txt").write_text(snippet.requirement + "\n")
        version = find_version(root, use.library)
    sources: list = []
    try:
        sources = searcher(use, version)
        verdict = judge(use, version, sources)
    except ResearchError as exc:
        verdict = unverifiable(use, version, str(exc))
    return SnippetRecord(
        snippet.id, snippet.target, snippet.expected, verdict.status.value,
        confidence(verdict).value, verdict.source_url, verdict.evidence, verdict.inference,
        verdict.note, len(sources), round(time.perf_counter() - started, 2))


def run_freshness(snippets: list[Snippet], searcher: Searcher, judge: Judge, *, out: Path,
                  on_record: Callable[[SnippetRecord], None] | None = None
                  ) -> list[SnippetRecord]:
    results = out / lookups.BENCH_FRESHNESS_RESULTS
    results.unlink(missing_ok=True)  # one complete pass per results dir
    records = []
    for snippet in snippets:
        rec = run_snippet(snippet, searcher, judge)
        with results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(rec)) + "\n")
        records.append(rec)
        if on_record:
            on_record(rec)
    return records


def score(records: list[dict[str, Any]]) -> dict[str, Any]:
    stale = [r for r in records if r["expected"] == "changed"]
    fresh = [r for r in records if r["expected"] == "supported"]
    tp = sum(r["status"] == "changed" for r in stale)
    fp = sum(r["status"] == "changed" for r in fresh)
    return {
        "snippets": len(records), "stale": len(stale), "current": len(fresh),
        "true_positive": tp, "false_positive": fp,
        "missed": sum(r["status"] == "unverifiable" for r in stale),
        "wrong_supported": sum(r["status"] == "supported" for r in stale),
        "current_confirmed": sum(r["status"] == "supported" for r in fresh),
        "current_unverifiable": sum(r["status"] == "unverifiable" for r in fresh),
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / len(stale) if stale else None,
        "avg_seconds": (sum(r["seconds"] for r in records) / len(records)) if records else 0.0,
    }
