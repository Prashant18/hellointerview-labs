# Lab 00 — Setup

> **Time budget:** 30 minutes. **Property proved:** the lab harness is real — gateway, observability, and load gen close the loop end to end.

## Goal

Stand up the smallest possible version of the system and prove every part of the feedback loop works:

- A FastAPI gateway with `/health`, `/v1/check`, `/metrics`.
- Prometheus scraping the gateway every 5s.
- Grafana attached to Prometheus (anonymous admin so we don't burn time on logins).
- k6 in the same docker network so we can fire load.
- A `verify.sh` that boots the stack, generates load, and asserts Prometheus actually saw it.

There is **no rate limiting yet**. `/v1/check` always returns `{"allowed": true}`. We're proving the *infrastructure* works before we put anything interesting on top of it. If we can't trust the harness, every later "I observed X" claim is suspect.

## Architecture

```
   k6  ──HTTP──▶  gateway:8000  ──/metrics──▶  prometheus:9090  ──▶  grafana:3000
                  (FastAPI)
```

Everything runs in a single Docker network created by `docker compose`. Ports `8000`, `9090`, and `3000` are mapped to the host so you can hit them from your browser.

## Files

```
labs/00-setup/
├── README.md              ← you are here
├── Makefile               ← up / down / smoke / verify / sync / clean
├── docker-compose.yml     ← gateway + prometheus + grafana + k6 (test profile)
├── verify.sh              ← one-command end-to-end check
├── gateway/
│   ├── Dockerfile         ← ghcr.io/astral-sh/uv:python3.12-bookworm-slim
│   ├── pyproject.toml     ← deps (fastapi, uvicorn, prometheus-client)
│   ├── uv.lock            ← committed; reproducible installs
│   ├── .python-version    ← 3.12
│   └── app/main.py        ← /health, /v1/check, /metrics + middleware
└── k6/
    └── smoke.js           ← 100 RPS for 5s, asserts 200s and p99 < 200ms
```

## Local dev (IDE language servers, type-check, run without Docker)

```bash
cd gateway
uv sync             # creates gateway/.venv with the same deps the image uses
```

Point your IDE's Python interpreter at `gateway/.venv/bin/python` — Pyright/Pylance/Pylsp will pick up FastAPI, Pydantic, etc. and stop nagging you about missing imports. To run the gateway directly without Docker:

```bash
cd gateway
uv run uvicorn app.main:app --reload --port 18000
```

Adding a new dependency? `uv add <pkg>` — it updates both `pyproject.toml` and `uv.lock` atomically. The next `make up` rebuild will pick it up.

The Prometheus + Grafana provisioning configs live one level up at `RateLimiter/shared/observability/` so every later lab can reuse them.

## Run it

```bash
make verify           # build + up + smoke + assert + summary
```

Or step by step:

```bash
make up               # build & start gateway, prometheus, grafana
make health           # curl /health
make smoke            # run k6 once
make logs             # tail gateway logs
make down             # stop everything (keeps images)
make clean            # stop + drop volumes + remove gateway image
```

URLs after `make up` (default host ports — see "ports" note below):

| Service    | URL                                | Notes                                  |
| ---------- | ---------------------------------- | -------------------------------------- |
| Gateway    | http://localhost:18000/health      | Liveness probe.                        |
|            | http://localhost:18000/v1/check    | Will become the rate-limit check.      |
|            | http://localhost:18000/metrics     | Prometheus exposition format.          |
| Prometheus | http://localhost:19090             | Try query `gateway_requests_total`.    |
| Grafana    | http://localhost:13000             | Anonymous admin, datasource preloaded. |

> **Ports**: We deliberately publish on `18000 / 19090 / 13000` instead of the canonical `8000 / 9090 / 3000` to avoid clashing with anything else you might have running (a real Sonar/Prometheus stack, another Grafana, etc.). Override with `make up GATEWAY_PORT=8000 PROM_PORT=9090 GRAFANA_PORT=3000` if those are free on your machine.

## What to look at

- After `make smoke`, hit Prometheus and run:
  ```
  sum by (path, status) (rate(gateway_requests_total[1m]))
  histogram_quantile(0.99, sum by (le, path) (rate(gateway_request_duration_seconds_bucket[1m])))
  ```
  You should see ~100 RPS on `/v1/check` and a p99 under 10ms (FastAPI on uvicorn, single worker, no real work).
- In Grafana, the **Prometheus** datasource is already wired up. Go to *Explore* and run the same queries.

## Trade-offs we made (and the alternatives we rejected)

| Decision                              | Alternative                  | Why we made it                                                                                                            |
| ------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| FastAPI + uvicorn (1 worker)          | Go `net/http` / Rust `axum`  | Iteration speed > raw throughput in a learning lab. We're proving patterns, not chasing TechEmpower.                      |
| `prometheus-client` directly          | `prometheus-fastapi-instrumentator` | One less dependency, and we control the metric names — important for staying consistent across labs.                   |
| Anonymous Grafana                     | Real auth                    | Burns zero seconds on login. Lab-only.                                                                                    |
| k6 as a docker-compose service (`profiles: test`) | k6 installed on host         | Keeps the host clean; the user only needs Docker. `docker compose run --rm k6 ...` puts it on the same network for free. |
| Prometheus retention 1h               | Default 15d                  | Tiny disk footprint; we're not preserving history across reboots in a lab.                                                |

## What to say in the interview

After running this lab, you can confidently say:

- "I always start a system design implementation by closing the observability loop first — service exposing `/metrics`, scraper hitting it, dashboard reading it, load generator sharing the network. If I don't have that, every claim about latency or correctness later is unverifiable."
- "I prefer **prometheus-client** in-process metrics over an APM agent for a rate limiter because the metrics this thing produces (allow/deny rate, fail-mode counter, lua-script-miss counter) are domain-specific and need to live next to the code that increments them."
- "Histogram buckets are not free — every label combination is a separate time series. I picked 10 latency buckets in the 1ms–1s range because that's the SLO range for a rate limiter; I would not just `default_buckets` it."

## Operational concerns (what we'd worry about in prod)

- **Cardinality**: `path` as a label is fine for a rate-limit gateway because the path set is bounded. If we ever templated it (e.g. `/users/{id}`), we'd explode the metric series count. Real gateways normalize path before labeling.
- **Health checks vs. readiness**: `/health` here is liveness only. In prod we'd add `/ready` that fails when Redis is unreachable so the load balancer can drain us during a Redis incident. (We'll add this in lab 06.)
- **Grafana anonymous-admin**: never in production. Yes, we know.

## Verifying the lab passed

`./verify.sh` returns `PASS — lab 00 is up.` if and only if:

1. The gateway becomes `/health`-healthy within 30s of build.
2. k6 finishes its 5-second 100 RPS run with `<1%` failures and `p99<200ms`.
3. Prometheus has `>= 400` samples in `sum(gateway_requests_total)` (k6 sent 500; we tolerate scrape race).

If any assertion fails, the script dumps gateway logs and exits non-zero.

## Next: lab 01 — In-memory Token Bucket

We replace the placeholder `/v1/check` with a real token-bucket allowance check, **single-replica, in-memory**. We'll add unit tests for the refill math, edge cases, and the `X-RateLimit-*` headers. Then in lab 02 we scale to 3 replicas and watch the limit leak — which is what motivates Redis.
