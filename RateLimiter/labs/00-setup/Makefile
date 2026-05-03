.PHONY: up down restart logs ps smoke burst verify clean health metrics open sync test test-watch run-local

COMPOSE = docker compose

# Host ports (override on the command line, e.g. `make up GATEWAY_PORT=8000`).
# Defaults intentionally avoid common collisions (8000/9090/3000).
GATEWAY_PORT ?= 18000
PROM_PORT    ?= 19090
GRAFANA_PORT ?= 13000
export GATEWAY_PORT PROM_PORT GRAFANA_PORT

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart: down up

logs:
	$(COMPOSE) logs -f gateway

ps:
	$(COMPOSE) ps

health:
	@curl -fsS http://localhost:$(GATEWAY_PORT)/health | python3 -m json.tool

metrics:
	@curl -fsS http://localhost:$(GATEWAY_PORT)/metrics | head -40

smoke:
	$(COMPOSE) run --rm k6 run /scripts/smoke.js

burst:
	$(COMPOSE) run --rm k6 run /scripts/burst.js

race:
	$(COMPOSE) run --rm k6 run /scripts/race.js

# Open a redis-cli inside the cluster (defaults to redis-1; flips to other
# nodes via -ASK/-MOVED redirects automatically).
redis-cli:
	$(COMPOSE) exec redis-1 redis-cli -c

# Inspect the cluster topology + slot assignment.
cluster-check:
	$(COMPOSE) exec redis-1 redis-cli --cluster check redis-1:6379

# Unit tests against the TokenBucket implementation. Runs locally via uv —
# no docker needed, just the gateway/.venv populated by `make sync`.
test:
	cd gateway && uv run pytest -xvs

# Re-run pytest on file changes. Requires `pip install pytest-watch` or use
# `find ... | entr` if you have entr installed.
test-watch:
	cd gateway && uv run pytest -xvs --looponfail

verify:
	./verify.sh

clean:
	$(COMPOSE) down -v --remove-orphans
	docker image rm ratelimiter-gateway:lab00 2>/dev/null || true

open:
	@echo "  gateway    http://localhost:$(GATEWAY_PORT)"
	@echo "  prometheus http://localhost:$(PROM_PORT)"
	@echo "  grafana    http://localhost:$(GRAFANA_PORT)"

# Local dev — set up the venv your IDE will read.
sync:
	cd gateway && uv sync

# Run the gateway directly on the host (no Docker). Useful for breakpoint debugging.
run-local:
	cd gateway && uv run uvicorn app.main:app --reload --port $(GATEWAY_PORT)
