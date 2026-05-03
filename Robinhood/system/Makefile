.PHONY: up down restart logs ps smoke verify clean health open sync test run-local ddb-shell

COMPOSE = docker compose

API_PORT      ?= 18021
DDB_PORT      ?= 18022
EXCHANGE_PORT ?= 18023
PROM_PORT     ?= 19093
GRAFANA_PORT  ?= 13003
export API_PORT DDB_PORT EXCHANGE_PORT PROM_PORT GRAFANA_PORT

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

restart: down up

logs:
	$(COMPOSE) logs -f api

ps:
	$(COMPOSE) ps

health:
	@curl -fsS http://localhost:$(API_PORT)/health | python3 -m json.tool
	@curl -fsS http://localhost:$(EXCHANGE_PORT)/health | python3 -m json.tool

smoke:
	$(COMPOSE) run --rm k6 run /scripts/smoke.js

verify:
	./verify.sh

clean:
	$(COMPOSE) down -v --remove-orphans
	docker image rm broker-api:latest broker-mock-exchange:latest 2>/dev/null || true

sync:
	cd api && uv sync
	cd mock-exchange && uv sync

run-local:
	cd api && DDB_ENDPOINT=http://localhost:$(DDB_PORT) AWS_DEFAULT_REGION=us-east-1 \
	  AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
	  EXCHANGE_URL=http://localhost:$(EXCHANGE_PORT) \
	  uv run uvicorn app.main:app --reload --port $(API_PORT)

test:
	cd api && uv run pytest -xvs

ddb-shell:
	docker run --rm -it --network broker_default \
	  -e AWS_ACCESS_KEY_ID=local -e AWS_SECRET_ACCESS_KEY=local -e AWS_DEFAULT_REGION=us-east-1 \
	  amazon/aws-cli --endpoint-url http://dynamodb-local:8000 dynamodb list-tables

open:
	@echo "  api          http://localhost:$(API_PORT)"
	@echo "  mock-exch    http://localhost:$(EXCHANGE_PORT)"
	@echo "  ddb local    http://localhost:$(DDB_PORT)"
	@echo "  prometheus   http://localhost:$(PROM_PORT)"
	@echo "  grafana      http://localhost:$(GRAFANA_PORT)"
