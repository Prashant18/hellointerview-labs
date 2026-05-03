"""Lab 04 — look-aside cache contract.

Fakes for Redis (get/setex/delete) and the DDB client (get_item). Tests
describe what `cache.get_or_load_item` and `cache.invalidate_item` MUST do;
implement them in `app/cache.py` to make these green.
"""

from __future__ import annotations

import json

import pytest

from app import cache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.set_calls: list[tuple[str, int, bytes]] = []
        self.del_calls: list[str] = []

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, ttl: int, value):
        b = value if isinstance(value, bytes) else value.encode()
        self.set_calls.append((key, ttl, b))
        self.store[key] = b

    async def delete(self, key: str) -> int:
        self.del_calls.append(key)
        return 1 if self.store.pop(key, None) is not None else 0


class _FakeDDB:
    def __init__(self) -> None:
        self.calls = 0
        self.items: dict[str, dict] = {}  # item_id -> AttributeValue dict

    async def get_item(self, *, TableName: str, Key: dict):
        self.calls += 1
        item_id = Key["item_id"]["S"]
        if item_id not in self.items:
            return {}
        return {"Item": self.items[item_id]}


def _ddb_item(item_id: str, *, title: str = "widget", high: float | None = None) -> dict:
    av: dict = {
        "item_id": {"S": item_id},
        "title": {"S": title},
        "start_price": {"N": "10"},
        "end_time_epoch": {"N": "9999999999"},
    }
    if high is not None:
        av["current_high_bid"] = {"N": str(high)}
        av["current_high_bidder"] = {"S": "alice"}
    return av


@pytest.mark.asyncio
async def test_miss_loads_from_ddb_and_populates_cache() -> None:
    r, d = _FakeRedis(), _FakeDDB()
    d.items["abc"] = _ddb_item("abc", high=100.0)

    out = await cache.get_or_load_item("abc", r, d)

    assert out is not None
    assert out["item_id"] == "abc"
    assert out["current_high_bid"] == 100.0
    assert d.calls == 1
    # cache populated with TTL
    assert len(r.set_calls) == 1
    key, ttl, value = r.set_calls[0]
    assert key == "item:abc"
    assert ttl == cache.ITEM_TTL_SECONDS
    assert json.loads(value)["item_id"] == "abc"


@pytest.mark.asyncio
async def test_hit_returns_without_calling_ddb() -> None:
    r, d = _FakeRedis(), _FakeDDB()
    r.store["item:xyz"] = json.dumps(
        {
            "item_id": "xyz",
            "title": "cached",
            "start_price": 5.0,
            "end_time_epoch": 9999999999,
            "current_high_bid": 200.0,
            "current_high_bidder": "bob",
            "closed_by": None,
        }
    ).encode()

    out = await cache.get_or_load_item("xyz", r, d)

    assert out is not None
    assert out["title"] == "cached"
    assert out["current_high_bid"] == 200.0
    assert d.calls == 0, "must not hit DDB on cache hit"
    assert len(r.set_calls) == 0, "must not re-populate on hit"


@pytest.mark.asyncio
async def test_miss_returns_none_when_item_missing_in_ddb() -> None:
    r, d = _FakeRedis(), _FakeDDB()  # both empty

    out = await cache.get_or_load_item("ghost", r, d)

    assert out is None
    assert d.calls == 1
    assert len(r.set_calls) == 0, "must not cache 'not found' (would mask later creation)"


@pytest.mark.asyncio
async def test_invalidate_deletes_key() -> None:
    r = _FakeRedis()
    r.store["item:abc"] = b'{"item_id":"abc"}'

    await cache.invalidate_item("abc", r)

    assert "item:abc" not in r.store
    assert r.del_calls == ["item:abc"]


@pytest.mark.asyncio
async def test_invalidate_is_idempotent() -> None:
    r = _FakeRedis()  # key not present

    await cache.invalidate_item("nothing", r)
    await cache.invalidate_item("nothing", r)

    assert r.del_calls == ["item:nothing", "item:nothing"]
