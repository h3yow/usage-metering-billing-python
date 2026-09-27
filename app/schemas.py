"""HTTP 入参/出参 schema（Pydantic）。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel


class UsageSubmitRequest(BaseModel):
    event_id: str | None = None
    account_id: str | None = None
    meter: str | None = None
    quantity: Any = None          # 容忍字符串/数字，非法值由服务层区分拒绝
    occurred_at: Any = None       # ISO8601 字符串或缺失


class UsageSubmitResponse(BaseModel):
    event_id: str
    account_id: str
    meter: str
    quantity: str
    occurred_at: datetime
    period_start: str
    status: str
    detail: str | None = None
    reject_reason: str | None = None


class SubscribeRequest(BaseModel):
    plan_id: str
    effective_from: datetime | None = None


class ChangePlanRequest(BaseModel):
    new_plan_id: str
    effective_at: datetime | None = None


class CancelRequest(BaseModel):
    effective_at: datetime | None = None
    refund_amount: Decimal | None = None


class BillResponse(BaseModel):
    account_id: str
    period_start: str
    period_end: str
    status: str
    total_amount: str
    version: int
    line_items: list[dict[str, Any]]


class SimpleMessage(BaseModel):
    ok: bool = True
    message: str = ""
