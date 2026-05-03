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
