#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

API_PORT="${API_PORT:-18001}"
DDB_PORT="${DDB_PORT:-18002}"
PROM_PORT="${PROM_PORT:-19091}"
GRAFANA_PORT="${GRAFANA_PORT:-13001}"
export API_PORT DDB_PORT PROM_PORT GRAFANA_PORT

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

step "create item, bid, read back..."
ITEM=$(curl -fsS -X POST "http://localhost:${API_PORT}/v1/items" \
  -H 'Content-Type: application/json' \
  -d '{"title":"verify-widget","start_price":10,"end_time_epoch":9999999999}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["item_id"])')
echo "[verify] item_id=$ITEM"

BID_STATUS=$(curl -fsS -o /tmp/bid.json -w '%{http_code}' -X POST "http://localhost:${API_PORT}/v1/items/${ITEM}/bids" \
  -H 'Content-Type: application/json' \
  -d '{"bidder":"alice","amount":42}')
[[ "$BID_STATUS" == "201" ]] || fail "bid expected 201, got $BID_STATUS"

GOT=$(curl -fsS "http://localhost:${API_PORT}/v1/items/${ITEM}" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["current_high_bid"], d["current_high_bidder"])')
[[ "$GOT" == "42.0 alice" ]] || fail "expected '42.0 alice', got '$GOT'"
ok "bid round-trip works."

step "running k6 smoke..."
docker compose run --rm k6 run /scripts/smoke.js

step "running k6 race (lab 02 — invariant: exactly one 201, 29× 409)..."
docker compose run --rm k6 run /scripts/race.js

step "running SSE fanout latency probe (lab 03 — p95 < 200ms)..."
(cd api && API_URL="http://localhost:${API_PORT}" uv run --quiet python scripts/sse_latency.py) \
  || fail "SSE fanout probe failed"

step "running k6 cache load (lab 04 — p95 read <5ms; hit ratio >95%)..."
docker compose run --rm k6 run /scripts/cache_load.js

step "PASS"
cat <<EOF

  api         http://localhost:${API_PORT}
  ddb local   http://localhost:${DDB_PORT}
  prometheus  http://localhost:${PROM_PORT}
  grafana     http://localhost:${GRAFANA_PORT}

To tear down: make down  (or  make clean  to also drop volumes)
EOF
