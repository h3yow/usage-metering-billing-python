"""HTTP 路由层：把请求交给服务层，并把 BillingError 转成结构化错误响应。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .billing_service import BillingService
from .errors import BillingError, ErrorCode
from .schemas import (
    BillResponse,
    CancelRequest,
    ChangePlanRequest,
    SubscribeRequest,
    UsageSubmitRequest,
)
from .subscription_service import SubscriptionService
from .usage_service import UsageService

logger = logging.getLogger("billing.api")

router = APIRouter()


def get_store(request: Request):
    return request.app.state.store


def billing_error_response(exc: BillingError) -> JSONResponse:
    """统一错误体：{error: {code, message, details}}，HTTP 409（冲突类）/422（其余）。"""
    conflict_codes = {"DUPLICATE_CONFLICT", "BILL_ALREADY_FINALIZED",
                      "ALREADY_SUBSCRIBED", "CONFLICT"}
    status_code = 409 if exc.code.value in conflict_codes else 422
    logger.info("拒绝请求 code=%s message=%s", exc.code.value, exc.message)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": exc.code.value,
                           "message": exc.message,
                           "details": exc.details}},
    )


def _naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bill_to_response(bill) -> BillResponse:
    return BillResponse(
        account_id=bill.account_id,
        period_start=bill.period_start.isoformat(),
        period_end=bill.period_end.isoformat(),
        status=bill.status.value,
        total_amount=str(bill.total_amount),
        version=bill.version,
        line_items=[li.to_dict() for li in bill.line_items],
    )


@router.post("/usage")
def submit_usage(req: UsageSubmitRequest, request: Request):
    store = get_store(request)
    svc = UsageService(store)
    try:
        event = svc.submit_usage(
            event_id=req.event_id, account_id=req.account_id, meter=req.meter,
            quantity=req.quantity, occurred_at=req.occurred_at,
        )
    except BillingError as exc:
        return billing_error_response(exc)
    return {
        "event_id": event.event_id,
        "account_id": event.account_id,
        "meter": event.meter,
        "quantity": str(event.quantity),
        "occurred_at": event.occurred_at,
        "period_start": event.period_start.isoformat(),
        "status": event.status.value,
        "detail": "幂等重复，未重复计费" if event.status.value == "DUPLICATE" else None,
        "reject_reason": None,
    }


@router.post("/accounts/{account_id}/subscribe")
def subscribe(account_id: str, req: SubscribeRequest, request: Request):
    svc = SubscriptionService(get_store(request))
    try:
        svc.subscribe(account_id, req.plan_id, _naive_utc(req.effective_from))
    except BillingError as exc:
        return billing_error_response(exc)
    return {"ok": True, "message": f"已订阅 {req.plan_id}"}


@router.post("/accounts/{account_id}/change-plan")
def change_plan(account_id: str, req: ChangePlanRequest, request: Request):
    svc = SubscriptionService(get_store(request))
    try:
        svc.change_plan(account_id, req.new_plan_id, _naive_utc(req.effective_at))
    except BillingError as exc:
        return billing_error_response(exc)
    return {"ok": True, "message": f"已变更为 {req.new_plan_id}"}


@router.post("/accounts/{account_id}/cancel")
def cancel(account_id: str, req: CancelRequest, request: Request):
    svc = SubscriptionService(get_store(request))
    try:
        _sub, granted, refundable = svc.cancel(
            account_id, _naive_utc(req.effective_at), req.refund_amount
        )
    except BillingError as exc:
        return billing_error_response(exc)
    return {"ok": True, "granted_refund": str(granted),
            "refundable_credit": str(refundable)}


@router.post("/accounts/{account_id}/bills/{period_start}/finalize")
def finalize_bill(account_id: str, period_start: str, request: Request):
    try:
        ps = date.fromisoformat(period_start)
    except ValueError:
        return billing_error_response(BillingError(
            ErrorCode.INVALID_FIELD,
            f"period_start 必须是 YYYY-MM-DD: {period_start!r}"))
    svc = BillingService(get_store(request))
    try:
        bill = svc.finalize(account_id, ps)
    except BillingError as exc:
        return billing_error_response(exc)
    return _bill_to_response(bill)


@router.get("/accounts/{account_id}/bills/{period_start}", response_model=BillResponse)
def get_bill(account_id: str, period_start: str, request: Request):
    from .errors import ErrorCode
    try:
        ps = date.fromisoformat(period_start)
    except ValueError:
        raise BillingError(ErrorCode.INVALID_FIELD,
                           f"period_start 必须是 YYYY-MM-DD: {period_start!r}")
    return _bill_to_response(BillingService(get_store(request)).get_bill(account_id, ps))
