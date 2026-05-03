"""Lab 03 — SSE event-stream contract.

These tests exercise `_event_stream` against a fake pubsub. The function is
yours to implement (see `app/main.py`); the assertions below describe its
observable behavior.
"""

from __future__ import annotations

import pytest

from app.main import _event_stream


class _FakePubSub:
    def __init__(self) -> None:
        self.messages: list[bytes] = []
        self.subscribed_to: str | None = None
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed_to = channel

    async def unsubscribe(self, *_channels: str) -> None:
        self.unsubscribed = True

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(self, ignore_subscribe_messages: bool = True, timeout: float = 0.0):
        if self.messages:
            return {"type": "message", "data": self.messages.pop(0)}
        return None


class _FakeRedis:
    def __init__(self) -> None:
        self.ps = _FakePubSub()

    def pubsub(self) -> _FakePubSub:
        return self.ps


class _FakeRequest:
    """Returns False for the first `disconnect_after_polls` checks, then True."""

    def __init__(self, disconnect_after_polls: int = 3) -> None:
        self._n = 0
        self._after = disconnect_after_polls

    async def is_disconnected(self) -> bool:
        self._n += 1
        return self._n > self._after


async def _drain(gen) -> list[bytes]:
    out: list[bytes] = []
    async for frame in gen:
        out.append(frame)
    return out


@pytest.mark.asyncio
async def test_yields_one_sse_frame_per_message() -> None:
    r = _FakeRedis()
    r.ps.messages = [b'{"amount":100,"bidder":"alice"}']
    req = _FakeRequest(disconnect_after_polls=3)

    frames = await _drain(_event_stream(r, "auction:x", req))

    assert frames == [b'data: {"amount":100,"bidder":"alice"}\n\n']


@pytest.mark.asyncio
async def test_subscribes_to_correct_channel() -> None:
    r = _FakeRedis()
    req = _FakeRequest(disconnect_after_polls=1)

    await _drain(_event_stream(r, "auction:abc", req))

    assert r.ps.subscribed_to == "auction:abc"


@pytest.mark.asyncio
async def test_cleans_up_pubsub_on_disconnect() -> None:
    r = _FakeRedis()
    req = _FakeRequest(disconnect_after_polls=1)

    await _drain(_event_stream(r, "auction:y", req))

    assert r.ps.unsubscribed, "must unsubscribe on exit"
    assert r.ps.closed, "must aclose() the pubsub on exit"


@pytest.mark.asyncio
async def test_emits_multiple_messages_in_order() -> None:
    r = _FakeRedis()
    r.ps.messages = [b'{"amount":100}', b'{"amount":150}', b'{"amount":200}']
    req = _FakeRequest(disconnect_after_polls=5)

    frames = await _drain(_event_stream(r, "auction:z", req))

    assert frames == [
        b'data: {"amount":100}\n\n',
        b'data: {"amount":150}\n\n',
        b'data: {"amount":200}\n\n',
    ]
