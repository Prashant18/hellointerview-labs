# Robinhood — staff-level lab plan (broker, NOT exchange)

We're building a **broker** that routes orders to an external exchange and re-publishes the exchange's trade feed to UI clients. The matching engine, order book, fills, settlement, and price discovery all live INSIDE the (mocked) exchange. Our system tracks order state, dedupes, and fans out prices.

## Functional (per problem.txt)

- Users see live prices of stocks (one or many at once).
- Users place / cancel orders (market, limit). List own orders.

## Non-functional

- Order placement p99 < 200ms.
- High consistency for order state — every PENDING converges to a terminal status (SUBMITTED/FILLED/REJECTED/CANCELED) even under failures.
- **Minimize active connections to the external exchange** (THE staff+ constraint — exchange feeds are expensive).
- Tick → SSE-client p95 < 100ms.
- Scale: 20M DAU × 5 trades/day; 1000s of symbols.

## Out of scope (DO NOT BUILD — was wrongly in the prior draft)

Matching engine, order book, cash balances, deposits/withdrawals, positions, portfolio, P&L, mark-to-market, ledger, pre-trade risk, `TransactWriteItems` settlement, market-hours, options/crypto/ETFs, historical data, multi-exchange routing, fees/PFOF.

## Services (Docker)

| Service | Role |
|---|---|
| `caddy` | LB, sticky-session for SSE. Reuse OnlineAuction pattern. |
| `api` (×3) | FastAPI. Order REST + per-replica SSE for live prices + per-user SSE for order updates. Holds in-process `symbol → set<conn_id>` map. |
| `mock-exchange` (×1) | Standalone FastAPI service. Sync REST `POST /exchange/orders`, `DELETE /exchange/orders/{eid}`, `GET /exchange/orders/by-client/{cid}` (idempotent on `clientOrderId`). Async SSE `GET /exchange/feed?symbols=...` emits trade ticks (random-walk pricer + resting-limit fills). Failure injection knobs via env (timeout %, 5xx %, drop %). |
| `trade-processor` (×1) | THE ONLY service that opens connections to `mock-exchange`. Holds ONE upstream SSE feed; `PUBLISH quotes:{symbol}` to Redis on each tick; for trades carrying `externalOrderId`, looks up via DDB GSI and `UpdateItem` order row. Manages upstream subscription set via Redis control channel (`_control/symbol-needed` / `_control/symbol-released`) — refcount-based lazy add/drop. |
| `cleanup-worker` (×1) | Polls every 5s. Scans orders in PENDING/PENDING_CANCEL older than threshold via status-GSI; reconciles via `GET /exchange/orders/by-client/{cid}`. |
| `redis` | Pub/sub + control channels. Reuse OnlineAuction pattern. |
| `dynamodb-local` | Order state. `-inMemory` (no persistence between restarts). |
| `prometheus` + `grafana` | Reuse OnlineAuction pattern. |
| `k6` | Load + chaos profiles. |

## DDB schema

| Table | PK | SK | GSIs | Notes |
|---|---|---|---|---|
| `orders` | `user_id` | `order_id` | **G1**: `external_order_id` (HASH), projected `(user_id, order_id)` — for trade-processor reverse lookup. **G2** sparse: `status` (HASH), `created_at` (RANGE) — for cleanup-worker scan of PENDING/PENDING_CANCEL only (status is REMOVEd from terminal rows to keep the GSI small). **G3**: `client_order_id` (HASH) — for idempotent retry on `POST /v1/orders`. | Attrs: `status` ∈ {PENDING, SUBMITTED, PARTIAL, FILLED, REJECTED, PENDING_CANCEL, CANCELED}, `external_order_id`, `client_order_id`, `position` (buy/sell), `symbol`, `price_cents`, `num_shares`, `num_filled`, `created_at`, `updated_at`. |
| `symbols` | `symbol` | — | — | Last-known price (cold-read seed for SSE clients on connect). Throttled write by trade-processor. |

No fills table, no positions, no users-with-balance.

## API surface (v1)

- `POST /v1/orders` (header `Idempotency-Key` → server uses as `client_order_id`) → 201 `{order_id, status}`.
- `DELETE /v1/orders/{id}` → 200 / 409.
- `GET /v1/orders` → cursor-paginated by `(user_id, order_id)`.
- `GET /v1/quotes/stream?symbols=AAPL,META` → SSE; emits `data: {symbol, price_cents, ts}\n\n`.
- `GET /v1/orders/stream` → SSE for the user's own order updates (lab 3).

## Lab plan (4 phases)

| # | Title | Adds | Property proved |
|---|---|---|---|
| 0 | Setup + happy-path order placement | api×3 + ddb-local + redis + caddy + obs + mock-exchange (sync place/cancel only). Naive `POST /v1/orders` → DDB(PENDING) → exchange call → DDB(SUBMITTED). `GET /v1/orders` paginated. | Smoke: place returns 201/SUBMITTED; row in DDB has `external_order_id`; mock-exchange `/debug/orders` lists it. |
| 1 | Live price fanout + per-symbol subscription mgmt | trade-processor opens ONE upstream SSE to mock-exchange, fans out to Redis `quotes:{symbol}`. api SSE endpoint with per-process `symbol → set<conn_id>` map; lazy Redis SUBSCRIBE on first user, UNSUBSCRIBE when set empties; refcount via `_control/*` channels drives trade-processor's upstream subscriptions. | k6: 200 SSE clients × 5 symbols across 3 api replicas. Asserts: (a) tick→client p95 < 100ms, (b) `mock-exchange /debug/feed-subscribers` shows exactly 5 upstream subscribers (NOT 200), (c) after teardown, `PUBSUB NUMSUB quotes:*` and upstream subscribers both drop to 0 within 2s. |
| 2 | Trade feed → order updates via GSI reverse lookup | trade-processor handles trade events: `Query orders.G1 by external_order_id` → `UpdateItem` `num_filled +=`, flip status to PARTIAL/FILLED at thresholds. Idempotent on `(external_order_id, exchange_trade_id)` via ConditionExpression on a `applied_trades` set attribute. | Probe: 50 orders; mock-exchange emits 1-3 fills per order plus duplicate replays. Asserts: every order's `num_filled == sum(unique trades)`, status correct, replaying the entire feed twice is a no-op. `trade_apply_total{result="duplicate"}` > 0. |
| 3 | Order consistency under failures (clientOrderId clean-up) | mock-exchange gains failure-injection knobs (timeout %, 5xx %). `POST /v1/orders` writes PENDING first, then calls exchange. cleanup-worker reconciles PENDING/PENDING_CANCEL via `GET /exchange/orders/by-client/{cid}`. Cancel flow: PENDING_CANCEL → exchange → CANCELED. | Chaos: 100 orders, 30% exchange timeouts + 30% post-submit DDB failures + idempotency-key retries. After 30s convergence: 0 stuck PENDING; `count(distinct external_order_id) == count(orders submitted)`; same Idempotency-Key retried = 1 exchange order, not 2. |

## TDD split per lab

- Lab 0: Claude writes everything (pure infra).
- Lab 1: USER implements the per-replica `symbol → set<conn_id>` map management (subscribe/unsubscribe, refcount control-channel publishes); Claude writes the SSE generator scaffolding + tests + k6 + trade-processor upstream multiplexer.
- Lab 2: USER implements `apply_trade(trade)` (GSI lookup → conditional UpdateItem with idempotent set-add); Claude writes tests + probe.
- Lab 3: USER implements the `cleanup_one_pending_order(order)` reconciliation logic; Claude writes the polling loop + tests + chaos probe + mock-exchange failure-injection wiring.

## Staff+ talking points unlocked

1. **Why one upstream connection per symbol, not per user**: 50k AAPL watchers see ONE exchange subscriber. The refcount-via-Redis-control-channel pattern is the implementation; the in-process `symbol → set<conn_id>` map is the user-side bookkeeping.
2. **`externalOrderId` GSI as the reverse-lookup pattern**: orders partition by `user_id` for fast `GET /orders`, but the trade feed only knows `externalOrderId` — GSI bridges that gap. (Problem suggests RocksDB; DDB GSI is the equivalent.)
3. **Sparse GSI on status**: only PENDING/PENDING_CANCEL rows live in the cleanup-scan GSI; REMOVE on terminal status drops them out, keeping the index O(in-flight) not O(history).
4. **Order state machine + clientOrderId reconciliation**: write PENDING BEFORE exchange call so a crash-between-write-and-call is recoverable; reconcile via the exchange's lookup-by-client-id endpoint.
5. **Idempotent trade application**: trade-processor may receive duplicate trade events (mock-exchange replay, network retry); ConditionExpression on `applied_trades` set-add makes apply exactly-once at the order row.
6. **Sticky sessions for SSE**: a user's SSE connection is bound to one api replica → the in-process subscriber map is consistent for the connection lifetime. Reconnect = rebuild on (possibly different) replica.
7. **Why SSE between trade-processor and mock-exchange (not webhook)**: pull semantics, single consumer, matches our shipped SSE generator pattern; in real production we'd use the exchange's webhook over a Kafka buffer.

## Tech stack

Python 3.12 + FastAPI + uv (carry-over). DDB Local. Redis. Caddy. Prom + Grafana. k6.

**Ports**: API **18021**, DDB Local **18022**, Mock-exchange **18023**, Redis **16382**, Prom **19093**, Grafana **13003**.

## Conventions

Single-folder evolution under `Robinhood/system/`. JOURNAL ≤8 lines/lab. No RECAP unless asked. Per-lab git loop: green verify → commit → tag `Robinhood/lab-NN` → push (Y/N). Bloat budget HARD per repo CLAUDE.md.
