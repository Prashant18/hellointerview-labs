"""Lab 05 — closer contract.

Pure helpers (`_bucket_for`, `_buckets_in_range`) tested directly.
`try_claim` tested against a fake DDB that simulates the specific
ConditionExpression we use (`attribute_exists(item_id) AND
attribute_not_exists(closed_by)`) and the `REMOVE closing_bucket` clause.

`find_ending_auctions` is left to the chaos probe — modeling GSI Query
semantics in a fake adds more lines than it proves.
"""

from __future__ import annotations

import asyncio

import pytest

from app import closer


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_bucket_for_minute_boundary() -> None:
    # 2024-05-03 06:01:00 UTC → 1714716060
    assert closer._bucket_for(1714716060) == "2024-05-03T06:01"


def test_bucket_for_is_pure_function() -> None:
    a = closer._bucket_for(1714716060)
    b = closer._bucket_for(1714716060)
    assert a == b


def test_buckets_in_range_inclusive_across_minute_boundary() -> None:
    # 06:00:30 → 06:01:30 should yield TWO buckets.
    start = 1714716030
    end = start + 60
    out = closer._buckets_in_range(start, end)
    assert out == ["2024-05-03T06:00", "2024-05-03T06:01"]


def test_buckets_in_range_empty_when_inverted() -> None:
    assert closer._buckets_in_range(2000, 1000) == []


# ---------------------------------------------------------------------------
# Fake DDB for try_claim
# ---------------------------------------------------------------------------


class _CCFE(Exception):
    """Fake of botocore's ConditionalCheckFailedException."""


class _FakeExceptions:
    ConditionalCheckFailedException = _CCFE


class _FakeDDB:
    """Models exactly the conditional predicate try_claim uses:
       `attribute_exists(item_id) AND attribute_not_exists(closed_by)`.
       And the exact UpdateExpression: SET closed_by/closed_at, REMOVE closing_bucket.
    """

    def __init__(self) -> None:
        self.exceptions = _FakeExceptions()
        # item_id -> attribute dict (DDB AttributeValue typed dicts)
        self.items: dict[str, dict] = {}
        # async lock so concurrent claim attempts serialize like a single DDB partition
        self._lock = asyncio.Lock()

    def put(self, item_id: str, **attrs) -> None:
        item: dict = {"item_id": {"S": item_id}}
        for k, v in attrs.items():
            if isinstance(v, str):
                item[k] = {"S": v}
            elif isinstance(v, (int, float)):
                item[k] = {"N": str(v)}
        self.items[item_id] = item

    async def update_item(
        self,
        *,
        TableName: str,
        Key: dict,
        UpdateExpression: str,
        ConditionExpression: str,
        ExpressionAttributeValues: dict,
    ):
        async with self._lock:
            item_id = Key["item_id"]["S"]
            item = self.items.get(item_id)

            # ConditionExpression evaluation
            if "attribute_exists(item_id)" in ConditionExpression:
                if item is None:
                    raise _CCFE("item_id missing")
            if "attribute_not_exists(closed_by)" in ConditionExpression:
                if item is not None and "closed_by" in item:
                    raise _CCFE("already closed")

            assert item is not None  # both predicates guarantee this
            # Apply SET clauses
            if "SET closed_by = :me" in UpdateExpression:
                item["closed_by"] = ExpressionAttributeValues[":me"]
            if "closed_at = :ts" in UpdateExpression:
                item["closed_at"] = ExpressionAttributeValues[":ts"]
            # Apply REMOVE
            if "REMOVE closing_bucket" in UpdateExpression:
                item.pop("closing_bucket", None)
            return {}


# ---------------------------------------------------------------------------
# try_claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_claim_succeeds_when_unclosed() -> None:
    d = _FakeDDB()
    d.put("a1", title="widget", end_time_epoch=1714716060, closing_bucket="2024-05-03T06:01")

    won = await closer.try_claim("a1", d, replica_id="r1", now_epoch=1714716100)

    assert won is True
    assert d.items["a1"]["closed_by"]["S"] == "r1"
    assert "closing_bucket" not in d.items["a1"], "REMOVE must drop the GSI attribute"


@pytest.mark.asyncio
async def test_try_claim_fails_when_already_closed() -> None:
    d = _FakeDDB()
    d.put("a2", title="widget", end_time_epoch=1714716060)
    d.items["a2"]["closed_by"] = {"S": "other-replica"}  # pre-claimed

    won = await closer.try_claim("a2", d, replica_id="r1", now_epoch=1714716100)

    assert won is False
    assert d.items["a2"]["closed_by"]["S"] == "other-replica", "must not overwrite"


@pytest.mark.asyncio
async def test_try_claim_concurrent_only_one_winner() -> None:
    d = _FakeDDB()
    d.put("a3", title="widget", end_time_epoch=1714716060, closing_bucket="2024-05-03T06:01")

    results = await asyncio.gather(
        *[closer.try_claim("a3", d, replica_id=f"r{i}", now_epoch=1714716100) for i in range(10)]
    )

    assert sum(1 for r in results if r) == 1, "exactly one replica must win"
    assert "closed_by" in d.items["a3"]
    assert "closing_bucket" not in d.items["a3"]
