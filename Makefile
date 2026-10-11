# SPOT dashboard: compose stack + every service's tests. See docs/RUNNING.md.
# Tests run in each service's own venv (create them with `make venvs`).

DB_PORT ?= $(or $(shell sed -n 's/^DB_PORT=\([0-9][0-9]*\).*/\1/p' .env 2>/dev/null),5433)
PY_BACKEND ?= backend/.venv/bin/python
PY_SYSTEM1 ?= amd/system1/.venv/bin/python
PY_MCP     ?= amd/mcp/.venv/bin/python
PY_SYSTEM2 ?= amd/system2/.venv/bin/python
PY_GATEWAY ?= amd/gateway/.venv/bin/python
PY_VISION  ?= amd/vision/.venv/bin/python
PY_ORCH    ?= amd/orchestrator/.venv/bin/python
LOCAL_DB   ?= postgresql://postgres:postgres@localhost:$(DB_PORT)/spot

.PHONY: help up down wipe ps logs test test-policy test-compose test-live-runner test-contract test-backend test-backend-integration test-system1 \
        test-system1-fast test-mcp test-system2 test-gateway test-vision test-vision-fast test-orchestrator test-db test-graph \
        test-chat-db test-chat-contract test-web \
        smoke e2e e2e-serial e2e-parallel venvs

help:
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

up: ## build + start the stack for COMPOSE_PROFILES in .env (default local), wait until healthy
	docker compose up -d --build --wait

down: ## stop the stack (keeps the pgdata and hf-cache volumes)
	docker compose down

wipe: ## stop and delete the database volume (the hf-cache model volume is kept)
	docker compose down -v --remove-orphans

ps:
	docker compose ps -a

logs:
	docker compose logs -f --tail 50

test: test-policy test-compose test-live-runner test-backend test-system1 test-mcp test-system2 test-gateway test-vision test-contract test-orchestrator test-chat-contract test-web ## all unit suites + drift guards + contracts + web tests (DB: test-chat-db, test-graph, test-db)

test-policy: ## policy.py vs db/policy_rules.py vs backend validator vs MCP vendored copy, on all 297 kit events
	$(PY_BACKEND) -m pytest tests/test_policy_consistency.py -q -p no:cacheprovider

test-compose: ## docker-compose.yml vs its header, contract + RUNNING.md port tables; healthchecks, depends_on, BIND_ADDR
	$(PY_BACKEND) -m pytest tests/test_compose_consistency.py -q -p no:cacheprovider

test-live-runner: ## scripts/live_runner.py streaming: drafts to /draft, fallbacks, whole-call deadline (fake servers, ~10 s)
	$(PY_BACKEND) -m pytest tests/test_live_runner_stream.py -q -p no:cacheprovider

test-backend: ## backend unit tests (integration test skipped; see test-backend-integration)
	cd backend && SKIP_INTEGRATION=1 .venv/bin/python -m pytest -q

test-backend-integration: ## backend integration test against the compose db (re-seeds it)
	cd backend && INTEGRATION_DATABASE_URL=$(LOCAL_DB) .venv/bin/python -m pytest -q tests/test_integration.py

test-system1: ## system1 service tests (loads real base Laya on CPU, ~20 s)
	cd amd/system1 && .venv/bin/python -m pytest service/tests -q

test-system1-fast:
	cd amd/system1 && SYSTEM1_SKIP_MODEL=1 .venv/bin/python -m pytest service/tests -q

test-mcp: ## MCP unit tests (integration tests skip without DATABASE_URL + EMBED_URL)
	cd amd/mcp && .venv/bin/python -m pytest -q -p no:cacheprovider

test-system2: ## System 2 contract, grounding, multimodal and pipeline tests (offline, ~45 s)
	cd amd/system2 && .venv/bin/python -m pytest -q -p no:cacheprovider tests

test-gateway: ## gateway unit + e2e tests (real LiteLLM proxy against offline stubs, ~15 s)
	cd amd/gateway && .venv/bin/python -m pytest -q -p no:cacheprovider

test-vision: ## vision/OCR unit tests (offline; kit-image tests skip without SPOTDATA/), ~90 s
	cd amd/vision && .venv/bin/python -m pytest tests -q -p no:cacheprovider

test-vision-fast: ## vision tests without the all-Orbit-screenshots sweep, ~30 s
	cd amd/vision && .venv/bin/python -m pytest tests -q -m "not slow" -p no:cacheprovider

test-contract: ## real vision /v1/vision -> orchestrator V# rows -> System 2 parser + /v1/assess (vision venv, ~5 s)
	$(PY_VISION) -m pytest tests/contract -q -p no:cacheprovider

test-chat-contract: ## chat golden SSE transcripts (real backend turns, mocked Gemini) + shared citation fixtures (backend venv)
	$(PY_BACKEND) -m pytest tests/contract/test_chat_contract.py -q -p no:cacheprovider

test-web: ## web lint + chat tests (incl. the golden SSE replay) + flag-route tests (needs `npm ci` in web/)
	cd web && npm run lint && npm run test:chat && npm run test:flags

test-orchestrator: ## orchestrator graph tests (fakes; contract tests vs real System 2 app + System 1 schemas)
	cd amd/orchestrator && .venv/bin/python -m pytest -q -p no:cacheprovider

test-db: ## seed is non-destructive (audit M5), on a throwaway DB in the running compose Postgres
	DB_PORT=$(DB_PORT) $(PY_BACKEND) -m pytest tests/test_seed_nondestructive.py -q -p no:cacheprovider

test-graph: ## knowledge graph (db/graph_build.py + 009_graph.sql) on a throwaway DB in the running compose Postgres
	DB_PORT=$(DB_PORT) $(PY_BACKEND) -m pytest db/tests -q -p no:cacheprovider

test-chat-db: ## AI Assistance chat (010/011 migrations + backend/app/db/chat.py ChatRepo) on a throwaway DB in the compose Postgres
	DB_PORT=$(DB_PORT) $(PY_BACKEND) -m pytest db/tests/test_chat.py -q -p no:cacheprovider

smoke: ## db smoke test inside the stack: 3 RAG queries + graph queries + role permissions
	docker compose run --rm --no-deps --entrypoint db/job.sh db-init smoke --roles

e2e: ## end-to-end proof against the running stack, both TRIAGE_MODEs
	DB_PORT=$(DB_PORT) $(PY_BACKEND) scripts/e2e.py

e2e-serial:
	DB_PORT=$(DB_PORT) E2E_MODES=serial $(PY_BACKEND) scripts/e2e.py

e2e-parallel:
	DB_PORT=$(DB_PORT) E2E_MODES=parallel $(PY_BACKEND) scripts/e2e.py

venvs: ## create the per-service test venvs (CPU torch for system1)
	test -x $(PY_BACKEND) || (python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt)
	test -x $(PY_MCP) || (python3 -m venv amd/mcp/.venv && amd/mcp/.venv/bin/pip install -r amd/mcp/requirements.txt pytest anyio)
	test -x $(PY_SYSTEM1) || (python3 -m venv amd/system1/.venv && amd/system1/.venv/bin/pip install torch \
	  && amd/system1/.venv/bin/pip install -r amd/system1/service/requirements-dev.txt)
	test -x $(PY_SYSTEM2) || (python3 -m venv amd/system2/.venv && amd/system2/.venv/bin/pip install -r amd/system2/requirements.txt)
	# gateway: the hashed lock is compiled for Python 3.12 (see amd/gateway/README.md for the unhashed fallback)
	test -x $(PY_GATEWAY) || (python3 -m venv amd/gateway/.venv && amd/gateway/.venv/bin/pip install --require-hashes \
	  -r amd/gateway/requirements.txt && amd/gateway/.venv/bin/pip install -r amd/gateway/requirements-dev.txt)
	# vision venv also runs tests/contract (adds langgraph for orchestrator.assess), as in CI
	test -x $(PY_VISION) || (python3 -m venv amd/vision/.venv && amd/vision/.venv/bin/pip install \
	  -r amd/vision/requirements-dev.txt -r tests/contract/requirements.txt)
	test -x $(PY_ORCH) || (python3 -m venv amd/orchestrator/.venv && amd/orchestrator/.venv/bin/pip install \
	  -r amd/orchestrator/requirements-dev.txt)
