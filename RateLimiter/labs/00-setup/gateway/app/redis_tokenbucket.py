"""
Lab 03 — Redis-backed Token Bucket.

YOU are implementing the body of `RedisTokenBucket.allow`. The class
signature and the dataclass return type (`Decision` from app.tokenbucket)
are already wired up. Run the integration tests with:

    make verify

`make verify` boots the stack and runs three k6 scenarios:
    - smoke   : 100 RPS for 5s, distinct clients (harness liveness)
    - burst   : 50 reqs from ONE client, capacity=10. MUST give exactly
                10 allowed and 40 denied. This proves your implementation
                centralized state correctly (lab 02's leak is gone).
    - race    : 20 VUs × 100 reqs against ONE client, capacity=10. MUST
                allow MORE than 10. The whole point of this lab is to
                expose a TOCTOU race in the naive HMGET/HSET pattern, so
                lab 04 has something to fix with a Lua script. If your
                impl is too clever (e.g., uses WATCH or a Redis lock) and
                gives exactly 10 here, you're getting ahead of yourself.

Algorithm — implement EXACTLY this naive shape:

    1) HMGET the bucket key for the two fields: tokens, last_refill.
       (Redis returns [None, None] for an unseen client.)
    2) Compute "now" using the injected `_clock` (defaults to time.time —
       wall clock — so all replicas agree on what "now" means).
    3) tokens     = capacity if redis returned None else float(value)
       last_refill = now      if redis returned None else float(value)
    4) elapsed = max(0.0, now - last_refill)
       tokens  = min(capacity, tokens + elapsed * refill_per_sec)
    5) Decide:
         if tokens >= 1:
             tokens -= 1
             allowed = True
             reset_after = (capacity - tokens) / refill_per_sec
                           (or 0.0 if refill_per_sec == 0)
         else:
             allowed = False
             reset_after = (1 - tokens) / refill_per_sec
                           (or math.inf if refill_per_sec == 0)
    6) Write the new state back via a MULTI/EXEC pipeline:
         async with self._redis.pipeline(transaction=True) as pipe:
             pipe.hset(key, mapping={"tokens": ..., "last_refill": ...})
             pipe.expire(key, self.TTL_SECONDS)
             await pipe.execute()
       (The TTL is so idle buckets don't accumulate forever in Redis.)
    7) Return Decision(allowed, remaining=int(math.floor(tokens)),
       reset_after=...).

Why we use `time.time()` (wall clock) here, NOT `time.monotonic()`:
    monotonic clocks are per-process — each gateway replica has its own
    "0" reference, so `last_refill` written by replica A would be
    nonsensical to replica B. Wall clock + NTP is the pragmatic answer.
    (Lab 04's Lua script will switch to `redis.call('TIME')` so the clock
    lives next to the state.)

Where the bug lives (intentionally):
    Steps (1) and (6) are TWO separate Redis round-trips. Between them,
    another request for the same client can also do step (1), see the
    same `tokens` count, decide "allow", and step (6). Both decisions
    return 200 to their clients before either write completes. The
    MULTI/EXEC pipeline only makes the *writes* atomic with respect to
    each other — it does NOT close the read-modify-write window. That
    window is the race that race.js exposes, and lab 04 fixes.
"""

from __future__ import annotations

import math
import time
from typing import Callable

from redis.asyncio import Redis

from app.tokenbucket import Decision, Rule


class RedisTokenBucket:
    """Distributed Token Bucket whose state lives in Redis."""

    KEY_PREFIX = "bucket:"
    TTL_SECONDS = 3600  # idle buckets evict after an hour to bound memory

    def __init__(
        self,
        rule: Rule,
        redis_client: Redis,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._rule = rule
        self._redis = redis_client
        self._clock = clock

    async def allow(self, client_id: str) -> Decision:
        # TODO(you): implement the naive Redis-backed token bucket.
        # See the docstring at the top of this file for the algorithm
        # and the explicit "do NOT make it atomic yet" warning.
        #
        allowed = False
        reset_after = 0

        key = f"{self.KEY_PREFIX}{client_id}"
        state = await self._redis.hmget(key, "tokens", "last_refill")
        now = self._clock()
        tokens = float(state[0] if state[0] is not None else float(self._rule.capacity))
        last_refill = float(state[1] if state[1] is not None else now)

        elapsed = max(0.0, now - last_refill)
        tokens = min(self._rule.capacity, tokens + elapsed * self._rule.refill_per_sec)

        if tokens >= 1:
            tokens -= 1
            allowed = True
            reset_after = (
                (self._rule.capacity - tokens) / self._rule.refill_per_sec
                if self._rule.refill_per_sec > 0
                else 0.0
            )
        else:
            allowed = False
            reset_after = (
                (1 - tokens) / self._rule.refill_per_sec
                if self._rule.refill_per_sec > 0
                else math.inf
            )

        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={"tokens": tokens, "last_refill": now})
            pipe.expire(key, self.TTL_SECONDS)
            await pipe.execute()

        return Decision(
            allowed=allowed, remaining=int(math.floor(tokens)), reset_after=reset_after
        )
