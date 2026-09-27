"""重复上报：同一 event_id 不重复计费；负载不一致按冲突拒绝；非法输入原因可区分。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.errors import BillingError, ErrorCode
from app.models import PlanStatus, Subscription, SubscriptionSegment
from app.usage_service import UsageService

log = logging.getLogger("test.idempotency")

SEP = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _subscribe(store, account="acct-1", plan="basic"):
    store.put_subscription(Subscription(account, [
        SubscriptionSegment(plan, SEP, None, PlanStatus.ACTIVE)
    ]))


def test_duplicate_event_not_double_charged(store):
    svc = UsageService(store)
    _subscribe(store)
    payload = dict(event_id="evt-1", account_id="acct-1", meter="api_calls",
                   quantity="5000", occurred_at="2026-09-10T08:00:00+00:00")
    log.info("输入事件(首次): %s", payload)

    first = svc.submit_usage(now=datetime(2026, 9, 10, 9, tzinfo=timezone.utc), **payload)
    log.info("判定依据: 首次见到 event_id=%s -> %s，归集账期=%s",
             first.event_id, first.status.value, first.period_start)
    assert first.status.value == "ACCEPTED"

    log.info("输入事件(重复重放，内容完全一致): %s", payload)
    second = svc.submit_usage(now=datetime(2026, 9, 11, tzinfo=timezone.utc), **payload)
    log.info("判定依据: event_id=%s 已存在且负载一致 -> %s，不落第二次账",
             second.event_id, second.status.value)
    assert second.status.value == "DUPLICATE"

    events = store.list_events("acct-1", date(2026, 9, 1))
    log.info("账期内已接受流水数=%d（期望 1）", len(events))
    assert len(events) == 1
    assert store.get_event("evt-1").status.value == "ACCEPTED"


def test_duplicate_conflicting_payload_rejected(store):
    svc = UsageService(store)
    _subscribe(store)
    first_payload = dict(event_id="evt-9", account_id="acct-1", meter="api_calls",
                         quantity="100", occurred_at="2026-09-10T08:00:00+00:00")
    log.info("输入事件(首次): %s", first_payload)
    svc.submit_usage(now=datetime(2026, 9, 10, 9, tzinfo=timezone.utc), **first_payload)

    conflict_payload = {**first_payload, "quantity": "200"}
    log.info("输入事件(同 event_id 但 quantity 不同): %s", conflict_payload)
    with pytest.raises(BillingError) as ei:
        svc.submit_usage(now=datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
                         **conflict_payload)
    log.info("判定依据: 同 event_id 负载不一致 -> code=%s", ei.value.code.value)
    assert ei.value.code is ErrorCode.DUPLICATE_CONFLICT
    # 冲突不能污染原账
    assert store.get_event("evt-9").quantity == Decimal("100")


def test_missing_and_invalid_fields_distinguished(store):
    svc = UsageService(store)
    _subscribe(store)
    now = datetime(2026, 9, 10, 9, tzinfo=timezone.utc)
    base = dict(event_id="e", account_id="acct-1", meter="api_calls",
                quantity="1", occurred_at="2026-09-10T08:00:00+00:00")

    cases = [
        ({**base, "event_id": None}, ErrorCode.MISSING_FIELD),
        ({**base, "account_id": None}, ErrorCode.MISSING_FIELD),
        ({**base, "meter": "  "}, ErrorCode.MISSING_FIELD),
        ({**base, "quantity": None}, ErrorCode.MISSING_FIELD),
        ({**base, "occurred_at": None}, ErrorCode.MISSING_FIELD),
        ({**base, "event_id": "bad1", "quantity": "-1"}, ErrorCode.NEGATIVE_USAGE),
        ({**base, "event_id": "bad2", "quantity": "abc"}, ErrorCode.INVALID_FIELD),
        ({**base, "event_id": "bad3", "quantity": 1.5}, ErrorCode.INVALID_FIELD),
        ({**base, "event_id": "bad4", "occurred_at": "not-a-date"},
         ErrorCode.INVALID_FIELD),
        ({**base, "event_id": "bad5",
          "occurred_at": "2026-09-20T00:00:00+00:00"}, ErrorCode.USAGE_IN_FUTURE),
        ({**base, "event_id": "bad6", "meter": "unknown_meter"},
         ErrorCode.UNKNOWN_METER),
        ({**base, "account_id": "nobody", "event_id": "bad7"},
         ErrorCode.ACCOUNT_NOT_FOUND),
    ]
    for payload, expected in cases:
        log.info("输入事件: %s 期望拒绝码=%s", payload, expected.value)
        with pytest.raises(BillingError) as ei:
            svc.submit_usage(now=now, **payload)
        log.info("判定依据: -> code=%s (%s)", ei.value.code.value, ei.value.message)
        assert ei.value.code is expected, (payload, ei.value.code)


def test_usage_without_active_plan_rejected(store):
    svc = UsageService(store)  # acct-1 存在但未订阅
    log.info("输入事件: 未订阅账户上报用量")
    with pytest.raises(BillingError) as ei:
        svc.submit_usage(
            event_id="e", account_id="acct-1", meter="api_calls",
            quantity="1", occurred_at="2026-09-10T08:00:00+00:00",
            now=datetime(2026, 9, 10, 9, tzinfo=timezone.utc),
        )
    log.info("判定依据: 发生时刻无有效套餐 -> code=%s", ei.value.code.value)
    assert ei.value.code is ErrorCode.NO_ACTIVE_PLAN
