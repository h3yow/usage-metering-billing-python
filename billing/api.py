"""FastAPI router exposing the billing service over HTTP.

Error mapping
-------------
* ``BillingError`` (validation / lifecycle) -> ``400`` with
  ``{"error": {"code": ..., "detail": ...}}`` so clients can branch on
  machine-readable reason codes.
* Rejected usage reports -> ``422`` with the same body shape; accepted
  and duplicate reports -> ``200``/``201``.

State
-----
A process-wide :class:`BillingService` singleton persists to
``./billing_data`` (override with ``BILLING_DATA_DIR``).  Tests should
call :func:`reset_state` or use ``billing.api.app_state`` fixtures.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from billing.errors import BillingError, ReasonCode
from billing.models import Bill, UsageEvent
from billing.service import BillingService
from billing.storage import Storage

router = APIRouter()

_service = BillingService(Storage(Path(os.environ.get("BILLING_DATA_DIR", "billing_data"))))


def get_service() -> BillingService:
    """The process-wide service instance (overridable in tests)."""
    return _service


def reset_state() -> None:
    """Drop all persisted state (used by tests and demos)."""
    _service._storage.reset()


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class AccountRequest(BaseModel):
    account_id: str = Field(min_length=1)


class PlanRequest(BaseModel):
    plan_code: str = Field(min_length=1)
    effective: date | None = None


class EffectiveRequest(BaseModel):
    effective: date | None = None


class RefundRequest(BaseModel):
    amount: Decimal
    reason: str = "manual_refund"


class UsageRequest(BaseModel):
    """Internal usage shape (explicit, reason-coded validation happens in
    the endpoint so clients can distinguish MISSING_FIELD from
    INVALID_VALUE)."""

    event_id: str
    account_id: str
    metric: str
    quantity: Decimal
    occurred_at: date


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------

def _dec(value: Decimal) -> str:
    return str(value)


def _bill_to_json(bill: Bill) -> dict[str, Any]:
    return {
        "account_id": bill.account_id,
        "period": bill.period_key,
        "period_start": bill.period_start.isoformat(),
        "period_end": bill.period_end.isoformat(),
        "base_fee_lines": [
            {
                "plan_code": line.plan_code,
                "start": line.start.isoformat(),
                "end": line.end.isoformat(),
                "days_active": line.days_active,
                "period_days": line.period_days,
                "monthly_fee": _dec(line.monthly_fee),
                "amount": _dec(line.amount),
            }
            for line in bill.base_fee_lines
        ],
        "usage_lines": [
            {
                "event_id": line.event_id,
                "metric": line.metric,
                "occurred_at": line.occurred_at.isoformat(),
                "plan_code": line.plan_code,
                "quantity": _dec(line.quantity),
                "free_portion": _dec(line.free_portion),
                "charged_portion": _dec(line.charged_portion),
                "tier_breakdown": [
                    {
                        "start": _dec(t.start),
                        "end": None if t.end is None else _dec(t.end),
                        "quantity": _dec(t.quantity),
                        "unit_price": _dec(t.unit_price),
                        "amount": _dec(t.amount),
                    }
                    for t in line.tier_breakdown
                ],
                "amount": _dec(line.amount),
            }
            for line in bill.usage_lines
        ],
        "credits": [
            {
                "reason": c.reason,
                "amount": _dec(c.amount),
                "created_at": c.created_at.isoformat(),
            }
            for c in bill.credits
        ],
        "total_base_fee": _dec(bill.total_base_fee),
        "total_usage": _dec(bill.total_usage),
        "total_credits": _dec(bill.total_credits),
        "total": _dec(bill.total),
        "generated_at": bill.generated_at.isoformat() if bill.generated_at else None,
    }


def _error_body(code: str, detail: str) -> dict[str, Any]:
    return {"error": {"code": code, "detail": detail}}


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

from fastapi import Response  # noqa: E402  (kept near endpoints for readability)


@router.post("/accounts", status_code=201)
def create_account(req: AccountRequest, response: Response):
    try:
        get_service().create_account(req.account_id)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return {"account_id": req.account_id, "status": "created"}


@router.post("/accounts/{account_id}/subscription")
def subscribe(account_id: str, req: PlanRequest, response: Response):
    try:
        get_service().subscribe(account_id, req.plan_code, req.effective)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return {"account_id": account_id, "plan_code": req.plan_code, "status": "subscribed"}


@router.post("/accounts/{account_id}/plan-change")
def change_plan(account_id: str, req: PlanRequest, response: Response):
    try:
        get_service().change_plan(account_id, req.plan_code, req.effective)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return {"account_id": account_id, "plan_code": req.plan_code, "status": "changed"}


@router.post("/accounts/{account_id}/cancel")
def cancel(account_id: str, req: EffectiveRequest, response: Response):
    try:
        get_service().cancel(account_id, req.effective)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return {"account_id": account_id, "status": "cancelled"}


@router.post("/accounts/{account_id}/refund")
def refund(account_id: str, req: RefundRequest, response: Response):
    try:
        get_service().refund(account_id, req.amount, req.reason)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return {"account_id": account_id, "status": "refunded", "amount": _dec(req.amount)}


@router.post("/usage")
def report_usage(body: dict[str, Any], response: Response):
    """Ingest a usage report.

    Body fields are validated explicitly so rejection causes stay
    distinguishable:

    * missing/empty required fields -> ``422 MISSING_FIELD``
    * unparseable date, non-numeric/NaN/Inf/non-positive quantity
      -> ``422 INVALID_VALUE``
    """
    required = ("event_id", "account_id", "metric", "quantity", "occurred_at")
    missing = [
        key
        for key in required
        if key not in body or body[key] is None or (isinstance(body[key], str) and not body[key].strip())
    ]
    if missing:
        response.status_code = 422
        return _error_body(
            ReasonCode.MISSING_FIELD,
            f"missing or empty required field(s): {', '.join(missing)}",
        )

    try:
        occurred = date.fromisoformat(str(body["occurred_at"]))
    except ValueError:
        response.status_code = 422
        return _error_body(
            ReasonCode.INVALID_VALUE,
            f"occurred_at must be an ISO date (YYYY-MM-DD), got {body['occurred_at']!r}",
        )

    raw_quantity = body["quantity"]
    try:
        if isinstance(raw_quantity, bool):
            raise InvalidOperation("boolean is not a quantity")
        quantity = Decimal(str(raw_quantity))
    except (InvalidOperation, ValueError):
        response.status_code = 422
        return _error_body(
            ReasonCode.INVALID_VALUE, f"quantity must be a number, got {raw_quantity!r}"
        )
    if not quantity.is_finite():
        response.status_code = 422
        return _error_body(
            ReasonCode.INVALID_VALUE, f"quantity must be finite, got {raw_quantity!r}"
        )

    event = UsageEvent(
        event_id=str(body["event_id"]),
        account_id=str(body["account_id"]),
        metric=str(body["metric"]),
        quantity=quantity,
        occurred_at=occurred,
    )
    result = get_service().report_usage(event)
    if result == "accepted":
        response.status_code = 201
        return {"event_id": event.event_id, "status": "accepted"}
    if result == "duplicate":
        return {"event_id": event.event_id, "status": "duplicate"}
    code = result.split(":", 1)[1] if ":" in result else ReasonCode.INVALID_VALUE
    response.status_code = 422
    # The authoritative detail is persisted; echo the reason code.
    return _error_body(code, f"usage report {event.event_id!r} rejected: {code}")


@router.post("/accounts/{account_id}/bills/close")
def close_period(account_id: str, period: date | None = None, response: Response = None):
    try:
        bill = get_service().close_period(account_id, period)
    except BillingError as exc:
        response.status_code = 400
        return _error_body(exc.code, exc.detail)
    return _bill_to_json(bill)


@router.get("/accounts/{account_id}/bills/{period}")
def get_bill(account_id: str, period: str, response: Response):
    try:
        bill = get_service().get_bill(account_id, period)
    except BillingError as exc:
        response.status_code = 404
        return _error_body(exc.code, exc.detail)
    return _bill_to_json(bill)


@router.get("/accounts/{account_id}/rejections")
def list_rejections(account_id: str):
    rejections = get_service()._storage.rejections_for_account(account_id)
    return {
        "account_id": account_id,
        "rejections": [
            {"event_id": r.event_id, "reason": r.reason, "detail": r.detail}
            for r in rejections
        ],
    }
