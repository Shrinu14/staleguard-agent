"""
The agent loop: a hand-written tool-calling loop (no agent framework), so
every token sent is visible and under our control.

One run:
  1. messages = [system prompt, "Task + repo map"]
  2. ask the model (through Bifrost) with the six tool definitions
  3. if it calls tools: execute each through ToolExecutor (writes need the
     user's approval), append the results, go to 2
  4. if it answers in plain text: that is the final answer, stop
  5. hard stop after MAX_AGENT_TURNS

Budget (optional BudgetGuard from agent/policy.py): before every call the
guard estimates its cost and may refuse it -> StopReason.BUDGET. The run
keeps its history, so the CLI can offer "continue on the local model" and
call resume() with a local-only LLM: the same conversation goes on for free.

Some local models (e.g. qwen2.5-coder:7b on Ollama) write a tool call as
JSON text instead of the API's structured `tool_calls` field. We accept that
too, but only when the JSON names one of our tools — and either way the call
goes through the same executor and approval gate.

Everything that happens is emitted as an event (see lookups.EventType). The
CLI prints events; agent/session.py writes them to a JSONL log.
"""

import json
import re
import secrets
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from agent import lookups
from agent.llm import LLM, LLMBudgetError, LLMError, LLMResult
from agent.lookups import EventType, StopReason, Tool
from agent.policy import BudgetExceeded, BudgetGuard
from agent.tools import ToolExecutor

Event = dict[str, Any]
EventSink = Callable[[Event], None]

_TOOL_NAMES = frozenset(t.value for t in Tool)
_TAGGED = re.compile(lookups.TEXT_TOOL_CALL_TAG_PATTERN, re.DOTALL)
_FENCED = re.compile(lookups.TEXT_TOOL_CALL_FENCE_PATTERN, re.DOTALL)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None = the model sent invalid JSON
    raw_arguments: str


@dataclass(slots=True)
class RunStats:
    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    tool_calls: Counter[str] = field(default_factory=Counter)
    text_tool_calls: int = 0  # calls recovered from plain-text replies
    models: Counter[str] = field(default_factory=Counter)  # served model -> replies

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def record(self, result: LLMResult) -> None:
        self.turns += 1
        self.prompt_tokens += result.usage.prompt_tokens
        self.completion_tokens += result.usage.completion_tokens
        self.cached_tokens += result.usage.cached_tokens
        self.models[result.served_model] += 1


@dataclass(frozen=True, slots=True)
class RunResult:
    reason: StopReason
    answer: str
    stats: RunStats


@lru_cache(maxsize=1)
def tool_schemas() -> tuple[dict[str, Any], ...]:
    """The tool definitions sent to the model (parsed once from lookups)."""
    return tuple(json.loads(lookups.TOOL_SCHEMAS_JSON))


class AgentLoop:
    def __init__(
        self,
        llm: LLM,
        executor: ToolExecutor,
        *,
        model: str,
        on_event: EventSink | None = None,
        include_repo_map: bool = True,
        max_turns: int = lookups.MAX_AGENT_TURNS,
        fallbacks: Sequence[str] | None = None,  # None = MODEL_FALLBACKS
        budget: BudgetGuard | None = None,
    ) -> None:
        self.llm = llm
        self.executor = executor
        self.model = model
        self.fallbacks = fallbacks
        self.budget = budget
        self.on_event = on_event or (lambda _event: None)
        self.include_repo_map = include_repo_map
        self.max_turns = max_turns
        # Conversation state, kept so a stopped run can be resumed.
        self._messages: list[dict[str, Any]] = []
        self._stats = RunStats()
        self._last_served: str | None = None

    def run(self, task: str) -> RunResult:
        self._stats = RunStats()
        self._last_served = None
        self._messages = [
            {"role": "system", "content": lookups.SYSTEM_PROMPT},
            {"role": "user", "content": self._first_message(task)},
        ]
        self._emit(EventType.RUN_START, task=task, model=self.model,
                   repo=str(self.executor.repo.root), repo_map=self.include_repo_map)
        return self._drive()

    def resume(self, llm: LLM, *, model: str, fallbacks: Sequence[str] = ()) -> RunResult:
        """Continue a stopped run (same history and stats) on another LLM/model."""
        if not self._messages:
            raise RuntimeError("resume() called before run().")
        self.llm, self.model, self.fallbacks = llm, model, tuple(fallbacks)
        self._emit(EventType.POLICY, notice=f"Continuing on {model} (local only).")
        return self._drive()

    def _drive(self) -> RunResult:
        messages, stats = self._messages, self._stats
        tools = list(tool_schemas())
        while stats.turns < self.max_turns:
            turn = stats.turns + 1
            if self.budget is not None:
                try:
                    self.budget.check(self.model, self._chain(), messages, tools)
                except BudgetExceeded as exc:
                    return self._finish(StopReason.BUDGET, str(exc), stats)
            try:
                result = self.llm.chat(
                    messages, model=self.model, fallbacks=self.fallbacks, tools=tools,
                    temperature=lookups.AGENT_TEMPERATURE,
                )
            except LLMBudgetError as exc:  # Bifrost's hard limit
                return self._finish(StopReason.BUDGET, str(exc), stats)
            except LLMError as exc:
                return self._finish(StopReason.ERROR, str(exc), stats)

            stats.record(result)
            if self.budget is not None:
                self.budget.record(result)
                if warning := self.budget.warning():
                    self._emit(EventType.BUDGET_WARNING, turn=turn, notice=warning)
            if result.served_model != self._last_served and result.switch_notice:
                self._emit(EventType.MODEL_SWITCH, turn=turn, notice=result.switch_notice,
                           requested=result.requested_model, served=result.served_model)
            self._last_served = result.served_model

            calls, from_text = extract_tool_calls(result)
            stats.text_tool_calls += len(calls) if from_text else 0
            self._emit(
                EventType.LLM_REPLY, turn=turn, served_model=result.served_model,
                is_fallback=result.is_fallback, content=result.content,
                tool_calls=[c.name for c in calls], tool_calls_from_text=from_text,
                usage={"prompt": result.usage.prompt_tokens,
                       "completion": result.usage.completion_tokens,
                       "cached": result.usage.cached_tokens},
            )
            if not calls:
                return self._finish(StopReason.DONE, result.content.strip(), stats)

            messages.append(_assistant_message(result, calls, from_text))
            messages += [self._run_tool(call, stats) for call in calls]

        return self._finish(
            StopReason.MAX_TURNS,
            f"Stopped after {self.max_turns} turns without a final answer.",
            stats,
        )

    # ── helpers ──────────────────────────────────────────────
    def _chain(self) -> tuple[str, ...]:
        if self.fallbacks is not None:
            return tuple(self.fallbacks)
        return getattr(getattr(self.llm, "settings", None), "fallbacks", ())

    def _first_message(self, task: str) -> str:
        text = f"Task:\n{task.strip()}"
        if self.include_repo_map:
            text += "\n\n" + self.executor.execute(Tool.REPO_MAP).output
        return text

    def _run_tool(self, call: ToolCall, stats: RunStats) -> dict[str, Any]:
        stats.tool_calls[call.name] += 1
        self._emit(EventType.TOOL_CALL, id=call.id, name=call.name, arguments=call.arguments)
        if call.arguments is None:
            ok, approved = False, None
            output = f"Arguments were not valid JSON: {call.raw_arguments[:200]}"
        else:
            res = self.executor.execute(call.name, call.arguments)
            ok, approved, output = res.ok, res.approved, res.output
        self._emit(EventType.TOOL_RESULT, id=call.id, name=call.name,
                   ok=ok, approved=approved, output=output)
        return {"role": "tool", "tool_call_id": call.id, "content": output}

    def _finish(self, reason: StopReason, answer: str, stats: RunStats) -> RunResult:
        self._emit(EventType.RUN_END, reason=reason, answer=answer, turns=stats.turns,
                   prompt_tokens=stats.prompt_tokens, completion_tokens=stats.completion_tokens,
                   cached_tokens=stats.cached_tokens, tool_calls=dict(stats.tool_calls),
                   text_tool_calls=stats.text_tool_calls, models=dict(stats.models))
        return RunResult(reason, answer, stats)

    def _emit(self, kind: EventType, **data: Any) -> None:
        self.on_event({"type": kind.value, **data})


# ─────────────────────────────────────────────────────────────
# Tool-call extraction
# ─────────────────────────────────────────────────────────────
def extract_tool_calls(result: LLMResult) -> tuple[list[ToolCall], bool]:
    """Structured tool_calls if present, else JSON-in-text calls. (calls, from_text)"""
    native = result.message.tool_calls or []
    if native:
        calls = [
            ToolCall(tc.id, tc.function.name, _parse_args(tc.function.arguments),
                     tc.function.arguments or "")
            for tc in native if tc.type == "function"
        ]
        return calls, False
    calls = parse_text_tool_calls(result.content)
    return calls, bool(calls)


def parse_text_tool_calls(text: str) -> list[ToolCall]:
    """Tool calls written as JSON text instead of structured tool_calls.

    Accepted: <tool_call>-tagged or ```fenced``` JSON anywhere, or bare JSON that
    ENDS the reply ("Let me read it: {...}"). JSON followed by more prose is
    not a call (e.g. an example quoted in a final answer). Every object must
    name a known tool, otherwise nothing is treated as a call.
    """
    blocks = _TAGGED.findall(text) or _FENCED.findall(text)
    if blocks:
        objects: list[Any] = []
        for block in blocks:
            decoded = _decode_all(block)
            if decoded is None:
                return []
            objects += decoded
        return _all_calls(objects)
    # Bare JSON: try each "{" from the left; the first that starts a pure-JSON tail wins.
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        decoded = _decode_all(text[start:])
        if decoded is not None:
            return _all_calls(decoded)
    return []


def _all_calls(objects: list[Any]) -> list[ToolCall]:
    calls = [_to_call(obj) for obj in objects]
    if not calls or any(c is None for c in calls):
        return []
    return [c for c in calls if c is not None]


def _decode_all(text: str) -> list[Any] | None:
    """All JSON values in text separated only by whitespace; None if anything else."""
    decoder, pos, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while pos < len(text):
        try:
            value, end = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            return None
        out.append(value)
        pos = end
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return out


def _to_call(obj: Any) -> ToolCall | None:
    if not isinstance(obj, dict):
        return None
    fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
    name = fn.get("name")
    if name not in _TOOL_NAMES:
        return None
    raw = fn.get("arguments", fn.get("parameters", {}))
    args = _parse_args(raw) if isinstance(raw, str) else raw
    if args is not None and not isinstance(args, dict):
        args = None
    raw_text = raw if isinstance(raw, str) else json.dumps(raw)
    return ToolCall(f"call_{secrets.token_hex(4)}", name, args, raw_text)


def _parse_args(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _assistant_message(
    result: LLMResult, calls: list[ToolCall], from_text: bool
) -> dict[str, Any]:
    """The assistant turn as it goes back into the history."""
    if not from_text:
        return result.message.model_dump(exclude_none=True)
    # Rewrite a JSON-in-text reply as a proper tool-call message.
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": c.raw_arguments}}
            for c in calls
        ],
    }
