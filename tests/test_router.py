"""
Tests for Piece 7: model routing (agent/router.py), history compaction
(agent/context.py + loop) and the stable-prefix switch. Offline (FakeLLM).
"""

from pathlib import Path

import pytest

from agent import context, lookups
from agent.config import Settings
from agent.llm import LLMError
from agent.lookups import EventType, ModelRole, RouteReason, StopReason
from agent.loop import AgentLoop
from agent.policy import ModelPlan
from agent.router import Router
from agent.tools import ToolExecutor
from tests.test_loop import approve, reply

PLANNER, EDITOR, LOCAL = "gemini/planner-test", "groq/editor-test", "ollama/qwen2.5-coder:7b"


class RecordingLLM:
    """Scripted replies; records the model and messages of every call."""

    def __init__(self, *replies) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, list[dict]]] = []

    def chat(self, messages, **kwargs):
        self.calls.append((kwargs["model"], [dict(m) for m in messages]))
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("x = 1\n" * 5)
    return tmp_path


def settings() -> Settings:
    return Settings(_env_file=None, model_local=LOCAL, model_planner=PLANNER,
                    model_editor=EDITOR, model_fallbacks="")


# ── router rules ─────────────────────────────────────────────
@pytest.mark.parametrize("turn, failed, compacted, role, reason", [
    (1, False, False, ModelRole.PLANNER, RouteReason.FIRST_TURN),
    (2, False, False, ModelRole.EDITOR, RouteReason.FOLLOW_THROUGH),
    (3, True, False, ModelRole.PLANNER, RouteReason.RECOVER),
    (4, False, True, ModelRole.PLANNER, RouteReason.AFTER_COMPACTION),
])
def test_router_rules(turn, failed, compacted, role, reason) -> None:
    route = Router(PLANNER, EDITOR).choose(turn=turn, last_tool_failed=failed,
                                           just_compacted=compacted)
    assert (route.role, route.reason) == (role, reason)
    assert route.model == (PLANNER if role is ModelRole.PLANNER else EDITOR)


def test_routing_off_always_planner() -> None:
    r = Router(PLANNER, EDITOR, enabled=False)
    assert r.choose(turn=5, last_tool_failed=False, just_compacted=False).model == PLANNER


def test_from_plan_respects_confidential_mode() -> None:
    local = Router.from_plan(settings(), ModelPlan(LOCAL, (), local_only=True))
    assert local.planner == local.editor == LOCAL
    normal = Router.from_plan(settings(), ModelPlan(PLANNER, (), local_only=False))
    assert (normal.planner, normal.editor) == (PLANNER, EDITOR)


def test_loop_routes_editor_after_first_turn_and_planner_after_failure(repo: Path) -> None:
    llm = RecordingLLM(
        reply(calls=[("read_file", {"path": "a.py"})]),
        reply(calls=[("read_file", {"path": "missing.py"})]),  # fails
        reply(calls=[("read_file", {"path": "a.py"})]),
        reply("done"),
    )
    loop = AgentLoop(llm, ToolExecutor(repo, approve), router=Router(PLANNER, EDITOR),  # type: ignore[arg-type]
                     compaction=False)
    result = loop.run("t")
    assert [m for m, _ in llm.calls] == [PLANNER, EDITOR, PLANNER, EDITOR]
    assert result.stats.roles == {"planner": 2, "editor": 2}


def test_declined_action_routes_back_to_planner(repo: Path) -> None:
    llm = RecordingLLM(
        reply(calls=[("run_command", {"command": "echo hi"})]),
        reply("ok"),
    )
    loop = AgentLoop(llm, ToolExecutor(repo, lambda a: False),  # type: ignore[arg-type]
                     router=Router(PLANNER, EDITOR))
    loop.run("t")
    assert [m for m, _ in llm.calls] == [PLANNER, PLANNER]


# ── compaction: pure functions ───────────────────────────────
def history(turns: int) -> list[dict]:
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Task"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "read_file", "arguments": f'{{"path": "f{i}.py"}}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"})
    return msgs


def test_plan_compaction_threshold_and_cut() -> None:
    assert context.plan_compaction(history(4), after_turns=4, keep_recent=2,
                                   min_old_tokens=0) is None
    plan = context.plan_compaction(history(5), after_turns=4, keep_recent=2, min_old_tokens=0)
    assert plan is not None and plan.old_turns == 3
    assert plan.head == history(5)[:2]
    # recent starts with an assistant turn, so every tool result keeps its call
    assert plan.recent[0]["role"] == "assistant" and len(plan.recent) == 4
    assert plan.recent[1]["tool_call_id"] == plan.recent[0]["tool_calls"][0]["id"]


def test_earlier_summary_is_folded_into_the_next_one() -> None:
    msgs = history(0) + [context.summary_message("- read f0.py")] + history(5)[2:]
    plan = context.plan_compaction(msgs, after_turns=4, keep_recent=2, min_old_tokens=0)
    assert plan is not None and context.is_summary(plan.old[0])
    text = context.render_transcript(plan.old, result_chars=50)
    assert "EARLIER SUMMARY" in text and "- read f0.py" in text and "CALL read_file" in text


# ── compaction in the loop ───────────────────────────────────
def test_loop_compacts_and_keeps_head(repo: Path) -> None:
    turns = [reply(calls=[("read_file", {"path": "a.py"})]) for _ in range(4)]
    summary = reply("- read a.py four times; nothing changed yet")
    llm = RecordingLLM(*turns[:3], summary, turns[3], reply("done"))
    events: list[dict] = []
    loop = AgentLoop(llm, ToolExecutor(repo, approve), router=Router(PLANNER, EDITOR),  # type: ignore[arg-type]
                     compact_after=2, keep_recent=1, compact_min_tokens=0,
                     on_event=events.append)
    result = loop.run("t")

    assert result.reason is StopReason.DONE
    model, sent = llm.calls[3]  # the summary call goes to the cheap editor model
    assert model == EDITOR and sent[0]["content"] == lookups.COMPACT_SYSTEM_PROMPT
    after = llm.calls[4][1]
    assert after[:2] == llm.calls[0][1][:2]            # cacheable head untouched
    assert context.is_summary(after[2]) and "four times" in after[2]["content"]
    assert llm.calls[4][0] == PLANNER                  # re-orient after compaction
    comp = [e for e in events if e["type"] == EventType.COMPACTION]
    assert len(comp) == 1 and comp[0]["turns_summarized"] == 2
    assert result.stats.compactions == 1 and result.stats.turns == 5  # summary isn't a turn


def test_summary_failure_falls_back_to_mechanical(repo: Path) -> None:
    turns = [reply(calls=[("read_file", {"path": "a.py"})]) for _ in range(3)]
    llm = RecordingLLM(*turns, LLMError("down"), reply("done"))
    events: list[dict] = []
    loop = AgentLoop(llm, ToolExecutor(repo, approve), model=LOCAL,  # type: ignore[arg-type]
                     compact_after=2, keep_recent=1, compact_min_tokens=0,
                     on_event=events.append)
    assert loop.run("t").reason is StopReason.DONE
    comp = next(e for e in events if e["type"] == EventType.COMPACTION)
    assert comp["summarized_by"] == "mechanical" and "CALL read_file" in comp["summary"]


def test_compaction_off(repo: Path) -> None:
    turns = [reply(calls=[("read_file", {"path": "a.py"})]) for _ in range(4)]
    llm = RecordingLLM(*turns, reply("done"))
    loop = AgentLoop(llm, ToolExecutor(repo, approve), model=LOCAL,  # type: ignore[arg-type]
                     compaction=False, compact_after=1)
    assert loop.run("t").stats.compactions == 0
    assert len(llm.calls[-1][1]) == 2 + 4 * 2  # nothing removed


# ── stable prefix ────────────────────────────────────────────
def test_unstable_prefix_changes_system_prompt_each_turn(repo: Path) -> None:
    llm = RecordingLLM(reply(calls=[("repo_map", {})]), reply("done"))
    AgentLoop(llm, ToolExecutor(repo, approve), model=LOCAL,  # type: ignore[arg-type]
              stable_prefix=False).run("t")
    first, second = (sent[0]["content"] for _, sent in llm.calls)
    assert first != second and first.startswith(lookups.SYSTEM_PROMPT)


def test_keep_recent_is_capped_at_threshold() -> None:
    plan = context.plan_compaction(history(2), after_turns=1, keep_recent=3, min_old_tokens=0)
    assert plan is not None and plan.old_turns == 1  # never a negative cut


def test_small_histories_are_not_worth_a_summary_call() -> None:
    assert context.plan_compaction(history(5), after_turns=4, keep_recent=2,
                                   min_old_tokens=10_000) is None


def test_summary_that_saves_nothing_is_discarded(repo: Path) -> None:
    turns = [reply(calls=[("read_file", {"path": "a.py"})]) for _ in range(3)]
    llm = RecordingLLM(*turns, reply("word " * 3000), reply("done"))
    events: list[dict] = []
    loop = AgentLoop(llm, ToolExecutor(repo, approve), model=LOCAL,  # type: ignore[arg-type]
                     compact_after=2, keep_recent=1, compact_min_tokens=0,
                     on_event=events.append)
    result = loop.run("t")
    comp = next(e for e in events if e["type"] == EventType.COMPACTION)
    assert comp["skipped"] and result.stats.compactions == 0
    assert len(llm.calls[-1][1]) == 2 + 3 * 2  # history unchanged
