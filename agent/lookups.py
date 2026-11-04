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


# ─────────────────────────────────────────────────────────────
# Context and token-saving limits
# ─────────────────────────────────────────────────────────────
MAX_AGENT_TURNS: Final = 25          # hard stop for the tool-calling loop
MAX_READ_LINES: Final = 200          # largest line range read_file returns
MAX_TOOL_OUTPUT_LINES: Final = 40    # keep only the tail of long outputs
COMPACT_AFTER_TURNS: Final = 8       # summarize history after this many turns
SEARCH_MAX_RESULTS: Final = 50

IGNORED_DIRS: Final = frozenset({
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".staleguard",
})

# ─────────────────────────────────────────────────────────────
# Repo policy and local storage
# ─────────────────────────────────────────────────────────────
POLICY_FILENAME: Final = ".agent.toml"      # per-repo policy (e.g. confidential = true)
STATE_DIRNAME: Final = ".staleguard"        # per-repo runtime data folder
CACHE_DB_FILENAME: Final = "cache.sqlite3"  # verdict cache + cost ledger
SESSION_LOG_DIRNAME: Final = "sessions"     # JSONL logs, one file per session


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
