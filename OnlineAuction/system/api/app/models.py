"""Pydantic request/response models. DDB items are dicts (no ORM)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start_price: float = Field(ge=0)
    end_time_epoch: int = Field(ge=0)  # unix seconds


class ItemResponse(BaseModel):
    item_id: str
    title: str
    start_price: float
    end_time_epoch: int
    current_high_bid: float | None = None
    current_high_bidder: str | None = None
    closed_by: str | None = None


class BidRequest(BaseModel):
    bidder: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0)


class BidResponse(BaseModel):
    bid_id: str
    item_id: str
    bidder: str
    amount: float
    accepted: bool
