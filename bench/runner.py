"""
Coding benchmark: real agent runs on small pytest-graded tasks, once per
ablation mode (lookups.BENCH_MODES), repeated N times.

Per run:
  1. copy the task's repo into a throwaway temp dir (the original is never touched)
  2. run the agent loop unattended with BenchApprover (below)
  3. copy the HIDDEN grader (grade_test.py) in and run pytest -> success / fail
  4. record real token usage from the API responses, cost, turns, time

BenchApprover is deliberately narrow. Benchmarks can't stop to ask, but they
must not get a blanket "approve everything":
  - edit_file: allowed (paths are already confined to the sandbox by Repo)
  - run_command: only BENCH_ALLOWED_COMMANDS prefixes, with no shell
    metacharacters (no `;`, `&&`, `|`, `>`, `$(`, quotes ...)
  - git: denied (the sandbox is not a git repo; nothing to do)
Every decision is logged, so a run can be audited afterwards.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import lookups
from agent.approval import PendingAction
from agent.config import Settings
from agent.llm import LLM, LLMResult
from agent.lookups import Tool
from agent.loop import AgentLoop, Event
from agent.policy import BudgetGuard, RepoPolicy, plan_models
from agent.router import Router
from agent.tools import ToolExecutor


# ── tasks ────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class Task:
    id: str
    prompt: str
    test_command: str
    repo: Path
    grader: Path


def load_tasks(root: Path, only: Iterable[str] | None = None) -> list[Task]:
    wanted = set(only or ())
    tasks = []
    for d in sorted(p for p in root.iterdir() if (p / lookups.BENCH_TASK_FILE).is_file()):
        if wanted and d.name not in wanted:
            continue
        meta = tomllib.loads((d / lookups.BENCH_TASK_FILE).read_text(encoding="utf-8"))
        tasks.append(Task(d.name, meta["prompt"].strip(), meta["test_command"],
                          d / lookups.BENCH_TASK_REPO_DIR, d / lookups.BENCH_GRADE_FILE))
    missing = wanted - {t.id for t in tasks}
    if missing:
        raise ValueError(f"unknown task(s): {', '.join(sorted(missing))}")
    return tasks


# ── the unattended approver ──────────────────────────────────
def command_allowed(command: str) -> bool:
    cmd = " ".join(command.split())
    if any(ch in lookups.BENCH_SHELL_METACHARS for ch in command):
        return False
    return any(cmd == p or cmd.startswith(p + " ") for p in lookups.BENCH_ALLOWED_COMMANDS)


@dataclass(slots=True)
class BenchApprover:
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, action: PendingAction) -> bool:
        if action.tool is Tool.EDIT_FILE:
            ok = True
        elif action.tool is Tool.RUN_COMMAND:
            ok = command_allowed(action.command or "")
        else:
            ok = False
        self.decisions.append({"tool": action.tool.value, "summary": action.summary,
                               "approved": ok})
        return ok


class CostMeter:
    """BudgetGuard that also sums the cost of this run's calls."""

    def __init__(self, guard: BudgetGuard) -> None:
        self.guard, self.total = guard, 0.0

    def check(self, *args: Any, **kwargs: Any) -> Any:
        return self.guard.check(*args, **kwargs)

    def record(self, result: LLMResult) -> float:
        cost = self.guard.record(result)
        self.total += cost
        return cost

    def warning(self) -> str | None:
        return self.guard.warning()


# ── one run ──────────────────────────────────────────────────
@dataclass(slots=True)
class RunRecord:
    task: str
    mode: str
    run: int
    success: bool
    reason: str
    turns: int
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost_usd: float
    seconds: float
    tool_calls: dict[str, int]
    text_tool_calls: int
    models: dict[str, int]
    roles: dict[str, int]
    compactions: int
    denied: int
    grade_output: str = ""

    @property
    def key(self) -> tuple[str, str, int]:
        return self.task, self.mode, self.run


def grade(sandbox: Path, grader: Path) -> tuple[bool, str]:
    shutil.copy(grader, sandbox / lookups.BENCH_GRADE_FILE)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             lookups.BENCH_GRADE_FILE],
            cwd=sandbox, capture_output=True, text=True,
            timeout=lookups.BENCH_GRADE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, "grader timed out"
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
    return proc.returncode == 0, "\n".join(tail)


def run_task(task: Task, mode: str, run: int, settings: Settings, *, model: str | None,
             log_dir: Path) -> RunRecord:
    flags = lookups.BENCH_MODES[mode]
    with tempfile.TemporaryDirectory(prefix=f"sg-{task.id}-") as tmp:
        sandbox = Path(tmp) / "repo"
        shutil.copytree(task.repo, sandbox)
        approver = BenchApprover()
        executor = ToolExecutor(sandbox, approver, map_signatures=flags["signatures"],
                                truncate=flags["truncate"])
        plan = plan_models(settings, RepoPolicy(), requested=model)
        meter = CostMeter(BudgetGuard.from_settings(settings, repo=f"bench:{task.id}"))
        log_path = log_dir / f"{task.id}__{mode}__{run}.jsonl"
        loop = AgentLoop(
            LLM(settings), executor, router=Router.from_plan(settings, plan,
                                                             enabled=flags["routing"]),
            fallbacks=plan.fallbacks, budget=meter,  # type: ignore[arg-type]
            on_event=_jsonl_writer(log_path), include_repo_map=flags["repo_map"],
            max_turns=lookups.BENCH_MAX_TURNS, compaction=flags["compaction"],
            stable_prefix=flags["stable_prefix"])
        started = time.perf_counter()
        result = loop.run(task.prompt)
        seconds = time.perf_counter() - started
        success, grade_out = grade(sandbox, task.grader)
        _jsonl_writer(log_path)({"type": "approver", "decisions": approver.decisions,
                                 "graded": success, "grade_output": grade_out})
    st = result.stats
    return RunRecord(
        task.id, mode, run, success, result.reason.value, st.turns, st.prompt_tokens,
        st.completion_tokens, st.cached_tokens, round(meter.total, 6), round(seconds, 2),
        dict(st.tool_calls), st.text_tool_calls, dict(st.models), dict(st.roles),
        st.compactions, sum(not d["approved"] for d in approver.decisions), grade_out)


def _jsonl_writer(path: Path) -> Callable[[Event], None]:
    def write(event: Event) -> None:
        record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), **event}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return write


# ── the whole benchmark ──────────────────────────────────────
def new_results_dir(base: Path) -> Path:
    path = base / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path.mkdir(parents=True)
    return path


def load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_meta(out: Path, **fields: Any) -> None:
    meta_path = out / lookups.BENCH_META_FILE
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta.update(fields)
    meta_path.write_text(json.dumps(meta, indent=2, default=str))


def git_commit(cwd: Path) -> str:
    proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd,
                          capture_output=True, text=True)
    return proc.stdout.strip() or "unknown"


def run_coding(tasks: list[Task], modes: list[str], runs: int, settings: Settings, *,
               model: str | None, out: Path,
               on_record: Callable[[RunRecord], None] | None = None) -> list[RunRecord]:
    """Run every (task, mode, run) not already in out/coding.jsonl (so it resumes)."""
    results_file = out / lookups.BENCH_CODING_RESULTS
    log_dir = out / lookups.BENCH_LOGS_DIRNAME
    log_dir.mkdir(exist_ok=True)
    done = {(r["task"], r["mode"], r["run"]) for r in load_records(results_file)}
    records = []
    for run in range(1, runs + 1):          # run-major: partial results cover every mode
        for mode in modes:
            for task in tasks:
                if (task.id, mode, run) in done:
                    continue
                rec = run_task(task, mode, run, settings, model=model, log_dir=log_dir)
                with results_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(rec)) + "\n")
                records.append(rec)
                if on_record:
                    on_record(rec)
    return records
