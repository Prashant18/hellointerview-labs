"""DynamoDB Local glue: aioboto3 client + idempotent table creation.

Three GSIs on `orders` set up for the staff+ deep dives that arrive in later labs:
  G1 (by-external-order-id) — trade-processor reverse lookup (lab 2).
  G2 (pending-by-status)    — sparse; cleanup-worker scan (lab 3).
  G3 (by-client-order-id)   — idempotent retry on POST /v1/orders (used in lab 0).

Sparse on G2 means: only rows that still have `status` set appear in the index.
Terminal-status flow (FILLED/CANCELED/REJECTED) writes the status normally —
and at that point we don't need it in the cleanup scan, so we leave it. The
"sparse" payoff only really kicks in if we choose to REMOVE on terminal; we
keep it simple for lab 0 and project KEYS_ONLY so the index stays small even
if it ends up populated for terminal rows too. (Lab 3 may revisit.)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import aioboto3

DDB_ENDPOINT = os.getenv("DDB_ENDPOINT", "http://dynamodb-local:8000")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
AWS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "local")
AWS_SECRET = os.getenv("AWS_SECRET_ACCESS_KEY", "local")

ORDERS_TABLE = "orders"
SYMBOLS_TABLE = "symbols"

GSI_BY_EXTERNAL = "by-external-order-id"
GSI_BY_STATUS = "pending-by-status"
GSI_BY_CLIENT = "by-client-order-id"


def session() -> aioboto3.Session:
    return aioboto3.Session(
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET,
        region_name=AWS_REGION,
    )


def client_ctx(sess: aioboto3.Session):
    return sess.client("dynamodb", endpoint_url=DDB_ENDPOINT)


async def _create_if_missing(client, *, table_name: str, key_schema, attr_defs, gsis=None) -> None:
    kwargs: dict[str, Any] = {
        "TableName": table_name,
        "KeySchema": key_schema,
        "AttributeDefinitions": attr_defs,
        "BillingMode": "PAY_PER_REQUEST",
    }
    if gsis:
        kwargs["GlobalSecondaryIndexes"] = gsis
    try:
        await client.create_table(**kwargs)
    except client.exceptions.ResourceInUseException:
        pass
    await client.get_waiter("table_exists").wait(TableName=table_name)


async def ensure_tables(client) -> None:
    """Create `orders` + `symbols` if missing. Retries until DDB is reachable.
    Safe under concurrent startup (3 api replicas race on first boot)."""
    for _ in range(30):
        try:
            await client.list_tables()
            break
        except Exception:
            await asyncio.sleep(1)
    else:
        raise RuntimeError("DynamoDB Local never came up")

    await _create_if_missing(
        client,
        table_name=ORDERS_TABLE,
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "order_id", "KeyType": "RANGE"},
        ],
        attr_defs=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "external_order_id", "AttributeType": "S"},
            {"AttributeName": "client_order_id", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "N"},
        ],
        gsis=[
            {
                "IndexName": GSI_BY_EXTERNAL,
                "KeySchema": [
                    {"AttributeName": "external_order_id", "KeyType": "HASH"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["user_id", "order_id", "status"],
                },
            },
            {
                "IndexName": GSI_BY_STATUS,
                "KeySchema": [
                    {"AttributeName": "status", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                "IndexName": GSI_BY_CLIENT,
                "KeySchema": [
                    {"AttributeName": "client_order_id", "KeyType": "HASH"},
                ],
                "Projection": {
                    "ProjectionType": "INCLUDE",
                    "NonKeyAttributes": ["user_id", "order_id", "status"],
                },
            },
        ],
    )

    await _create_if_missing(
        client,
        table_name=SYMBOLS_TABLE,
        key_schema=[{"AttributeName": "symbol", "KeyType": "HASH"}],
        attr_defs=[{"AttributeName": "symbol", "AttributeType": "S"}],
    )


def unwrap_order(item: dict) -> dict:
    """Translate a DDB AttributeValue dict for an orders row into a plain dict."""
    g = item.get
    return {
        "user_id": g("user_id", {}).get("S", ""),
        "order_id": g("order_id", {}).get("S", ""),
        "status": g("status", {}).get("S", "PENDING"),
        "external_order_id": g("external_order_id", {}).get("S"),
        "client_order_id": g("client_order_id", {}).get("S", ""),
        "position": g("position", {}).get("S", ""),
        "symbol": g("symbol", {}).get("S", ""),
        "price_cents": int(g("price_cents", {}).get("N", "0")),
        "num_shares": int(g("num_shares", {}).get("N", "0")),
        "num_filled": int(g("num_filled", {}).get("N", "0")),
        "order_type": g("order_type", {}).get("S", "limit"),
        "created_at": int(g("created_at", {}).get("N", "0")),
        "updated_at": int(g("updated_at", {}).get("N", "0")),
    }
