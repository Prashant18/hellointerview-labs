# OnlineAuction — JOURNAL

Time machine: `git checkout OnlineAuction/lab-NN`.

## Lab 00 — Setup (2026-05-02)

- **Stack**: api×3 (FastAPI + aioboto3) behind Caddy, `amazon/dynamodb-local`, Prom + Grafana, k6.
- **Endpoints**: `POST /items`, `GET /items/{id}`, `POST /items/{id}/bids` (naive — no race protection; sets up lab 1).
- **DDB**: tables `items` (pk: item_id) and `bids` (pk: item_id, sk: bid_id) auto-created on api lifespan startup.
- **Property**: `make verify` green — create item, bid, get back; smoke test passes.
- **Next**: lab 01 — k6 chaos against the naive bid endpoint to demonstrate the race.
