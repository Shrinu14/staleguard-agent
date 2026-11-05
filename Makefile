# StaleGuard Agent — common tasks. Needs: uv, Docker, Ollama (running natively).
#   make setup    install, pull the model, create .env, start Bifrost, check
#   make demo     freshness check + verify + an agent run you approve step by step
#   make bench    full benchmark (12 tasks x 6 modes x 3 runs + 20 snippets) and report
#   make report   rebuild docs/index.html from RESULTS=bench/results/<dir>
#   make check    lint + tests (what CI runs)

MODEL   ?= qwen2.5-coder:7b
RUNS    ?= 3
RESULTS ?= bench/results/latest
SG      := uv run staleguard

.PHONY: help setup demo bench report lint test check

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

setup:
	uv sync
	ollama pull $(MODEL)
	$(SG) init
	docker compose up -d
	@echo "Waiting for Bifrost on http://127.0.0.1:8080 ..."
	@for i in $$(seq 1 30); do curl -sf -o /dev/null http://127.0.0.1:8080/health && break; sleep 1; done
	$(SG) doctor
	$(SG) ping

demo:
	@echo "== 1. Freshness check: stale OpenAI v0 / Pydantic v1 code (read only) =="
	-$(SG) check chat.py --repo bench/repos/stale_api
	@echo "== 2. Verify a chatbot answer that contains stale claims =="
	-$(SG) verify --file bench/answers/stale_python_answer.md
	@echo "== 3. The agent fixes a bug in a temp copy of mini_calc; you approve each write =="
	@d=$$(mktemp -d -t staleguard-demo) && cp -R bench/repos/mini_calc/. "$$d" && \
		echo "repo copy: $$d" && \
		$(SG) run "test_mean fails. Fix the bug in calc.py and run python3 -m unittest -q" --repo "$$d"

bench:
	mkdir -p $(RESULTS)
	$(SG) bench coding --runs $(RUNS) --out $(RESULTS)
	$(SG) bench freshness --out $(RESULTS)
	$(SG) bench report $(RESULTS)

report:
	$(SG) bench report $(RESULTS)

lint:
	uv run ruff check .

test:
	uv run pytest -q

check: lint test
