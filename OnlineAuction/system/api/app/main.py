"""OnlineAuction api — see ../../JOURNAL.md for what each lab adds."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app import ddb, redis_client
from app.models import BidRequest, BidResponse, ItemCreate, ItemResponse

API_VERSION = "lab03"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open one DDB client + one Redis pool per process; ensure tables exist."""
    sess = ddb.session()
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(ddb.client_ctx(sess))
        await ddb.ensure_tables(client)
        app.state.ddb = client
        app.state.redis = await stack.enter_async_context(redis_client.client_ctx())
        yield


app = FastAPI(title="auction-api", version=API_VERSION, lifespan=lifespan)

REQUESTS = Counter(
    "api_requests_total",
    "HTTP requests handled, labeled by path and status.",
    ["path", "status"],
)
LATENCY = Histogram(
    "api_request_duration_seconds",
    "Request handler latency.",
    ["path"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
BIDS_ACCEPTED = Counter("auction_bids_accepted_total", "Bids accepted (status 201).")
BIDS_REJECTED = Counter(
    "auction_bids_rejected_total",
    "Bids rejected.",
    ["reason"],  # too_low | item_missing
)


@app.middleware("http")
async def observe(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        elapsed = time.perf_counter() - start
        path = request.url.path
        REQUESTS.labels(path=path, status=str(status)).inc()
        LATENCY.labels(path=path).observe(elapsed)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": API_VERSION})


@app.get("/metrics")
async def metrics() -> Response:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _item_from_ddb(item: dict) -> ItemResponse:
    """Translate a DDB Item (typed AttributeValue dict) to our response model."""
    g = item.get
    return ItemResponse(
        item_id=g("item_id", {}).get("S", ""),
        title=g("title", {}).get("S", ""),
        start_price=float(g("start_price", {}).get("N", "0")),
        end_time_epoch=int(g("end_time_epoch", {}).get("N", "0")),
        current_high_bid=(
            float(item["current_high_bid"]["N"]) if "current_high_bid" in item else None
        ),
        current_high_bidder=item.get("current_high_bidder", {}).get("S"),
        closed_by=item.get("closed_by", {}).get("S"),
    )


@app.post("/v1/items", status_code=201)
async def create_item(body: ItemCreate, request: Request) -> ItemResponse:
    item_id = uuid.uuid4().hex
    await request.app.state.ddb.put_item(
        TableName=ddb.ITEMS_TABLE,
        Item={
            "item_id": {"S": item_id},
            "title": {"S": body.title},
            "start_price": {"N": str(body.start_price)},
            "end_time_epoch": {"N": str(body.end_time_epoch)},
        },
    )
    return ItemResponse(
        item_id=item_id,
        title=body.title,
        start_price=body.start_price,
        end_time_epoch=body.end_time_epoch,
    )


@app.get("/v1/items/{item_id}")
async def get_item(item_id: str, request: Request) -> ItemResponse:
    resp = await request.app.state.ddb.get_item(
        TableName=ddb.ITEMS_TABLE, Key={"item_id": {"S": item_id}}
    )
    if "Item" not in resp:
        raise HTTPException(404, f"item {item_id} not found")
    return _item_from_ddb(resp["Item"])


@app.post("/v1/items/{item_id}/bids", status_code=201)
async def place_bid(item_id: str, body: BidRequest, request: Request) -> BidResponse:
    """Lab 02: single conditional UpdateItem — DynamoDB enforces the
    'strictly higher than current_high_bid' invariant server-side. Concurrent
    equal-amount bids: exactly one wins, the rest get ConditionalCheckFailed → 409."""
    client = request.app.state.ddb

    bid_id = f"{int(time.time() * 1000):013d}-{uuid.uuid4().hex[:8]}"
    await client.put_item(
        TableName=ddb.BIDS_TABLE,
        Item={
            "item_id": {"S": item_id},
            "bid_id": {"S": bid_id},
            "bidder": {"S": body.bidder},
            "amount": {"N": str(body.amount)},
            "ts": {"N": str(int(time.time() * 1000))},
        },
    )

    try:
        await client.update_item(
            TableName=ddb.ITEMS_TABLE,
            Key={"item_id": {"S": item_id}},
            UpdateExpression="SET current_high_bid = :a, current_high_bidder = :b",
            ConditionExpression="attribute_exists(item_id) AND (attribute_not_exists(current_high_bid) OR current_high_bid < :a)",
            ExpressionAttributeValues={
                ":a": {"N": str(body.amount)},
                ":b": {"S": body.bidder},
            },
        )
    except client.exceptions.ConditionalCheckFailedException:
        BIDS_REJECTED.labels(reason="too_low").inc()
        raise HTTPException(409, "bid no longer beats the current high")

    # Lab 03: fanout. Best-effort publish — if Redis is down the bid is still
    # committed (DDB is source of truth); pubsub is the live-update channel only.
    payload = json.dumps(
        {
            "amount": float(body.amount),
            "bidder": body.bidder,
            "ts": int(time.time() * 1000),
        }
    )
    try:
        await request.app.state.redis.publish(f"auction:{item_id}", payload)
    except Exception:
        pass

    BIDS_ACCEPTED.inc()
    return BidResponse(
        bid_id=bid_id,
        item_id=item_id,
        bidder=body.bidder,
        amount=body.amount,
        accepted=True,
    )


@app.get("/v1/items/{item_id}/stream")
async def stream(item_id: str, request: Request) -> StreamingResponse:
    """SSE stream of bid events for one item."""
    return StreamingResponse(
        _event_stream(request.app.state.redis, f"auction:{item_id}", request),
        media_type="text/event-stream",
    )


async def _event_stream(redis, channel: str, request: Request) -> AsyncIterator[bytes]:
    """Lab 03 — USER TO IMPLEMENT.

    Yield SSE frames for each message published to `channel`.

    Contract (see tests/test_stream.py for the assertions):
      - Get a pubsub object: ``ps = redis.pubsub()`` then ``await ps.subscribe(channel)``.
      - Loop: poll ``await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)``.
        * If msg is not None and msg.get("type") == "message":
            yield  b"data: " + msg["data"] + b"\\n\\n"
        * Periodically check ``await request.is_disconnected()`` — break out if True.
      - On exit (via ``finally``): ``await ps.unsubscribe(channel)`` then ``await ps.aclose()``.

    Why this shape:
      - Yielding bytes lets FastAPI write the SSE frame straight to the socket.
      - Polling with a 1s timeout (vs ``listen()``) lets us check the disconnect
        signal between polls — otherwise a slow auction would hold the connection
        open forever after the client navigated away.
      - ``finally`` cleanup matters: without it, every dropped client leaks a
        Redis subscriber until the pool fills up.
    """
    ps = redis.pubsub()
    try:
        await ps.subscribe(channel)
        while not await request.is_disconnected():
            msg = await ps.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg and msg.get("type") == "message":
                yield b"data: " + msg["data"] + b"\n\n"
    finally:
        await ps.unsubscribe(channel)
        await ps.aclose()
