"""Mock external exchange.

Lab 0: sync REST place/cancel + lookup-by-client. In-memory state.
Idempotent on `client_order_id` — required by HelloInterview's order
consistency deep dive (clean-up worker reconciles via this lookup in lab 3).

Lab 1 will add an SSE trade feed.
Lab 3 will add failure-injection knobs (env-driven timeout/5xx rates).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field

VERSION = "lab00"


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.lock = asyncio.Lock()
    app.state.orders = {}        # external_order_id -> order dict
    app.state.client_index = {}  # client_order_id -> external_order_id
    yield


app = FastAPI(title="mock-exchange", version=VERSION, lifespan=lifespan)

PLACED = Counter("exchange_orders_placed_total", "Orders accepted by the mock exchange.")
PLACED_REPLAYED = Counter("exchange_orders_replayed_total", "Idempotent replays returning existing externalOrderId.")
CANCELED = Counter("exchange_orders_canceled_total", "Orders canceled.")


class PlaceOrder(BaseModel):
    client_order_id: str = Field(min_length=1, max_length=128)
    position: Literal["buy", "sell"]
    symbol: str
    price_cents: int = Field(ge=1)
    num_shares: int = Field(ge=1)
    order_type: Literal["market", "limit"]


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": VERSION})


@app.get("/metrics")
async def metrics() -> Response:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/exchange/orders")
async def place(body: PlaceOrder, request: Request) -> JSONResponse:
    state = request.app.state
    async with state.lock:
        if body.client_order_id in state.client_index:
            eid = state.client_index[body.client_order_id]
            PLACED_REPLAYED.inc()
            return JSONResponse({"external_order_id": eid, "status": "ACCEPTED", "replayed": True})

        eid = f"EXT-{uuid.uuid4().hex[:16]}"
        order = {
            "external_order_id": eid,
            "client_order_id": body.client_order_id,
            "position": body.position,
            "symbol": body.symbol.upper(),
            "price_cents": body.price_cents,
            "num_shares": body.num_shares,
            "order_type": body.order_type,
            "status": "ACCEPTED",
            "created_at": int(time.time() * 1000),
        }
        state.orders[eid] = order
        state.client_index[body.client_order_id] = eid
        PLACED.inc()
        return JSONResponse({"external_order_id": eid, "status": "ACCEPTED", "replayed": False})


@app.delete("/exchange/orders/{external_order_id}")
async def cancel(external_order_id: str, request: Request) -> JSONResponse:
    state = request.app.state
    async with state.lock:
        order = state.orders.get(external_order_id)
        if order is None:
            raise HTTPException(404, "unknown external_order_id")
        order["status"] = "CANCELED"
        CANCELED.inc()
        return JSONResponse({"external_order_id": external_order_id, "status": "CANCELED"})


@app.get("/exchange/orders/by-client/{client_order_id}")
async def lookup_by_client(client_order_id: str, request: Request) -> JSONResponse:
    state = request.app.state
    async with state.lock:
        eid = state.client_index.get(client_order_id)
        if eid is None:
            raise HTTPException(404, "unknown client_order_id")
        return JSONResponse(state.orders[eid])


@app.get("/debug/orders")
async def debug_orders(request: Request) -> JSONResponse:
    state = request.app.state
    async with state.lock:
        return JSONResponse({"count": len(state.orders), "orders": list(state.orders.values())})
