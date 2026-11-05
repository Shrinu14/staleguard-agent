"""
The approval gate: the only place where a human says yes or no.

Key idea: the model can only *request* an action. A write tool (edit_file,
run_command, git) never touches anything itself; it returns a PendingAction
with a preview and an apply() function. The executor in agent/tools.py asks
an Approver, and calls apply() only on a yes. Because the decision lives in
Python code, no prompt — and no prompt injection — can skip it.

Reviewers (optional): before the user is asked, a reviewer may attach a
Review to the action — e.g. the freshness checker's report on the new code.
The user sees it next to the diff; its note for the model is appended to the
tool result either way, so the agent can propose a fix.

Approvers:
  ConsoleApprover  real user: shows the preview, asks [y/N] (default No)
  AllowAll         tests/benchmarks that must run unattended
  DenyAll          safe default for non-interactive use
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax

from agent.lookups import Tool


@dataclass(frozen=True, slots=True)
class CodeChange:
    """The file content before and after an edit (for reviewers)."""

    path: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class Review:
    display: Any       # a Rich renderable shown under the preview
    model_note: str    # appended to the tool result ("" = nothing to tell the model)


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A write the model asked for, held until a human decides."""

    tool: Tool
    summary: str  # one line, e.g. "Edit agent/app.py (+3 −1)"
    preview: str  # what the user reviews: a unified diff or the exact command
    apply: Callable[[], str] = field(repr=False)  # performs it; returns output text
    preview_lexer: str = "text"  # "diff" for edits, "bash" for commands
    change: CodeChange | None = None  # set by edit_file
    command: str | None = None        # set by run_command: the exact shell command
    review: Review | None = None      # set by a reviewer before approval


class Reviewer(Protocol):
    def __call__(self, action: PendingAction) -> Review | None: ...


class Approver(Protocol):
    def __call__(self, action: PendingAction) -> bool: ...


class AllowAll:
    def __call__(self, action: PendingAction) -> bool:
        return True


class DenyAll:
    def __call__(self, action: PendingAction) -> bool:
        return False


class ConsoleApprover:
    """Show the preview in the terminal and ask the user. Default is No."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    def __call__(self, action: PendingAction) -> bool:
        if not sys.stdin.isatty():
            # Nobody to ask (piped input, CI): refuse rather than guess.
            self.console.print(f"[red]Denied (no interactive terminal):[/red] {action.summary}")
            return False
        body = Syntax(action.preview, action.preview_lexer, theme="ansi_dark", word_wrap=True)
        self.console.print(
            Panel(body, title=f"[bold]{action.tool.value}[/bold] — {action.summary}",
                  border_style="yellow")
        )
        if action.review is not None:
            self.console.print(action.review.display)
        return Confirm.ask("Approve?", default=False, console=self.console)
