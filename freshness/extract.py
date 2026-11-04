"""
Step 1 of the freshness pipeline: find third-party API uses in Python code.
No LLM — Python's own `ast` parser, so it is exact and free.

How a use is found:
  - imports build an alias table:  import numpy as np      -> np     = numpy
                                    from openai import OpenAI -> OpenAI = openai.OpenAI
  - every maximal name/attribute chain is resolved through it:
        openai.ChatCompletion.create(...)  -> "openai.ChatCompletion.create" (call)
        openai.api_key = "..."             -> "openai.api_key"              (attribute)
  - stdlib modules and the repo's own modules are skipped.
  - names used only as a base class or in a type annotation are skipped:
    `class User(BaseModel)` is not a call whose API can go stale (the stale
    parts are the methods it calls, which are checked separately).
  - method hints (lookups.LIBRARY_METHOD_HINTS): `user.dict()` in a file that
    imports pydantic -> "pydantic.BaseModel.dict" (heuristic: ast can't know
    the type of `user`, so the report marks it as such).

new_api_uses(old, new) returns only what the edit ADDS, so existing code the
edit didn't touch is not re-checked on every change.
"""

import ast
from dataclasses import dataclass

from agent import lookups


@dataclass(frozen=True, slots=True)
class ApiUse:
    library: str      # top-level import name, e.g. "openai"
    symbol: str       # fully qualified, e.g. "openai.ChatCompletion.create"
    line: int
    kind: str         # "call" | "attribute" | "method (heuristic)"


def extract_api_uses(source: str, *, local_modules: frozenset[str] = frozenset()) -> list[ApiUse]:
    """All third-party API uses in `source`, first occurrence of each, in line order."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    aliases = _aliases(tree, local_modules)
    if not aliases:
        return []

    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    inner |= _type_only_nodes(tree)
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    seen: dict[str, ApiUse] = {}

    for node in ast.walk(tree):
        if id(node) in inner or not isinstance(node, ast.Attribute | ast.Name):
            continue
        chain = _dotted(node)
        if chain is None:
            continue
        root, _, rest = chain.partition(".")
        target = aliases.get(root)
        if target is None:
            continue
        symbol = f"{target}.{rest}" if rest else target
        if symbol == target and target.count(".") == 0:
            continue  # a bare module name (`openai` passed around) is not an API use
        kind = "call" if id(node) in call_funcs else "attribute"
        _keep(seen, ApiUse(target.split(".")[0], symbol, node.lineno, kind))

    _method_hints(tree, aliases, seen)
    return sorted(seen.values(), key=lambda u: (u.line, u.symbol))


def new_api_uses(old: str, new: str, *, local_modules: frozenset[str] = frozenset()
                 ) -> list[ApiUse]:
    """API uses in `new` that `old` didn't already have."""
    before = {u.symbol for u in extract_api_uses(old, local_modules=local_modules)}
    return [u for u in extract_api_uses(new, local_modules=local_modules)
            if u.symbol not in before]


# ── helpers ──────────────────────────────────────────────────
def _aliases(tree: ast.Module, local: frozenset[str]) -> dict[str, str]:
    """Local name -> fully qualified third-party name."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if _third_party(a.name, local):
                    if a.asname:
                        out[a.asname] = a.name
                    else:  # `import a.b` binds `a`
                        top = a.name.split(".")[0]
                        out[top] = top
        elif (isinstance(node, ast.ImportFrom) and node.module and node.level == 0
              and _third_party(node.module, local)):
            for a in node.names:
                if a.name != "*":
                    out[a.asname or a.name] = f"{node.module}.{a.name}"
    return out


def _third_party(module: str, local: frozenset[str]) -> bool:
    top = module.split(".")[0]
    return top not in lookups.STDLIB_MODULES and top not in local and top != "__future__"


def _dotted(node: ast.expr) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None  # e.g. f().x or "s".join — no resolvable root
    parts.append(node.id)
    return ".".join(reversed(parts))


def _type_only_nodes(tree: ast.Module) -> set[int]:
    """ids of every node inside class bases and annotations."""
    roots: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            roots += node.bases
        elif isinstance(node, ast.AnnAssign | ast.arg):
            if node.annotation is not None:
                roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns:
            roots.append(node.returns)
    return {id(sub) for root in roots for sub in ast.walk(root)}


def _keep(seen: dict[str, ApiUse], use: ApiUse) -> None:
    if use.symbol not in seen or use.line < seen[use.symbol].line:
        seen[use.symbol] = use


def _method_hints(tree: ast.Module, aliases: dict[str, str], seen: dict[str, ApiUse]) -> None:
    imported = {target.split(".")[0] for target in aliases.values()}
    for library, (cls, methods) in lookups.LIBRARY_METHOD_HINTS.items():
        if library not in imported:
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in methods):
                continue
            receiver = _dotted(node.func.value)
            if receiver is not None and receiver.split(".")[0] in aliases:
                continue  # an imported name: the main pass already resolved it
            _keep(seen, ApiUse(library, f"{library}.{cls}.{node.func.attr}",
                               node.lineno, "method (heuristic)"))
