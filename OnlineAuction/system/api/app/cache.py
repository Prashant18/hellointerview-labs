"""Lab 04 — read-through cache for `GET /v1/items/{id}`.

Look-aside pattern with write-invalidate (NOT write-through). Trade-off: a
brief miss after every successful bid in exchange for simpler consistency
under concurrent writes — DDB stays the source of truth, Redis is a hint.

USER implements `get_or_load_item` and `invalidate_item`; tests in
`tests/test_cache.py` describe the contract.
"""

from __future__ import annotations

import json
from typing import Any

from prometheus_client import Counter

from app import ddb

CACHE_HITS = Counter("cache_hits_total", "Item-cache hits.")
CACHE_MISSES = Counter("cache_misses_total", "Item-cache misses (loaded from DDB).")

ITEM_TTL_SECONDS = 60


def _key(item_id: str) -> str:
    return f"item:{item_id}"


async def get_or_load_item(item_id: str, redis, ddb_client) -> dict[str, Any] | None:
    """Lab 04 — USER TO IMPLEMENT.

    Look-aside cache:
      1. ``cached = await redis.get(_key(item_id))``
      2. If cached: increment ``CACHE_HITS``; return ``json.loads(cached)``.
      3. Else: increment ``CACHE_MISSES``; ``GetItem`` from ``ddb_client``
         (TableName=``ddb.ITEMS_TABLE``, Key={"item_id":{"S": item_id}}).
         If ``"Item" not in resp`` → return ``None`` (do NOT populate cache).
         Else: ``unwrapped = ddb.unwrap_item(resp["Item"])``;
               ``await redis.setex(_key(item_id), ITEM_TTL_SECONDS, json.dumps(unwrapped))``;
               return ``unwrapped``.

    Why ``setex`` (TTL) and not plain ``set``? TTL is the safety net for
    invalidation bugs — even if a future change forgets to call
    ``invalidate_item``, the cache self-heals within 60 seconds.
    """
    cached = await redis.get(_key(item_id=item_id))
    if cached:
        CACHE_HITS.inc()
        return json.loads(cached)
    else:
        CACHE_MISSES.inc()
        item = await ddb_client.get_item(
            TableName=ddb.ITEMS_TABLE, Key={"item_id": {"S": item_id}}
        )
        if "Item" not in item:
            return None
        else:
            unwrapped = ddb.unwrap_item(item["Item"])
            await redis.setex(
                _key(item_id=item_id), ITEM_TTL_SECONDS, json.dumps(unwrapped)
            )
            return unwrapped


async def invalidate_item(item_id: str, redis) -> None:
    """Lab 04 — USER TO IMPLEMENT.

    ``await redis.delete(_key(item_id))``. Idempotent — DEL on a missing key
    is a no-op. Called from ``place_bid`` after the conditional UpdateItem
    succeeds so the next read repopulates with fresh state.
    """
    await redis.delete(_key(item_id))
