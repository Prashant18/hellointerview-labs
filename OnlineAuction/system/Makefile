.PHONY: up down restart logs ps smoke verify clean health open sync test run-local ddb-shell

COMPOSE = docker compose

API_PORT     ?= 18001
DDB_PORT     ?= 18002
PROM_PORT    ?= 19091
GRAFANA_PORT ?= 13001
export API_PORT DDB_PORT PROM_PORT GRAFANA_PORT

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

smoke:
	$(COMPOSE) run --rm k6 run /scripts/smoke.js

race:
	$(COMPOSE) run --rm k6 run /scripts/race.js

verify:
	./verify.sh

clean:
	$(COMPOSE) down -v --remove-orphans
	docker image rm auction-api:latest 2>/dev/null || true

# Local dev: populate gateway/.venv for IDE language servers.
sync:
	cd api && uv sync

run-local:
	cd api && DDB_ENDPOINT=http://localhost:$(DDB_PORT) AWS_DEFAULT_REGION=us-east-1 \
	  AWS_ACCESS_KEY_ID=local AWS_SECRET_ACCESS_KEY=local \
	  uv run uvicorn app.main:app --reload --port $(API_PORT)

test:
	cd api && uv run pytest -xvs

# Open an aws-cli shell against DynamoDB Local (handy for inspecting tables).
ddb-shell:
	docker run --rm -it --network auction_default \
	  -e AWS_ACCESS_KEY_ID=local -e AWS_SECRET_ACCESS_KEY=local -e AWS_DEFAULT_REGION=us-east-1 \
	  amazon/aws-cli --endpoint-url http://dynamodb-local:8000 dynamodb list-tables

open:
	@echo "  api         http://localhost:$(API_PORT)"
	@echo "  ddb local   http://localhost:$(DDB_PORT)"
	@echo "  prometheus  http://localhost:$(PROM_PORT)"
	@echo "  grafana     http://localhost:$(GRAFANA_PORT)"
