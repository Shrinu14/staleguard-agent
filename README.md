# StaleGuard Agent

A token-efficient coding agent (Geek Room Global AI Hackathon, Problem 01) with a built-in
API-freshness checker (Problem 05), powered by **Bifrost by Maxim AI** and **Tavily**.

> Work in progress. Full documentation is added as the project is built.

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), [Docker Desktop](https://www.docker.com/),
[Ollama](https://ollama.com) (runs natively so it can use the Mac GPU).

```bash
# 1. Python deps into .venv
uv sync

# 2. Local model
ollama pull qwen2.5-coder:7b

# 3. Config: copy, then fill TAVILY_API_KEY and generate the Bifrost values
cp .env.example .env
#   BIFROST_VK_DEFAULT / BIFROST_VK_LOCAL:  echo "sk-bf-$(openssl rand -hex 24)"
#   BIFROST_SETUP_TOKEN:                    openssl rand -hex 32

# 4. Start the Bifrost gateway (http://localhost:8080)
docker compose up -d

# 5. Check everything
uv run staleguard doctor
uv run staleguard ping
```

## How model calls flow

```
staleguard ──(virtual key)──> Bifrost :8080 ──> Ollama (host, :11434)
                                    └──────────> cloud providers (when keys are added)
```

- Every call goes through Bifrost; the agent never holds provider API keys.
- Two virtual keys: **default** (any provider) and **local-only** (Ollama only, enforced
  by the gateway — used for confidential repos).
- If the primary model fails, Bifrost tries the fallback chain (`MODEL_FALLBACKS`) and the
  CLI announces the switch:

```console
$ uv run staleguard ping -m ollama/no-such-model:1b -f ollama/qwen2.5-coder:7b
⚠ Switched to ollama/qwen2.5-coder:7b — ollama/no-such-model:1b unavailable.
✔ ollama/qwen2.5-coder:7b replied: 'pong'
  tokens: 36 in / 2 out (35 cached)  •  key: default
```

## Safety: reads are free, writes need approval

The model can only *request* a write. `edit_file`, `run_command` and `git` return a
preview (a colored diff, or the exact command) and nothing happens until you answer `y`.
The rule lives in Python (`TOOLS_REQUIRE_APPROVAL`), so no prompt can bypass it.
Secret files such as `.env` can never be read, so they never reach a model.

```bash
uv run staleguard tool read_file '{"path": "agent/llm.py", "start": 1, "end": 20}'
uv run staleguard tool edit_file '{"path": "README.md", "search": "# StaleGuard Agent", "replace": "# StaleGuard"}'
```

## Fewer tokens: repo map instead of whole files

The agent first sees a compact outline of the repo, built with Python's `ast`
(no LLM), then reads only the line ranges it needs:

```text
agent/llm.py
  class LLM  [L86-130]
    def chat(self, messages: Sequence[dict[str, Any]], *, model: str, fallbacks: Iterable[str] | None=None, **params: Any) -> LLMResult  [L102-130]
```

```bash
uv run staleguard map        # print the map + token comparison
```

On this repo the map is ~3k tokens versus ~17k for reading every Python file (82% less,
tiktoken estimate). Long command output (e.g. pytest) keeps the exit code and the last
40 lines.

## Project layout

| Path | Purpose |
|---|---|
| `agent/lookups.py` | Every static constant and mapping, in one place |
| `agent/config.py` | Loads `.env` once; the only place secrets are read |
| `agent/llm.py` | The single LLM client (OpenAI SDK → Bifrost); usage + fallback detection |
| `agent/tools.py` | Repo tools (`repo_map`, `read_file`, `search`, `edit_file`, `run_command`, `git`) and the executor that enforces approval |
| `agent/context.py` | Token savers: `ast` repo map with line ranges, output truncation |
| `agent/approval.py` | The approval gate: write actions are previewed and need a `[y/N]` |
| `agent/cli.py` | Command-line entry point (`doctor`, `ping`, `tool`, `map`) |
| `bifrost/config.json` | Gateway config: providers and virtual keys |
| `docker-compose.yml` | Runs Bifrost |
| `freshness/` | API-freshness checker (Tavily) |

## Development

```bash
uv run ruff check .
uv run pytest
```
