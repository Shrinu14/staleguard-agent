"""
Context builders: give the model what it needs in as few tokens as possible.

1. Repo map. Instead of reading whole files to understand a repo, the model
   gets one compact outline built with Python's `ast` module (no LLM, no
   guessing): every file, and for Python files their classes, functions and
   methods with arguments and [line ranges]. The ranges let the model call
   read_file for just the lines it needs.

2. Output truncation. Commands like `pytest` can print thousands of lines.
   The model rarely needs more than the end (the failures and the summary),
   so long outputs keep their first line ("exit code N") and their tail.

3. Token estimates (tiktoken). Used only for previews such as
   `staleguard map`; real token counts always come from the API's `usage`.

Each technique can be switched off so the benchmark can measure its effect.
"""

import ast
import copy
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from agent import lookups

if TYPE_CHECKING:
    import tiktoken


# ─────────────────────────────────────────────────────────────
# Repo map
# ─────────────────────────────────────────────────────────────
def build_repo_map(root: Path, files: Iterable[Path], *, signatures: bool = True) -> str:
    """Outline of the repo, capped at REPO_MAP_MAX_LINES lines."""
    out: list[str] = [lookups.REPO_MAP_LEGEND]
    file_list = list(files)
    for i, path in enumerate(file_list):
        entry = [path.relative_to(root).as_posix()]
        if signatures and path.suffix in lookups.PYTHON_SUFFIXES:
            entry += _outline_file(path)
        if len(out) + len(entry) > lookups.REPO_MAP_MAX_LINES:
            left = len(file_list) - i
            out.append(f"… {left} more files not shown; use search to find code.")
            break
        out += entry
    return "\n".join(out) if file_list else "(empty repository)"


def _outline_file(path: Path) -> list[str]:
    try:
        source = path.read_bytes().decode(lookups.FILE_ENCODING)
        return outline_python(source)
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return ["  (could not parse)"]


def outline_python(source: str) -> list[str]:
    """Top-level classes/functions (and methods), indented under the file."""
    tree = ast.parse(source)
    lines: list[str] = []
    constants: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            lines.append("  " + _function_line(node))
        elif isinstance(node, ast.ClassDef):
            lines.append("  " + _class_line(node))
            lines += [
                "    " + _function_line(child)
                for child in node.body
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
            ]
        else:
            constants += _constant_names(node)
    if constants:
        shown = constants[: lookups.MAP_MAX_CONSTANTS]
        more = len(constants) - len(shown)
        lines.insert(0, "  consts: " + ", ".join(shown) + (f", … (+{more})" if more else ""))
    return lines


def _function_line(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = _StripAnnotated().visit(copy.deepcopy(node.args))
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    text = f"{kind} {node.name}({ast.unparse(args)}){returns}"
    return _clip(text) + _span(node)


class _StripAnnotated(ast.NodeTransformer):
    """Annotated[str, typer.Option(...)] -> str: keep the type, drop metadata."""

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        name = node.value.attr if isinstance(node.value, ast.Attribute) else (
            node.value.id if isinstance(node.value, ast.Name) else None
        )
        if name == "Annotated" and isinstance(node.slice, ast.Tuple) and node.slice.elts:
            return self.visit(node.slice.elts[0])
        return self.generic_visit(node)


def _class_line(node: ast.ClassDef) -> str:
    bases = [ast.unparse(b) for b in node.bases] + [ast.unparse(k) for k in node.keywords]
    text = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
    return _clip(text) + _span(node)


def _constant_names(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    else:
        return []
    return [t.id for t in targets if isinstance(t, ast.Name) and _is_constant_name(t.id)]


def _is_constant_name(name: str) -> bool:
    return name.isupper() and not name.startswith("__")


def _span(node: ast.stmt) -> str:
    return f"  [L{node.lineno}-{node.end_lineno}]"


def _clip(text: str) -> str:
    limit = lookups.MAP_MAX_SIGNATURE_CHARS
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─────────────────────────────────────────────────────────────
# Output truncation
# ─────────────────────────────────────────────────────────────
def truncate_output(
    text: str,
    *,
    max_lines: int = lookups.MAX_TOOL_OUTPUT_LINES,
    max_line_chars: int = lookups.MAX_TOOL_LINE_CHARS,
    keep_head: int = 0,
) -> str:
    """Keep the first `keep_head` lines and the last lines, up to max_lines total."""
    lines = [_clip_line(line, max_line_chars) for line in text.splitlines()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = lines[:keep_head]
    tail = lines[len(lines) - (max_lines - keep_head):]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"[… {omitted} lines omitted …]", *tail])


def _clip_line(line: str, limit: int) -> str:
    return line if len(line) <= limit else f"{line[:limit]}… [+{len(line) - limit} chars]"


# ─────────────────────────────────────────────────────────────
# Token estimates
# ─────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _encoder() -> "tiktoken.Encoding":
    import tiktoken  # imported lazily: slow, and only needed for estimates

    return tiktoken.get_encoding(lookups.TOKENIZER_ENCODING)


def count_tokens(text: str) -> int:
    """Estimated token count (tiktoken). Real counts come from API usage."""
    return len(_encoder().encode(text, disallowed_special=()))
