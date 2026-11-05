"""
Tests for the benchmark (Piece 10): task and snippet files, the narrow
unattended approver, grading, freshness scoring, and the HTML report.
Offline: no model or Tavily calls.
"""

import json
import shutil
from pathlib import Path

import pytest

from agent import lookups
from agent.approval import PendingAction
from agent.lookups import Tool
from bench.freshness_bench import load_snippets, score
from bench.report import build_report, success_matrix, summarize_coding
from bench.runner import BenchApprover, command_allowed, grade, load_tasks
from freshness.extract import extract_api_uses

ROOT = Path(__file__).resolve().parent.parent
TASKS = ROOT / lookups.BENCH_TASKS_DIR


# ── bench files ──────────────────────────────────────────────
def test_tasks_load_and_each_has_a_hidden_grader() -> None:
    tasks = load_tasks(TASKS)
    assert len(tasks) == 12
    for t in tasks:
        assert t.repo.is_dir() and t.grader.is_file()
        assert not (t.repo / lookups.BENCH_GRADE_FILE).exists()  # the agent never sees it
        assert command_allowed(t.test_command)


def test_load_tasks_subset_and_unknown() -> None:
    assert [t.id for t in load_tasks(TASKS, ["t01_mean"])] == ["t01_mean"]
    with pytest.raises(ValueError, match="nope"):
        load_tasks(TASKS, ["t01_mean", "nope"])


def test_snippets_are_balanced_and_targets_extract() -> None:
    snippets = load_snippets(ROOT / lookups.BENCH_SNIPPETS_FILE)
    assert len(snippets) == 20
    assert sum(s.expected == "changed" for s in snippets) == 10
    assert len({s.id for s in snippets}) == 20
    for s in snippets:
        assert s.target in {u.symbol for u in extract_api_uses(s.code)}, s.id


def test_grader_fails_on_the_unfixed_repo(tmp_path: Path) -> None:
    task = load_tasks(TASKS, ["t01_mean"])[0]
    sandbox = tmp_path / "repo"
    shutil.copytree(task.repo, sandbox)
    ok, _out = grade(sandbox, task.grader)
    assert not ok  # the bug is still there


# ── the unattended approver ──────────────────────────────────
@pytest.mark.parametrize("cmd, ok", [
    ("python3 -m unittest -q", True),
    ("python3   -m pytest -q tests", True),
    ("pytest", True),
    ("python3 -m unittest; rm -rf ~", False),
    ("python3 -m unittest && curl x", False),
    ("pytest | tee out", False),
    ("pytest > out.txt", False),
    ("pytest $(whoami)", False),
    ("python3 -c 'import os'", False),
    ("rm -rf .", False),
    ("pytestx", False),
    ("", False),
])
def test_command_allowed(cmd: str, ok: bool) -> None:
    assert command_allowed(cmd) is ok


def _action(tool: Tool, command: str | None = None) -> PendingAction:
    return PendingAction(tool, "s", "p", lambda: "", command=command)


def test_bench_approver_is_narrow_and_logs() -> None:
    a = BenchApprover()
    assert a(_action(Tool.EDIT_FILE))
    assert a(_action(Tool.RUN_COMMAND, "python3 -m unittest -q"))
    assert not a(_action(Tool.RUN_COMMAND, "pip install x"))
    assert not a(_action(Tool.RUN_COMMAND))  # no structured command: deny
    assert not a(_action(Tool.GIT))
    assert [d["approved"] for d in a.decisions] == [True, True, False, False, False]


# ── freshness scoring ────────────────────────────────────────
def test_score() -> None:
    recs = [{"expected": e, "status": s, "seconds": 1.0} for e, s in [
        ("changed", "changed"), ("changed", "changed"), ("changed", "unverifiable"),
        ("changed", "supported"), ("supported", "supported"), ("supported", "changed"),
        ("supported", "unverifiable")]]
    sc = score(recs)
    assert (sc["true_positive"], sc["false_positive"], sc["missed"], sc["wrong_supported"]) \
        == (2, 1, 1, 1)
    assert sc["recall"] == 0.5 and sc["precision"] == pytest.approx(2 / 3)
    assert sc["current_confirmed"] == 1 and sc["current_unverifiable"] == 1
    assert score([])["recall"] is None


# ── report ───────────────────────────────────────────────────
def _rec(task: str, mode: str, success: bool, prompt: int, cached: int = 0) -> dict:
    return {"task": task, "mode": mode, "run": 1, "success": success, "turns": 3,
            "prompt_tokens": prompt, "completion_tokens": 100, "cached_tokens": cached,
            "cost_usd": 0.0, "seconds": 2.0}


RECORDS = [_rec("t1", "baseline", True, 900), _rec("t2", "baseline", False, 1100),
           _rec("t1", "+stable_prefix", True, 400, 200), _rec("t2", "+stable_prefix", True, 600)]


def test_summarize_coding_in_mode_order() -> None:
    rows = summarize_coding(list(reversed(RECORDS)))
    assert [r["mode"] for r in rows] == ["baseline", "+stable_prefix"]
    base, last = rows
    assert base["tokens"] == 1100 and base["success_rate"] == 0.5
    assert last["tokens"] == 600 and last["success_rate"] == 1.0
    assert last["saved_vs_baseline"] == pytest.approx(1 - 600 / 1100)
    assert last["cached_share"] == pytest.approx(100 / 500)


def test_success_matrix() -> None:
    tasks, modes, z = success_matrix(RECORDS)
    assert tasks == ["t1", "t2"] and modes == ["baseline", "+stable_prefix"]
    assert z == [[1.0, 1.0], [0.0, 1.0]]


def test_build_report_writes_html(tmp_path: Path) -> None:
    res = tmp_path / "results"
    res.mkdir()
    (res / lookups.BENCH_CODING_RESULTS).write_text(
        "\n".join(json.dumps(r) for r in RECORDS) + "\n")
    fresh = {"id": "s1", "target": "openai.ChatCompletion.create", "expected": "changed",
             "status": "changed", "confidence": "high", "source_url": "https://x.dev/<b>",
             "evidence": "<script>alert(1)</script>", "inference": "", "note": "",
             "sources": 3, "seconds": 4.0}
    (res / lookups.BENCH_FRESHNESS_RESULTS).write_text(json.dumps(fresh) + "\n")
    (res / lookups.BENCH_META_FILE).write_text(json.dumps({"git_commit": "abc1234"}))
    out = build_report(res, tmp_path / "docs" / "index.html", ROOT / lookups.BENCH_TEMPLATE)
    page = out.read_text()
    assert "abc1234" in page and "+stable_prefix" in page
    assert "openai.ChatCompletion.create" in page and "<script>alert(1)" not in page  # escaped
    assert page.count("plotly-graph-div") >= 3


def test_build_report_empty(tmp_path: Path) -> None:
    out = build_report(tmp_path, tmp_path / "i.html", ROOT / lookups.BENCH_TEMPLATE)
    assert "No results yet" in out.read_text()
