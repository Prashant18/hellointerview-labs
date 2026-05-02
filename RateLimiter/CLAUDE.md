# RateLimiter — Lab Plan

Source: `problem.txt` (HelloInterview, Evan King, Jul 2025).

## What we're building

A distributed, sharded, highly-available **API-Gateway-embedded rate limiter** using the **Token Bucket** algorithm with Redis Cluster as the central state store. Target shape (final lab): **1M req/s capacity**, sub-10ms overhead, fail-closed under Redis loss, dynamic rule reload, hot-key auto-blocking.

We deliberately rebuild it phase by phase from "obviously broken" → production-shaped, so you can **feel** every design decision instead of being told about it.

## Final architecture (what the last lab demonstrates)

```
            ┌───────────────────┐
   client ─►│  k6 / curl / etc. │
            └──────────┬────────┘
                       │
                       ▼
            ┌──────────────────────┐         ┌──────────────────┐
            │  API Gateway (Go)    │ ◄──────►│ etcd  (rules)    │
            │  — Token Bucket      │  watch  └──────────────────┘
            │  — Lua atomic check  │
            │  — /metrics          │         ┌──────────────────┐
            │  3+ replicas         │ ◄──────►│ Redis Cluster    │
            │                      │  EVALSHA│ 6 nodes          │
            └──────────┬───────────┘         │ 3 master/3 repl  │
                       │                     └──────────────────┘
                       ▼
              upstream service                ┌──────────────────┐
                                              │ Prom + Grafana   │
                                              └──────────────────┘
```

## Tech stack & justification

| Component | Choice | Why |
| --- | --- | --- |
| Gateway service | **Python 3.12 + FastAPI + uvicorn** | User-preferred. uvicorn on uvloop hits ~10–20k RPS per replica, plenty for what the labs prove. Async story (`asyncio` + `redis.asyncio`) keeps the concurrency model legible. |
| Packaging | **uv** (`pyproject.toml` + `uv.lock`) | Reproducible installs in Docker (`uv sync --frozen --no-dev`) and locally (`uv sync` → `.venv` for IDE). Never `pip install` or `requirements.txt`. |
| Redis client | **redis-py 5.x** (`redis.asyncio`) | Native asyncio, supports Cluster, supports `EVALSHA` script caching, supports connection pooling. |
| State store | **redis:7** (single → cluster) | Sub-ms latency, atomic Lua scripts, native cluster mode. Same as Stripe/Cloudflare in production. |
| Config plane | **etcd v3** (via `etcd3-py` or `python-etcd3`) | Watch API for live rule reload. ZooKeeper would also work; etcd is lighter in Docker. |
| Load gen | **k6** | Scriptable, exports Prometheus metrics, ramping built in. Run as a Docker service per lab. |
| Observability | **Prometheus + Grafana** + `prometheus-client` | One shared config tree under `shared/observability/`, mounted into each lab's compose. |
| Chaos | **pumba** + `docker pause`/`kill` | Inject latency, kill containers, partition. |

> **Note on language**: We're using Python because the user is fluent in it and lab iteration speed matters more than runtime realism. If a specific phase really needs raw throughput to make a point (it shouldn't — we're proving patterns, not chasing TechEmpower numbers), the lab README will say so.

## Phased lab plan (10 phases, ~6–8 hours total)

**One codebase, evolving in place.** All labs modify the same folder (`labs/00-setup/`); there are no per-lab subfolders. Each phase appends an entry to `JOURNAL.md` describing what changed, what property it proves, and the staff+ talking points it unlocks. Time budgets are reading + running, not deep practice.

### 00 — Setup (~30 min)
**Goal:** `docker compose up` brings the stack to life. Gateway returns 200 on `/health`. k6 hello-world hits it.
**You'll have:** Go module skeleton, Dockerfile, compose file with `prom + grafana + gateway`, Makefile.
**Verify:** `make up && make smoke` — k6 fires 100 RPS for 5s, gateway logs them, Grafana shows the request counter.

### 01 — In-memory Token Bucket (~45 min)
**Goal:** Implement the TB algorithm correctly in pure Go, no Redis. Single replica.
**You'll have:** `pkg/tokenbucket/bucket.go` with `Allow(clientID) (allowed bool, remaining int, reset time.Time)`. Unit tests covering refill math, burst, idle clients, edge cases.
**Verify:** `go test ./...` passes; `make load-burst` shows exactly N requests pass, then 429s, with proper `X-RateLimit-*` headers.
**Pattern named:** Token Bucket vs. Fixed Window vs. Sliding Window — README has the trade-off table you'd put on the whiteboard.

### 02 — Multi-replica breakage (~30 min)
**Goal:** Scale gateway to 3 replicas behind a load balancer. **Watch the limit leak.**
**You'll have:** compose scales to 3, nginx (or Caddy) round-robins. k6 sends "100 req/min" for one user.
**Verify:** `./verify.sh` shows ~300 requests passing for a "100/min" rule. The bug is reproduced. README explains *why* this motivates centralized state.

### 03 — Redis-backed Token Bucket (naive) (~45 min)
**Goal:** Move state to Redis. Use plain `HMGET` + `HSET` + `MULTI/EXEC` (the "obvious" implementation from the HelloInterview write-up). Reproduce the race condition.
**You'll have:** `pkg/limiter/redis_naive.go`, a chaos test that fires 200 concurrent requests for the same client.
**Verify:** With limit=100 and concurrency=200, more than 100 requests pass. README quantifies "how many over" and explains the read-then-write gap.

### 04 — Lua atomic fix (~45 min)
**Goal:** Replace the TOCTOU sequence with a single `EVALSHA` Lua script. Re-run the race test, prove the bug is gone.
**You'll have:** `scripts/tokenbucket.lua`, `pkg/limiter/redis_lua.go`, script-load-on-startup with SHA caching.
**Verify:** Same chaos test from lab 03 — exactly 100 requests pass, every run.
**Pattern named:** "Expanding the atomic boundary" — the deep-dive technique behind every distributed counter.

### 05 — Redis Cluster sharding (~45 min)
**Goal:** Replace single Redis with a 6-node cluster (3 master, 3 replica). Confirm keys distribute. Load test that the gateway scales linearly.
**You'll have:** `redis-cluster/` compose with `redis-cli --cluster create`, `pkg/limiter/cluster.go` using `go-redis/v9` Cluster client.
**Verify:** `redis-cli --cluster check` healthy; k6 ramp from 1k → 50k RPS; latency p99 stays flat. Prometheus dashboard shows ops spread across 3 masters.
**Pattern named:** Consistent hashing via hash slots; why Redis Cluster vs. client-side hashing.

### 06 — Failover & fail-closed (~45 min)
**Goal:** Kill a master. Observe replica promotion. Decide and implement **fail-closed** behavior in the gateway when Redis is unreachable.
**You'll have:** `chaos/kill-master.sh`, gateway circuit-breaker that returns 503 when Redis times out, metric `ratelimit_failmode_total`.
**Verify:** During the ~2s failover window, gateway returns 503s (not 200s). After failover, traffic resumes. README compares fail-open vs. fail-closed with the social-media-platform reasoning from the source.
**Pattern named:** Circuit breaker + bulkhead.

### 07 — Load test & observability (~60 min)
**Goal:** Run a real k6 ramp (target 100k RPS local, extrapolate to 1M). Build a Grafana dashboard. Define SLOs and alert rules.
**You'll have:** `k6/ramp.js`, Grafana JSON dashboard checked into `shared/observability/dashboards/`, Prometheus alert rules for latency p99 > 10ms, error rate > 1%, fail-mode > 0.
**Verify:** `make bench` runs the ramp; dashboard auto-loads; one alert intentionally fires when you `tc qdisc` 50ms latency on Redis.
**Pattern named:** RED metrics (Rate, Errors, Duration) + golden signals.

### 08 — Hot keys & auto-blocklist (~45 min)
**Goal:** Synthetic single-client DDoS. Detect and auto-block.
**You'll have:** `pkg/blocklist/` that tracks "hits limit ≥ 10x in 60s → block 5min", a Redis-backed blocklist checked early in the request path.
**Verify:** `k6 single-client.js` floods one user; gateway starts dropping at the blocklist layer (cheaper than the TB layer); metric `ratelimit_blocklist_hits_total` climbs.
**Pattern named:** Layered defense — cheap check before expensive check.

### 09 — Dynamic rule config via etcd (~45 min)
**Goal:** Move rate limit rules out of code. Live-reload them via etcd watch. No gateway restart.
**You'll have:** etcd in compose, `pkg/rules/etcd_watcher.go` with snapshot + watch fallback to long-poll, an `etcdctl put` demo that drops a user's limit from 1000 → 10 and watches it take effect within 1s.
**Verify:** Run `make reload-demo` — you'll see the limit change reflected in <1s without restarting any gateway. README discusses ZooKeeper alternative + push-vs-pull trade-off.
**Pattern named:** Dynamic configuration / watch-based propagation.

### 10 — Integration & RECAP (~30 min)
**Goal:** Compose all 9 phases. Run a single scenario that exercises sharding + Lua + failover + dynamic config + blocklist simultaneously. Write the `RECAP.md` interview cheat-sheet.
**You'll have:** `make full` brings up everything; `k6 full-scenario.js` mixes traffic; `RECAP.md` contains the staff+ talking points and "questions you should be able to answer cold."
**Verify:** Read `RECAP.md` aloud and feel comfortable with every line. If anything is fuzzy, we re-do that lab.

## Out of scope (acknowledge but don't build)

- Multi-region replication with CRDTs (sketch in `RECAP.md`, but no working code — too much infra for marginal interview value).
- Long-term analytics on rate limit data (the source explicitly puts this below the line).
- Client-side SDK with adaptive backoff (covered conceptually in lab 07's README).

## How we work

1. Approve this plan or push back.
2. We start lab 00. I write the code, you run it, we both inspect outputs.
3. After every lab, before moving on, you tell me one thing you'd say to the interviewer about it. If it's vague, we add a deeper sub-task before advancing.
4. If a lab takes longer than its budget, that's a signal — we either split it or you drive harder; we don't paper over it.

## Open questions before we start lab 00

- **Language**: Go OK, or do you want Python? (Go is my recommendation; reasons in the table above.)
- **Time budget**: are you doing this in one sitting, or across multiple sessions? Affects whether I checkpoint state aggressively.
- **Local resources**: how much RAM can Docker eat on your machine? Redis Cluster + observability + 3 gateway replicas is ~2GB. If that's tight, I'll trim cluster to 3 nodes and skip Grafana in the early labs.
