"""
The approval gate: the only place where a human says yes or no.

Key idea: the model can only *request* an action. A write tool (edit_file,
run_command, git) never touches anything itself; it returns a PendingAction
with a preview and an apply() function. The executor in agent/tools.py asks
an Approver, and calls apply() only on a yes. Because the decision lives in
Python code, no prompt — and no prompt injection — can skip it.

Approvers:
  ConsoleApprover  real user: shows the preview, asks [y/N] (default No)
  AllowAll         tests/benchmarks that must run unattended
  DenyAll          safe default for non-interactive use
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax

from agent.lookups import Tool


@dataclass(frozen=True, slots=True)
class PendingAction:
    """A write the model asked for, held until a human decides."""

    tool: Tool
    summary: str  # one line, e.g. "Edit agent/app.py (+3 −1)"
    preview: str  # what the user reviews: a unified diff or the exact command
    apply: Callable[[], str] = field(repr=False)  # performs it; returns output text
    preview_lexer: str = "text"  # "diff" for edits, "bash" for commands


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
        return Confirm.ask("Approve?", default=False, console=self.console)
