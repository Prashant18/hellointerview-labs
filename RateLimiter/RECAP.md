# RateLimiter — Recap

Built across 5 labs (of 10 originally planned). Lab 05 was the natural stopping point — every *architectural* lever is in place (algorithm choice, distributed state, atomicity, sharding). The remaining labs (06–09) cover production ops (failover/fail-closed, hot-key auto-blocking, dynamic rule config, integration scenario). Lab 06's gateway-side fail-closed wiring landed as a bonus; the chaos test that would have proved it end-to-end is scaffolded in `labs/00-setup/chaos/failover.sh` for future work.

## What you actually built

| File | What it is |
|------|-----------|
| `gateway/app/tokenbucket.py` | In-memory `TokenBucket` (lab 01) — algorithm + thread-safety primer |
| `gateway/app/redis_tokenbucket.py` | Redis-backed naive `RedisTokenBucket` (lab 03) — TOCTOU-racy by construction |
| `gateway/app/lua_tokenbucket.py` | `LuaTokenBucket` (lab 04) — Python wrapper over EVALSHA |
| `gateway/scripts/tokenbucket.lua` | The atomic algorithm running inside Redis (lab 04) |
| `gateway/app/main.py` | FastAPI gateway with `BUCKET_BACKEND` switch + fail-closed (lab 06 partial) |
| `caddy/Caddyfile` | Round-robin LB across gateway replicas (lab 02) |
| `docker-compose.yml` | 3 gateway replicas + 6-node Redis Cluster + Prom + Grafana + k6 |
| `gateway/tests/test_tokenbucket.py` | 11 pytest cases pinning the algorithm |
| `k6/{smoke,burst,race}.js` | Load tests pinning every property the labs introduced |
| `verify.sh` | One command — pytest, smoke, burst, race, Prometheus assertions |

## Patterns earned

| Pattern | Lab | What you can say cold |
|---------|-----|----------------------|
| Token Bucket vs. Fixed/Sliding Window | 01 | "Burst capacity and sustained rate as two separate knobs in one rule" |
| Per-replica state leaks the global limit | 02 | "N replicas each enforcing capacity = N× the limit globally" |
| Centralized state via Redis | 03 | "One source of truth across replicas; per-key" |
| TOCTOU race in read-then-write | 03 | "MULTI/EXEC makes writes atomic, NOT the gap to the read" |
| Lua atomicity in Redis | 04 | "Single-threaded server; while a script runs nothing else does" |
| Clock co-located with state | 04 | "`redis.call('TIME')` is strictly better than NTP-synced wall clocks" |
| Hash-slot sharding (Redis Cluster) | 05 | "Per-key atomicity scales for free; cross-slot transactions don't" |
| Fail-closed circuit breaker | 06 | "Bound per-call latency; on failure, 503 + Retry-After to keep load off downstream" |
| Liveness vs. readiness probes | 06 | "/health = process alive; /ready = can serve real traffic" |
| `register_script()` for EVALSHA caching | 04 | "SHA cached client-side; auto-recover from NOSCRIPT" |
| Cardinality discipline in Prometheus labels | 00 | "Bounded sets only — never client_id, never un-templated paths" |
| `time.perf_counter` for durations | 00 | "Monotonic; wall clock can move backward via NTP" |
| FastAPI `lifespan` for connection pools | 03 | "One pool per process; never per-request" |
| Inject the clock for testability | 01 | "FakeClock advances on demand; tests run in 10ms not 30s" |

## Ten cold-question rehearsals

When an interviewer probes any of these, you should have a complete answer in under 60 seconds:

1. **Walk me through a request from the edge.**
   Caddy LB picks a gateway replica → middleware times the request and records `gateway_requests_total{path,status}` → `_client_id_from(request)` reads `x-client-id` / `x-api-key` / IP → `asyncio.wait_for(bucket.allow(client_id), 100ms)` → `LuaTokenBucket` calls EVALSHA against the cluster (key hashed via CRC16 to one master) → script atomically reads `tokens`/`last_refill`, computes `now` from `redis.call('TIME')`, applies refill capped at capacity, decides, writes back, EXPIREs the key → returns `{allowed, floor(tokens), tostring(reset_after)}` → gateway returns 200 (allow) or 429 (deny) with `X-RateLimit-{Limit,Remaining,Reset}` headers (and `Retry-After` on 429), or 503 if Redis times out.

2. **Why Token Bucket?** Burst + sustained as separate knobs in one rule; no boundary moment for clients to game.

3. **Why was the in-memory version broken at scale?** Per-replica state. Each gateway saw only its slice of any client's traffic and over-allowed by N.

4. **Why didn't MULTI/EXEC fix the Redis-backed version?** It made the *writes* atomic with respect to each other. The race window was the gap between the prior `HMGET` and the `EXEC`. Two concurrent requests could both `HMGET` the same `tokens=1`, both decide allow, both write — neither saw the other.

5. **Why does Lua close it?** Redis is single-threaded. While a script runs no other client's commands execute. The whole read-modify-write happens atomically inside the script.

6. **Wall clock vs. monotonic vs. Redis TIME?** Monotonic is per-process — useless across replicas. Wall clock + NTP works but drifts. `redis.call('TIME')` is the cleanest answer because the clock and the state are in the same process.

7. **How does this scale to 1M req/sec?** Redis Cluster: 16384 hash slots routed by CRC16. Each master ~100k ops/sec; 10 masters → ~1M req/sec aggregate. Per-client throughput still bound by single-master throughput because hash-slot routing is deterministic — that's the **hot-key problem** (would have been lab 08).

8. **What happens when a Redis master dies?** Cluster auto-promotes a replica in ~5–10s. During the gap, requests targeting that master's slots time out at the gateway's 100ms budget; we return 503 + `Retry-After: 1`. Other slots keep working because we built the client with `require_full_coverage=False`. `/ready` returns 503 so the LB drains.

9. **What's `RL_FAILMODE` for?** It's the canary metric. `rate(failmode[1m]) > 0` for 2 min should page on-call. Pre-Lua, you'd see it during cascading slow Redis. Post-Lua, you see it during real Redis trouble.

10. **What would you do differently in production?** Tighter timeout based on measured p99; per-tier limits via auth context; client-side rate-limit-header consumption to smooth traffic; per-IP limits at the LB *in addition to* per-user at the gateway; an opt-in pre-flight `/v1/limit?probe=true` for clients that need to know their headroom; structured logs correlating gateway request id ↔ Redis script load events.

## Where to time-travel

Each lab's tag in `git tag -l 'RateLimiter/*'` checks out the full state at lab boundary:

```bash
git checkout RateLimiter/lab-00   # initial harness only
git checkout RateLimiter/lab-01   # in-memory token bucket
git checkout RateLimiter/lab-02   # 3-replica leak demonstrated
git checkout RateLimiter/lab-03   # Redis-backed, race exposed
git checkout RateLimiter/lab-04   # Lua atomic, race fixed
git checkout RateLimiter/lab-05   # Redis Cluster sharding (CURRENT)
git checkout main                 # back to head
```

## What's deferred (and why it's fine)

- **Lab 06 chaos test** — skeleton in `chaos/failover.sh`, gateway-side fail-closed wiring committed. Implementing the script is `make chaos-failover` waiting for a future session. Talking points stand on their own.
- **Lab 07 hot-key blocklist** — would add a Redis-backed `blocked:<client_id>` set checked before the bucket, with auto-add on N-deny. Architecturally a layered defense. Skipped.
- **Lab 08 dynamic rules via etcd** — `etcd3` watch + in-memory rule cache with sub-1s propagation. Skipped.
- **Lab 09 integration recap** — would compose all of the above into one `make full-scenario`. Skipped.

The architecture story is **complete at lab 05**. The remaining labs are *ops* polish — important in production, optional for an interview narrative.

## License & author

MIT. Built by Prashant Patel ([Prashant18](https://github.com/Prashant18)) with Claude Code as a tutoring partner.
