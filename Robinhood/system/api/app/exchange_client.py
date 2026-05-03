"""Async httpx client to mock-exchange. Lifespan-managed (one pool per process)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx

EXCHANGE_URL = os.getenv("EXCHANGE_URL", "http://mock-exchange:8000")
TIMEOUT_S = float(os.getenv("EXCHANGE_TIMEOUT_S", "5.0"))


@asynccontextmanager
async def client_ctx():
    async with httpx.AsyncClient(base_url=EXCHANGE_URL, timeout=TIMEOUT_S) as c:
        yield c


async def place(
    client: httpx.AsyncClient,
    *,
    client_order_id: str,
    position: str,
    symbol: str,
    price_cents: int,
    num_shares: int,
    order_type: str,
) -> dict:
    """POST /exchange/orders. Idempotent on client_order_id at the exchange."""
    resp = await client.post(
        "/exchange/orders",
        json={
            "client_order_id": client_order_id,
            "position": position,
            "symbol": symbol,
            "price_cents": price_cents,
            "num_shares": num_shares,
            "order_type": order_type,
        },
    )
    resp.raise_for_status()
    return resp.json()


async def cancel(client: httpx.AsyncClient, *, external_order_id: str) -> int:
    """DELETE /exchange/orders/{eid}. Returns status code (200 or 404)."""
    resp = await client.delete(f"/exchange/orders/{external_order_id}")
    return resp.status_code


async def lookup_by_client(client: httpx.AsyncClient, *, client_order_id: str) -> dict | None:
    """GET /exchange/orders/by-client/{cid}. Returns body or None (404)."""
    resp = await client.get(f"/exchange/orders/by-client/{client_order_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()
