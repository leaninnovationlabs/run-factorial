# The repository's commands as runnable targets.

.DEFAULT_GOAL := help
.PHONY: help sync dev db migrate image up down test check e2b-template api worker terminal assistant ui design-system design-system-update stack-ready

help:               ## list targets
	@grep -E '^[a-z0-9-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

sync:               ## install python and ui dependencies
	uv sync
	pnpm --dir ui install

dev: db migrate     ## the app on the host, with reload: database, migrations, then the API, worker, terminal, assistant and ui (Ctrl-C stops all; make down first)
	$(MAKE) -j5 api worker terminal assistant ui

db: stack-ready     ## the database container alone, for make dev -> 127.0.0.1:5432 (make image first)
	docker compose --env-file /dev/null up -d --wait database

migrate:            ## apply alembic migrations on the host
	uv run alembic upgrade head

image:              ## build the local image (decisions 52, 73)
	docker build --pull -t factorial:local .

up: stack-ready     ## the image as ECS runs it, beside its database -> http://localhost:8000/factorial/
	docker compose --env-file /dev/null up -d --wait --wait-timeout 300

down:               ## stop the containers; factorial-data keeps the database and runs
	docker compose --env-file /dev/null down

test:               ## the pinned suite, as CI runs it
	uv run pytest -c pyproject.toml orchestrator/tests runtime/tests

check: test         ## everything a pull request to main must pass
	uv run ruff check .
	uv run pyright
	pnpm --dir ui build
	pnpm --dir ui test
	pnpm --dir ui check:ci
	./scripts/check-ste.sh README.md

e2b-template:       ## build and smoke the E2B templates in the team E2B_API_KEY names (needs the model keys); tags builds that pass the session smoke
	uv run python -m orchestrator.e2b_template

# The parts of make dev, one each; also run alone (make api, make worker). They log text: make dev
# puts five processes in one terminal. FACTORIAL_LOG_FORMAT=json in the shell still wins.
api worker terminal assistant: export FACTORIAL_LOG_FORMAT ?= text
api: # gitleaks:allow
	FACTORIAL_API_DOCS=true uv run uvicorn orchestrator.api.serve:app --reload

worker:
	uv run python -m orchestrator.worker

terminal:
	uv run python -m orchestrator.terminal

assistant:
	uv run python -m orchestrator.assistant

ui:
	pnpm --dir ui dev

design-system:      ## the design system's reference pages alone on :5180, beside make dev's :5173 (docs/design-system.md)
	@echo "guide      http://localhost:5180/factorial/dev/guide"
	@echo "catalogue  http://localhost:5180/factorial/dev/catalogue"
	@echo "sample     http://localhost:5180/factorial/dev/sample"
	pnpm --dir ui dev --port 5180 --strictPort

design-system-update: ## write ui/design-system/*.html again after a design primitive, token, reference page or docs/design-system.md changes
	pnpm --dir ui exec vitest run -u src/pages/GuidePage.test.tsx src/pages/CataloguePage.test.tsx src/pages/SamplePage.test.tsx

# Before db and up: the image, the volume, and no container outside the stack on that volume.
stack-ready:
	@docker image inspect factorial:local >/dev/null 2>&1 || { echo "make: no factorial:local image; run make image first" >&2; exit 1; }
	@docker volume create factorial-data >/dev/null
	@others="$$(docker ps -a --filter volume=factorial-data --format '{{.Names}} {{.Label "com.docker.compose.project"}}' | awk '$$2 != "factorial" { print $$1 }')"; \
	if [ -n "$$others" ]; then echo "make: factorial-data is mounted outside the stack by: $$others. Two Postgres servers on one cluster corrupt it; stop and remove those containers first" >&2; exit 1; fi
