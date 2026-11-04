"""
Command-line entry point. `staleguard <command>` maps to functions here.
Commands for verify / bench are added in later pieces.
"""

import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from agent import lookups
from agent.approval import AllowAll, Approver, ConsoleApprover
from agent.config import Settings, get_settings
from agent.context import count_tokens
from agent.llm import LLM, LLMError, is_local_model
from agent.loop import AgentLoop, Event, RunResult
from agent.policy import (
    BudgetExceeded,
    BudgetGuard,
    ModelPlan,
    PolicyError,
    load_repo_policy,
    plan_models,
    user_state_dir,
)
from agent.router import Router
from agent.session import SessionLog
from agent.tools import Repo, ToolError, ToolExecutor, repo_map
from freshness.cache import VerdictCache
from freshness.checker import FreshnessChecker
from freshness.judge import Judge
from freshness.report import confidence
from freshness.report import render as render_freshness
from freshness.research import ResearchError, TavilySearcher
from freshness.verify import Verifier, VerifyReport

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.callback()
def main() -> None:
    """StaleGuard: token-efficient coding agent with live API-freshness checks."""


@app.command()
def doctor() -> None:
    """Check that configuration is loaded and complete."""
    try:
        s = get_settings()
    except ValidationError as exc:
        console.print("[red]Invalid configuration in .env:[/red]")
        for err in exc.errors():
            console.print(f"  • {'.'.join(map(str, err['loc']))}: {err['msg']}")
        raise typer.Exit(1) from exc

    ok, missing = "[green]✔ set[/green]", "[yellow]✘ not set[/yellow]"

    table = Table(title="StaleGuard configuration", show_lines=False)
    table.add_column("Setting")
    table.add_column("Value")

    table.add_row("Bifrost URL", s.bifrost_base_url)
    table.add_row("Bifrost VK (default)", ok if s.bifrost_vk_default else missing)
    table.add_row("Bifrost VK (local-only)", ok if s.bifrost_vk_local else missing)
    table.add_row("Tavily API key", ok if s.tavily_api_key else missing)
    spent = BudgetGuard.from_settings(s).spent()
    table.add_row("Budget per user", f"${s.user_budget_usd:.2f} / month  "
                  f"(user '{s.user_id}', spent ${spent:.4f})")
    policy = load_repo_policy(Path.cwd())
    table.add_row("This folder", f"[bold]confidential[/bold] — {policy.reason}"
                  if policy.confidential else "normal (no confidential policy)")
    table.add_row("Fallback chain", " → ".join(s.fallbacks) or "[dim](none)[/dim]")

    unpriced = []
    for role in lookups.ModelRole:
        model = s.model_for(role)
        priced = model in lookups.MODEL_PRICING_PER_MTOK
        if not priced:
            unpriced.append(model)
        note = "" if priced else "  [yellow](no price)[/yellow]"
        table.add_row(f"Model: {role.value}", model + note)

    console.print(table)

    if unpriced:
        console.print(
            "[yellow]Add prices for these models to MODEL_PRICING_PER_MTOK "
            f"in agent/lookups.py:[/yellow] {', '.join(sorted(set(unpriced)))}"
        )
    if not s.tavily_api_key:
        console.print("[yellow]TAVILY_API_KEY is required for freshness checks.[/yellow]")


@app.command()
def ping(
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Primary model (default: MODEL_LOCAL).")
    ] = None,
    fallback: Annotated[
        list[str] | None,
        typer.Option(
            "--fallback", "-f",
            help="Fallback model; repeat for a chain (default: MODEL_FALLBACKS).",
        ),
    ] = None,
    local: Annotated[
        bool, typer.Option("--local", help="Use the local-only virtual key (Ollama only).")
    ] = False,
) -> None:
    """Send one tiny request through Bifrost and show who answered."""
    s = get_settings()
    requested = model or s.model_for(lookups.ModelRole.LOCAL)
    messages = [{"role": "user", "content": lookups.PING_PROMPT}]
    guard = BudgetGuard.from_settings(s)
    try:
        llm = LLM(s, local_only=local)
        if not local:  # the local-only key can only reach free models
            guard.check(requested, s.fallbacks if fallback is None else fallback, messages)
        result = llm.chat(
            messages,
            model=requested,
            fallbacks=fallback,  # None -> MODEL_FALLBACKS from .env
        )
    except (LLMError, BudgetExceeded) as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(1) from exc
    guard.record(result)

    if result.switch_notice:
        console.print(f"[bold yellow]⚠ {result.switch_notice}[/bold yellow]")
    u = result.usage
    console.print(f"[green]✔[/green] {result.served_model} replied: {result.content.strip()!r}")
    console.print(
        f"  tokens: {u.prompt_tokens} in / {u.completion_tokens} out"
        f" ({u.cached_tokens} cached)  •  key: {'local-only' if local else 'default'}"
    )


@app.command("tool")
def tool_cmd(
    name: Annotated[str, typer.Argument(help="Tool name, e.g. read_file or edit_file.")],
    arguments: Annotated[
        str, typer.Argument(help='Tool arguments as JSON, e.g. \'{"path": "README.md"}\'.')
    ] = "{}",
    repo: Annotated[Path, typer.Option("--repo", "-r", help="Repository root.")] = Path("."),
) -> None:
    """Call one agent tool by hand, through the same approval gate the agent uses."""
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError as exc:
        console.print(f"[red]✘ arguments must be JSON: {exc}[/red]")
        raise typer.Exit(2) from exc
    if not isinstance(parsed, dict):
        console.print("[red]✘ arguments must be a JSON object.[/red]")
        raise typer.Exit(2)
    try:
        executor = ToolExecutor(repo, ConsoleApprover(console))
    except ToolError as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(2) from exc

    result = executor.execute(name, parsed)
    style = "green" if result.ok else "red"
    gate = {None: "no approval needed", True: "approved", False: "declined"}[result.approved]
    console.print(f"[{style}]{'✔' if result.ok else '✘'} {result.tool}[/{style}] ({gate})")
    console.print(result.output, markup=False, highlight=False)
    if not result.ok:
        raise typer.Exit(1)


@app.command("map")
def map_cmd(
    repo: Annotated[Path, typer.Option("--repo", "-r", help="Repository root.")] = Path("."),
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Only show the numbers.")] = False,
) -> None:
    """Show the repo map and its token cost versus reading the Python sources whole."""
    try:
        r = Repo(repo)
    except ToolError as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(2) from exc
    sources = [f for f in r.iter_files() if f.suffix in lookups.PYTHON_SUFFIXES]
    repo_map_text = repo_map(r)
    source_text = "\n".join(
        f.read_bytes().decode(lookups.FILE_ENCODING, "replace") for f in sources
    )

    if not quiet:
        console.print(repo_map_text, markup=False, highlight=False)
    map_tokens, source_tokens = count_tokens(repo_map_text), count_tokens(source_text)
    saved = 1 - map_tokens / source_tokens if source_tokens else 0.0
    table = Table(title="Repo map vs. reading every Python file (tiktoken estimate)")
    for col in ("Python files", "Repo map", "Python sources", "Saved"):
        table.add_column(col, justify="right")
    table.add_row(
        str(len(sources)), f"{map_tokens:,} tok", f"{source_tokens:,} tok", f"{saved:.0%}"
    )
    console.print(table)
    if "more files not shown" in repo_map_text:
        console.print(
            f"[yellow]Map hit its {lookups.REPO_MAP_MAX_LINES}-line cap; some files are "
            "summarized as a count, so 'Saved' is overstated for this repo.[/yellow]"
        )

@app.command()
def check(
    path: Annotated[Path, typer.Argument(help="Python file to check (path inside --repo).")],
    repo: Annotated[Path, typer.Option("--repo", "-r", help="Repository root.")] = Path("."),
) -> None:
    """Freshness-check every third-party API used in a Python file (live docs)."""
    s = get_settings()
    try:
        r = Repo(repo)
        target = r.resolve(str(path))
        plan = plan_models(s, load_repo_policy(r.root))
        llm = LLM(s, local_only=plan.local_only)
    except (ToolError, LLMError, PolicyError) as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(2) from exc
    for notice in plan.notices:
        console.print(f"[bold magenta]🔒 {notice}[/bold magenta]")
    checker = _make_checker(s, plan, llm, BudgetGuard.from_settings(s, repo=str(r.root)), r.root)
    if checker is None:
        raise typer.Exit(2)
    source = target.read_text(encoding=lookups.FILE_ENCODING)
    report = checker.check("", source)
    if not report.verdicts:
        console.print("No third-party API uses found.")
        return
    console.print(render_freshness(report.verdicts, title=f"Freshness: {r.rel(target)}",
                                   skipped=report.skipped))
    console.print(f"[dim]{len(report.verdicts)} checked · {report.cached} from cache · "
                  f"{len(report.changed)} changed[/dim]")
    if report.changed:
        raise typer.Exit(1)


@app.command()
def verify(
    text: Annotated[
        str | None, typer.Argument(help='The LLM answer to check, or "-" to read stdin.')
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read the answer from a file.")
    ] = None,
    repo: Annotated[
        Path, typer.Option("--repo", "-r", help="Repo for library versions and policy.")
    ] = Path("."),
    json_out: Annotated[
        bool, typer.Option("--json", help="Print verdicts as JSON (for benchmarks).")
    ] = False,
) -> None:
    """Check an LLM answer: find time-sensitive claims and re-check them live."""
    if file is not None:
        answer = file.read_text(encoding=lookups.FILE_ENCODING)
    elif text == "-" or (text is None and not sys.stdin.isatty()):
        answer = sys.stdin.read()
    elif text:
        answer = text
    else:
        console.print('[red]✘ give the answer as an argument, with --file, or "-" for stdin.[/red]')
        raise typer.Exit(2)

    s = get_settings()
    out = Console(stderr=True) if json_out else console  # keep stdout clean for --json
    try:
        root = Repo(repo).root
        plan = plan_models(s, load_repo_policy(root))
        llm = LLM(s, local_only=plan.local_only)
    except (ToolError, LLMError, PolicyError) as exc:
        out.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(2) from exc
    for notice in plan.notices:
        out.print(f"[bold magenta]🔒 {notice}[/bold magenta]")
    if plan.local_only:
        out.print("[magenta]Prose claims are sent to Tavily as short search queries; "
                  "code blocks only as library/API names.[/magenta]")

    budget = BudgetGuard.from_settings(s, repo=str(root))
    checker = _make_checker(s, plan, llm, budget, root, out=out)
    if checker is None:
        raise typer.Exit(2)
    verifier = Verifier(
        judge=checker.judge, searcher=checker.searcher,  # type: ignore[arg-type]
        code_checker=checker, budget=budget,
        prose_cache=VerdictCache(
            user_state_dir() / lookups.VERIFY_CACHE_FILENAME,
            ttl_days=lookups.VERIFY_CLAIM_CACHE_TTL_DAYS),
        on_progress=lambda m: out.print(f"[dim]  ⟳ {m}[/dim]", highlight=False),
    )
    report = verifier.verify(answer)
    if json_out:
        print(json.dumps(_report_json(report), indent=2, ensure_ascii=False))
    else:
        _print_verify(report)
    if report.count(lookups.FreshnessStatus.CHANGED):
        raise typer.Exit(1)


def _report_json(report: VerifyReport) -> dict:
    return {
        "verdicts": [{**v.model_dump(), "kind": v.kind, "line": v.line, "quote": v.quote,
                      "confidence": confidence(v).value} for v in report.verdicts],
        "dropped_claims": report.dropped, "skipped": report.skipped,
        "cached": report.cached, "notes": report.notes,
    }


def _print_verify(report: VerifyReport) -> None:
    for note in report.notes:
        console.print(f"[yellow]{note}[/yellow]")
    if not report.verdicts:
        console.print("No time-sensitive claims or library API uses found.")
        return
    console.print(render_freshness(report.verdicts, title="Verify: time-sensitive claims",
                                   skipped=report.skipped))
    S = lookups.FreshnessStatus
    console.print(f"[dim]{report.count(S.SUPPORTED)} supported · {report.count(S.CHANGED)} "
                  f"changed · {report.count(S.UNVERIFIABLE)} unverifiable · "
                  f"{report.cached} from cache · {report.dropped} extracted claims dropped "
                  "(quote not in the answer)[/dim]")


def _make_checker(s: Settings, plan: ModelPlan, llm: LLM, budget: BudgetGuard,
                  root: Path, *, out: Console | None = None) -> FreshnessChecker | None:
    """Tavily for research, the judge model through Bifrost (local if confidential)."""
    key = s.tavily_api_key.get_secret_value() if s.tavily_api_key else ""
    try:
        searcher = TavilySearcher(key)
    except ResearchError as exc:
        (out or console).print(f"[yellow]Freshness check off: {exc}[/yellow]")
        return None
    judge_model = plan.model if plan.local_only else s.model_for(lookups.ModelRole.JUDGE)
    judge = Judge(llm, model=judge_model, fallbacks=plan.fallbacks, budget=budget)
    return FreshnessChecker(root, searcher=searcher, judge=judge,
                            cache=VerdictCache.for_repo(root),
                            on_progress=lambda m: (out or console).print(f"[dim]  ⟳ {m}[/dim]"))


@app.command()
def run(
    task: Annotated[str, typer.Argument(help="What the agent should do, in plain words.")],
    repo: Annotated[Path, typer.Option("--repo", "-r", help="Repository root.")] = Path("."),
    model: Annotated[
        str | None, typer.Option("--model", "-m", help="Model (default: MODEL_PLANNER).")
    ] = None,
    auto_approve: Annotated[
        bool, typer.Option("--auto-approve", help="Approve every write. Only for sandboxes.")
    ] = False,
    repo_map_on: Annotated[
        bool, typer.Option("--repo-map/--no-repo-map", help="Send the repo map up front.")
    ] = True,
    signatures: Annotated[
        bool, typer.Option("--signatures/--no-signatures", help="Signatures in the map.")
    ] = True,
    truncate: Annotated[
        bool, typer.Option("--truncate/--no-truncate", help="Truncate long command output.")
    ] = True,
    routing: Annotated[
        bool, typer.Option("--routing/--no-routing",
                           help="Planner model plans, cheap editor model follows through.")
    ] = True,
    compaction: Annotated[
        bool, typer.Option("--compaction/--no-compaction",
                           help="Summarize older turns once the history gets long.")
    ] = True,
    compact_after: Annotated[
        int, typer.Option("--compact-after", min=1,
                          help="Compact when the history has more turns than this.")
    ] = lookups.COMPACT_AFTER_TURNS,
    freshness: Annotated[
        bool, typer.Option("--freshness/--no-freshness",
                           help="Check new library API uses in each edit against live docs.")
    ] = True,
    stable_prefix: Annotated[
        bool, typer.Option("--stable-prefix/--no-stable-prefix",
                           help="Identical prompt prefix every turn (prompt caching).")
    ] = True,
    max_turns: Annotated[
        int, typer.Option("--max-turns", min=1, help="Hard stop for the loop.")
    ] = lookups.MAX_AGENT_TURNS,
) -> None:
    """Run the coding agent on a task. Reads are free; writes ask for approval."""
    s = get_settings()
    approver: Approver = AllowAll() if auto_approve else ConsoleApprover(console)
    if auto_approve:
        console.print("[bold red]--auto-approve: every edit and command will run "
                      "without asking.[/bold red]")
    try:
        executor = ToolExecutor(repo, approver, map_signatures=signatures, truncate=truncate)
        # Policy is decided here, from .agent.toml, before the task text is used.
        plan = plan_models(s, load_repo_policy(executor.repo.root), requested=model)
        llm = LLM(s, local_only=plan.local_only)
    except (ToolError, LLMError, PolicyError) as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(2) from exc
    budget = BudgetGuard.from_settings(s, repo=str(executor.repo.root))
    checker = _make_checker(s, plan, llm, budget, executor.repo.root) if freshness else None
    if checker is not None:
        executor.reviewer = checker.review

    session = SessionLog(executor.repo.root)
    printer = _EventPrinter(console)

    def on_event(event: Event) -> None:
        session.write(event)
        printer(event)

    for notice in plan.notices:
        on_event({"type": lookups.EventType.POLICY.value, "notice": notice})
    loop = AgentLoop(
        llm, executor, router=Router.from_plan(s, plan, enabled=routing),
        fallbacks=plan.fallbacks, budget=budget, on_event=on_event,
        include_repo_map=repo_map_on, max_turns=max_turns,
        compaction=compaction, compact_after=compact_after, stable_prefix=stable_prefix,
    )
    result = loop.run(task)
    if result.reason is lookups.StopReason.BUDGET and _offer_local(result):
        local = s.model_for(lookups.ModelRole.LOCAL)
        try:
            local_llm = LLM(s, local_only=True)
        except LLMError as exc:
            console.print(f"[red]✘ {exc}[/red]")
            raise typer.Exit(2) from exc
        result = loop.resume(local_llm, model=local,
                             fallbacks=[m for m in s.fallbacks if is_local_model(m) and m != local])
    _print_run_summary(result, session)
    if result.reason is not lookups.StopReason.DONE:
        raise typer.Exit(1)


def _offer_local(result: RunResult) -> bool:
    """At the budget limit: ask to continue on the free local model (default No)."""
    console.print(f"[bold red]■ {result.answer}[/bold red]")
    if not console.is_terminal:
        console.print("[dim]No terminal to ask; stopping.[/dim]")
        return False
    return Confirm.ask("Continue on the local model only (free, nothing leaves "
                       "this machine)?", default=False, console=console)


class _EventPrinter:
    """Turns loop events into short terminal lines."""

    def __init__(self, out: Console) -> None:
        self.out = out

    def __call__(self, event: Event) -> None:
        kind = event["type"]
        if kind == lookups.EventType.RUN_START:
            models = " / ".join(event["models"])
            self.out.print(f"[bold]▶ {event['task']}[/bold]  [dim]({models})[/dim]")
        elif kind == lookups.EventType.MODEL_SWITCH:
            self.out.print(f"[bold yellow]⚠ {event['notice']}[/bold yellow]")
        elif kind == lookups.EventType.POLICY:
            self.out.print(f"[bold magenta]🔒 {event['notice']}[/bold magenta]")
        elif kind == lookups.EventType.COMPACTION and event.get("skipped"):
            self.out.print(f"[dim]⇣ compaction skipped: summary (~{event['tokens_after']:,}) "
                           f"would not beat the history (~{event['tokens_before']:,})[/dim]")
        elif kind == lookups.EventType.COMPACTION:
            self.out.print(f"[bold blue]⇣ compacted {event['turns_summarized']} turns: "
                           f"~{event['tokens_before']:,} → ~{event['tokens_after']:,} tokens "
                           f"(summary by {event['summarized_by']})[/bold blue]")
        elif kind == lookups.EventType.BUDGET_WARNING:
            self.out.print(f"[bold yellow]$ {event['notice']}[/bold yellow]")
        elif kind == lookups.EventType.LLM_REPLY:
            u = event["usage"]
            self.out.print(f"[dim]turn {event['turn']} · {event['role']} · "
                           f"{event['served_model']} · {u['prompt']} in / "
                           f"{u['completion']} out · {u['cached']} cached[/dim]")
        elif kind == lookups.EventType.TOOL_CALL:
            args = event["arguments"] or {}
            shown = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
            self.out.print(f"[cyan]→ {event['name']}({shown})[/cyan]", highlight=False)
        elif kind == lookups.EventType.TOOL_RESULT:
            first = (event["output"].splitlines() or [""])[0]
            mark = "[green]✔[/green]" if event["ok"] else "[red]✘[/red]"
            self.out.print(f"  {mark} [dim]{_short(first, 100)}[/dim]", highlight=False)


def _short(value: object, limit: int = 60) -> str:
    """Strings are shown quoted, other values as JSON (so 4 stays 4)."""
    if not isinstance(value, str):
        text = json.dumps(value)
        return text if len(text) <= limit else text[:limit] + "…"
    text = value.replace("\n", "⏎")
    return repr(text[:limit] + "…") if len(text) > limit else repr(text)


def _print_run_summary(result: RunResult, session: SessionLog) -> None:
    ok = result.reason is lookups.StopReason.DONE
    console.print(Panel(result.answer or "(no answer)", title=f"result: {result.reason}",
                        border_style="green" if ok else "red"))
    st = result.stats
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("turns", str(st.turns))
    table.add_row("tokens", f"{st.prompt_tokens:,} in / {st.completion_tokens:,} out "
                            f"({st.cached_tokens:,} cached)")
    table.add_row("tool calls", ", ".join(f"{k}×{v}" for k, v in st.tool_calls.items()) or "-")
    table.add_row("models", ", ".join(f"{k}×{v}" for k, v in st.models.items()) or "-")
    table.add_row("roles", ", ".join(f"{k}×{v}" for k, v in st.roles.items()) or "-")
    if st.compactions:
        table.add_row("compactions", f"{st.compactions} (~{st.compaction_saved_tokens:,} "
                                     "tokens removed from history)")
    table.add_row("session log", str(session.path))
    console.print(table)


if __name__ == "__main__":
    app()
