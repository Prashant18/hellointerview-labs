"""Robinhood broker api — see ../../JOURNAL.md for what each lab adds.

Lab 00: happy-path order placement. Naive flow:
  PutItem PENDING → POST mock-exchange → UpdateItem SUBMITTED + external_order_id.
Idempotency: Idempotency-Key header → client_order_id; pre-call dedupe via GSI G3.
No SSE, no trade-feed, no failure recovery yet — labs 1-3.
"""

from __future__ import annotations

import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from app import ddb, exchange_client
from app.models import OrderCreate, OrderList, OrderResponse

API_VERSION = "lab00"

# Lab 0 has no auth; every request is "user-1". Real systems extract from JWT.
DEMO_USER_ID = "user-1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    sess = ddb.session()
    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(ddb.client_ctx(sess))
        await ddb.ensure_tables(client)
        app.state.ddb = client
        app.state.exchange = await stack.enter_async_context(exchange_client.client_ctx())
        yield


app = FastAPI(title="broker-api", version=API_VERSION, lifespan=lifespan)

REQUESTS = Counter(
    "api_requests_total", "HTTP requests handled.", ["path", "status"]
)
LATENCY = Histogram(
    "api_request_duration_seconds", "Request handler latency.", ["path"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
ORDERS_PLACED = Counter("orders_placed_total", "Orders accepted by mock-exchange.")
ORDERS_REJECTED = Counter("orders_rejected_total", "Orders rejected by mock-exchange.", ["reason"])
ORDERS_REPLAYED = Counter("orders_replayed_total", "Idempotent replays returning existing order.")


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


async def _find_by_client_order_id(ddb_client, client_order_id: str) -> Optional[dict]:
    """GSI lookup. Returns the FULL order row (re-read by PK) if found, else None."""
    resp = await ddb_client.query(
        TableName=ddb.ORDERS_TABLE,
        IndexName=ddb.GSI_BY_CLIENT,
        KeyConditionExpression="client_order_id = :c",
        ExpressionAttributeValues={":c": {"S": client_order_id}},
        Limit=1,
    )
    items = resp.get("Items", [])
    if not items:
        return None
    # GSI projects (user_id, order_id, status). Re-read for full row.
    user_id = items[0]["user_id"]["S"]
    order_id = items[0]["order_id"]["S"]
    full = await ddb_client.get_item(
        TableName=ddb.ORDERS_TABLE,
        Key={"user_id": {"S": user_id}, "order_id": {"S": order_id}},
    )
    return full.get("Item")


@app.post("/v1/orders", status_code=201)
async def place_order(
    body: OrderCreate,
    request: Request,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> OrderResponse:
    """Lab 00: PENDING → exchange → SUBMITTED (or REJECTED). Idempotent on
    client_order_id (server-generated UUID4 if Idempotency-Key absent)."""
    ddb_client = request.app.state.ddb
    exchange = request.app.state.exchange

    client_order_id = idempotency_key or uuid.uuid4().hex
    now_ms = int(time.time() * 1000)

    # 1) Idempotency: did we already process this client_order_id?
    existing = await _find_by_client_order_id(ddb_client, client_order_id)
    if existing is not None:
        ORDERS_REPLAYED.inc()
        return OrderResponse(**ddb.unwrap_order(existing))

    # 2) Write PENDING first so a crash before the exchange call leaves a
    #    recoverable row keyed by client_order_id (lab 3 cleanup-worker).
    order_id = uuid.uuid4().hex
    await ddb_client.put_item(
        TableName=ddb.ORDERS_TABLE,
        Item={
            "user_id": {"S": DEMO_USER_ID},
            "order_id": {"S": order_id},
            "status": {"S": "PENDING"},
            "client_order_id": {"S": client_order_id},
            "position": {"S": body.position},
            "symbol": {"S": body.symbol},
            "price_cents": {"N": str(body.price_cents)},
            "num_shares": {"N": str(body.num_shares)},
            "num_filled": {"N": "0"},
            "order_type": {"S": body.order_type},
            "created_at": {"N": str(now_ms)},
            "updated_at": {"N": str(now_ms)},
        },
        ConditionExpression="attribute_not_exists(order_id)",
    )

    # 3) Call exchange.
    try:
        ex_resp = await exchange_client.place(
            exchange,
            client_order_id=client_order_id,
            position=body.position,
            symbol=body.symbol,
            price_cents=body.price_cents,
            num_shares=body.num_shares,
            order_type=body.order_type,
        )
    except httpx.HTTPStatusError as e:
        # 4xx from exchange → REJECTED.
        if e.response.status_code < 500:
            await _update_status(ddb_client, order_id, "REJECTED", now_ms)
            ORDERS_REJECTED.labels(reason="exchange_4xx").inc()
            raise HTTPException(400, f"exchange rejected: {e.response.text}")
        raise  # 5xx bubbles → row stays PENDING; lab 3 cleanup recovers
    except Exception:
        raise  # network/timeout → PENDING; lab 3 cleanup recovers

    # 4) UpdateItem to SUBMITTED with external_order_id.
    external_order_id = ex_resp["external_order_id"]
    now_ms = int(time.time() * 1000)
    await ddb_client.update_item(
        TableName=ddb.ORDERS_TABLE,
        Key={"user_id": {"S": DEMO_USER_ID}, "order_id": {"S": order_id}},
        UpdateExpression="SET #s = :s, external_order_id = :e, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": {"S": "SUBMITTED"},
            ":e": {"S": external_order_id},
            ":u": {"N": str(now_ms)},
        },
    )

    ORDERS_PLACED.inc()
    return OrderResponse(
        user_id=DEMO_USER_ID,
        order_id=order_id,
        status="SUBMITTED",
        external_order_id=external_order_id,
        client_order_id=client_order_id,
        position=body.position,
        symbol=body.symbol,
        price_cents=body.price_cents,
        num_shares=body.num_shares,
        num_filled=0,
        order_type=body.order_type,
        created_at=now_ms,
        updated_at=now_ms,
    )


async def _update_status(ddb_client, order_id: str, status: str, now_ms: int) -> None:
    await ddb_client.update_item(
        TableName=ddb.ORDERS_TABLE,
        Key={"user_id": {"S": DEMO_USER_ID}, "order_id": {"S": order_id}},
        UpdateExpression="SET #s = :s, updated_at = :u",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": {"S": status}, ":u": {"N": str(now_ms)}},
    )


@app.delete("/v1/orders/{order_id}")
async def cancel_order(order_id: str, request: Request) -> JSONResponse:
    """Lab 00: best-effort cancel — status flip + exchange call. Lab 3 hardens
    this with PENDING_CANCEL state and cleanup-worker reconciliation."""
    ddb_client = request.app.state.ddb
    exchange = request.app.state.exchange

    resp = await ddb_client.get_item(
        TableName=ddb.ORDERS_TABLE,
        Key={"user_id": {"S": DEMO_USER_ID}, "order_id": {"S": order_id}},
    )
    if "Item" not in resp:
        raise HTTPException(404, f"order {order_id} not found")
    item = resp["Item"]
    status = item["status"]["S"]
    if status not in ("SUBMITTED", "PARTIAL"):
        raise HTTPException(409, f"cannot cancel order in status {status}")

    external_order_id = item.get("external_order_id", {}).get("S")
    if external_order_id:
        await exchange_client.cancel(exchange, external_order_id=external_order_id)

    await _update_status(ddb_client, order_id, "CANCELED", int(time.time() * 1000))
    return JSONResponse({"ok": True, "order_id": order_id, "status": "CANCELED"})


@app.get("/v1/orders")
async def list_orders(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
) -> OrderList:
    """Cursor-paginated by (user_id, order_id). Cursor is the last seen order_id."""
    ddb_client = request.app.state.ddb
    kwargs = {
        "TableName": ddb.ORDERS_TABLE,
        "KeyConditionExpression": "user_id = :u",
        "ExpressionAttributeValues": {":u": {"S": DEMO_USER_ID}},
        "Limit": limit,
        "ScanIndexForward": False,  # newest first
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = {
            "user_id": {"S": DEMO_USER_ID},
            "order_id": {"S": cursor},
        }
    resp = await ddb_client.query(**kwargs)
    orders = [OrderResponse(**ddb.unwrap_order(it)) for it in resp.get("Items", [])]
    next_cursor = None
    last = resp.get("LastEvaluatedKey")
    if last:
        next_cursor = last["order_id"]["S"]
    return OrderList(orders=orders, next_cursor=next_cursor)
