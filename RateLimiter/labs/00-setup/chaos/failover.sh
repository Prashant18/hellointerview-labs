#!/usr/bin/env bash
# chaos/failover.sh — kill a Redis Cluster master and observe the gap.
#
# YOU implement the marked TODO sections. The setup, helpers, and reporting
# format are provided so the harness consistently produces interpretable
# output across runs.
#
# What this script SHOULD do, end to end:
#   1) Pick a victim master from the running Redis Cluster.
#   2) Start a background traffic generator hitting /v1/check and recording
#      per-second status code counts ([200, 429, 503, other]).
#   3) Mark t=0, then `docker compose stop` the victim.
#   4) Poll `cluster_state` on a surviving node every 250ms until it reads
#      `ok` again — that's when a replica was promoted and slot coverage
#      is restored.
#   5) Stop the traffic generator, render a per-second timeline showing
#      when 503s appeared and when they stopped.
#   6) `docker compose start` the victim to restore the cluster shape.
#
# What this proves:
#   - During the failover window, /v1/check returns 503 (fail-closed) NOT
#     500 (gateway crash) — the asyncio.wait_for + try/except in main.py
#     is doing its job. The RL_FAILMODE counter increments.
#   - Requests for keys on OTHER masters keep working because RedisCluster
#     was constructed with require_full_coverage=False — degraded service,
#     not total outage.
#   - Cluster recovers within ~5–10s for a clean kill (cluster-node-timeout
#     in compose is 5000ms; promotion plus topology gossip adds a few s).
#
# How to run it:
#   make chaos-failover
#
# Output format you should produce (suggested):
#   t=  0s  kill redis-2
#   t=  1s  200=18  429=2   503=0
#   t=  2s  200=12  429=3   503=5      ← failover gap starts
#   t=  3s  200=8   429=2   503=10
#   ...
#   t=  8s  cluster_state=ok           ← replica promoted
#   t=  9s  200=20  429=0   503=0      ← back to normal
#   t= 10s  start redis-2 (restoring 3-master shape)
#
# DESIGN HINTS:
#   - Pick a master:
#       docker compose exec -T redis-1 redis-cli cluster nodes \
#         | awk '/master/ && !/myself/ {print $2; exit}'
#     Returns something like "redis-3:6379@16379,redis-3"
#     The hostname before the colon is what you stop with `docker compose stop redis-3`.
#
#   - Traffic loop (background):
#       trap 'kill 0' EXIT  # kill child processes when this script exits
#       (
#         while true; do
#           code=$(curl -s -o /dev/null -w '%{http_code}' \
#             "http://localhost:18000/v1/check" -H "x-client-id: chaos-$RANDOM")
#           echo "$(date +%s) $code"
#         done
#       ) > /tmp/chaos-traffic.log 2>&1 &
#
#   - Polling cluster state on a SURVIVING node (don't poll the killed one):
#       docker compose exec -T redis-1 redis-cli cluster info \
#         | grep cluster_state | tr -d '\r' | cut -d: -f2
#
#   - Bucket per-second counts with awk:
#       awk '{ counts[$1, $2]++; secs[$1]=1 } END {
#         for (s in secs) {
#           print s, "200="counts[s,"200"]+0, "429="counts[s,"429"]+0, "503="counts[s,"503"]+0
#         }
#       }' /tmp/chaos-traffic.log | sort
#
# LIMITS / things to be aware of:
#   - This kills a real container; if the smoke test happens to be running
#     in the same `make verify`, you could double-fail. Don't combine.
#   - The kill is graceful (SIGTERM). For a harder test, use `docker compose
#     kill --signal SIGKILL <node>`. Cluster failover behavior should be
#     similar but the gap is longer because peers wait the full
#     cluster-node-timeout (5s) before declaring failure.

set -euo pipefail
cd "$(dirname "$0")/.."

GATEWAY_PORT="${GATEWAY_PORT:-18000}"

step() { printf "\n\033[1;36m[chaos] %s\033[0m\n" "$*"; }
ok()   { printf "\033[1;32m[chaos] %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m[chaos] %s\033[0m\n" "$*"; exit 1; }

# Sanity: stack must be running
if ! curl -fsS "http://localhost:${GATEWAY_PORT}/health" >/dev/null 2>&1; then
  fail "gateway not reachable on :${GATEWAY_PORT} — run \`make up\` first."
fi

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #1: Identify the victim master.
#   Pick one of the masters reported by `cluster nodes`. Set $VICTIM
#   to the docker compose service name (e.g. "redis-2" or "redis-3").
# ──────────────────────────────────────────────────────────────────────────
VICTIM=""  # ← set this
if [[ -z "$VICTIM" ]]; then
  fail "TODO #1: pick a victim master and assign \$VICTIM (see hint at top)."
fi
step "victim = $VICTIM"

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #2: Start the background traffic generator.
#   Write per-line records of "<unix_ts> <http_code>" to /tmp/chaos-traffic.log
#   and remember the PID so you can kill it later. ~10 RPS is plenty.
# ──────────────────────────────────────────────────────────────────────────
TRAFFIC_PID=""  # ← set this when you start the loop
TRAFFIC_LOG=/tmp/chaos-traffic.log
> "$TRAFFIC_LOG"

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #3: Capture t=0, then `docker compose stop $VICTIM`.
# ──────────────────────────────────────────────────────────────────────────
T0=""

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #4: Poll cluster_state on a SURVIVING node every 250ms until
#   it reads "ok" — that's the failover-completed signal. Print a line
#   "t=Xs cluster_state=ok" when it flips.
#   Bound the wait to 30s so a wedged cluster doesn't hang the script.
# ──────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #5: Stop the traffic generator. Render the per-second timeline.
# ──────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────
# TODO(you) #6: Restore the cluster shape with `docker compose start $VICTIM`.
#   Optional: poll cluster info to confirm we're back to 3 master / 3 replica.
# ──────────────────────────────────────────────────────────────────────────

ok "chaos failover scenario complete (assuming all TODOs implemented)."
echo "Traffic log: $TRAFFIC_LOG"
