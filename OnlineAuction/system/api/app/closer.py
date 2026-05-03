"""Lab 05 — exactly-once auction close via per-replica polling + race-claim.

Each api replica runs `run_close_loop` as a background asyncio task. Every
INTERVAL_S the loop:
  1) `find_ending_auctions` — Query the sparse `closing-index` GSI for items
     whose `end_time_epoch` is in the past (within a lookback window).
  2) `try_claim` — single conditional `UpdateItem` per candidate. Winner
     atomically sets `closed_by`/`closed_at` AND `REMOVE`s `closing_bucket`,
     dropping the item from the GSI so future polls don't see it. Losers get
     `ConditionalCheckFailedException` and skip silently.
  3) `close_auction` — post-claim work: publish event, increment metric.

The conditional write IS the leader election — per auction, no coordinator,
no lease renewal, cheapest distributed lock primitive in DDB's surface.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from prometheus_client import Counter

from app import ddb

log = logging.getLogger("closer")

AUCTIONS_CLOSED = Counter(
    "auctions_closed_total",
    "Auctions this replica claimed and closed (won the conditional UpdateItem race).",
)

INTERVAL_S = 3
LOOKBACK_S = 300  # cover up to 5 min of closer downtime — items linger in the GSI until claimed


# ---------------------------------------------------------------------------
# Pure helpers (Claude-written)
# ---------------------------------------------------------------------------


def _bucket_for(end_time_epoch: int) -> str:
    """1-minute UTC bucket string. The GSI partition key.

    Bucketing the partition key spreads writes across many DDB partitions
    instead of hot-spotting on a single status="OPEN" pk. At 10k auctions/min
    that's ~167 items per partition — well under DDB's 1k WCU partition cap.
    """
    return datetime.fromtimestamp(int(end_time_epoch), tz=UTC).strftime("%Y-%m-%dT%H:%M")


def _buckets_in_range(from_epoch: int, to_epoch: int) -> list[str]:
    """Every minute-bucket touched by the inclusive [from, to] window."""
    if to_epoch < from_epoch:
        return []
    start = datetime.fromtimestamp(from_epoch, tz=UTC).replace(second=0, microsecond=0)
    end = datetime.fromtimestamp(to_epoch, tz=UTC).replace(second=0, microsecond=0)
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%dT%H:%M"))
        cur += timedelta(minutes=1)
    return out


# ---------------------------------------------------------------------------
# USER-implemented (Lab 05)
# ---------------------------------------------------------------------------


async def find_ending_auctions(ddb_client, now_epoch: int, lookback_s: int = LOOKBACK_S) -> list[str]:
    """Lab 05 — USER TO IMPLEMENT.

    Query the sparse GSI `closing-index` for items that have already ended
    (end_time_epoch <= now) and have not been claimed yet (sparse: closed
    items dropped out of the GSI when `closing_bucket` was REMOVEd).

    Algorithm:
      1. ``buckets = _buckets_in_range(now_epoch - lookback_s, now_epoch)``.
      2. For each bucket string, ``await ddb_client.query(...)`` with:
         - ``TableName=ddb.ITEMS_TABLE``
         - ``IndexName=ddb.CLOSING_INDEX``
         - ``KeyConditionExpression="closing_bucket = :b AND end_time_epoch <= :now"``
         - ``ExpressionAttributeValues={":b": {"S": bucket}, ":now": {"N": str(now_epoch)}}``
      3. From each response, extract ``item["item_id"]["S"]`` for every item in
         ``resp.get("Items", [])``. Return the aggregated list.

    Why a fixed lookback (and not just the current bucket)? If a closer pod
    crashed and was down for 4 minutes, items that ended during those 4 min
    are still in the GSI (no one REMOVEd `closing_bucket`). When the closer
    comes back, the lookback sweeps them up. Without it: dropped close events.

    See `tests/test_closer.py` for the contract — but note that this function
    is best validated by the chaos probe end-to-end (the GSI semantics are
    cleaner to assert against real DDB Local than a fake).
    """
    candidates: list[str] = []
    for bucket in _buckets_in_range(now_epoch - lookback_s, now_epoch):
        resp = await ddb_client.query(
            TableName=ddb.ITEMS_TABLE,
            IndexName=ddb.CLOSING_INDEX,
            KeyConditionExpression="closing_bucket = :b AND end_time_epoch <= :now",
            ExpressionAttributeValues={
                ":b": {"S": bucket},
                ":now": {"N": str(now_epoch)},
            },
        )
        for item in resp.get("Items", []):
            candidates.append(item["item_id"]["S"])
    return candidates


async def try_claim(
    item_id: str, ddb_client, replica_id: str, now_epoch: int
) -> bool:
    """Lab 05 — USER TO IMPLEMENT.

    Single conditional ``UpdateItem`` that atomically (a) marks the item as
    claimed by this replica, and (b) removes it from the GSI so future polling
    cycles don't see it.

    Algorithm:
      ``await ddb_client.update_item(``
        ``TableName=ddb.ITEMS_TABLE,``
        ``Key={"item_id": {"S": item_id}},``
        ``UpdateExpression="SET closed_by = :me, closed_at = :ts REMOVE closing_bucket",``
        ``ConditionExpression="attribute_exists(item_id) AND attribute_not_exists(closed_by)",``
        ``ExpressionAttributeValues={":me": {"S": replica_id}, ":ts": {"N": str(now_epoch)}},``
      ``)``
      → return True

    On ``ddb_client.exceptions.ConditionalCheckFailedException`` → return False
    (someone else won this auction; skip silently — NOT an error).

    Why ``REMOVE closing_bucket`` in the same UpdateItem? It's atomic with
    the ``closed_by`` set. Two writes (claim, then remove) would let a second
    replica see the item still in the GSI between the two and try to claim,
    paying an extra UpdateItem RCU per loser. One write closes that gap.
    """
    try:
        await ddb_client.update_item(
            TableName=ddb.ITEMS_TABLE,
            Key={"item_id": {"S": item_id}},
            UpdateExpression="SET closed_by = :me, closed_at = :ts REMOVE closing_bucket",
            ConditionExpression="attribute_exists(item_id) AND attribute_not_exists(closed_by)",
            ExpressionAttributeValues={
                ":me": {"S": replica_id},
                ":ts": {"N": str(now_epoch)},
            },
        )
        return True
    except ddb_client.exceptions.ConditionalCheckFailedException:
        return False


# ---------------------------------------------------------------------------
# Orchestration (Claude-written)
# ---------------------------------------------------------------------------


async def close_auction(item_id: str, ddb_client, redis_client) -> None:
    """Post-claim work: publish event so subscribers learn the auction closed,
    bump the per-replica counter. Best-effort publish — DDB already has the
    truth (`closed_by` is set); pubsub failure must not roll back the close."""
    payload = json.dumps({"item_id": item_id, "event": "auction.closed"})
    try:
        await redis_client.publish(f"auction:{item_id}", payload)
    except Exception as e:
        log.warning("publish failed for closed auction %s: %s", item_id, e)
    AUCTIONS_CLOSED.inc()


async def run_close_loop(
    ddb_client,
    redis_client,
    *,
    replica_id: str,
    interval_s: float = INTERVAL_S,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Forever-loop until `stop_event` is set. One poll cycle per `interval_s`.
    Catches all exceptions so a transient DDB hiccup doesn't kill the task."""
    if stop_event is None:
        stop_event = asyncio.Event()

    log.info("closer loop started replica=%s interval=%ss lookback=%ss", replica_id, interval_s, LOOKBACK_S)

    while not stop_event.is_set():
        try:
            now = int(datetime.now(tz=UTC).timestamp())
            candidates = await find_ending_auctions(ddb_client, now)
            for item_id in candidates:
                won = await try_claim(item_id, ddb_client, replica_id, now)
                if won:
                    await close_auction(item_id, ddb_client, redis_client)
        except Exception as e:
            log.warning("close loop iteration failed: %s", e)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

    log.info("closer loop stopped replica=%s", replica_id)


# Re-exported helpers used by main.py's create_item path.
__all__ = [
    "_bucket_for",
    "_buckets_in_range",
    "find_ending_auctions",
    "try_claim",
    "close_auction",
    "run_close_loop",
    "AUCTIONS_CLOSED",
    "INTERVAL_S",
    "LOOKBACK_S",
]


def bucket_for(end_time_epoch: int) -> str:
    """Public alias for `_bucket_for` so other modules don't import a private name."""
    return _bucket_for(end_time_epoch)
