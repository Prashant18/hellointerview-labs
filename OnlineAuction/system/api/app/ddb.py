"""DynamoDB Local glue: aioboto3 client + idempotent table creation.

We use the low-level `client` (not the resource) so the shape of every call
matches the AWS docs you'd reference at the whiteboard. Conditional writes
in lab 02+ are written against this surface directly.
"""

from __future__ import annotations

import asyncio
import os

import aioboto3

DDB_ENDPOINT = os.getenv("DDB_ENDPOINT", "http://dynamodb-local:8000")
AWS_REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
# Dummy creds — DynamoDB Local accepts anything; aioboto3 needs them set.
AWS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "local")
AWS_SECRET = os.getenv("AWS_SECRET_ACCESS_KEY", "local")

ITEMS_TABLE = "items"
BIDS_TABLE = "bids"


def session() -> aioboto3.Session:
    return aioboto3.Session(
        aws_access_key_id=AWS_KEY,
        aws_secret_access_key=AWS_SECRET,
        region_name=AWS_REGION,
    )


def client_ctx(sess: aioboto3.Session):
    """Return the async context manager for the DDB client."""
    return sess.client("dynamodb", endpoint_url=DDB_ENDPOINT)


async def _create_if_missing(client, *, table_name: str, key_schema, attr_defs) -> None:
    """Idempotent create. ResourceInUseException is benign — another replica
    raced us to it; we just wait for the table to be active."""
    try:
        await client.create_table(
            TableName=table_name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",
        )
    except client.exceptions.ResourceInUseException:
        pass  # someone else created it; fall through to the waiter
    await client.get_waiter("table_exists").wait(TableName=table_name)


async def ensure_tables(client) -> None:
    """Create items + bids tables if they don't exist. Retries until DDB is reachable.
    Safe under concurrent startup (3 api replicas race to create on first boot)."""
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
        table_name=ITEMS_TABLE,
        key_schema=[{"AttributeName": "item_id", "KeyType": "HASH"}],
        attr_defs=[{"AttributeName": "item_id", "AttributeType": "S"}],
    )
    await _create_if_missing(
        client,
        table_name=BIDS_TABLE,
        key_schema=[
            {"AttributeName": "item_id", "KeyType": "HASH"},
            {"AttributeName": "bid_id", "KeyType": "RANGE"},
        ],
        attr_defs=[
            {"AttributeName": "item_id", "AttributeType": "S"},
            {"AttributeName": "bid_id", "AttributeType": "S"},
        ],
    )
