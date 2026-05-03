"""OnlineAuction api — see ../../JOURNAL.md for what each lab adds.

Lab 00: setup. /v1 endpoints exist; bid handler is INTENTIONALLY naive (no
race protection) — lab 01 will demolish it, lab 02 will fix it with a
ConditionExpression on UpdateItem.
"""

from __future__ import annotations

import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from decimal import Decimal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app import ddb
from app.models import BidRequest, BidResponse, ItemCreate, ItemResponse

API_VERSION = "lab00"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open one DDB client per process; ensure tables exist on startup."""
    sess = ddb.session()
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(ddb.client_ctx(sess))
        await ddb.ensure_tables(client)
        app.state.ddb = client
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
    "auction_bids_rejected_total", "Bids rejected.", ["reason"]  # too_low | item_missing
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
    """Lab 00 NAIVE bid handler — read item, check in Python, write.

    This implementation has a TOCTOU race that lab 01 demonstrates and
    lab 02 fixes by collapsing into a single conditional UpdateItem."""
    client = request.app.state.ddb

    # 1) Read current item state.
    resp = await client.get_item(
        TableName=ddb.ITEMS_TABLE, Key={"item_id": {"S": item_id}}
    )
    if "Item" not in resp:
        BIDS_REJECTED.labels(reason="item_missing").inc()
        raise HTTPException(404, f"item {item_id} not found")
    item = resp["Item"]
    current_high = float(item["current_high_bid"]["N"]) if "current_high_bid" in item else 0.0
    start_price = float(item["start_price"]["N"])

    # 2) Validate in the application — naive (the bug lab 01 will expose).
    floor = max(current_high, start_price)
    if body.amount <= floor:
        BIDS_REJECTED.labels(reason="too_low").inc()
        raise HTTPException(409, f"bid {body.amount} not greater than current floor {floor}")

    # 3) Insert bid record.
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

    # 4) Update item's current high — UNCONDITIONAL. Last write wins. BUG.
    await client.update_item(
        TableName=ddb.ITEMS_TABLE,
        Key={"item_id": {"S": item_id}},
        UpdateExpression="SET current_high_bid = :a, current_high_bidder = :b",
        ExpressionAttributeValues={
            ":a": {"N": str(body.amount)},
            ":b": {"S": body.bidder},
        },
    )

    BIDS_ACCEPTED.inc()
    return BidResponse(
        bid_id=bid_id, item_id=item_id, bidder=body.bidder, amount=body.amount, accepted=True
    )
