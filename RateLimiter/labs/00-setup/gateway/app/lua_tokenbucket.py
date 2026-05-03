"""
Lab 04 — Lua-atomic Token Bucket. Pure glue.

The algorithm lives in `gateway/scripts/tokenbucket.lua`. This class loads
the script once at construction time and runs it via EVALSHA on every
allow(). `redis_client.register_script()` hands back a `Script` wrapper
that:
  - Computes the SHA1 once, calls EVALSHA on every invocation (saves
    sending the script body over the wire).
  - Auto-recovers from NOSCRIPT errors (Redis sometimes flushes its
    script cache; the wrapper transparently re-uploads via EVAL).
  - Encodes keys/args correctly for the Redis protocol.

Why this is so much shorter than RedisTokenBucket: the algorithm moved
INSIDE Redis. The Python side is now just "marshal args, await result,
unpack". No more two round trips, no more race window between them.
"""

from __future__ import annotations

from pathlib import Path

from redis.asyncio import Redis

from app.tokenbucket import Decision, Rule

_SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "tokenbucket.lua"


class LuaTokenBucket:
    """Distributed Token Bucket whose entire allow() runs as one atomic Lua call."""

    KEY_PREFIX = "bucket:"
    TTL_SECONDS = 3600

    def __init__(self, rule: Rule, redis_client: Redis) -> None:
        self._rule = rule
        self._redis = redis_client
        self._script = redis_client.register_script(_SCRIPT_PATH.read_text())

    async def allow(self, client_id: str) -> Decision:
        key = f"{self.KEY_PREFIX}{client_id}"
        result = await self._script(
            keys=[key],
            args=[
                self._rule.capacity,
                self._rule.refill_per_sec,
                self.TTL_SECONDS,
            ],
        )
        # Lua returns {allowed_int, remaining_int, reset_after_str}. With
        # decode_responses=True on the Redis client, numbers come back as
        # ints and strings as str — but defensive int()/float() casts keep
        # us safe across redis-py versions.
        allowed_int, remaining, reset_after_str = result
        return Decision(
            allowed=bool(int(allowed_int)),
            remaining=int(remaining),
            reset_after=float(reset_after_str),
        )
