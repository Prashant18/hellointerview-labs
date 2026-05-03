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
    - lab 04: race fixed by moving the entire read-compute-write inside a
              Lua script (atomic by construction). Set BUCKET_BACKEND=redis
              to compare against the lab 03 racy implementation.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from redis.asyncio import Redis
from redis.asyncio.cluster import ClusterNode, RedisCluster

from app.lua_tokenbucket import LuaTokenBucket
from app.redis_tokenbucket import RedisTokenBucket
from app.tokenbucket import Rule

GATEWAY_VERSION = "lab06"
BUCKET_BACKEND = os.getenv("BUCKET_BACKEND", "lua")  # "lua" (atomic) or "redis" (racy, lab 03)

# Fail-closed budget for a single rate-limit check. If Redis can't answer in
# this window, we reject the request with 503 to keep load off downstream.
# 100ms is generous for sub-ms Redis ops; tighten in production.
RL_TIMEOUT_SECONDS = float(os.getenv("RL_TIMEOUT_SECONDS", "0.1"))

RULE = Rule(
    capacity=int(os.getenv("RL_CAPACITY", "10")),
    refill_per_sec=float(os.getenv("RL_REFILL_PER_SEC", "1.0")),
)

# Lab 05: Redis Cluster connection.
# REDIS_CLUSTER_NODES = comma-separated "host:port" seeds. The cluster client
# discovers the rest of the topology automatically. For backwards compat with
# pre-cluster local dev, fall back to REDIS_URL pointing at a single node.
REDIS_CLUSTER_NODES = os.getenv("REDIS_CLUSTER_NODES", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://redis-1:6379/0")


def _parse_cluster_nodes(spec: str) -> list[ClusterNode]:
    nodes = []
    for hostport in spec.split(","):
        hostport = hostport.strip()
        if not hostport:
            continue
        host, _, port = hostport.partition(":")
        nodes.append(ClusterNode(host=host, port=int(port or "6379")))
    return nodes


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open one Redis(Cluster) connection pool per gateway process."""
    if REDIS_CLUSTER_NODES:
        # Cluster mode (lab 05+). RedisCluster discovers every master/replica
        # from the seed list and keeps its slot map fresh on its own.
        # socket_timeout caps the per-command wait — combined with the
        # asyncio.wait_for() in /v1/check, this gives bounded latency under
        # Redis stalls (lab 06 fail-closed).
        redis_client = RedisCluster(
            startup_nodes=_parse_cluster_nodes(REDIS_CLUSTER_NODES),
            decode_responses=True,
            require_full_coverage=False,  # tolerate transient partial slot coverage during failover
            socket_timeout=RL_TIMEOUT_SECONDS,
            socket_connect_timeout=0.5,
        )
    else:
        # Single-instance fallback (lab 03/04 local dev).
        redis_client = Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_timeout=RL_TIMEOUT_SECONDS,
            socket_connect_timeout=0.5,
        )
    # Fail fast at startup if Redis is unreachable.
    await redis_client.ping()
    if BUCKET_BACKEND == "lua":
        app.state.bucket = LuaTokenBucket(RULE, redis_client)
    elif BUCKET_BACKEND == "redis":
        # The lab 03 racy implementation, kept around for A/B comparison.
        app.state.bucket = RedisTokenBucket(RULE, redis_client)
    else:
        raise ValueError(f"Unknown BUCKET_BACKEND={BUCKET_BACKEND!r} (use 'lua' or 'redis')")
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
RL_FAILMODE = Counter(
    "gateway_ratelimit_failmode_total",
    "Requests rejected with 503 because the rate limiter could not reach Redis (timeout / connection error). "
    "Page on this — it is the canary for Redis trouble.",
    ["reason"],  # bounded: timeout, connection, redis_error
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
    # Liveness — "is this process running?" Doesn't touch Redis. The LB
    # uses this to detect crashed processes; killing on /health failure
    # would correctly restart us.
    return JSONResponse({"status": "ok", "version": GATEWAY_VERSION})


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness — 'should the LB send me real traffic right now?' Touches
    Redis. Returns 503 when Redis is unreachable so Caddy/k8s can drain us
    before serving requests we'd just have to fail-close on. Different
    semantics from /health: liveness vs. ability-to-serve."""
    try:
        await asyncio.wait_for(request.app.state.redis.ping(), timeout=0.5)
        return JSONResponse({"ready": True, "version": GATEWAY_VERSION})
    except (asyncio.TimeoutError, redis.RedisError, ConnectionError, OSError) as e:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "reason": type(e).__name__, "detail": str(e)[:200]},
        )


def _client_id_from(request: Request) -> str:
    return (
        request.headers.get("x-client-id")
        or request.headers.get("x-api-key")
        or (request.client.host if request.client else "anonymous")
    )


def _failclosed_response(message: str) -> JSONResponse:
    """503 with a sensible Retry-After when the rate limiter can't reach Redis."""
    return JSONResponse(
        status_code=503,
        content={
            "error": "rate_limit_unavailable",
            "message": (
                f"{message}. Rate limiter is temporarily unavailable; "
                "rejecting request to protect downstream services."
            ),
        },
        headers={"Retry-After": "1"},
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
    bucket = request.app.state.bucket  # LuaTokenBucket or RedisTokenBucket per BUCKET_BACKEND

    # Fail-closed: bound the per-request wait. If Redis can't answer in
    # RL_TIMEOUT_SECONDS, reject the request with 503 instead of letting
    # FastAPI return a 500 (or worse, hanging). The HelloInterview source
    # argues for fail-CLOSED on a social-media-platform rate limiter so a
    # Redis outage doesn't avalanche traffic into the backends.
    try:
        decision = await asyncio.wait_for(bucket.allow(client_id), timeout=RL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        RL_FAILMODE.labels(reason="timeout").inc()
        return _failclosed_response("rate-limit check timed out")
    except (ConnectionError, OSError):
        RL_FAILMODE.labels(reason="connection").inc()
        return _failclosed_response("rate-limit store unreachable")
    except redis.RedisError as e:
        RL_FAILMODE.labels(reason="redis_error").inc()
        return _failclosed_response(f"rate-limit store error: {type(e).__name__}")

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
