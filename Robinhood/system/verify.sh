#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

API_PORT="${API_PORT:-18021}"
DDB_PORT="${DDB_PORT:-18022}"
EXCHANGE_PORT="${EXCHANGE_PORT:-18023}"
PROM_PORT="${PROM_PORT:-19093}"
GRAFANA_PORT="${GRAFANA_PORT:-13003}"
export API_PORT DDB_PORT EXCHANGE_PORT PROM_PORT GRAFANA_PORT

step() { printf "\n\033[1;36m[verify] %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m[verify] %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m[verify] %s\033[0m\n" "$*"; exit 1; }

step "pytest..."
(cd api && uv run --quiet pytest -q) || fail "pytest failed"
ok "pytest pass."

step "building + starting stack..."
docker compose up -d --build

step "waiting for api /health (via Caddy)..."
for i in {1..60}; do
  if curl -fsS "http://localhost:${API_PORT}/health" >/dev/null 2>&1; then
    ok "api healthy"
    break
  fi
  if [[ $i -eq 60 ]]; then
    docker compose logs api | tail -50 || true
    fail "api never became healthy"
  fi
  sleep 1
done

step "waiting for mock-exchange /health..."
for i in {1..30}; do
  if curl -fsS "http://localhost:${EXCHANGE_PORT}/health" >/dev/null 2>&1; then
    ok "mock-exchange healthy"
    break
  fi
  [[ $i -eq 30 ]] && fail "mock-exchange never healthy"
  sleep 1
done

step "happy-path POST /v1/orders + GET..."
ORDER=$(curl -fsS -X POST "http://localhost:${API_PORT}/v1/orders" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: verify-1' \
  -d '{"position":"buy","symbol":"AAPL","price_cents":15000,"num_shares":10,"order_type":"limit"}')
echo "[verify] $ORDER"
STATUS=$(echo "$ORDER" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
EXT=$(echo "$ORDER" | python3 -c 'import json,sys; print(json.load(sys.stdin)["external_order_id"])')
[[ "$STATUS" == "SUBMITTED" ]] || fail "expected SUBMITTED, got $STATUS"
[[ -n "$EXT" && "$EXT" != "None" ]] || fail "expected external_order_id, got '$EXT'"
ok "order submitted with external_order_id=$EXT"

step "idempotency: same Idempotency-Key returns same order, NOT a duplicate at exchange..."
REPLAY=$(curl -fsS -X POST "http://localhost:${API_PORT}/v1/orders" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: verify-1' \
  -d '{"position":"buy","symbol":"AAPL","price_cents":15000,"num_shares":10,"order_type":"limit"}')
EXT2=$(echo "$REPLAY" | python3 -c 'import json,sys; print(json.load(sys.stdin)["external_order_id"])')
[[ "$EXT" == "$EXT2" ]] || fail "idempotency broken: first=$EXT, replay=$EXT2"

EX_COUNT=$(curl -fsS "http://localhost:${EXCHANGE_PORT}/debug/orders" | python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])')
[[ "$EX_COUNT" == "1" ]] || fail "expected 1 order at mock-exchange, got $EX_COUNT"
ok "idempotency holds: 1 order at exchange after 2 calls."

step "GET /v1/orders lists the order..."
LIST=$(curl -fsS "http://localhost:${API_PORT}/v1/orders")
COUNT=$(echo "$LIST" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["orders"]))')
[[ "$COUNT" == "1" ]] || fail "expected 1 order in list, got $COUNT"
ok "list returns 1 order."

step "running k6 smoke..."
docker compose run --rm k6 run /scripts/smoke.js

step "PASS"
cat <<EOF

  api          http://localhost:${API_PORT}
  mock-exch    http://localhost:${EXCHANGE_PORT}
  ddb local    http://localhost:${DDB_PORT}
  prometheus   http://localhost:${PROM_PORT}
  grafana      http://localhost:${GRAFANA_PORT}

To tear down: make down  (or  make clean  to also drop images)
EOF
