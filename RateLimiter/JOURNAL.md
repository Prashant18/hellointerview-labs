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

---

## Lab 03 — Redis-backed Token Bucket (naive HMGET/HSET) (2026-05-02)

**Files added:**
- `labs/00-setup/gateway/app/redis_tokenbucket.py` — `RedisTokenBucket` class. Naive HMGET → compute → HSET inside a MULTI/EXEC pipeline. Uses wall-clock (`time.time`) so all replicas agree on "now". Lazy-init for unseen clients (full bucket, `last_refill = now`); 1-hour TTL on idle keys.
- `labs/00-setup/k6/race.js` — chaos test: 20 VUs × 100 iterations against ONE client_id with `capacity=10`, `refill_per_sec=1`. Asserts `count(status=200) >= 11` — any overshoot proves the TOCTOU race.

**Files changed:**
- `labs/00-setup/docker-compose.yml` — adds `redis:7-alpine` service with healthcheck; gateway gains `REDIS_URL=redis://redis:6379/0` env var and `depends_on: { redis: service_healthy }`.
- `labs/00-setup/gateway/pyproject.toml` — adds `redis==5.1.1`; configures `[tool.basedpyright] typeCheckingMode = "standard"` to silence redis-py's `ResponseT` typing-stub limitation that trips strict pyright on every `await`.
- `labs/00-setup/gateway/app/main.py` — FastAPI `lifespan` now opens a Redis connection pool on startup, instantiates `RedisTokenBucket`, exposes it on `app.state.bucket`, awaits `bucket.allow()` from `/v1/check`, and closes the pool on shutdown.
- `labs/00-setup/k6/burst.js` — flipped thresholds back to `count==10/40` (centralized state means the lab 02 leak is fixed; this is now a regression assertion).
- `labs/00-setup/verify.sh` — adds a "race" stage; computes before/after Prometheus deltas of `sum(gateway_ratelimit_allowed_total)` for both burst (must be exactly 10) and race (must overshoot >10). 10-request LB warmup before assertions to avoid Caddy stale-DNS jitter after compose recreates containers.
- `labs/00-setup/Makefile` — adds `make race` and `make redis-cli` targets.

**Property proved:** With Redis as the single source of truth for bucket state, the lab 02 per-replica leak is GONE — `burst.js` (50 sequential reqs from one client, capacity=10) gives exactly 10 allowed and 40 denied. **But** under concurrency, `race.js` (20 VUs × 100 reqs against the same client) lets 40–70+ requests through for a configured capacity of 10. The TOCTOU race between HMGET and HSET lets multiple in-flight requests each observe the same token count and each decide "allow" before any write lands.

**Staff+ talking points unlocked:**
- *Centralizing state isn't enough; you also have to make the read-modify-write atomic.* Redis `MULTI/EXEC` makes a sequence of *writes* atomic with respect to each other. It does NOT close the gap between an earlier HMGET and the EXEC. Senior+ candidates often stop at "we use Redis"; staff+ candidates immediately ask "where's the read in this picture, and what happens between read and write?"
- *Wall clock vs. monotonic clock for distributed counters.* Monotonic is per-process — replica A's "100s" is meaningless to replica B. For shared state, you need wall clock (NTP-synced, "good enough") or — better — a clock that lives next to the state, like `redis.call('TIME')` inside a Lua script. Lab 04 will switch to that.
- *Lazy creation + TTL is your memory bound.* Don't pre-create per-client state. Initialize on first request (`HMGET → [None, None] → full bucket`). Set `EXPIRE` on every write so abandoned clients evict in 1 hour. Without TTL, you pay forever for a one-shot user. Same pattern as Stripe/Cloudflare-style limiters in production.
- *redis-py's `ResponseT` is a typing trap.* `ResponseT = Union[Awaitable[T], T]` collapses to `T` under strict pyright/basedpyright, which then complains about `await`. Pragmatic fix: relax `typeCheckingMode = "standard"`. Real fix: wait for redis-py's overload-typed async client (in flight).
- *FastAPI `lifespan` is the right home for connection pools.* Don't open Redis connections per-request (TCP handshake = death at scale). Open one pool per process at startup, share via `app.state`, close on shutdown. Same shape for any backing store.

**The "I broke it once" debugging story:** First implementation passed `burst` (10/40) but I'd written `pipe.hset(... "last_refill": last_refill ...)` — preserving the OLD timestamp instead of writing `now`. Result: every subsequent request saw an ever-growing `elapsed` window since the original `last_refill`, and one extra request would slip through (11/39). The lesson: writing the algorithm correctly is half the job; *committing the new state* — including the new clock — is the other half.

**What's next:** Lab 04 — replace HMGET/HSET with a **Lua script** that executes inside Redis. The script reads, computes, writes, and returns the decision in one atomic step. `race.js`'s assertion flips from "must overshoot" to "must give exactly 10". Same chaos load, fixed implementation.

---

## Lab 04 — Lua atomic Token Bucket (2026-05-02)

**Files added:**
- `labs/00-setup/gateway/scripts/tokenbucket.lua` — the entire allow() algorithm as a single Lua script. Reads via `HMGET`, computes `now` from `redis.call('TIME')` (clock lives next to state — no NTP dependency, no per-replica drift), lazy-inits new clients, applies refill capped at capacity, decides without pushing tokens negative, writes back via `HSET` + `EXPIRE`, returns `{allowed, floor(tokens), tostring(reset_after)}`.
- `labs/00-setup/gateway/app/lua_tokenbucket.py` — Python wrapper. Loads the script via `redis_client.register_script()` (which gives us EVALSHA caching + NOSCRIPT auto-recovery for free), exposes `allow(client_id) → Decision`. ~30 lines, pure glue.

**Files changed:**
- `labs/00-setup/gateway/Dockerfile` — adds `COPY scripts ./scripts` so the Lua file is in the image.
- `labs/00-setup/gateway/app/main.py` — new `BUCKET_BACKEND` env var (default: `lua`); lifespan instantiates either `LuaTokenBucket` or `RedisTokenBucket` so you can A/B compare the racy and atomic implementations without code changes. Version bumped to `lab04`.
- `labs/00-setup/k6/race.js` — thresholds flipped from `count>=11` (overshoot proof) to `count==10/==90` (atomicity proof). Also fixed a sneaky test bug: was using `Date.now()` at the top-level init context, which runs once per VU and gave each VU a slightly different timestamp → 3 distinct client_ids → 3 distinct buckets → fake-overshoot of 30 even with atomic Lua. Now uses k6's `setup()` function which runs ONCE before any VU starts and shares its return value to every iteration.
- `labs/00-setup/verify.sh` — flipped race assertion from "expect overshoot" to "expect EXACTLY 10".

**Property proved:** Same chaos load as lab 03 (20 VUs × 100 concurrent requests against one client_id, capacity=10, refill=1/s) now allows EXACTLY 10 and denies EXACTLY 90 — every single time, no jitter. Burst regression (10/40 sequential) still passes. The TOCTOU race window from lab 03 is closed because the read, decide, and write all happen inside Redis as one atomic unit.

**Staff+ talking points unlocked:**
- *Lua scripts in Redis are atomic by construction.* Redis is single-threaded; while a script is running, no other client's commands execute. That's the entire mechanism that closes the read-modify-write window. "Atomic" here is the strongest possible meaning — no interleaving, no order ambiguity, no reordering.
- *`register_script()` is the right pattern, not raw `eval()`.* It hashes the body once, calls EVALSHA on every invocation (saves bandwidth), and transparently re-uploads via EVAL when Redis returns NOSCRIPT (after a `SCRIPT FLUSH` or restart). Treat the Script object as the unit of compilation.
- *Put the clock next to the state.* `redis.call('TIME')` inside the script means all replicas see the same "now" — there is no clock drift between replicas, no NTP dependency, no last_refill timestamps from one replica being misinterpreted by another. This is a strictly better answer than the wall-clock approach lab 03 used.
- *Redis Lua return type quirks bite you.* Lua `true` → RESP integer 1, but Lua `false` → RESP nil (which becomes `None` in Python and crashes `int(None)`). Always return explicit `0`/`1` integers. Lua numbers truncate to integers on return — wrap floats in `tostring()`. Lua tables → Python lists.
- *HMGET returns Lua `false`, not `nil`, for missing fields.* But `tonumber(false)` and `tonumber(nil)` both return `nil`, so the `if not x` lazy-init pattern handles both cleanly.

**The "I broke it twice" debugging story (worth telling in interviews):** First impl had three lingering bugs from the typed-out Lua: `tokens > 0` instead of `tokens >= 1` (subtle off-by-fraction), `(tokens - capacity)` sign-flip on `reset_after`, and `(1 - capacity)` instead of `(1 - tokens)` in the deny path. Once the script was right, the test still showed "30 allowed" — exactly 3× the limit, identical to the lab 02 in-memory leak pattern. Two days lost would have happened here if I'd assumed the script was wrong; instead I dumped Redis state with `redis-cli KEYS 'bucket:*'` and discovered the test was creating THREE different buckets because k6's init-context `Date.now()` runs once per VU. Moved client_id derivation into `setup()`, race went to exactly 10. **Lesson: when overshoot mirrors a structural number (3× = N replicas, 20× = N VUs), suspect a test-side cardinality bug before suspecting the algorithm.**

**What's next:** Lab 05 — single-Redis becomes the bottleneck at scale. Replace the single `redis:7-alpine` instance with **Redis Cluster** (3 master + 3 replica nodes). The Lua script keeps working with zero changes — Redis Cluster ensures all keys for a given hash slot land on the same node, so atomicity is preserved. We'll add a sharding load test that distributes ~50k req/s across many client_ids and shows the load spread across master nodes in Grafana.

---

## Lab 05 — Redis Cluster sharding (2026-05-02)

**Files added:**
- (none — purely a topology change; the algorithm code is unchanged.)

**Files changed:**
- `labs/00-setup/docker-compose.yml` — single `redis` service replaced with **6 nodes**: `redis-1` … `redis-6`. Each runs `redis-server --cluster-enabled yes --cluster-config-file /data/nodes.conf --cluster-node-timeout 5000 --cluster-announce-hostname redis-N --cluster-preferred-endpoint-type hostname`. New one-shot `redis-cluster-init` service waits for all six to be healthy then runs `redis-cli --cluster create redis-1:6379 ... redis-6:6379 --cluster-replicas 1 --cluster-yes`. The init container is idempotent (skips create if `cluster_state==ok`).
- `labs/00-setup/gateway/app/main.py` — `lifespan` now picks up `REDIS_CLUSTER_NODES` (comma-separated seed list). When set, instantiates `redis.asyncio.cluster.RedisCluster(startup_nodes=[...], require_full_coverage=False)`. Single-node `REDIS_URL` path retained as a fallback for local non-cluster dev. Version bumped to `lab05`.
- `labs/00-setup/gateway/Dockerfile` — unchanged (the `scripts/` dir was already copied in lab 04).
- `labs/00-setup/Makefile` — `make redis-cli` now opens `redis-cli -c` against `redis-1` (the `-c` flag follows MOVED/ASK redirects across the cluster). Added `make cluster-check` to dump topology + slot coverage via `redis-cli --cluster check`.

**Property proved:** Existing `burst.js` (10/40 sequential) and `race.js` (10/90 concurrent under one client_id) both pass on the cluster with **zero test changes**. The smoke test's 500+ unique client_ids distribute cleanly across all three masters — `make cluster-check` after a verify run reports `503 keys in 3 masters` with all 16384 slots covered.

**Staff+ talking points unlocked:**
- *Redis Cluster preserves the per-key atomicity guarantee.* Hash slots are computed by `CRC16(key) mod 16384`. Each slot is owned by exactly one master at any moment. Any Lua script touching ONE key (or a `{tag}`-grouped set of keys) runs on the master that owns its slot, atomically, with no other client able to interleave. Translation: if your atomic primitive worked on a single Redis, it works on a Redis Cluster — *for single-key operations*. Multi-key transactions across slots break.
- *No code change for the algorithm; one config change for the client.* The Python switch is `Redis.from_url(...)` → `RedisCluster(startup_nodes=[...])`. The cluster client discovers the topology from the seed list and refreshes its slot map on `MOVED`. From the application's perspective, atomicity is unchanged.
- *Hostname-based cluster announce beats IPs in Docker.* `--cluster-announce-hostname redis-1 --cluster-preferred-endpoint-type hostname` makes nodes advertise their service hostname. Containers come up with different IPs every restart; with hostnames, the cluster topology survives `docker compose up -d` cleanly. Without this, you'd see "Could not connect to <stale-ip>" on every restart.
- *Throughput math: 3× the headroom, NOT 3× the per-client limit.* Each master handles ~100k ops/sec for hash operations. 3 masters → ~300k ops/sec aggregate. Our rate-limit check is 1 EVALSHA per request; effective ceiling ≈ 300k req/sec. **A single client's traffic still lands on ONE master**, so per-key throughput is unchanged — that's the hot-key problem (lab 08's territory).
- *`require_full_coverage=False` is preparation for failover.* During a master failure, the slot it owned is briefly unreachable until a replica is promoted (~5–10s). With `require_full_coverage=True` (the default), the entire client refuses to make ANY request until coverage is restored. With `False`, requests targeting other slots still succeed; only requests targeting the failing slot's keys 503. We'll exploit this in lab 06 to demonstrate fail-closed semantics with a small blast radius instead of a total outage.
- *Idempotent cluster init is the boring detail that ships you on Friday.* `redis-cli --cluster create` errors with "Node is not empty" if you re-run against a formed cluster. The init container checks `cluster_state` first and skips create when already `ok`. This means `make verify` is rerunnable without `docker compose down -v` in between — important for an iteration loop.

**What's next:** Lab 06 — chaos engineering. We `docker kill` a master mid-traffic and observe (a) the failover gap (how long until a replica is promoted and the cluster reaches `cluster_state:ok` again), (b) what the gateway actually returns to clients during that gap (timeouts? connection refused? a graceful 503?), and (c) implement the `fail-closed` policy from the HelloInterview source so that gateway returns 503 to keep load off downstream services. The chaos test (`chaos/failover.sh`) is a TDD task for the user.

---

## Wrap-up — RateLimiter complete at lab 05 (2026-05-02)

The architecture story for the rate limiter is complete at lab 05. Every major design lever — algorithm, distribution, atomicity, sharding — is in place and provably exercised by the verify pipeline. We chose to stop here rather than slog through the production-ops labs (06 chaos, 07 hot-keys, 08 dynamic config, 09 integration recap), which are valuable but bounded in interview value relative to time invested.

**Bonus committed alongside the wrap-up (without a `lab-06` tag, since the chaos test wasn't built):**
- Gateway-side `fail-closed` wiring — `asyncio.wait_for(bucket.allow, 100ms)` + try/except → `503` with `Retry-After: 1`. New `RL_FAILMODE` counter labeled by reason (`timeout` / `connection` / `redis_error`).
- New `/ready` endpoint — pings Redis, returns 503 when unreachable so the LB can drain. Distinct from `/health` which is liveness only.
- Configurable `RL_TIMEOUT_SECONDS` env var (default 100ms) and `socket_timeout` on the RedisCluster client matching it.
- Bonus smoke-tested live: paused all six Redis nodes mid-traffic, observed exactly the right behavior — 503s with the proper headers, `gateway_ratelimit_failmode_total{reason="timeout"}` incremented, instant recovery on unpause.
- Skeleton at `labs/00-setup/chaos/failover.sh` with 6 TODO blocks, hints, and the expected output format. A future session can implement it as `make chaos-failover` and add the `lab-06` tag.

**Ten cold-question rehearsals + the full pattern table live in `RateLimiter/RECAP.md`.** That's the interview cheat-sheet — if anything in there is fuzzy, redo the relevant lab.

**Total commits:** 7 (initial + lab 03 + lab 04 + lab 05 + this wrap-up). Tags: `RateLimiter/lab-{00,01,02,03,04,05}`. Wrap-up is intentionally untagged.

Onto the next problem.


