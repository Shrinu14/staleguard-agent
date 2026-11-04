"""
Repo tools the model can call, and the executor that enforces approval.

Two kinds of tools:
  read  (repo_map, read_file, search)        -> return text, run freely
  write (edit_file, run_command, git)        -> return a PendingAction only;
                                                nothing changes until approved

ToolExecutor.execute(name, arguments) is the single entry point. Whether a
tool needs approval is decided by lookups.TOOLS_REQUIRE_APPROVAL — fixed
Python data — never by anything the model says.

Safety rails that apply to every tool:
  - paths are confined to the repo root (no "../", no absolute escapes)
  - secret files (.env, *.pem, ...) can't be read, searched or edited,
    so their contents never reach the model
  - an edit re-checks the file before writing: if it changed after the
    preview was shown, the edit is refused (you approved a different diff)
"""

import difflib
import fnmatch
import inspect
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType, NoneType, UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from agent import lookups
from agent.approval import Approver, PendingAction
from agent.context import build_repo_map, truncate_output
from agent.lookups import Tool


class ToolError(Exception):
    """A tool could not do what was asked; the message goes back to the model."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool: str
    ok: bool
    output: str  # text returned to the model
    approved: bool | None = None  # None = tool needed no approval


# ─────────────────────────────────────────────────────────────
# Repo: path safety
# ─────────────────────────────────────────────────────────────
def is_secret(path: Path) -> bool:
    name = path.name.lower()
    if name in lookups.SECRET_FILE_EXCEPTIONS:
        return False
    return any(fnmatch.fnmatch(name, pat) for pat in lookups.SECRET_FILE_PATTERNS)


def is_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        return b"\0" in fh.read(lookups.BINARY_SNIFF_BYTES)


class Repo:
    """The directory the agent works in. Every path goes through resolve()."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ToolError(f"Repo root is not a directory: {self.root}")

    def resolve(self, rel: str, *, must_exist: bool = True) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise ToolError(f"Path '{rel}' is outside the repository.")
        if any(part in lookups.IGNORED_DIRS for part in path.relative_to(self.root).parts):
            raise ToolError(f"Path '{rel}' is in an ignored directory.")
        if is_secret(path):
            raise ToolError(f"'{rel}' looks like a secrets file; the agent may not access it.")
        if must_exist and not path.exists():
            raise ToolError(f"'{rel}' does not exist.")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def iter_files(self, start: Path | None = None) -> Iterator[Path]:
        """All non-ignored, non-secret files under start, sorted."""
        base = start or self.root
        if base.is_file():
            yield base
            return
        for entry in sorted(base.iterdir()):
            if entry.name in lookups.IGNORED_DIRS or entry.is_symlink():
                continue
            if entry.is_dir():
                yield from self.iter_files(entry)
            elif not is_secret(entry):
                yield entry


# ─────────────────────────────────────────────────────────────
# Read tools (no approval)
# ─────────────────────────────────────────────────────────────
def repo_map(repo: Repo) -> str:
    """Files plus Python classes/functions with line ranges (see context.py)."""
    return build_repo_map(repo.root, repo.iter_files(), signatures=True)


def repo_file_list(repo: Repo) -> str:
    """Plain file list: the repo_map tool when signatures are switched off."""
    return build_repo_map(repo.root, repo.iter_files(), signatures=False)


def read_file(repo: Repo, path: str, start: int = 1, end: int | None = None) -> str:
    """Return lines start..end (1-based, inclusive), at most MAX_READ_LINES."""
    target = repo.resolve(path)
    if not target.is_file():
        raise ToolError(f"'{path}' is not a file.")
    lines = _read_text(target, path).splitlines()
    total = len(lines)
    if start < 1 or (end is not None and end < start):
        raise ToolError("need 1 <= start <= end.")
    last = min(end or total, total, start + lookups.MAX_READ_LINES - 1)
    if total == 0:
        return f"{path} (empty file)"
    if start > total:
        raise ToolError(f"start={start} is past the end of '{path}' ({total} lines).")
    body = "\n".join(f"{n:>5}| {lines[n - 1]}" for n in range(start, last + 1))
    header = f"{path} (lines {start}-{last} of {total})"
    if last < total:
        body += f"\n… {total - last} more lines; call again with start={last + 1}"
    return f"{header}\n{body}"


def search(repo: Repo, pattern: str, path: str = ".") -> str:
    """Regex search; returns 'file:line: text' rows, at most SEARCH_MAX_RESULTS."""
    if not pattern:
        raise ToolError("pattern must not be empty.")
    target = repo.resolve(path)
    rows = _search_rg(repo, pattern, target) if shutil.which("rg") else None
    if rows is None:  # ripgrep missing: pure-Python fallback
        rows = _search_python(repo, pattern, target)
    if not rows:
        return f"No matches for /{pattern}/."
    shown = rows[: lookups.SEARCH_MAX_RESULTS]
    extra = len(rows) - len(shown)
    tail = f"\n… {extra} more matches; narrow the pattern or path" if extra > 0 else ""
    return "\n".join(shown) + tail


def _search_rg(repo: Repo, pattern: str, target: Path) -> list[str]:
    cmd = ["rg", "--line-number", "--no-heading", "--color=never", "--max-columns=300"]
    for d in sorted(lookups.IGNORED_DIRS):
        cmd += ["--glob", f"!{d}/"]
    cmd += ["-e", pattern]
    if target != repo.root:  # given ".", rg would print "./path"; omit it instead
        cmd += ["--", repo.rel(target)]
    proc = subprocess.run(
        cmd, cwd=repo.root, capture_output=True, text=True,
        timeout=lookups.COMMAND_TIMEOUT_SECONDS, check=False,
    )
    if proc.returncode == 2:  # 1 = no matches, 2 = error (e.g. bad regex)
        raise ToolError(f"search failed: {proc.stderr.strip()}")
    # rg skips hidden files, but drop any secret file it still matched.
    return [
        row for row in proc.stdout.splitlines()
        if not is_secret(Path(row.split(":", 1)[0]))
    ]


def _search_python(repo: Repo, pattern: str, target: Path) -> list[str]:
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regex: {exc}") from exc
    rows: list[str] = []
    for file in repo.iter_files(target):
        if is_binary(file):
            continue
        text = file.read_bytes().decode(lookups.FILE_ENCODING, errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                rows.append(f"{repo.rel(file)}:{n}:{line}")
                if len(rows) > lookups.SEARCH_MAX_RESULTS:
                    return rows
    return rows


# ─────────────────────────────────────────────────────────────
# Write tools (return PendingAction; executor asks before apply)
# ─────────────────────────────────────────────────────────────
def edit_file(repo: Repo, path: str, search: str, replace: str) -> PendingAction:
    """Replace exactly one occurrence of `search` with `replace`.

    To create a new file, pass search="" and the full content as replace.
    """
    target = repo.resolve(path, must_exist=False)
    exists = target.is_file()
    old = _read_text(target, path) if exists else ""
    if exists:
        if not search:
            raise ToolError(f"'{path}' exists; search must quote the text to replace.")
        count = old.count(search)
        if count == 0:
            raise ToolError(
                f"search text not found in '{path}'. Read the file and copy the exact "
                "lines, including indentation."
            )
        if count > 1:
            raise ToolError(
                f"search text matches {count} places in '{path}'; include more "
                "surrounding lines so it matches exactly one."
            )
        new = old.replace(search, replace, 1)
    else:
        if search:
            raise ToolError(f"'{path}' does not exist; to create it, pass search=\"\".")
        new = replace
    if new == old:
        raise ToolError("this edit would not change the file.")

    rel = repo.rel(target)
    diff_lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="/dev/null" if not exists else f"a/{rel}", tofile=f"b/{rel}",
    ))
    added = sum(1 for d in diff_lines if d.startswith("+") and not d.startswith("+++"))
    removed = sum(1 for d in diff_lines if d.startswith("-") and not d.startswith("---"))
    verb = "Edit" if exists else "Create"

    def apply() -> str:
        current = _read_text(target, rel) if target.is_file() else None
        if current != (old if exists else None):
            raise ToolError(f"'{rel}' changed after the preview; re-read it and retry.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(new.encode(lookups.FILE_ENCODING))
        done = "Edited" if exists else "Created"
        return f"{done} {rel} (+{added} −{removed})."

    return PendingAction(
        tool=Tool.EDIT_FILE,
        summary=f"{verb} {rel} (+{added} −{removed})",
        preview="".join(_ensure_newlines(diff_lines)),
        apply=apply,
        preview_lexer="diff",
    )


def run_command(repo: Repo, command: str) -> PendingAction:
    """Run a shell command in the repo root (e.g. `uv run pytest -q`)."""
    if not command.strip():
        raise ToolError("command must not be empty.")

    def apply() -> str:
        return _run(command, repo.root, shell=True)

    return PendingAction(
        tool=Tool.RUN_COMMAND,
        summary=f"Run: {command}",
        preview=f"$ {command}\n# in {repo.root}",
        apply=apply,
        preview_lexer="bash",
    )


def git(repo: Repo, action: str, args: list[str] | None = None) -> PendingAction:
    """Run an allow-listed git subcommand, e.g. action="commit", args=["-m", "msg"]."""
    if action not in lookups.GIT_ALLOWED_ACTIONS:
        allowed = ", ".join(sorted(lookups.GIT_ALLOWED_ACTIONS))
        raise ToolError(f"git '{action}' is not allowed. Allowed: {allowed}.")
    argv = ["git", "--no-pager", action, *(str(a) for a in (args or []))]

    def apply() -> str:
        return _run(argv, repo.root, shell=False)

    shown = shlex.join(["git", action, *argv[3:]])
    return PendingAction(
        tool=Tool.GIT,
        summary=shown,
        preview=f"$ {shown}\n# in {repo.root}",
        apply=apply,
        preview_lexer="bash",
    )


def _read_text(path: Path, shown: str) -> str:
    """Exact file text (line endings preserved); clear error if not text."""
    if is_binary(path):
        raise ToolError(f"'{shown}' is a binary file.")
    try:
        return path.read_bytes().decode(lookups.FILE_ENCODING)
    except UnicodeDecodeError as exc:
        raise ToolError(f"'{shown}' is not valid {lookups.FILE_ENCODING} text.") from exc


def _run(cmd: str | list[str], cwd: Path, *, shell: bool) -> str:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=shell, capture_output=True, text=True,
            timeout=lookups.COMMAND_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"timed out after {exc.timeout:.0f}s.") from exc
    output = (proc.stdout + proc.stderr).rstrip()
    return f"exit code {proc.returncode}\n{output}" if output else f"exit code {proc.returncode}"


def _ensure_newlines(lines: list[str]) -> Iterator[str]:
    for line in lines:
        yield line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"


# ─────────────────────────────────────────────────────────────
# Argument coercion: small models send "4" for 4, or "--short" for ["--short"]
# ─────────────────────────────────────────────────────────────
def coerce_arguments(handler: Callable[..., Any], args: dict[str, Any]) -> dict[str, Any]:
    """Convert loosely-typed model arguments to the handler's annotated types."""
    hints = get_type_hints(handler)
    return {name: _coerce(name, value, hints.get(name, Any)) for name, value in args.items()}


def _coerce(name: str, value: Any, expected: Any) -> Any:
    options = [t for t in get_args(expected) if t is not NoneType] if (
        get_origin(expected) in (Union, UnionType)
    ) else [expected]
    if value is None and len(options) < len(get_args(expected) or [expected]):
        return None  # Optional parameter
    target = options[0] if len(options) == 1 else Any
    if target is int:
        if isinstance(value, bool):
            raise ToolError(f"'{name}' must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise ToolError(f"'{name}' must be an integer, got {value!r}")
    if target is str:
        if isinstance(value, str):
            return value
        if isinstance(value, int | float) and not isinstance(value, bool):
            return str(value)
        raise ToolError(f"'{name}' must be a string")
    if get_origin(target) is list:
        if isinstance(value, str):
            return shlex.split(value)
        if isinstance(value, list):
            return [str(v) for v in value]
        raise ToolError(f"'{name}' must be a list of strings")
    return value


# ─────────────────────────────────────────────────────────────
# Executor: the gate
# ─────────────────────────────────────────────────────────────
HANDLERS: MappingProxyType[Tool, Callable[..., Any]] = MappingProxyType({
    Tool.REPO_MAP: repo_map,
    Tool.READ_FILE: read_file,
    Tool.SEARCH: search,
    Tool.EDIT_FILE: edit_file,
    Tool.RUN_COMMAND: run_command,
    Tool.GIT: git,
})


class ToolExecutor:
    """Runs model-requested tool calls; write tools only after approval."""

    def __init__(
        self,
        repo_root: Path | str,
        approver: Approver,
        *,
        map_signatures: bool = True,
        truncate: bool = True,
    ) -> None:
        """map_signatures / truncate are token-saving switches (for benchmarks)."""
        self.repo = Repo(repo_root)
        self.approver = approver
        self.truncate = truncate
        self._handlers = dict(HANDLERS)
        if not map_signatures:
            self._handlers[Tool.REPO_MAP] = repo_file_list

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        try:
            tool = Tool(name)
        except ValueError:
            known = ", ".join(t.value for t in Tool)
            return ToolResult(name, False, f"Unknown tool '{name}'. Available: {known}.")

        handler = self._handlers[tool]
        args = arguments or {}
        try:
            inspect.signature(handler).bind(self.repo, **args)
            args = coerce_arguments(handler, args)
        except (TypeError, ToolError) as exc:
            return ToolResult(tool, False, f"Bad arguments for {tool}: {exc}.")

        try:
            outcome = handler(self.repo, **args)
        except ToolError as exc:
            return ToolResult(tool, False, str(exc))
        except Exception as exc:  # a tool bug must not crash the whole run
            return ToolResult(tool, False, f"{tool} failed: {type(exc).__name__}: {exc}")

        if tool not in lookups.TOOLS_REQUIRE_APPROVAL:
            return ToolResult(tool, True, str(outcome))

        # Write tool: outcome is a PendingAction; nothing has happened yet.
        if not isinstance(outcome, PendingAction):
            raise TypeError(f"{tool} must return a PendingAction")
        if not self.approver(outcome):
            return ToolResult(tool, False, lookups.TOOL_DECLINED_MESSAGE, approved=False)
        try:
            output = outcome.apply()
        except ToolError as exc:
            return ToolResult(tool, False, str(exc), approved=True)
        except Exception as exc:
            msg = f"{tool} failed: {type(exc).__name__}: {exc}"
            return ToolResult(tool, False, msg, approved=True)
        if self.truncate and tool in lookups.TRUNCATED_TOOLS:
            output = truncate_output(output, keep_head=lookups.TRUNCATE_KEEP_HEAD_LINES)
        return ToolResult(tool, True, output, approved=True)
