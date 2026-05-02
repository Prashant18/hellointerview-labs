# RateLimiter — Lab Journal

Chronological log of what each lab phase added or changed. The codebase lives in `labs/00-setup/` and evolves in place; this file is the time-machine that explains why it looks the way it does. To revisit an earlier state, after the repo is `git init`'d, tag each lab and `git checkout lab-NN`.

---

## Lab 00 — Setup (2026-05-01)

**Files added:**
- `labs/00-setup/gateway/{Dockerfile,pyproject.toml,uv.lock,.python-version}` — uv-based Python 3.12 image.
- `labs/00-setup/gateway/app/main.py` — FastAPI gateway with `/health`, `/v1/check` (placeholder), `/metrics` + `observe` middleware (Counter `gateway_requests_total`, Histogram `gateway_request_duration_seconds` with custom sub-10ms buckets, `try/finally` so 5xx still gets counted).
- `labs/00-setup/docker-compose.yml` — gateway + Prometheus + Grafana + k6 (test profile). Host ports `18000/19090/13000` to avoid clashing with other locally-running stacks.
- `labs/00-setup/k6/smoke.js` — 100 RPS for 5s smoke test, asserts 200s and `p99 < 200ms`.
- `labs/00-setup/{Makefile,verify.sh,README.md}` — one-command up/down/verify lifecycle.
- `shared/observability/prometheus/prometheus.yml`, `shared/observability/grafana/provisioning/datasources/datasource.yml` — provisioning configs mounted by compose.

**Property proved:** the harness closes the loop end-to-end. `verify.sh` boots the stack, k6 fires 500 requests, Prometheus scrapes and stores ≥400 samples within one scrape interval, the assertion passes.

**Staff+ talking points unlocked:**
- *Wire observability before logic.* Without `/metrics → Prometheus → dashboard → load gen` working first, every later claim about latency or correctness is unverifiable.
- *Cardinality discipline.* Label by `path` and `status` (bounded sets). Never label by `user_id` or un-templated paths — one bad label decision = days of Prometheus ingestion outages.
- *Bucket boundaries are an SLO statement.* Rate-limiter SLO is sub-10ms, so we picked sub-millisecond resolution in the 1–10ms range; default `prometheus_client` buckets would have made p99 unobservable below 5ms.
- *`time.perf_counter` for durations, never `time.time`.* NTP can move the wall clock backward mid-request; you'd record negative latencies.
- *`try/finally` in the metrics middleware.* If `call_next` raises, the exception bubbles up and your counters undercount errors. Initialize `status_code = 500` before the try, set on success, record in finally.

**What's next:** Lab 01 — replace the placeholder `/v1/check` with a real in-memory Token Bucket.

---

## Lab 01 — In-memory Token Bucket (2026-05-01)

**Files added:**
- `labs/00-setup/gateway/app/tokenbucket.py` — `TokenBucket` class with per-client `(tokens, last_refill_ts)` state, single bucket-wide `threading.Lock`, injectable clock for testable time, lazy refill on `allow()`. Concrete `Rule` and `Decision` dataclasses.
- `labs/00-setup/gateway/tests/test_tokenbucket.py` — 11 unit tests pinning the algorithm's contract: lazy-init full bucket, drain by 1, deny beyond capacity, deny doesn't go negative, refill rate, capacity cap, fractional accumulation, client isolation, `reset_after` semantics, threading safety.
- `labs/00-setup/k6/burst.js` — 50 requests from one client, asserts exactly 10 allowed / 40 denied + presence of all `X-RateLimit-*` headers and `Retry-After`.

**Files changed:**
- `labs/00-setup/gateway/app/main.py` — wires `TokenBucket` into `/v1/check`. New `RL_ALLOWED` / `RL_DENIED` counters. 429 response with `X-RateLimit-Limit`, `-Remaining`, `-Reset` (absolute Unix ts), and `Retry-After` (ceil seconds).
- `labs/00-setup/gateway/pyproject.toml` — adds `pytest` dev dependency, configures `testpaths`.
- `labs/00-setup/k6/smoke.js` — unique `client_id` per `(VU, ITER)` so smoke stays a *harness liveness* check rather than a rate-limit check. (Forgetting this was the first verify failure: smoke was reusing client IDs and hitting limits.)
- `labs/00-setup/verify.sh` — pytest stage now runs first; full pipeline is `pytest → up → /health → smoke → burst → Prometheus assert`.
- `labs/00-setup/Makefile` — adds `test`, `test-watch`, `burst`, `run-local`, `sync` targets.

**Property proved:** With `capacity=10, refill_per_sec=1`, a single client firing 50 requests in tight succession gets exactly 10 successes and 40 429s; allow/deny metrics scrape into Prometheus; HTTP headers conform to the standard rate-limit header set.

**Staff+ talking points unlocked:**
- *Read-modify-write under one lock.* Without it, two threads can both observe `tokens=1`, both decrement, and your "100/min" limit silently becomes "200/min" under load. Same race shape we'll reproduce against Redis (with HMGET/HSET) in lab 03.
- *Denied requests must NOT push the counter negative.* A naive `tokens -= 1; if tokens < 0: deny` lets denials accumulate "debt" that eats the next refill. Read first, decide, then deduct only when allowed.
- *Inject the clock; never call `time.monotonic` inside the algorithm.* Lets tests advance time deterministically with `FakeClock` instead of `time.sleep()`, which makes refill tests fast and non-flaky.
- *`time.monotonic` for durations, never `time.time`.* NTP can slew the wall clock backward mid-request and leave you with negative elapsed times; monotonic clocks can't.
- *Token Bucket has no boundary moment.* Unlike Fixed Window where a client can fire `2 * limit` in 2ms across the window edge, token bucket is a continuous function — there's no "reset event" to abuse.
- *Smoke vs. burst is a test-design separation.* Harness liveness ("the gateway responds, observability works") and feature behavior ("the limit fires exactly N times") are different assertions. Mixing them creates flaky tests.

**What's next:** Lab 02 — scale the gateway to 3 replicas behind a load balancer. **Watch the per-replica in-memory bucket leak the global limit** — a single client gets ~3× their nominal allowance because each replica thinks it's the only one. This is what motivates moving state to Redis in lab 03.

---

## Lab 02 — Multi-replica leak (2026-05-01)

**Files added:**
- `labs/00-setup/caddy/Caddyfile` — `reverse_proxy` with `dynamic a` upstreams (re-resolves the `gateway` A record every 1s via Docker DNS) and `lb_policy round_robin`. Per-request load balancing across replicas.

**Files changed:**
- `labs/00-setup/docker-compose.yml` — gateway gets `deploy.replicas: 3` and loses its `container_name`/`ports` (multiple instances can't share either). New `caddy` service publishes on host port `${GATEWAY_PORT}` as the user-facing entry. Project name renamed from `ratelimiter-lab00` → `ratelimiter` (was lab-numbered, now timeless). All container names stripped of "lab02" prefix.
- `RateLimiter/shared/observability/prometheus/prometheus.yml` — replaced `static_configs` with `dns_sd_configs` (`type: A, name: gateway, port: 8000, refresh_interval: 5s`). Each replica now appears as a distinct scrape target with its own `instance` label.
- `labs/00-setup/k6/burst.js` — flipped thresholds. Was `count==10` allowed; now `count≥15, count≤40`. The OLD assertion failing IS the new property of the lab.
- `labs/00-setup/verify.sh` — Prometheus assertions now use `sum()` across instances (because each replica has its own counter); query construction switched to `curl -G --data-urlencode` to handle the inner quotes in `up{job="gateway"}` correctly.

**Property proved:** With 3 gateway replicas behind a round-robin Caddy LB, a single client firing 50 requests against the same `client_id` gets **30 allowed and 20 denied** instead of the configured 10/40 — exactly 3× the limit. This is observable three ways: (1) k6 status-code histogram, (2) HTTP `X-RateLimit-*` headers (each replica still claims `Limit: 10, Remaining: <its-own-count>`), (3) Prometheus `sum(gateway_ratelimit_allowed_total)` across the 3 `instance` labels.

**Staff+ talking points unlocked:**
- *In-memory state can't enforce a global limit.* The token-bucket algorithm is correct; the topology is wrong. Each replica observes only ~1/N of the traffic for any given client and over-allows by a factor of N. This generalizes to any "per-X" counter where X-traffic is sharded across replicas.
- *Per-instance metric labeling matters.* Without `dns_sd_configs` giving each replica its own `instance` label, Prometheus would see one time series jumping between replica counts and complain about counter resets (looks non-monotonic). The fix is structural: per-replica labels + sum-at-query-time, not per-replica metric names.
- *DNS-based service discovery beats static target lists.* As replicas scale up/down, `dns_sd` discovers them automatically. Same pattern for Caddy's `dynamic a` upstreams. In a Kubernetes-y environment you'd use SRV records or the Prometheus Operator's `ServiceMonitor`.
- *The "fail open" question shows up here too.* Right now if Caddy can't reach a replica, it returns 502 — fail-closed-ish. We accept that for a rate-limiter lab; in real life you'd configure `lb_try_duration` or a `passive_health_checks` block to retry on the next replica. (Caddy briefly returned 502s during the verify run when DNS hadn't refreshed yet — fixed by the second run, but worth noting that LBs need warm-up windows.)
- *The fix is centralized state, not a bigger LB.* You can't out-engineer this with stickier load balancing — sticky sessions only mask the problem until a replica dies. The solution is Redis (lab 03), where every replica reads from one source of truth.

**What's next:** Lab 03 — replace the in-memory bucket with **Redis-backed state** (single Redis instance, plain `HMGET`/`HSET` + `MULTI/EXEC`). The leak from lab 02 disappears. Then we'll deliberately introduce a TOCTOU race condition by firing concurrent requests, prove it's there with a chaos test, and use lab 04 to fix it with a Lua script.


