# OnlineAuction — JOURNAL

Time machine: `git checkout OnlineAuction/lab-NN`.

## Lab 00 — Setup (2026-05-02)

- **Stack**: api×3 (FastAPI + aioboto3) behind Caddy, `amazon/dynamodb-local`, Prom + Grafana, k6.
- **Endpoints**: `POST /items`, `GET /items/{id}`, `POST /items/{id}/bids` (naive — no race protection; sets up lab 1).
- **DDB**: tables `items` (pk: item_id) and `bids` (pk: item_id, sk: bid_id) auto-created on api lifespan startup.
- **Property**: `make verify` green — create item, bid, get back; smoke test passes.
- **Next**: lab 01 — k6 chaos against the naive bid endpoint to demonstrate the race.

## Lab 01 — Bid race demo (2026-05-02)

- **Added**: `k6/race.js` (30 VUs × 1 iter, all bid $100 on one item). Threshold `count(201)>=2` proves multi-winner.
- **Verified**: 24/30 got 201 against the same item — bids ledger contains many "accepted" $100 bids.
- **Property**: invariant "if status=201 you're the unique winner" demonstrably broken.
- **Next**: lab 02 — collapse `GetItem → check → UpdateItem` into one `UpdateItem ConditionExpression` so exactly one writer wins.

## Lab 02 — Conditional-write fix (2026-05-02)

- **Changed**: `place_bid` collapsed to one `UpdateItem` with `ConditionExpression="attribute_exists(item_id) AND (attribute_not_exists(current_high_bid) OR current_high_bid < :a)"`. Caught `ConditionalCheckFailedException` → 409. Removed the GetItem + Python check (dead code under conditional writes).
- **Test inverted**: `k6/race.js` thresholds flipped to `endpoint:bid,status:201 == 1` and `endpoint:bid,status:409 == 29`; tagged the bid POST with `endpoint:bid` so setup's item-create 201 doesn't pollute the count.
- **Verified**: 30 concurrent equal-amount bids → exactly 1× 201, 29× 409. Invariant restored without taking a lock.
- **Talking points**: (1) DynamoDB conditional writes are atomic per partition — no `TransactWriteItems` needed for single-item invariants, half the WCU. (2) Strict `<` rejects ties; first-arriving equal bid wins. (3) Orphan bid-ledger rows on conflict are an audit trail; lab 5 sweeps them at close.
- **Next**: lab 03 — Redis pub/sub + SSE so watchers get the new high-bid event <200ms after commit.

## Lab 03 — Redis pub/sub + SSE fanout (2026-05-02)

- **Added**: `redis:7-alpine` to compose; `redis==5.1.1` to api deps; `app/redis_client.py` (one pool per process via `redis.asyncio`). `place_bid` publishes `{amount,bidder,ts}` to `auction:<id>` after the conditional update wins (best-effort — Redis down ≠ failed bid; DDB is source of truth). New SSE route `GET /v1/items/{id}/stream` + `_event_stream` async generator (user-implemented per TDD).
- **Test contract**: `tests/test_stream.py` (4 tests, fake pubsub) covers frame format, channel name, cleanup on disconnect, and ordering. Latency probe `api/scripts/sse_latency.py` (httpx async stream) measures end-to-end p95 from a host process.
- **Verified**: 4/4 unit tests green; 5 bids → 5 SSE frames received, p95 **4.8ms** (budget 200ms).
- **Talking points**: (1) Polling `get_message(timeout=1.0)` over `pubsub.listen()` lets the handler check `request.is_disconnected()` between polls — without it, every closed tab leaks a Redis subscriber. (2) Cleanup belongs in `finally`, not `except` — only `finally` runs on `GeneratorExit` (the disconnect path FastAPI takes). (3) Pub/sub is fire-and-forget: a watcher that disconnects mid-bid loses that event. For replay-on-reconnect you'd reach for Redis Streams or DDB Streams + cursor — out of scope here. (4) Best-effort publish keeps writes unblocked when Redis is the live channel only; treating it as required would couple write-path latency to a non-source-of-truth.
- **Next**: lab 04 — read-through Redis cache for `GET /items/{id}` (write-invalidate on bid commit), assert p95 read latency drop and cache hit ratio >90%.

## Lab 04 — Look-aside cache + write-invalidate (2026-05-02)

- **Added**: `app/cache.py` (USER-implemented `get_or_load_item` + `invalidate_item`) wired into `GET /v1/items/{id}` and the bid-commit path. `_item_from_ddb` moved to `ddb.unwrap_item` to avoid circular imports between `main` and `cache`. Prometheus counters `cache_hits_total` / `cache_misses_total`. Best-effort `invalidate` lives in the same try/except as `publish` — Redis outage doesn't fail bids; TTL=60s is the safety net for any missed invalidate.
- **Test contract**: `tests/test_cache.py` (5 tests, fake Redis + fake DDB) covers hit / miss-loads-and-populates / 404-doesn't-cache / invalidate-deletes / invalidate-idempotent. `k6/cache_load.js` does 20 reader VUs + 1 bidder VU for 5s; teardown scrapes `/metrics` (per-replica counters approximate global ratio because Caddy LB is uniform and Redis state is shared).
- **Verified**: 5/5 tests green. Read load: 71k requests in 5s, **p95 = 2.45ms** (budget 5ms), **hit ratio 99.81%** (budget 95%), 0 failures.
- **Talking points**: (1) DEL-on-write over write-through `SET`: under concurrent winning bids, two replicas could `SET` the cache in the wrong order and pin a stale state — DEL forces the next read through DDB which already serialized them via the ConditionExpression. (2) TTL is belt-and-suspenders — bounds staleness to 60s even when an invalidate silently fails (the `try: ... except: pass` around publish/invalidate eats Redis outages). (3) Don't cache 404s — would mask later item creation; let them go to DDB every time. (4) Sampling `/metrics` from one replica is honest only when load is uniformly distributed AND cache state is shared (both true here); otherwise scrape Prometheus' aggregated `sum()`.
- **Next**: lab 05 — exactly-once auction close. 3 replicas race to claim ending auctions via `UpdateItem ConditionExpression="attribute_not_exists(closed_by)"`; chaos test asserts `auctions_closed_total` sums to exactly the number of ending auctions.
