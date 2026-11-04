"""
Command-line entry point. `staleguard <command>` maps to functions here.
Commands for run / verify / bench are added in later pieces.
"""

from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from agent import lookups
from agent.config import get_settings
from agent.llm import LLM, LLMError

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
    table.add_row("Budget per user", f"${s.user_budget_usd:.2f}")
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
    try:
        result = LLM(s, local_only=local).chat(
            [{"role": "user", "content": lookups.PING_PROMPT}],
            model=requested,
            fallbacks=fallback,  # None -> MODEL_FALLBACKS from .env
        )
    except LLMError as exc:
        console.print(f"[red]✘ {exc}[/red]")
        raise typer.Exit(1) from exc

    if result.switch_notice:
        console.print(f"[bold yellow]⚠ {result.switch_notice}[/bold yellow]")
    u = result.usage
    console.print(f"[green]✔[/green] {result.served_model} replied: {result.content.strip()!r}")
    console.print(
        f"  tokens: {u.prompt_tokens} in / {u.completion_tokens} out"
        f" ({u.cached_tokens} cached)  •  key: {'local-only' if local else 'default'}"
    )


if __name__ == "__main__":
    app()
