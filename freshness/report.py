"""
Step 6: show the verdicts. A Rich table for the user (next to the diff,
before approval) and a short plain-text note for the model (only the
`changed` findings, so it can propose a fix).

Evidence and inference are separate columns: evidence is a verbatim quote
from the linked page; inference is the judge model's own reasoning.
"""

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from agent import lookups
from agent.lookups import Confidence, FreshnessStatus
from freshness.judge import Verdict
from freshness.research import is_official

_STYLE = {
    FreshnessStatus.CHANGED: ("✘ changed", "bold red"),
    FreshnessStatus.SUPPORTED: ("✔ supported", "green"),
    FreshnessStatus.UNVERIFIABLE: ("? unverifiable", "yellow"),
}


def confidence(v: Verdict) -> Confidence:
    """Computed from the evidence, not asked from the model."""
    if not v.evidence_verified or v.source_url is None:
        return Confidence.LOW
    return Confidence.HIGH if is_official(v.source_url, v.library) else Confidence.MEDIUM


def render(verdicts: list[Verdict], *, title: str = "Freshness check (live docs)",
           skipped: int = 0) -> RenderableType:
    table = Table(title=title, show_lines=True, expand=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Claim / API (line)", ratio=2)
    table.add_column("Version")
    table.add_column("Evidence (quoted from source)", ratio=3)
    table.add_column("Inference (model) / fix", ratio=3)
    for v in sorted(verdicts, key=_order):
        label, style = _STYLE[v.status]
        label = f"{label}\n{confidence(v).value} confidence"
        api = _subject(v)
        if v.kind == "method (heuristic)":
            api += "\n[dim]receiver type guessed[/dim]"
        evidence = Text()
        if v.evidence:
            evidence.append(f"“{v.evidence}”\n")
        if v.source_url:
            evidence.append(v.source_url, style=f"link {v.source_url} cyan")
        if v.note:
            evidence.append(f"\n{v.note}", style="dim")
        inference = v.inference or "-"
        if v.suggested_fix:
            inference += f"\n[bold]Fix:[/bold] {v.suggested_fix}"
        table.add_row(Text(label, style=style), api, v.version or "-", evidence, inference)
    parts: list[RenderableType] = [table]
    if skipped:
        parts.append(Text(f"{skipped} more API uses not checked "
                          f"(limit {lookups.FRESHNESS_MAX_CHECKS} per edit).", style="dim"))
    return Group(*parts)


def model_note(verdicts: list[Verdict]) -> str:
    """Text appended to the tool result so the agent can fix stale APIs."""
    changed = [v for v in verdicts if v.status is FreshnessStatus.CHANGED]
    if not changed:
        return ""
    lines = [lookups.FRESHNESS_MODEL_NOTE_HEADER]
    for v in changed:
        line = f"- {v.symbol} (line {v.line}) is outdated in {v.library} {v.version}."
        if v.evidence:
            line += f' Docs: "{v.evidence}" ({v.source_url}).'
        if v.suggested_fix:
            line += f" Suggested: {v.suggested_fix}"
        lines.append(line)
    return "\n".join(lines)


def _subject(v: Verdict) -> str:
    if v.kind == "prose":
        return v.claim
    return f"{v.symbol} (L{v.line})" if v.line else v.symbol


def _order(v: Verdict) -> tuple[int, int]:
    rank = {FreshnessStatus.CHANGED: 0, FreshnessStatus.UNVERIFIABLE: 1,
            FreshnessStatus.SUPPORTED: 2}[v.status]
    return rank, v.line or 0
