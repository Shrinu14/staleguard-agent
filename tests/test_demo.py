"""
Demo assets (Piece 12): the scripted stale edit, and `staleguard tool --freshness`
wiring the same reviewer the agent uses into the approval gate. Offline.
"""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent import cli
from freshness.extract import new_api_uses

ROOT = Path(__file__).resolve().parent.parent
HERO = ROOT / "demo" / "hero_edit.json"


def test_hero_edit_adds_the_two_stale_apis() -> None:
    edit = json.loads(HERO.read_text())
    assert edit["search"] == ""  # creates a new file, so every API use counts as new
    symbols = {u.symbol for u in new_api_uses("", edit["replace"])}
    assert {"openai.ChatCompletion.create", "pydantic.BaseModel.dict"} <= symbols


def test_tool_freshness_runs_the_reviewer_before_asking(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "bench" / "repos" / "stale_api", repo)
    seen: list[str] = []

    class FakeChecker:
        def review(self, action):  # noqa: ANN001, ANN201
            seen.append(action.summary)
            return None

    monkeypatch.setattr(cli, "LLM", lambda *a, **k: object())
    monkeypatch.setattr(cli, "_make_checker", lambda *a, **k: FakeChecker())
    result = CliRunner().invoke(
        cli.app, ["tool", "edit_file", HERO.read_text(), "--repo", str(repo), "--freshness"],
        input="n\n")
    assert seen == ["Create summary.py (+17 −0)"]
    assert "declined" in result.output and not (repo / "summary.py").exists()

    seen.clear()
    CliRunner().invoke(cli.app, ["tool", "edit_file", HERO.read_text(), "--repo", str(repo)],
                       input="n\n")
    assert seen == []  # off by default
