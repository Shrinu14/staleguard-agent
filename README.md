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

## Project layout

| Path | Purpose |
|---|---|
| `agent/lookups.py` | Every static constant and mapping, in one place |
| `agent/config.py` | Loads `.env` once; the only place secrets are read |
| `agent/llm.py` | The single LLM client (OpenAI SDK → Bifrost); usage + fallback detection |
| `agent/cli.py` | Command-line entry point (`doctor`, `ping`) |
| `bifrost/config.json` | Gateway config: providers and virtual keys |
| `docker-compose.yml` | Runs Bifrost |
| `freshness/` | API-freshness checker (Tavily) |

## Development

```bash
uv run ruff check .
uv run pytest
```
