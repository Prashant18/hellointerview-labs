#!/usr/bin/env bash
# verify.sh — runs unit tests, boots the stack, and exercises three k6
# scenarios end-to-end: smoke (harness liveness), burst (regression for
# centralized state enforcing the limit), and race (proves the TOCTOU
# bug in the naive HMGET/HSET implementation).

set -euo pipefail

cd "$(dirname "$0")"

GATEWAY_PORT="${GATEWAY_PORT:-18000}"
PROM_PORT="${PROM_PORT:-19090}"
GRAFANA_PORT="${GRAFANA_PORT:-13000}"
REDIS_PORT="${REDIS_PORT:-16379}"
export GATEWAY_PORT PROM_PORT GRAFANA_PORT REDIS_PORT

step() { printf "\n\033[1;36m[verify] %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m[verify] %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m[verify] %s\033[0m\n" "$*"; exit 1; }

prom_query() {
  curl -fsS -G "http://localhost:${PROM_PORT}/api/v1/query" --data-urlencode "query=$1" \
    | python3 -c 'import json,sys; r=json.load(sys.stdin)["data"]["result"]; print(int(float(r[0]["value"][1])) if r else 0)'
}

# --------------------------------------------------------------------------
# Stage 1 — unit tests for the in-memory algorithm. Still relevant: the
# Redis variant uses the same math; the unit tests pin the math down.
# --------------------------------------------------------------------------
step "running unit tests (pytest, in-memory algorithm)..."
(cd gateway && uv run --quiet pytest -q) || fail "unit tests failed"
ok "unit tests passed."

# --------------------------------------------------------------------------
# Stage 2 — boot the stack (gateway + redis + caddy + prom + grafana).
# --------------------------------------------------------------------------
step "building and starting the stack..."
docker compose up -d --build

step "waiting for gateway /health (via Caddy)..."
for i in {1..30}; do
  if curl -fsS "http://localhost:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
    ok "gateway healthy"
    break
  fi
  if [[ $i -eq 30 ]]; then
    docker compose logs gateway || true
    fail "gateway never became healthy"
  fi
  sleep 1
done

# Caddy's `dynamic a` upstream cache may briefly hold stale IPs after the
# stack is recreated. Burn a few requests to warm the LB before assertions.
step "warming up the LB (10 throwaway requests)..."
for _ in {1..10}; do
  curl -fsS "http://localhost:${GATEWAY_PORT}/v1/check" -H "x-client-id: warmup" >/dev/null 2>&1 || true
done

# --------------------------------------------------------------------------
# Stage 3 — smoke (harness liveness).
# --------------------------------------------------------------------------
step "running k6 smoke (100 RPS for 5s, lots of distinct clients)..."
docker compose run --rm k6 run /scripts/smoke.js

# --------------------------------------------------------------------------
# Stage 4 — burst (regression: centralized state enforces the limit).
# Each gateway replica goes through Redis, so a single client gets exactly
# `capacity` allowed despite the round-robin LB. This is the property that
# distinguishes lab 03 from lab 02.
# --------------------------------------------------------------------------
sleep 6  # give Prometheus a scrape window for clean before/after deltas

allowed_before_burst=$(prom_query 'sum(gateway_ratelimit_allowed_total)')
denied_before_burst=$(prom_query 'sum(gateway_ratelimit_denied_total)')

step "running k6 burst (50 reqs, 1 client, capacity=10) — expect EXACTLY 10/40..."
docker compose run --rm k6 run /scripts/burst.js

sleep 6
allowed_after_burst=$(prom_query 'sum(gateway_ratelimit_allowed_total)')
denied_after_burst=$(prom_query 'sum(gateway_ratelimit_denied_total)')

burst_allowed=$((allowed_after_burst - allowed_before_burst))
burst_denied=$((denied_after_burst - denied_before_burst))
echo "[verify] burst delta: allowed=${burst_allowed}  denied=${burst_denied}"

if [[ "$burst_allowed" -ne 10 ]] || [[ "$burst_denied" -ne 40 ]]; then
  fail "burst expected 10 allowed / 40 denied (centralized state regression). Got ${burst_allowed}/${burst_denied}."
fi
ok "centralized state enforces the limit globally — leak from lab 02 is fixed."

# --------------------------------------------------------------------------
# Stage 5 — race (chaos): demonstrate the TOCTOU bug.
# 20 concurrent VUs against ONE client. Naive HMGET/HSET should let more
# than `capacity` through; lab 04's Lua script will pin it back to capacity.
# --------------------------------------------------------------------------
allowed_before_race=$allowed_after_burst

step "running k6 race (20 VUs × 100 reqs vs 1 client, capacity=10) — expect OVERSHOOT..."
docker compose run --rm k6 run /scripts/race.js

sleep 6
allowed_after_race=$(prom_query 'sum(gateway_ratelimit_allowed_total)')
race_allowed=$((allowed_after_race - allowed_before_race))
echo "[verify] race delta: allowed=${race_allowed}  (capacity=10; anything > 10 proves the race)"

if [[ "$race_allowed" -le 10 ]]; then
  fail "race did NOT overshoot. Got ${race_allowed} allowed but capacity is 10. Either the race didn't fire (try cranking VUs/iterations) or you accidentally fixed it (great, but lab 04 would be anticlimactic)."
fi
ok "TOCTOU race demonstrated end-to-end — ${race_allowed} allowed for a configured limit of 10."

# --------------------------------------------------------------------------
# Stage 6 — per-instance breakdown for the dashboard story.
# --------------------------------------------------------------------------
step "per-replica allow/deny breakdown:"
curl -fsS -G "http://localhost:${PROM_PORT}/api/v1/query" \
  --data-urlencode 'query=sum by (instance) (gateway_ratelimit_allowed_total)' \
  | python3 -c '
import json, sys
for r in json.load(sys.stdin)["data"]["result"]:
    inst = r["metric"]["instance"]
    val = int(float(r["value"][1]))
    print(f"   {inst:32s} allowed={val}")
'

step "PASS"
cat <<EOF

  gateway     http://localhost:${GATEWAY_PORT}/health
  /metrics    http://localhost:${GATEWAY_PORT}/metrics
  prometheus  http://localhost:${PROM_PORT}
  grafana     http://localhost:${GRAFANA_PORT}
  redis       redis://localhost:${REDIS_PORT}   (try: redis-cli -p ${REDIS_PORT} HGETALL bucket:warmup)

To tear down: make down  (or  make clean  to also drop volumes)
EOF
