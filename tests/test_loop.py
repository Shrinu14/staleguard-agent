"""
Tests for agent/loop.py and agent/session.py.
A scripted FakeLLM replaces Bifrost, so these run offline and deterministically.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from openai.types.chat import ChatCompletionMessage

from agent import lookups
from agent.approval import PendingAction
from agent.llm import LLMError, LLMResult, Usage
from agent.lookups import EventType, StopReason, Tool
from agent.loop import AgentLoop, parse_text_tool_calls, tool_schemas
from agent.session import SessionLog
from agent.tools import ToolExecutor

MODEL = "ollama/qwen2.5-coder:7b"


def reply(content: str = "", calls: list[tuple[str, dict]] | None = None,
          served: str = MODEL, fallback: bool = False) -> LLMResult:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [
            {"id": f"c{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(calls)
        ]
    return LLMResult(
        message=ChatCompletionMessage.model_validate(msg),
        requested_model=MODEL, served_model=served, is_fallback=fallback,
        usage=Usage(prompt_tokens=100, completion_tokens=10, total_tokens=110, cached_tokens=50),
    )


class FakeLLM:
    """Returns scripted replies in order and records what it was sent."""

    def __init__(self, *replies: LLMResult | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []

    def chat(self, messages, **kwargs) -> LLMResult:
        self.requests.append([dict(m) for m in messages])
        nxt = self.replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def approve(action: PendingAction) -> bool:
    return True


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text("def mean(v):\n    return sum(v) / (len(v) - 1)\n")
    return tmp_path


def make_loop(repo: Path, llm: FakeLLM, **kw) -> tuple[AgentLoop, list[dict]]:
    events: list[dict] = []
    loop = AgentLoop(llm, ToolExecutor(repo, approve), model=MODEL,  # type: ignore[arg-type]
                     on_event=events.append, **kw)
    return loop, events


def test_schemas_match_tools() -> None:
    assert {s["function"]["name"] for s in tool_schemas()} == {t.value for t in Tool}


def test_full_run_with_native_tool_calls(repo: Path) -> None:
    llm = FakeLLM(
        reply(calls=[("read_file", {"path": "calc.py"})]),
        reply(calls=[("edit_file", {"path": "calc.py", "search": "(len(v) - 1)",
                                    "replace": "len(v)"})]),
        reply("Fixed mean() to divide by len(v)."),
    )
    loop, events = make_loop(repo, llm)
    result = loop.run("Fix mean")

    assert result.reason is StopReason.DONE
    assert result.answer == "Fixed mean() to divide by len(v)."
    assert "sum(v) / len(v)" in (repo / "calc.py").read_text()
    assert result.stats.turns == 3 and result.stats.prompt_tokens == 300
    assert result.stats.tool_calls == {"read_file": 1, "edit_file": 1}

    first = llm.requests[0]
    assert first[0] == {"role": "system", "content": lookups.SYSTEM_PROMPT}
    assert first[1]["content"].startswith("Task:\nFix mean")
    assert lookups.REPO_MAP_LEGEND in first[1]["content"]
    # Tool results are fed back with the matching call id.
    tool_msgs = [m for m in llm.requests[2] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c0", "c0"]

    kinds = [e["type"] for e in events]
    assert kinds[0] == EventType.RUN_START and kinds[-1] == EventType.RUN_END
    assert kinds.count(EventType.TOOL_CALL) == 2


def test_system_prompt_is_identical_every_turn(repo: Path) -> None:
    llm = FakeLLM(reply(calls=[("repo_map", {})]), reply("done"))
    make_loop(repo, llm)[0].run("look around")
    assert llm.requests[0][0] == llm.requests[1][0]  # stable prefix -> prompt caching


def test_text_tool_calls_are_recovered(repo: Path) -> None:
    text_call = reply('{"name": "read_file", "arguments": {"path": "calc.py", "end": 1}}')
    llm = FakeLLM(text_call, reply("ok"))
    loop, events = make_loop(repo, llm)
    result = loop.run("read it")

    assert result.reason is StopReason.DONE and result.stats.text_tool_calls == 1
    assistant = llm.requests[1][2]
    assert assistant["role"] == "assistant" and assistant["content"] == ""
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    tool_out = llm.requests[1][3]
    assert tool_out["tool_call_id"] == assistant["tool_calls"][0]["id"]
    assert "def mean" in tool_out["content"]


def test_bad_json_arguments_reported_to_model(repo: Path) -> None:
    bad = reply()
    bad.message.tool_calls = ChatCompletionMessage.model_validate({
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "x", "type": "function",
                        "function": {"name": "read_file", "arguments": "{oops"}}],
    }).tool_calls
    llm = FakeLLM(bad, reply("gave up"))
    result = make_loop(repo, llm)[0].run("t")
    assert result.reason is StopReason.DONE
    assert "not valid JSON" in llm.requests[1][-1]["content"]


def test_declined_edit_is_told_to_model(repo: Path) -> None:
    llm = FakeLLM(
        reply(calls=[("edit_file", {"path": "calc.py", "search": "- 1", "replace": ""})]),
        reply("ok, leaving it"),
    )
    events: list[dict] = []
    loop = AgentLoop(llm, ToolExecutor(repo, lambda a: False), model=MODEL,  # type: ignore[arg-type]
                     on_event=events.append)
    loop.run("t")
    assert "- 1" in (repo / "calc.py").read_text()
    assert llm.requests[1][-1]["content"] == lookups.TOOL_DECLINED_MESSAGE


def test_max_turns(repo: Path) -> None:
    llm = FakeLLM(*[reply(calls=[("repo_map", {})]) for _ in range(3)])
    result = make_loop(repo, llm, max_turns=3)[0].run("loop forever")
    assert result.reason is StopReason.MAX_TURNS and result.stats.turns == 3


def test_llm_error_stops_cleanly(repo: Path) -> None:
    llm = FakeLLM(LLMError("Cannot reach Bifrost"))
    result = make_loop(repo, llm)[0].run("t")
    assert result.reason is StopReason.ERROR and "Cannot reach" in result.answer


def test_model_switch_announced_once(repo: Path) -> None:
    other = "groq/llama-test"
    llm = FakeLLM(
        reply(calls=[("repo_map", {})], served=other, fallback=True),
        reply("done", served=other, fallback=True),
    )
    loop, events = make_loop(repo, llm)
    loop.run("t")
    switches = [e for e in events if e["type"] == EventType.MODEL_SWITCH]
    assert len(switches) == 1 and switches[0]["served"] == other


def test_repo_map_can_be_switched_off(repo: Path) -> None:
    llm = FakeLLM(reply("done"))
    make_loop(repo, llm, include_repo_map=False)[0].run("t")
    assert llm.requests[0][1]["content"] == "Task:\nt"


@pytest.mark.parametrize("text, expected", [
    ('{"name": "search", "arguments": {"pattern": "x"}}', ["search"]),
    ('<tool_call>{"name": "repo_map", "arguments": {}}</tool_call>', ["repo_map"]),
    ('Reading:\n```json\n{"name": "read_file", "arguments": "{\\"path\\": \\"a\\"}"}\n```',
     ["read_file"]),
    ('{"name": "search", "arguments": {}}\n{"name": "repo_map", "arguments": {}}',
     ["search", "repo_map"]),
    ('{"name": "rm_rf", "arguments": {}}', []),                # unknown tool
    ('Let me read it.\n{"name": "read_file", "arguments": {"path": "calc.py"}}',
     ["read_file"]),                                            # prose, then call at the end
    ('Example: {"name": "search", "arguments": {}} is how to search.', []),  # JSON mid-prose
    ("All done, tests pass.", []),
    ('{"result": 4}', []),
])
def test_parse_text_tool_calls(text: str, expected: list[str]) -> None:
    assert [c.name for c in parse_text_tool_calls(text)] == expected


def test_session_log_is_jsonl_and_self_ignored(tmp_path: Path) -> None:
    log = SessionLog(tmp_path)
    log.write({"type": "run_start", "task": "x"})
    log.write({"type": "run_end", "reason": StopReason.DONE})
    lines = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [r["type"] for r in lines] == ["run_start", "run_end"]
    assert lines[1]["reason"] == "done" and "ts" in lines[0]
    state = tmp_path / lookups.STATE_DIRNAME
    assert (state / ".gitignore").read_text() == lookups.STATE_GITIGNORE_CONTENT
