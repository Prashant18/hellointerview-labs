"""
Gateway service — see ../../JOURNAL.md for what each lab phase added.

Current state:
    - lab 00: skeleton + Prometheus instrumentation + try/finally middleware.
    - lab 01: in-memory Token Bucket wired into /v1/check, with
              X-RateLimit-* headers, 429 deny path, allow/deny counters.
    - lab 02: gateway scaled to 3 replicas behind Caddy LB; per-replica
              in-memory state demonstrably leaks the global limit.
    - lab 03: bucket state moved to Redis (single instance). The leak is
              fixed; a TOCTOU race between HMGET and HSET takes its place.
"""

from __future__ import annotations

import math
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from redis.asyncio import Redis

from app.redis_tokenbucket import RedisTokenBucket
from app.tokenbucket import Rule

GATEWAY_VERSION = "lab03"

RULE = Rule(
    capacity=int(os.getenv("RL_CAPACITY", "10")),
    refill_per_sec=float(os.getenv("RL_REFILL_PER_SEC", "1.0")),
)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open one Redis connection pool per gateway process. Closed on shutdown."""
    redis_client = Redis.from_url(REDIS_URL, decode_responses=True)
    # Fail fast at startup if Redis is unreachable.
    await redis_client.ping()
    app.state.bucket = RedisTokenBucket(RULE, redis_client)
    app.state.redis = redis_client
    try:
        yield
    finally:
        await redis_client.aclose()


app = FastAPI(title="ratelimiter-gateway", version=GATEWAY_VERSION, lifespan=lifespan)

REQUESTS = Counter(
    "gateway_requests_total",
    "Total HTTP requests handled by the gateway, labeled by path and status.",
    ["path", "status"],
)
LATENCY = Histogram(
    "gateway_request_duration_seconds",
    "Wall-clock duration of a request through the gateway.",
    ["path"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
RL_ALLOWED = Counter(
    "gateway_ratelimit_allowed_total",
    "Requests the rate limiter allowed through.",
)
RL_DENIED = Counter(
    "gateway_ratelimit_denied_total",
    "Requests the rate limiter rejected with 429.",
)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        path = request.url.path
        REQUESTS.labels(path=path, status=str(status_code)).inc()
        LATENCY.labels(path=path).observe(elapsed)


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    # Liveness only. /ready will be added in lab 06 to gate on Redis health.
    return JSONResponse({"status": "ok", "version": GATEWAY_VERSION})


def _client_id_from(request: Request) -> str:
    return (
        request.headers.get("x-client-id")
        or request.headers.get("x-api-key")
        or (request.client.host if request.client else "anonymous")
    )


def _ratelimit_headers(remaining: int, reset_after: float) -> dict[str, str]:
    reset_epoch = int(time.time() + max(0.0, reset_after))
    return {
        "X-RateLimit-Limit": str(RULE.capacity),
        "X-RateLimit-Remaining": str(remaining),
        "X-RateLimit-Reset": str(reset_epoch),
    }


@app.get("/v1/check")
async def check(request: Request) -> JSONResponse:
    client_id = _client_id_from(request)
    bucket: RedisTokenBucket = request.app.state.bucket
    decision = await bucket.allow(client_id)
    headers = _ratelimit_headers(decision.remaining, decision.reset_after)

    if not decision.allowed:
        headers["Retry-After"] = str(max(1, math.ceil(decision.reset_after)))
        RL_DENIED.inc()
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "message": (
                    f"Exceeded {RULE.capacity} requests "
                    f"(burst) with refill rate {RULE.refill_per_sec}/s. "
                    f"Retry in {math.ceil(decision.reset_after)}s."
                ),
                "client_id": client_id,
            },
            headers=headers,
        )

    RL_ALLOWED.inc()
    return JSONResponse(
        content={
            "allowed": True,
            "client_id": client_id,
            "remaining": decision.remaining,
            "lab": GATEWAY_VERSION,
        },
        headers=headers,
    )


@app.get("/metrics")
async def metrics() -> Response:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)
