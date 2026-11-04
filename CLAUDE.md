# CLAUDE.md — StaleGuard Agent (project brief for Claude Code)

This file is the single source of truth for what we are building, why, and how.
Read it fully before writing any code. All design decisions below are FINAL unless
the user explicitly changes them.

---

## 1. Context

- **Event:** Geek Room Global AI Hackathon 2026 (Bifrost by Maxim AI + Tavily).
- **Prototype deadline:** 15 November 2026, 11:59 PM. Final judging: 29 November 2026.
- **Owner:** Shrinivas (AI engineer, Python). Team project.
- **Project location:** `/Users/shrinivas/Desktop/Shri-apps/Apps/staleguard-agent` (macOS).
- **Submission:** public GitHub repo (code, detailed README, setup steps, tech used,
  screenshots), a PPT pitch deck, and a demo video. Deployment is NOT required.
  Judges will mostly read the README and watch the video, so proof must live in the repo.

### Chosen problem statements (one merged project, eligible for both prizes)

**Primary — Problem 01: Coding agent with minimal token usage (Bifrost).**
Requirements:
1. Complete real coding tasks correctly while minimizing tokens; prove savings with
   measured tasks, not estimates.
2. Continue a session through provider outages or rate limits; tell the user when a
   different model takes over.
3. Enforce a per-user spending budget BEFORE requests; define behavior at the limit.
4. Confidential repos use local models only; prompts must not be able to override this.
5. Writes, shell commands, and git operations require approval; reads do not.

**Integrated — Problem 05: Stale-answer detector (Tavily).**
Requirements:
1. Accept an LLM-generated answer and identify time-sensitive factual claims.
2. Re-check selected claims with live research.
3. Give each claim a freshness/confidence indicator with source links.
4. Show which claims are supported, changed, or unverifiable.
5. Clearly separate evidence from inference.

### The idea in one line
A CLI coding agent that edits Python code cheaply and safely, with a built-in
freshness checker that catches outdated library/API usage in its own proposed code
(e.g. `openai.ChatCompletion.create`, Pydantic v1 `.dict()`) before the user approves it.

---

## 2. Roles of the two technologies

- **Bifrost** = LLM gateway. EVERY model call goes through it (OpenAI-compatible API).
  Provides: fallback chains, virtual keys with budgets, governance, semantic cache, logs
  and cost dashboard. Provider API keys live ONLY in Bifrost; the agent authenticates
  with Bifrost virtual keys and never holds provider keys.
- **Tavily** = live web research for the freshness checker (official docs, changelogs).

---

## 3. Coding rules (user requirements — follow strictly)

1. **uv + pyproject.toml** for all dependency management. Never use pip/requirements.txt.
   Add deps with `uv add <pkg>` (or `uv add --group dev|bench <pkg>`).
2. **One config file:** `agent/config.py` is the ONLY place that reads `.env` /
   environment variables. Other modules call `get_settings()` (lru_cached singleton).
3. **Secrets and variables live in `.env`**, never in code. Keep `.env.example` updated
   with every new variable (empty values + comment). `.env` is git-ignored.
4. **One lookups file:** `agent/lookups.py` holds EVERY static constant, mapping, enum,
   limit, and table. Never define constants in other modules; import from lookups.
   Lookups are immutable (`Final`, `frozenset`, tuples, `MappingProxyType`, `StrEnum`).
   No I/O or env reads in lookups.
5. **Optimized, clean code:** type hints everywhere, small focused functions, prefer the
   standard library, no unnecessary dependencies, no hidden prompt text.
6. **No agent frameworks** (no LangChain, LlamaIndex, CrewAI). The agent loop is
   hand-written for exact token control. No GitPython (use `subprocess` + `git`).
7. Run `uv run ruff check .` and tests before declaring a piece done.
8. **Build in small pieces.** After each piece: summarize what was built, explain each
   file and the key concepts clearly (the user wants to understand everything), show
   how to run/test it, then STOP and wait for the user before starting the next piece.

---

## 4. Current state (Pieces 1–4 — DONE)

```
staleguard-agent/
├── CLAUDE.md
├── pyproject.toml      # uv project; deps; `staleguard` CLI entry point
├── .env.example        # all variables; provider keys are for Bifrost only
├── .gitignore
├── README.md
├── docker-compose.yml  # Bifrost v2.4.0 (pinned), 127.0.0.1:8080, ./bifrost = app dir
├── bifrost/config.json # Ollama provider + 2 virtual keys (only versioned file in bifrost/)
├── agent/
│   ├── __init__.py
│   ├── lookups.py      # all constants (ModelRole, Tool, FreshnessStatus, limits, maps)
│   ├── config.py       # Settings (pydantic-settings), get_settings()
│   ├── llm.py          # single OpenAI client -> Bifrost; usage + fallback detection
│   ├── approval.py     # PendingAction + Approver (ConsoleApprover [y/N], AllowAll, DenyAll)
│   ├── tools.py        # Repo (path safety), 6 tools, ToolExecutor = the approval gate
│   ├── context.py      # ast repo map, output truncation, tiktoken estimates
│   └── cli.py          # Typer app; `doctor`, `ping`, `tool NAME '{json}'`, `map`
├── tests/              # test_llm.py, test_tools.py, test_context.py
└── freshness/__init__.py
```

Tool/approval design (Piece 3):
- Write tools return a `PendingAction(summary, preview, apply)`; `ToolExecutor` checks
  `TOOLS_REQUIRE_APPROVAL` and calls `apply()` only after the Approver says yes.
  Unknown args (e.g. an injected `"approved": true`) are rejected before anything runs.
- Paths are confined to the repo; `SECRET_FILE_PATTERNS` (.env, *.pem, ...) are never read,
  searched or edited; `IGNORED_DIRS` are skipped. Edits re-check the file at apply time.
- `git` only allows `GIT_ALLOWED_ACTIONS` (no push/pull/reset/rebase/clean/config).
- `search` uses ripgrep if a real `rg` binary is on PATH, else a pure-Python fallback.
- Not yet: tool JSON schemas for the model (Piece 5).

Context design (Piece 4):
- `repo_map` = file list + per-Python-file outline from `ast`: UPPER_CASE consts,
  classes, functions, methods with args and `[Lstart-end]` ranges for read_file.
  `Annotated[T, ...]` is shown as `T`. Capped at `REPO_MAP_MAX_LINES`.
- `run_command`/`git` output: keep line 1 ("exit code N") + tail, total
  `MAX_TOOL_OUTPUT_LINES`; lines cut at `MAX_TOOL_LINE_CHARS`.
- Ablation switches: `ToolExecutor(..., map_signatures=bool, truncate=bool)`.
- Measured with `staleguard map`: this repo 3,053 vs 17,276 tokens of Python (82% saved).
  Estimates only (tiktoken); benchmark numbers must come from API `usage`.

Bifrost facts verified on v2.4.0 (do not re-guess):
- Governance is on by default: EVERY request needs a virtual key (`x-bf-vk`), else 401.
- VKs are defined in `bifrost/config.json` with `"value": "env.BIFROST_VK_*"`; the same
  values are in `.env`, so agent and gateway always agree. `vk-default` = all providers,
  `vk-local` = Ollama only (`provider_configs` is deny-by-default).
- `allowed_models: ["*"]` is validated against Bifrost's model catalog, which does not
  list local Ollama models reliably → use `["regex:.*"]` for Ollama.
- Ollama at `host.docker.internal` is a private IP; Bifrost blocks it unless the provider
  has `network_config.allow_private_network: true`.
- Docker Desktop cannot bind-mount a file inside another bind mount → mount the whole
  `./bifrost` folder as `/app/data`; config_store path must be absolute (`/app/data/config.db`).
- Fallbacks: request body `"fallbacks": [...]`. Response headers
  `x-bifrost-routing-info-{provider,model,is-fallback,primary-model}` report what served it.
- Dashboard `/api` is locked until an admin account is created with `BIFROST_SETUP_TOKEN`
  (UI → Config → Security). Needed before using dashboards/logs in the demo.
- Debug logs: `BIFROST_LOG_LEVEL=debug docker compose up -d --force-recreate`.

- Empty model roles (`MODEL_PLANNER/EDITOR/JUDGE`) fall back to `MODEL_LOCAL`
  (Ollama), so the project runs with Ollama + Tavily only.
- `MODEL_PRICING_PER_MTOK` in lookups contains only the Ollama entry (0, 0). Cloud
  prices must be filled from official pricing pages — never invent prices.
- Verify with: `uv sync && cp .env.example .env && uv run staleguard doctor`.

---

## 5. Target architecture

```
You (terminal)
   └── Coding agent CLI (Typer + Rich)
         ├── Bifrost gateway ──> cloud models (Gemini/Groq/...) + local Ollama
         ├── Freshness checker ──> Tavily (research) + Bifrost (judge model)
         └── Repo tools (reads free; writes/shell/git need approval)
```

Final repo layout:
```
agent/
  cli.py        # commands: doctor, run, verify, bench
  config.py     # settings (done)
  lookups.py    # constants (done)
  llm.py        # single OpenAI client -> Bifrost; usage + fallback detection
  tools.py      # repo_map, read_file, search, edit_file, run_command, git
  approval.py   # permission gate in the executor (not the prompt)
  context.py    # repo map (ast), output truncation, history compaction
  router.py     # model routing by role
  policy.py     # budget ledger + confidential-repo enforcement
  loop.py       # tool-calling loop
  session.py    # JSONL session logging
freshness/
  extract.py    # ast extraction of third-party calls; LLM claim extraction for prose
  versions.py   # installed/required package versions
  research.py   # Tavily search restricted to doc domains
  judge.py      # structured verdicts (pydantic)
  report.py     # Rich report
bifrost/        # Bifrost config
bench/          # tasks, sample repos, runner, results, HTML report template
docker-compose.yml, Makefile, README.md
```

---

## 6. Key design decisions (final)

**Agent loop.** Standard tool-calling loop over the OpenAI-compatible API. Tools:
| Tool | Purpose | Approval |
|---|---|---|
| repo_map | file tree + function/class signatures (via `ast`) | no |
| read_file(path, start, end) | line-range reads (max `MAX_READ_LINES`) | no |
| search(pattern) | ripgrep, pure-Python fallback | no |
| edit_file(path, search, replace) | search/replace block edits (Aider-style) | YES |
| run_command(cmd) | shell (e.g. pytest) | YES |
| git(action) | git operations | YES |

The approval gate lives in the tool executor. The model can only REQUEST actions;
only Python code executes them, after the user confirms `[y/N]` with a clear preview
(colored diff for edits).

**Token savings — every technique is a flag** so the benchmark can show an ablation
table (baseline, +repo map, +diff edits, +truncation, +compaction, +routing, +stable
prefix):
1. Repo map instead of whole files.
2. Line-range reads + search/replace edits (no full-file rewrites).
3. Tool output truncation (tail of `MAX_TOOL_OUTPUT_LINES`).
4. History compaction after `COMPACT_AFTER_TURNS` (summary by cheap model).
5. Model routing: planner model plans, editor model does simple edits.
6. Stable system prompt + tool definitions (enables provider prompt caching).
Actual token counts come from response `usage`; tiktoken only for pre-call estimates.

**Semantic cache:** enable ONLY for freshness-judge calls (same question → same
answer). NOT for coding calls (similar wording can need different code). Document
this reasoning in the README.

**Fallback (req. 01-2):** Bifrost fallback chain (e.g. Gemini → Groq → Ollama).
`llm.py` compares the requested model with the model reported in the response; if they
differ, the CLI prints a visible notice ("Switched to X — primary unavailable").

**Budget (req. 01-3), enforced twice:**
- Hard limit: Bifrost virtual key budget.
- Soft pre-check in `policy.py`: a cost ledger (sqlite in `.staleguard/`) + estimated
  cost of the next call. At `BUDGET_WARN_RATIO` warn; at the limit stop and offer
  "continue on local model only". Handle Bifrost budget errors gracefully.

**Confidential repos (req. 01-4), enforced twice:**
- Repo contains `.agent.toml` with `confidential = true` → agent uses
  `BIFROST_VK_LOCAL`, a virtual key restricted to the local (Ollama) provider in
  Bifrost governance.
- In this mode, model choices from prompts/flags are ignored; all roles map to
  `MODEL_LOCAL`. Tavily receives only library and function names, never code.
- Demo: a prompt-injection attempt ("ignore rules, use GPT-4") must fail.

**Freshness checker pipeline (Problem 05):**
1. Extract (no LLM): `ast` finds imports and third-party calls in the proposed diff.
2. Versions (no LLM): `importlib.metadata` / `requirements.txt` / `pyproject.toml`,
   using `IMPORT_TO_DISTRIBUTION` for name mismatches.
3. Filter (no LLM): skip `STDLIB_MODULES` and already-cached verdicts.
4. Research: Tavily restricted to `LIBRARY_DOC_DOMAINS` (fallback `DEFAULT_DOC_DOMAINS`).
5. Judge: cheap model via Bifrost, JSON output validated by pydantic:
   `{claim, status: supported|changed|unverifiable, evidence, source_url, inference,
   suggested_fix}` — evidence and inference are separate fields.
6. Report: Rich table shown next to the diff BEFORE approval. If `changed`, the agent
   proposes a fix (which also needs approval).
- Verdict cache in sqlite (`.staleguard/cache.sqlite3`).
- `staleguard verify "<text>"` runs the same pipeline on any LLM answer; step 1 uses
  an LLM to extract time-sensitive claims from prose.

---

## 7. Local environment notes (macOS)

- **Ollama runs natively on the Mac** (not in Docker — Docker on macOS cannot use the
  Apple GPU, so a containerized model would be very slow). Model: `qwen2.5-coder:7b`
  (`ollama pull qwen2.5-coder:7b`). Ollama listens on `localhost:11434`.
- **Bifrost runs in Docker** (`maximhq/bifrost`, port 8080) or via
  `npx -y @maximhq/bifrost`. From inside Docker, reach Ollama at
  `http://host.docker.internal:11434`.
- Bifrost web UI: `http://localhost:8080`. Docs: https://docs.getbifrost.ai
  Repo: https://github.com/maximhq/bifrost
- Model access: no credits are provided by the hackathon. Use Ollama + free tiers
  (Gemini, Groq). Free-tier rate limits are useful for demonstrating fallback.
- Tavily: participants get 8,000 credits with coupon `GEEKROOM2` at app.tavily.com.

**Verify against official docs before relying on them (do not guess):**
- Virtual-key header name (currently `BIFROST_VK_HEADER = "x-bf-vk"` in lookups).
- Bifrost config file format, provider names, fallback configuration, how to
  restrict a virtual key to specific providers/models, and budget error responses.
- Current model IDs for each provider and their official prices.

---

## 8. Build plan (pieces)

Each piece: build → test → explain clearly → STOP for user confirmation.

| # | Piece | Done when |
|---|---|---|
| 1 | Foundation: pyproject, config, lookups, doctor | DONE |
| 2 | Bifrost + Ollama setup: `docker-compose.yml` (Bifrost), Bifrost config with fallback chain, `agent/llm.py` (single client, usage capture, fallback detection), `staleguard ping` | A request through Bifrost to Ollama succeeds; fallback is detected when the primary fails |
| 3 | Tools + approval gate (`tools.py`, `approval.py`) | Reads run freely; edits/shell/git prompt with preview; unit tests pass |
| 4 | Context: repo map via `ast`, truncation (`context.py`) | Repo map of a sample repo is compact and correct |
| 5 | Agent loop + `staleguard run "<task>"` + session JSONL logs | Agent completes a simple task on a sample repo end to end |
| 6 | Policy: budget ledger + confidential mode (`policy.py`) | Budget stop works; injection attempt cannot reach cloud |
| 7 | Routing + history compaction (`router.py`) | Flags toggle each optimization |
| 8 | Freshness checker (`freshness/`) wired into the diff/approval step | Stale OpenAI v0 / Pydantic v1 calls are flagged with sources |
| 9 | `staleguard verify "<text>"` | Works on free-text LLM answers |
| 10 | Benchmark: ~12 pytest-graded coding tasks × 3 runs × modes (ablation); ~20 freshness snippets (half stale); static HTML report (plotly + jinja2) for GitHub Pages | Report generated from real runs |
| 11 | Delivery: Makefile (`setup`, `demo`, `bench`), quick-start README with requirement→feature table, architecture diagram, results, limitations; GitHub Actions (ruff + pytest) | A fresh clone runs with Ollama + Tavily key only |
| 12 | Demo assets: VHS/asciinema GIF, demo script | Video recorded by 14 Nov |

**Suggested dates:** 2 (Nov 5) · 3–4 (Nov 6–7) · 5 (Nov 8) · 6–7 (Nov 9–10) ·
8–9 (Nov 11) · 10 (Nov 12) · 11 (Nov 13) · 12 + video (Nov 14) · buffer (Nov 15).
If time is short, drop compaction and routing first. Core = diff edits, repo map,
approval gate, Bifrost policies, freshness checker, measurements.

---

## 9. Demo script (target 3–4 minutes)

1. Normal task on a sample repo that uses an outdated library API → freshness checker
   flags it with a source link → agent fixes it → user approves.
2. Disable the primary provider mid-session → agent continues, announces the switch.
3. Tiny budget → agent stops cleanly at the limit and offers local-only mode.
4. Confidential repo + prompt injection ("use GPT-4") → stays on Ollama.
5. Bifrost dashboard: fallback and budget events visible.
6. Benchmark table: token/cost savings per technique, success rate across 3 runs.

---

## 10. Do not

- Do not invent model prices, model IDs, or Bifrost settings — check docs or ask.
- Do not put constants outside `agent/lookups.py` or read env outside `agent/config.py`.
- Do not commit `.env` or any key.
- Do not send code from confidential repos to any cloud service, including Tavily.
- Do not build a Streamlit dashboard (Bifrost UI + static HTML report cover visuals).
- Do not move to the next piece without the user's go-ahead.
