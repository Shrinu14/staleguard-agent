"""
Policy: what a run is allowed to do, decided BEFORE any model call.

Two independent safety rails, each enforced twice (here, and by Bifrost):

1. Confidential repos (per repo)
   A repo with `.agent.toml` containing `confidential = true`:
     - every role uses MODEL_LOCAL; `--model` is ignored (with a notice) and
       cloud fallbacks are dropped                         -> plan_models()
     - the LLM client is built with BIFROST_VK_LOCAL, a virtual key that
       Bifrost only lets reach Ollama (cloud = HTTP 403)   -> gateway
     - the LLM client also refuses a non-local model id before sending
       anything                                            -> llm.py
   Why prompt injection can't break this: the model id and the virtual key
   are chosen here, from a file on disk, before the task text is read. Text
   in the task, in a file, or in a model reply can only change *messages*,
   never which model or key is used.
   Fail closed: an unreadable or malformed `.agent.toml` counts as confidential.

2. Budget (per user, per calendar month, UTC)
     - soft limit, here: before each call BudgetGuard estimates its cost
       (tiktoken prompt count + BUDGET_EST_COMPLETION_TOKENS, priced at the
       most expensive model in the fallback chain). If spent + estimate would
       pass USER_BUDGET_USD the call is not made (BudgetExceeded).
       A cloud model with no entry in MODEL_PRICING_PER_MTOK is refused: we
       never guess a price.
     - hard limit, in Bifrost: the virtual key's budget. Over it, Bifrost
       answers 402 "budget_exceeded" -> llm.LLMBudgetError.
   Real usage from each response is written to a sqlite ledger in
   ~/.staleguard/ (per user, so switching repos does not reset the budget).
"""

import json
import sqlite3
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent import lookups
from agent.config import Settings
from agent.context import count_tokens
from agent.llm import LLMResult
from agent.llm import is_local_model as is_local
from agent.lookups import ModelRole

Price = tuple[float, float]  # USD per 1M tokens: (input, output)


# ─────────────────────────────────────────────────────────────
# Confidential mode
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RepoPolicy:
    confidential: bool = False
    reason: str = ""  # why, for the user: "set in .agent.toml", "unreadable ..."

    @property
    def local_only(self) -> bool:
        return self.confidential


def load_repo_policy(repo_root: Path) -> RepoPolicy:
    """Read <repo>/.agent.toml. Missing = normal repo; broken = confidential."""
    path = repo_root / lookups.POLICY_FILENAME
    if not path.exists():
        return RepoPolicy()
    try:
        data = tomllib.loads(path.read_text(encoding=lookups.FILE_ENCODING))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return RepoPolicy(True, f"{lookups.POLICY_FILENAME} is unreadable ({exc}); "
                                "treating the repo as confidential")
    value = data.get(lookups.POLICY_CONFIDENTIAL_KEY, False)
    if not isinstance(value, bool):
        return RepoPolicy(True, f"'{lookups.POLICY_CONFIDENTIAL_KEY}' in "
                                f"{lookups.POLICY_FILENAME} is not true/false; "
                                "treating the repo as confidential")
    if value:
        return RepoPolicy(True, f"confidential = true in {lookups.POLICY_FILENAME}")
    return RepoPolicy()


def provider_of(model: str) -> str:
    """'groq/llama-3.1-8b' -> 'groq'. No prefix -> '' (never counts as local)."""
    return model.split("/", 1)[0] if "/" in model else ""


@dataclass(frozen=True, slots=True)
class ModelPlan:
    model: str
    fallbacks: tuple[str, ...]
    local_only: bool  # build the LLM with the local-only virtual key
    notices: tuple[str, ...] = ()


def plan_models(
    settings: Settings,
    policy: RepoPolicy,
    *,
    requested: str | None = None,
    role: ModelRole = ModelRole.PLANNER,
) -> ModelPlan:
    """Pick the model, fallbacks and key for a run. Confidential wins over flags."""
    if not policy.confidential:
        return ModelPlan(requested or settings.model_for(role), settings.fallbacks, False)

    local = settings.model_for(ModelRole.LOCAL)
    notices = [f"Confidential repo ({policy.reason}): local models only, via the "
               "local-only virtual key."]
    if not is_local(local):
        raise PolicyError(f"MODEL_LOCAL is '{local}', which is not a "
                          f"{lookups.LOCAL_PROVIDER} model; refusing to run a "
                          "confidential repo on it.")
    if requested and requested != local:
        notices.append(f"Ignored --model {requested}: not allowed in a confidential repo.")
    dropped = [m for m in settings.fallbacks if not is_local(m)]
    if dropped:
        notices.append(f"Dropped cloud fallbacks: {', '.join(dropped)}.")
    fallbacks = tuple(m for m in settings.fallbacks if is_local(m) and m != local)
    return ModelPlan(local, fallbacks, True, tuple(notices))


class PolicyError(RuntimeError):
    """The configuration makes a safe run impossible."""


# ─────────────────────────────────────────────────────────────
# Budget
# ─────────────────────────────────────────────────────────────
class BudgetExceeded(RuntimeError):
    """The next call would pass the user's budget (or can't be priced)."""


def price_per_mtok(model: str, pricing: Mapping[str, Price]) -> Price | None:
    """Known price, (0, 0) for any local model, None for an unpriced cloud model."""
    if model in pricing:
        return pricing[model]
    return (0.0, 0.0) if is_local(model) else None


def cost_usd(price: Price, prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def ledger_path() -> Path:
    folder = Path.home() / lookups.USER_STATE_DIRNAME
    folder.mkdir(exist_ok=True)
    return folder / lookups.LEDGER_DB_FILENAME


class BudgetLedger:
    """Append-only spend records in sqlite. One row per model call."""

    _SCHEMA = """
        CREATE TABLE IF NOT EXISTS spend (
            ts                TEXT NOT NULL,   -- ISO-8601 UTC
            user              TEXT NOT NULL,
            model             TEXT NOT NULL,   -- the model that actually answered
            prompt_tokens     INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost_usd          REAL NOT NULL,
            repo              TEXT
        )"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or ledger_path()
        with self._connect() as db:
            db.execute(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def record(self, *, user: str, model: str, prompt_tokens: int,
               completion_tokens: int, cost: float, repo: str = "",
               when: datetime | None = None) -> None:
        ts = (when or datetime.now(UTC)).isoformat()
        with self._connect() as db:
            db.execute("INSERT INTO spend VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (ts, user, model, prompt_tokens, completion_tokens, cost, repo))

    def spent(self, user: str, *, since: datetime | None = None) -> float:
        """USD spent by `user` since `since` (default: start of this month, UTC)."""
        start = (since or month_start()).isoformat()
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend "
                             "WHERE user = ? AND ts >= ?", (user, start)).fetchone()
        return float(row[0])


@dataclass(frozen=True, slots=True)
class Estimate:
    cost: float
    prompt_tokens: int
    spent: float
    budget: float


@dataclass(slots=True)
class BudgetGuard:
    """Soft budget: check() before a call, record() after it."""

    ledger: BudgetLedger
    user: str
    budget_usd: float
    repo: str = ""
    pricing: Mapping[str, Price] = field(default_factory=lambda: lookups.MODEL_PRICING_PER_MTOK)
    warn_ratio: float = lookups.BUDGET_WARN_RATIO
    _warned: bool = False

    @classmethod
    def from_settings(cls, settings: Settings, *, repo: str = "",
                      ledger: BudgetLedger | None = None) -> "BudgetGuard":
        return cls(ledger or BudgetLedger(), settings.user_id,
                   settings.user_budget_usd, repo)

    def spent(self) -> float:
        return self.ledger.spent(self.user)

    def check(self, model: str, fallbacks: Iterable[str] = (),
              messages: Sequence[dict[str, Any]] = (),
              tools: Sequence[dict[str, Any]] = ()) -> Estimate:
        """Raise BudgetExceeded if the next call could pass the budget."""
        chain = [model, *fallbacks]
        prices = {m: price_per_mtok(m, self.pricing) for m in chain}
        unpriced = [m for m, p in prices.items() if p is None]
        if unpriced:
            raise BudgetExceeded(
                f"No price for {', '.join(unpriced)} in MODEL_PRICING_PER_MTOK "
                "(agent/lookups.py); refusing to spend money on an unpriced model.")
        prompt = count_tokens(json.dumps([list(messages), list(tools)], default=str))
        worst = max(cost_usd(p, prompt, lookups.BUDGET_EST_COMPLETION_TOKENS)
                    for p in prices.values() if p is not None)
        spent = self.spent()
        est = Estimate(worst, prompt, spent, self.budget_usd)
        if worst > 0 and spent + worst > self.budget_usd:
            raise BudgetExceeded(
                f"Budget reached: ${spent:.4f} spent of ${self.budget_usd:.2f} this month; "
                f"the next call could cost up to ${worst:.4f}.")
        return est

    def record(self, result: LLMResult) -> float:
        """Write a finished call's real usage to the ledger; return its cost."""
        price = price_per_mtok(result.served_model, self.pricing)
        if price is None:  # served by an unpriced model: charge the worst known price
            price = max(self.pricing.values(), key=lambda p: p[0] + p[1], default=(0.0, 0.0))
        u = result.usage
        cost = cost_usd(price, u.prompt_tokens, u.completion_tokens)
        self.ledger.record(user=self.user, model=result.served_model,
                           prompt_tokens=u.prompt_tokens,
                           completion_tokens=u.completion_tokens, cost=cost, repo=self.repo)
        return cost

    def warning(self) -> str | None:
        """A one-time notice once spend passes warn_ratio of the budget."""
        if self._warned:
            return None
        spent = self.spent()
        if spent < self.warn_ratio * self.budget_usd:
            return None
        self._warned = True
        return (f"Budget warning: ${spent:.4f} of ${self.budget_usd:.2f} used "
                f"this month ({spent / self.budget_usd:.0%}).")
