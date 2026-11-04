"""Tests for agent/context.py: repo map and output truncation."""

import textwrap
from pathlib import Path

from agent import lookups
from agent.approval import PendingAction
from agent.context import build_repo_map, outline_python, truncate_output
from agent.tools import ToolExecutor

SAMPLE = textwrap.dedent('''\
    """Module docstring."""
    import os

    API_URL = "https://example.com"
    TIMEOUT: int = 30
    _private = 1

    class Client(Base, metaclass=Meta):
        def __init__(self, key: str, *, retries: int = 3) -> None:
            def helper():  # nested: not listed
                pass
            self.key = key

        async def fetch(self, path, **params) -> dict:
            return {}

    def main(argv=None):
        return 0
''')


def test_outline_lists_classes_functions_and_ranges() -> None:
    assert outline_python(SAMPLE) == [
        "  consts: API_URL, TIMEOUT",
        "  class Client(Base, metaclass=Meta)  [L8-15]",
        "    def __init__(self, key: str, *, retries: int=3) -> None  [L9-12]",
        "    async def fetch(self, path, **params) -> dict  [L14-15]",
        "  def main(argv=None)  [L17-18]",
    ]


def test_ranges_point_at_real_lines() -> None:
    lines = SAMPLE.splitlines()
    assert lines[13].strip().startswith("async def fetch")  # L14
    assert lines[16].startswith("def main")  # L17


def test_long_signature_is_clipped() -> None:
    args = ", ".join(f"argument_{i}" for i in range(40))
    line = outline_python(f"def f({args}):\n    pass\n")[0]
    assert "…" in line and line.endswith("[L1-2]")
    assert len(line) < lookups.MAP_MAX_SIGNATURE_CHARS + 20


def test_map_handles_bad_python_and_non_python(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("def f(): pass\n")
    (tmp_path / "broken.py").write_text("def (:\n")
    (tmp_path / "README.md").write_text("# hi\n")
    files = sorted(tmp_path.iterdir())
    out = build_repo_map(tmp_path, files).splitlines()
    assert out[0] == lookups.REPO_MAP_LEGEND
    assert out[1:] == [
        "README.md",
        "broken.py", "  (could not parse)",
        "ok.py", "  def f()  [L1-1]",
    ]


def test_map_without_signatures_is_file_list(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("def f(): pass\n")
    out = build_repo_map(tmp_path, [tmp_path / "ok.py"], signatures=False)
    assert out.splitlines()[1:] == ["ok.py"]


def test_map_is_capped(tmp_path: Path) -> None:
    files = []
    for i in range(lookups.REPO_MAP_MAX_LINES + 50):
        f = tmp_path / f"m{i:04}.txt"
        f.write_text("")
        files.append(f)
    out = build_repo_map(tmp_path, files).splitlines()
    assert len(out) <= lookups.REPO_MAP_MAX_LINES + 1
    assert out[-1].startswith("… ") and "more files" in out[-1]


def test_truncate_keeps_head_and_tail() -> None:
    text = "exit code 1\n" + "\n".join(f"line {i}" for i in range(1, 101))
    out = truncate_output(text, max_lines=5, keep_head=1).splitlines()
    assert out == ["exit code 1", "[… 96 lines omitted …]",
                   "line 97", "line 98", "line 99", "line 100"]


def test_truncate_short_text_unchanged() -> None:
    assert truncate_output("a\nb", max_lines=5) == "a\nb"


def test_truncate_clips_huge_lines() -> None:
    out = truncate_output("x" * 1000, max_line_chars=10)
    assert out == "x" * 10 + "… [+990 chars]"


def _always(action: PendingAction) -> bool:
    return True


def test_executor_truncates_command_output_only_when_enabled(tmp_path: Path) -> None:
    cmd = {"command": "seq 1 500"}
    on = ToolExecutor(tmp_path, _always).execute("run_command", cmd).output.splitlines()
    off = ToolExecutor(tmp_path, _always, truncate=False).execute("run_command", cmd)
    assert on[0] == "exit code 0" and on[-1] == "500"
    assert len(on) == lookups.MAX_TOOL_OUTPUT_LINES + 1  # + the "omitted" marker
    assert len(off.output.splitlines()) == 501


def test_executor_map_signatures_switch(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f(): pass\n")
    with_sigs = ToolExecutor(tmp_path, _always).execute("repo_map").output
    without = ToolExecutor(tmp_path, _always, map_signatures=False).execute("repo_map").output
    assert "def f()" in with_sigs and "def f()" not in without


def test_annotated_metadata_is_dropped() -> None:
    src = (
        "def ping(model: Annotated[str | None, typer.Option('--model')] = None,\n"
        "         n: typing.Annotated[int, Field(gt=0)] = 1) -> None:\n"
        "    pass\n"
    )
    assert outline_python(src) == ["  def ping(model: str | None=None, n: int=1) -> None  [L1-3]"]
