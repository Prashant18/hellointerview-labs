"""
In-memory Token Bucket rate limiter (lab 01).

You are implementing the body of `TokenBucket.__init__` and `TokenBucket.allow`.
The dataclasses (`Rule`, `Decision`) are already done — you should not need to
change them. Run the tests with:

    cd gateway && uv run pytest -xvs

Algorithm reference:

    Each client has a bucket with two pieces of state:
      tokens         : float   — current credit (can be fractional)
      last_refill_ts : float   — monotonic timestamp of the last refill

    On `allow(client_id)`:
      1) If we've never seen this client, initialize their bucket with
         `tokens = capacity` and `last_refill_ts = now`.
      2) Compute `elapsed = now - last_refill_ts`.
      3) Add `elapsed * refill_per_sec` tokens, capped at `capacity`.
      4) Update `last_refill_ts = now`.
      5) If `tokens >= 1`: deduct one and return Decision(allowed=True, ...).
         Otherwise: return Decision(allowed=False, ...) WITHOUT going negative.

    `reset_after` semantics (drives X-RateLimit-Reset / Retry-After headers):
      - When DENIED: seconds until 1 token becomes available.
      - When ALLOWED: seconds until the bucket is full again (capacity tokens).

    Concurrency:
      - Multiple threads may call `allow(client_id)` for the same client at
        the same instant. The read-modify-write of (tokens, last_refill_ts)
        must be atomic. Use `threading.Lock`. A single bucket-wide lock is
        fine for lab 01; we'll move to per-client locks if it ever shows up
        in profiling.

Why we inject `clock` into the constructor:
    The tests pass a `FakeClock` so they can advance time deterministically
    without sleep(). In production we default to `time.monotonic`, which
    never goes backward (unlike `time.time()`, which NTP can slew).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Rule:
    """The static configuration of a rate limit."""

    capacity: int  # max tokens (also the burst size)
    refill_per_sec: float  # tokens added per second (steady-state rate)


@dataclass(frozen=True)
class Decision:
    """The result of a single allow() call. Returned to the HTTP layer."""

    allowed: bool
    remaining: int  # whole tokens left after this decision (>= 0)
    reset_after: float  # seconds until either +1 token (denied) or full (allowed)


class TokenBucket:
    """In-memory, thread-safe Token Bucket rate limiter, single replica.

    NOT distributed. Lab 02 will demonstrate why this isn't enough across
    multiple gateway replicas; lab 03+ moves the state to Redis.
    """

    def __init__(
        self,
        rule: Rule,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rule = rule
        self._clock = clock
        # Per-client state: client_id -> {"tokens": float, "last_refill_time": float}.
        self._state: dict[str, dict[str, float]] = {}
        # Single bucket-wide lock: every allow() does its read-modify-write
        # under this so concurrent calls can't observe a stale token count.
        self._lock = threading.Lock()

    def allow(self, client_id: str) -> Decision:
        with self._lock:
            now = self._clock()

            # Lazy-init for unseen clients: bucket starts FULL.
            if client_id not in self._state:
                self._state[client_id] = {
                    "tokens": float(self._rule.capacity),
                    "last_refill_time": now,
                }

            bucket = self._state[client_id]

            # Catch up on any refill since this bucket was last touched, then
            # advance last_refill_time. We always advance it — even on deny —
            # so the next call doesn't double-count the same elapsed window.
            elapsed = now - bucket["last_refill_time"]
            tokens = min(
                self._rule.capacity,
                bucket["tokens"] + elapsed * self._rule.refill_per_sec,
            )
            bucket["last_refill_time"] = now

            # Decide. We do NOT push tokens negative on deny — a denied request
            # must not "owe" a token, otherwise the next refilled token gets
            # eaten by the debt instead of being spendable.
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

            # Persist the new token count back into shared state.
            bucket["tokens"] = tokens

            return Decision(
                allowed=allowed,
                remaining=int(math.floor(tokens)),
                reset_after=reset_after,
            )
