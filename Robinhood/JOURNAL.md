# Robinhood — JOURNAL

Time machine: `git checkout Robinhood/lab-NN`.

## Lab 00 — Setup + happy-path order placement (2026-05-03)

- **Stack**: api×3 (FastAPI + aioboto3 + httpx) behind Caddy, `mock-exchange` (separate Docker service, in-memory state, idempotent on `client_order_id`), `amazon/dynamodb-local`, Prom + Grafana, k6.
- **DDB**: `orders` (PK=user_id, SK=order_id) with 3 GSIs ready (G1 by-external-order-id, G2 sparse pending-by-status, G3 by-client-order-id) and `symbols` (PK=symbol). G1+G2 sit empty until labs 2+3.
- **Endpoints**: `POST /v1/orders` (Idempotency-Key → client_order_id; PutItem PENDING → POST exchange → UpdateItem SUBMITTED), `DELETE /v1/orders/{id}`, `GET /v1/orders` cursor-paginated.
- **Verified**: `make verify` green — order returns SUBMITTED + external_order_id; same Idempotency-Key replays return same external_order_id and mock-exchange `/debug/orders` shows count=1.
- **Next**: lab 01 — live price fanout via Redis pub/sub + per-replica `symbol→set<conn>` map; mock-exchange gains an SSE trade feed.
