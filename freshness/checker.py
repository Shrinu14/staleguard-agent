"""
The freshness pipeline, end to end, for one code change:

  extract (ast) -> versions (repo files) -> filter (stdlib, cache, cap)
     -> research (Tavily, doc domains, names only) -> judge (Bifrost) -> report

Used two ways:
  - as a Reviewer in the approval gate: every edit_file on a .py file is
    checked BEFORE the user is asked; the report is shown under the diff and
    `changed` findings go back to the model so it can propose a fix;
  - directly, by `staleguard check FILE` (whole file = all uses are "new").
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent import lookups
from agent.approval import PendingAction, Review
from agent.lookups import FreshnessStatus
from freshness.cache import VerdictCache
from freshness.extract import ApiUse, new_api_uses
from freshness.judge import Judge, Verdict, unverifiable
from freshness.report import model_note, render
from freshness.research import ResearchError, Searcher
from freshness.versions import find_version

Progress = Callable[[str], None]


@dataclass(slots=True)
class FreshnessReport:
    verdicts: list[Verdict] = field(default_factory=list)
    skipped: int = 0        # uses over the per-edit cap
    cached: int = 0         # verdicts served from the cache

    @property
    def changed(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.status is FreshnessStatus.CHANGED]


def local_modules(repo_root: Path) -> frozenset[str]:
    """Top-level names importable from the repo itself (never third-party)."""
    names = {p.stem for p in repo_root.glob("*.py")}
    names |= {p.name for p in repo_root.iterdir()
              if p.is_dir() and (p / "__init__.py").exists()}
    return frozenset(names)


def priority(use: ApiUse) -> tuple[int, int]:
    """Calls and method hints first; bare class/attribute references last."""
    weight = 0 if use.kind != "attribute" else (1 if use.symbol.count(".") >= 2 else 2)
    return weight, use.line


class FreshnessChecker:
    def __init__(self, repo_root: Path, *, searcher: Searcher, judge: Judge,
                 cache: VerdictCache | None = None,
                 max_checks: int = lookups.FRESHNESS_MAX_CHECKS,
                 on_progress: Progress | None = None) -> None:
        self.root = repo_root
        self.searcher, self.judge, self.cache = searcher, judge, cache
        self.max_checks = max_checks
        self.on_progress = on_progress or (lambda _msg: None)
        self._local = local_modules(repo_root)

    def check(self, old: str, new: str) -> FreshnessReport:
        uses = sorted(new_api_uses(old, new, local_modules=self._local), key=priority)
        report = FreshnessReport(skipped=max(0, len(uses) - self.max_checks))
        for use in uses[: self.max_checks]:
            report.verdicts.append(self._check_one(use, report))
        return report

    def review(self, action: PendingAction) -> Review | None:
        """Reviewer hook for the approval gate (Python edits only)."""
        change = action.change
        if change is None or Path(change.path).suffix not in lookups.PYTHON_SUFFIXES:
            return None
        report = self.check(change.old, change.new)
        if not report.verdicts:
            return None
        return Review(render(report.verdicts, skipped=report.skipped),
                      model_note(report.verdicts))

    __call__ = review  # a checker IS a Reviewer: pass it (or .review) to ToolExecutor

    def _check_one(self, use: ApiUse, report: FreshnessReport) -> Verdict:
        version = find_version(self.root, use.library)
        if self.cache is not None:
            hit = self.cache.get(version.distribution, version.label, use.symbol)
            if hit is not None:
                report.cached += 1
                return hit.model_copy(update={"line": use.line, "kind": use.kind,
                                              "note": (hit.note + " (cached)").strip()})
        self.on_progress(f"checking {use.symbol} ({version.distribution} {version.label})…")
        try:
            sources = self.searcher(use, version)
        except ResearchError as exc:
            return unverifiable(use, version, str(exc))
        verdict = self.judge(use, version, sources)
        if self.cache is not None:
            self.cache.put(version.distribution, version.label, verdict)
        return verdict
