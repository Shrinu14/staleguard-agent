"""
Single source of truth for every static lookup in the project.

Rules for this file:
  1. Constants only: no I/O, no environment reads, no side effects.
  2. Everything is immutable (Final, frozenset, tuple, MappingProxyType),
     so no other module can change a lookup at runtime by accident.
  3. Secrets and per-machine values do NOT belong here; they live in .env
     and are loaded by agent/config.py.

Why importing this everywhere is cheap:
  Python executes a module only on its first import and caches it in
  sys.modules. Every later `from agent import lookups` returns that same
  cached object, so these tables are built exactly once per process.
"""

import sys
from enum import StrEnum
from types import MappingProxyType
from typing import Final

# ─────────────────────────────────────────────────────────────
# Bifrost
# ─────────────────────────────────────────────────────────────
# HTTP header carrying a Bifrost virtual key (verified against
# docs.getbifrost.ai/providers/request-options, Bifrost v2.4.0).
BIFROST_VK_HEADER: Final = "x-bf-vk"

# Response headers Bifrost adds to every inference response
# (docs.getbifrost.ai/providers/response-headers). They tell us which
# provider/model actually answered and whether a fallback served it.
BIFROST_HDR_PROVIDER: Final = "x-bifrost-routing-info-provider"
BIFROST_HDR_MODEL: Final = "x-bifrost-routing-info-model"
BIFROST_HDR_IS_FALLBACK: Final = "x-bifrost-routing-info-is-fallback"
BIFROST_HDR_PRIMARY_MODEL: Final = "x-bifrost-routing-info-primary-model"

# Request-body field listing fallback models, tried in order by Bifrost.
BIFROST_FALLBACKS_FIELD: Final = "fallbacks"

LOCAL_PROVIDER: Final = "ollama"

# LLM client behaviour. Retries are 0 on our side on purpose: Bifrost
# already retries and falls back, so client retries would double the spend.
LLM_TIMEOUT_SECONDS: Final = 120.0
LLM_CLIENT_MAX_RETRIES: Final = 0

# Smallest useful request, used by `staleguard ping`.
PING_PROMPT: Final = "Reply with just the word: pong"


# ─────────────────────────────────────────────────────────────
# Model roles and pricing
# ─────────────────────────────────────────────────────────────
class ModelRole(StrEnum):
    PLANNER = "planner"  # plans multi-step tasks (strongest model)
    EDITOR = "editor"    # executes simple edits (cheap model)
    JUDGE = "judge"      # freshness verdicts (cheap model)
    LOCAL = "local"      # confidential repos (Ollama only)


# USD per 1M tokens as (input, output). Used for the pre-call budget check
# and the benchmark cost columns. Fill cloud prices from each provider's
# official pricing page before benchmarking. `staleguard doctor` warns about
# any configured model missing from this table.
MODEL_PRICING_PER_MTOK: Final = MappingProxyType({
    "ollama/qwen2.5-coder:7b": (0.0, 0.0),
})

BUDGET_WARN_RATIO: Final = 0.8  # warn the user at 80% of their budget

# Pre-call budget check: completion size assumed before a call (we can't know
# it yet). Deliberately generous so the check errs on the safe side.
BUDGET_EST_COMPLETION_TOKENS: Final = 1024

# Bifrost's hard-limit error (docs.getbifrost.ai/features/governance/virtual-keys):
# HTTP 402 with {"type": "budget_exceeded"}.
BIFROST_BUDGET_STATUS: Final = 402
BIFROST_BUDGET_ERROR_TYPE: Final = "budget_exceeded"


# ─────────────────────────────────────────────────────────────
# Agent tools and approval
# ─────────────────────────────────────────────────────────────
class Tool(StrEnum):
    REPO_MAP = "repo_map"
    READ_FILE = "read_file"
    SEARCH = "search"
    EDIT_FILE = "edit_file"
    RUN_COMMAND = "run_command"
    GIT = "git"


TOOLS_AUTO_APPROVED: Final = frozenset({Tool.REPO_MAP, Tool.READ_FILE, Tool.SEARCH})
TOOLS_REQUIRE_APPROVAL: Final = frozenset({Tool.EDIT_FILE, Tool.RUN_COMMAND, Tool.GIT})

# Files the agent may never read or edit: their contents would be sent to
# the model. Matched (fnmatch) against the file name, case-insensitive.
SECRET_FILE_PATTERNS: Final = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", ".netrc", ".pypirc", "credentials*",
)
# Safe templates that match the patterns above but contain no secrets.
SECRET_FILE_EXCEPTIONS: Final = frozenset({".env.example", ".env.sample", ".env.template"})

# git subcommands the agent may request (each still needs approval).
# Deliberately excluded: push, pull, fetch, remote, reset, rebase, clean, config.
GIT_ALLOWED_ACTIONS: Final = frozenset({
    "status", "diff", "log", "show", "add", "commit", "restore", "branch", "switch", "stash",
})

COMMAND_TIMEOUT_SECONDS: Final = 120   # run_command / git hard timeout
FILE_ENCODING: Final = "utf-8"
BINARY_SNIFF_BYTES: Final = 1024       # a NUL byte in this prefix => binary file

# Returned to the model when the user says No, so it changes course
# instead of asking for the same action again.
TOOL_DECLINED_MESSAGE: Final = (
    "The user declined this action. Do not repeat it unchanged; "
    "explain what you wanted to do or try a different approach."
)


# ─────────────────────────────────────────────────────────────
# Context and token-saving limits
# ─────────────────────────────────────────────────────────────
MAX_AGENT_TURNS: Final = 25          # hard stop for the tool-calling loop
AGENT_TEMPERATURE: Final = 0.0       # deterministic runs, comparable benchmarks


# Some local models write tool calls as text. These find JSON inside
# <tool_call>...</tool_call> tags or ```json fences``` (agent/loop.py).
TEXT_TOOL_CALL_TAG_PATTERN: Final = r"<tool_call>\s*(.*?)\s*</tool_call>"
TEXT_TOOL_CALL_FENCE_PATTERN: Final = r"```(?:json)?\s*(.*?)```"


class StopReason(StrEnum):
    DONE = "done"            # model answered without calling a tool
    MAX_TURNS = "max_turns"  # hit MAX_AGENT_TURNS
    BUDGET = "budget"        # the next call would exceed the user's budget
    ERROR = "error"          # the LLM call failed (gateway down, ...)


class EventType(StrEnum):
    """Events the agent loop emits; the CLI prints them, the session log stores them."""
    RUN_START = "run_start"
    LLM_REPLY = "llm_reply"
    MODEL_SWITCH = "model_switch"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    POLICY = "policy"                  # confidential mode, ignored --model, ...
    BUDGET_WARNING = "budget_warning"  # spend crossed BUDGET_WARN_RATIO
    RUN_END = "run_end"
MAX_READ_LINES: Final = 200          # largest line range read_file returns
MAX_TOOL_OUTPUT_LINES: Final = 40    # keep only the tail of long outputs
COMPACT_AFTER_TURNS: Final = 8       # summarize history after this many turns
SEARCH_MAX_RESULTS: Final = 50
MAX_TOOL_LINE_CHARS: Final = 300     # cut single huge lines (minified, base64...)

# Tools whose output can be long and unpredictable, so it is truncated.
# (read_file and search already cap themselves; repo_map has its own cap.)
TRUNCATED_TOOLS: Final = frozenset({Tool.RUN_COMMAND, Tool.GIT})
TRUNCATE_KEEP_HEAD_LINES: Final = 1  # keep the "exit code N" line

# Repo map (agent/context.py)
REPO_MAP_LEGEND: Final = (
    "# Repo map: files; Python files list classes/functions with [line ranges]."
)
REPO_MAP_MAX_LINES: Final = 400      # whole map cap; beyond this, use search
MAP_MAX_SIGNATURE_CHARS: Final = 160  # one signature line, cut with "…"
MAP_MAX_CONSTANTS: Final = 12        # UPPER_CASE names listed per module
PYTHON_SUFFIXES: Final = frozenset({".py", ".pyi"})

# tiktoken encoding for pre-call estimates only. Real counts come from `usage`.
TOKENIZER_ENCODING: Final = "o200k_base"

IGNORED_DIRS: Final = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".staleguard",
})

# ─────────────────────────────────────────────────────────────
# Repo policy and local storage
# ─────────────────────────────────────────────────────────────
POLICY_FILENAME: Final = ".agent.toml"      # per-repo policy (e.g. confidential = true)
POLICY_CONFIDENTIAL_KEY: Final = "confidential"
STATE_DIRNAME: Final = ".staleguard"        # per-repo runtime data folder
CACHE_DB_FILENAME: Final = "cache.sqlite3"  # verdict cache + cost ledger
SESSION_LOG_DIRNAME: Final = "sessions"     # JSONL logs, one file per session
# The spend ledger is per USER, not per repo (a budget must not reset when you
# switch repos), so it lives in the home directory: ~/.staleguard/ledger.sqlite3
USER_STATE_DIRNAME: Final = ".staleguard"
LEDGER_DB_FILENAME: Final = "ledger.sqlite3"
# Written into .staleguard/ so the folder is never committed, without
# touching the user's own .gitignore (same trick as .pytest_cache).
STATE_GITIGNORE_CONTENT: Final = "# created by staleguard\n*\n"


# ─────────────────────────────────────────────────────────────
# Freshness checker
# ─────────────────────────────────────────────────────────────
class FreshnessStatus(StrEnum):
    SUPPORTED = "supported"
    CHANGED = "changed"
    UNVERIFIABLE = "unverifiable"


# Standard-library modules never need a freshness check.
STDLIB_MODULES: Final = frozenset(sys.stdlib_module_names)

# Import name -> PyPI distribution name, where they differ.
# Needed to look up the installed version of an imported package.
IMPORT_TO_DISTRIBUTION: Final = MappingProxyType({
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "dateutil": "python-dateutil",
    "jwt": "PyJWT",
    "google.generativeai": "google-generativeai",
})

# Official documentation domains per library, used to restrict Tavily
# searches to trustworthy sources.
LIBRARY_DOC_DOMAINS: Final = MappingProxyType({
    "openai": ("platform.openai.com", "github.com"),
    "pydantic": ("docs.pydantic.dev",),
    "sqlalchemy": ("docs.sqlalchemy.org",),
    "langchain": ("python.langchain.com",),
    "fastapi": ("fastapi.tiangolo.com",),
    "requests": ("requests.readthedocs.io",),
    "pandas": ("pandas.pydata.org",),
    "numpy": ("numpy.org",),
})

# Fallback domains for libraries not listed above.
DEFAULT_DOC_DOMAINS: Final = ("pypi.org", "github.com", "readthedocs.io")

TAVILY_MAX_RESULTS: Final = 5
TAVILY_SEARCH_DEPTH: Final = "basic"


# ─────────────────────────────────────────────────────────────
# Agent prompt and tool definitions (sent to the model)
# ─────────────────────────────────────────────────────────────
# Both are fixed text, identical on every request: a stable prefix lets
# providers reuse their prompt cache, and nothing is hidden from the user.
# The repo map and the task go in the first user message, not here.
SYSTEM_PROMPT: Final = """\
You are StaleGuard, a careful coding agent working inside a Python repository.

How to work:
- The first message has the task and a repo map with [line ranges]. Use it to decide \
what to read; read only the line ranges you need with read_file.
- Use search to find where something is defined or used.
- Change code with edit_file. `search` must be copied exactly from the file, \
including indentation, and must match exactly one place. Keep edits small.
- After changing code, run the tests with run_command if the repo has tests.
- The user must approve every edit, command and git action. If an action is \
declined, do not repeat it unchanged.
- Call one tool at a time and wait for its result.
- When the task is complete, reply with a short summary of what you changed and \
how you verified it, without calling a tool."""

# OpenAI-style function schemas, as JSON text so the constant stays immutable.
# agent/loop.py parses it once. Names must match the Tool enum.
TOOL_SCHEMAS_JSON: Final = """\
[
  {"type": "function", "function": {
    "name": "repo_map",
    "description": "List the repository files with Python classes/functions and their line ranges.",
    "parameters": {"type": "object", "properties": {}, "required": []}}},
  {"type": "function", "function": {
    "name": "read_file",
    "description": "Read lines start..end (1-based, inclusive) of a file. At most 200 lines per call.",
    "parameters": {"type": "object", "properties": {
      "path": {"type": "string", "description": "Path relative to the repo root."},
      "start": {"type": "integer", "description": "First line, default 1."},
      "end": {"type": "integer", "description": "Last line, default start+199."}},
     "required": ["path"]}}},
  {"type": "function", "function": {
    "name": "search",
    "description": "Regex search across the repository. Returns file:line: text rows.",
    "parameters": {"type": "object", "properties": {
      "pattern": {"type": "string", "description": "Regular expression."},
      "path": {"type": "string", "description": "File or folder to search, default the whole repo."}},
     "required": ["pattern"]}}},
  {"type": "function", "function": {
    "name": "edit_file",
    "description": "Replace one exact block of text in a file. To create a new file, use search=\\"\\" and put the whole content in replace. Needs user approval.",
    "parameters": {"type": "object", "properties": {
      "path": {"type": "string", "description": "Path relative to the repo root."},
      "search": {"type": "string", "description": "Exact existing text to replace; must match exactly once."},
      "replace": {"type": "string", "description": "New text."}},
     "required": ["path", "search", "replace"]}}},
  {"type": "function", "function": {
    "name": "run_command",
    "description": "Run a shell command in the repo root, e.g. python -m pytest -q. Needs user approval.",
    "parameters": {"type": "object", "properties": {
      "command": {"type": "string", "description": "The command line to run."}},
     "required": ["command"]}}},
  {"type": "function", "function": {
    "name": "git",
    "description": "Run a git subcommand (status, diff, log, show, add, commit, restore, branch, switch, stash). Needs user approval.",
    "parameters": {"type": "object", "properties": {
      "action": {"type": "string", "description": "The git subcommand."},
      "args": {"type": "array", "items": {"type": "string"}, "description": "Extra arguments."}},
     "required": ["action"]}}}
]"""
