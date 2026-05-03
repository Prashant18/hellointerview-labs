"""Redis pub/sub glue for lab 03+ (no caching yet — that's lab 04).

One connection pool per api process; opened in lifespan, closed on shutdown.
`decode_responses=False` so pubsub messages arrive as raw bytes — the SSE
handler emits them on the wire verbatim.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


@asynccontextmanager
async def client_ctx():
    client = redis_asyncio.from_url(REDIS_URL, decode_responses=False)
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()
