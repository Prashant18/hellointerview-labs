"""Pydantic surface for the broker API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

OrderStatus = Literal[
    "PENDING", "SUBMITTED", "PARTIAL", "FILLED",
    "REJECTED", "PENDING_CANCEL", "CANCELED", "FAILED",
]


class OrderCreate(BaseModel):
    position: Literal["buy", "sell"]
    symbol: str = Field(min_length=1, max_length=10)
    price_cents: int = Field(ge=1, description="Limit price in cents. For market orders, ignored by mock-exchange.")
    num_shares: int = Field(ge=1)
    order_type: Literal["market", "limit"]


class OrderResponse(BaseModel):
    user_id: str
    order_id: str
    status: OrderStatus
    external_order_id: Optional[str] = None
    client_order_id: str
    position: Literal["buy", "sell"]
    symbol: str
    price_cents: int
    num_shares: int
    num_filled: int
    order_type: Literal["market", "limit"]
    created_at: int
    updated_at: int


class OrderList(BaseModel):
    orders: list[OrderResponse]
    next_cursor: Optional[str] = None
