"""
Tests for the in-memory Token Bucket (lab 01).

These tests pin the algorithm's contract. Run them with:

    cd gateway && uv run pytest -xvs

Conventions used here:

- We inject a *fake clock* so time advances only when a test asks it to.
  This makes refill behavior deterministic — no `sleep()` calls, no flakiness.
- Each test has a docstring describing exactly which property of the algorithm
  it pins. When a test fails, read its docstring to learn what the production
  consequence of the failure would be.
"""

from __future__ import annotations

import threading
from typing import Callable

import pytest

from app.tokenbucket import Decision, Rule, TokenBucket


class FakeClock:
    """Monotonic-by-construction clock controlled by the test."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock can't go backward")
        self._now += seconds


def make_bucket(
    capacity: int = 10,
    refill_per_sec: float = 1.0,
    start: float = 0.0,
) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock(start)
    bucket = TokenBucket(Rule(capacity=capacity, refill_per_sec=refill_per_sec), clock=clock)
    return bucket, clock


# ---------------------------------------------------------------------------
# Basics: a brand-new client has a full bucket and gets through.
# ---------------------------------------------------------------------------


def test_first_request_for_new_client_is_allowed_with_full_minus_one_remaining() -> None:
    """A client we've never seen before starts with a FULL bucket. Their first
    `allow()` call must consume one token and report `remaining == capacity - 1`.

    If this fails, you're probably initializing new clients to 0 tokens — which
    would deny their very first request, breaking every API consumer's first
    call after a deploy."""
    bucket, _ = make_bucket(capacity=10)
    decision = bucket.allow("alice")
    assert decision.allowed is True
    assert decision.remaining == 9


def test_consecutive_calls_drain_the_bucket_one_token_at_a_time() -> None:
    """Each `allow()` consumes exactly one token. After `capacity` calls in a
    row at the same instant, we should have allowed all of them and reported
    remaining=0 on the last."""
    bucket, _ = make_bucket(capacity=5)
    for expected_remaining in (4, 3, 2, 1, 0):
        d = bucket.allow("alice")
        assert d.allowed is True, f"expected allow, got {d}"
        assert d.remaining == expected_remaining, f"wrong remaining: {d}"


def test_request_beyond_capacity_is_denied_with_remaining_zero() -> None:
    """The (capacity+1)-th request at the same instant must be denied AND the
    Decision must report `remaining == 0`. This is the burst-cap behavior."""
    bucket, _ = make_bucket(capacity=3)
    for _ in range(3):
        bucket.allow("alice")
    denied = bucket.allow("alice")
    assert denied.allowed is False
    assert denied.remaining == 0


def test_denied_request_does_not_consume_a_token() -> None:
    """Once the bucket is empty, denied requests must NOT push the token count
    negative. This guarantees that once you start refilling, the next refilled
    token is immediately spendable — not eaten by debt from the deny path.

    Subtle but important: a naive `tokens -= 1` after the check could leave
    tokens at -1, costing the user one extra denial after refill."""
    bucket, clock = make_bucket(capacity=2, refill_per_sec=1.0)
    # Drain.
    bucket.allow("alice")
    bucket.allow("alice")
    # Pile on denials. Each must NOT push us into negative-token territory.
    for _ in range(5):
        d = bucket.allow("alice")
        assert d.allowed is False
    # Wait long enough to refill exactly one token.
    clock.advance(1.0)
    # The next request should be allowed — if we'd been deducting tokens on
    # denials, we'd still owe 5 tokens and this would fail.
    assert bucket.allow("alice").allowed is True


# ---------------------------------------------------------------------------
# Refill semantics: tokens come back over time, capped at capacity.
# ---------------------------------------------------------------------------


def test_refill_grants_tokens_at_the_specified_rate() -> None:
    """With `refill_per_sec=2.0`, advancing time by 1 second after exhaustion
    must grant exactly two more allowed requests. No more, no fewer."""
    bucket, clock = make_bucket(capacity=2, refill_per_sec=2.0)
    bucket.allow("alice")
    bucket.allow("alice")
    assert bucket.allow("alice").allowed is False  # exhausted
    clock.advance(1.0)
    # Now we should have refilled 2 tokens. Use them.
    assert bucket.allow("alice").allowed is True
    assert bucket.allow("alice").allowed is True
    # And no more.
    assert bucket.allow("alice").allowed is False


def test_refill_is_capped_at_capacity_no_matter_how_long_we_wait() -> None:
    """A bucket that's been idle for an hour shouldn't accumulate an hour's
    worth of tokens. The cap protects you from a single client suddenly
    bursting `refill_rate * idle_time` requests after a long pause."""
    bucket, clock = make_bucket(capacity=5, refill_per_sec=10.0)
    # Sleep through a year.
    clock.advance(365 * 24 * 3600)
    # We can use AT MOST `capacity` tokens.
    allowed_count = 0
    for _ in range(100):
        if bucket.allow("alice").allowed:
            allowed_count += 1
        else:
            break
    assert allowed_count == 5


def test_fractional_tokens_accumulate_until_a_whole_one_is_available() -> None:
    """With `refill_per_sec=0.5`, after 1 second we have only 0.5 tokens — not
    enough to allow. After another 1 second we have 1.0 tokens — exactly one
    allow. The implementation must accumulate fractional credit, not drop it."""
    bucket, clock = make_bucket(capacity=1, refill_per_sec=0.5)
    bucket.allow("alice")  # consume the initial token
    clock.advance(1.0)  # +0.5 tokens — not enough
    assert bucket.allow("alice").allowed is False
    clock.advance(1.0)  # another +0.5, now we have 1.0
    assert bucket.allow("alice").allowed is True


# ---------------------------------------------------------------------------
# Multi-client isolation.
# ---------------------------------------------------------------------------


def test_clients_have_independent_buckets() -> None:
    """Alice exhausting her bucket must not affect Bob's. This is the entire
    point of per-client rate limiting."""
    bucket, _ = make_bucket(capacity=2)
    bucket.allow("alice")
    bucket.allow("alice")
    assert bucket.allow("alice").allowed is False
    # Bob is fresh.
    assert bucket.allow("bob").allowed is True
    assert bucket.allow("bob").allowed is True
    assert bucket.allow("bob").allowed is False


# ---------------------------------------------------------------------------
# Decision.reset_after — the data behind the Retry-After header.
# ---------------------------------------------------------------------------


def test_reset_after_when_denied_is_seconds_until_one_more_token() -> None:
    """When we deny, `reset_after` must be the number of seconds the client
    needs to wait before they can succeed. With refill=2/s and 0 tokens,
    reset_after ≈ 0.5s. This drives the `Retry-After` HTTP header — clients
    use it to back off intelligently instead of hammering us."""
    bucket, _ = make_bucket(capacity=1, refill_per_sec=2.0)
    bucket.allow("alice")  # consume the only token
    d = bucket.allow("alice")
    assert d.allowed is False
    assert d.reset_after == pytest.approx(0.5, abs=1e-6)


def test_reset_after_when_allowed_is_seconds_until_bucket_is_full_again() -> None:
    """When we allow, `reset_after` should report when the bucket would be
    completely refilled — that's the canonical `X-RateLimit-Reset` semantic.
    With capacity=10, refill=1/s, after one allow we have 9 tokens, so
    reset_after = 1.0s (one more token to be at full)."""
    bucket, _ = make_bucket(capacity=10, refill_per_sec=1.0)
    d = bucket.allow("alice")
    assert d.allowed is True
    assert d.remaining == 9
    assert d.reset_after == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Concurrency — the staff+ test.
# ---------------------------------------------------------------------------


def test_concurrent_allows_are_thread_safe_and_dont_overcount() -> None:
    """Fire `capacity * 4` threads at the same client at the same instant.
    Exactly `capacity` should be allowed — no more, no fewer. If this fails,
    you have a TOCTOU bug between the read of the token count and the write,
    and your in-memory rate limiter overshoots the limit under concurrent load.

    This is the EXACT same race we're going to reproduce against Redis in
    lab 03 and fix with a Lua script in lab 04 — we're proving it here in
    a simpler setting first."""
    capacity = 50
    threads_count = capacity * 4
    bucket, _ = make_bucket(capacity=capacity, refill_per_sec=0.0)  # no refill during the test

    barrier = threading.Barrier(threads_count)
    decisions: list[Decision] = []
    decisions_lock = threading.Lock()

    def fire() -> None:
        barrier.wait()  # release all threads at once
        d = bucket.allow("alice")
        with decisions_lock:
            decisions.append(d)

    threads = [threading.Thread(target=fire) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed_count = sum(1 for d in decisions if d.allowed)
    assert allowed_count == capacity, (
        f"expected exactly {capacity} allowed under concurrency, got {allowed_count}"
    )
