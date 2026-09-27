"""乱序/迟到事件：账期按 occurred_at 归属；出账前后行为明确、可复现。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.billing_service import BillingService
from app.errors import BillingError, ErrorCode
from app.models import PlanStatus, Subscription, SubscriptionSegment
from app.usage_service import UsageService

log = logging.getLogger("test.order")

SEP = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _setup(store):
    store.put_subscription(Subscription("acct-1", [
        SubscriptionSegment("basic", SEP, None, PlanStatus.ACTIVE)
    ]))
    return UsageService(store), BillingService(store)


def test_late_event_lands_in_correct_period(store):
    us, bs = _setup(store)
    # 先收到 9/20 的事件，再收到 9/5 的“迟到”事件；账期都应是 2026-09
    p1 = dict(event_id="late-1", account_id="acct-1", meter="api_calls",
              quantity="5000", occurred_at="2026-09-20T08:00:00+00:00")
    log.info("输入事件(先到): %s，接收时刻 9/20", p1)
    us.submit_usage(now=datetime(2026, 9, 20, 9, tzinfo=timezone.utc), **p1)

    p2 = dict(event_id="late-2", account_id="acct-1", meter="api_calls",
              quantity="5000", occurred_at="2026-09-05T08:00:00+00:00")
    log.info("输入事件(迟到/乱序): %s，接收时刻 9/25", p2)
    ev2 = us.submit_usage(now=datetime(2026, 9, 25, 9, tzinfo=timezone.utc), **p2)
    log.info("判定依据: 按 occurred_at(而非 received_at) 归集 -> 账期=%s", ev2.period_start)
    assert ev2.period_start == date(2026, 9, 1)

    bill = bs.build_draft("acct-1", date(2026, 9, 1))
    ids = [li.event_id for li in bill.line_items]
    log.info("草稿账单明细顺序(按 occurred_at 排序): %s", ids)
    assert ids == ["late-2", "late-1"]
    # 两笔各 80.00
    assert bill.total_amount == Decimal("160.00"), bill.total_amount


def test_draft_rebuild_is_reproducible(store):
    us, bs = _setup(store)
    for i, day in enumerate((3, 8, 22), start=1):
        payload = dict(event_id=f"r{i}", account_id="acct-1", meter="storage_gb",
                       quantity="10", occurred_at=f"2026-09-{day:02d}T00:00:00+00:00")
        log.info("输入事件: %s", payload)
        us.submit_usage(now=datetime(2026, 9, day, 12, tzinfo=timezone.utc), **payload)

    b1 = bs.build_draft("acct-1", date(2026, 9, 1))
    b2 = bs.build_draft("acct-1", date(2026, 9, 1))
    log.info("两次重算总额: %s / %s；明细数: %d / %d",
             b1.total_amount, b2.total_amount, len(b1.line_items), len(b2.line_items))
    assert b1.total_amount == b2.total_amount == Decimal("3.00")
    assert [li.to_dict() for li in b1.line_items] == [li.to_dict() for li in b2.line_items]


def test_late_event_after_finalize_is_rejected_not_silently_dropped(store):
    us, bs = _setup(store)
    log.info("输入事件: 9/10 正常用量并出账 9 月账期")
    us.submit_usage(event_id="f1", account_id="acct-1", meter="api_calls",
                    quantity="5000", occurred_at="2026-09-10T08:00:00+00:00",
                    now=datetime(2026, 9, 10, 9, tzinfo=timezone.utc))
    bill = bs.finalize("acct-1", date(2026, 9, 1))
    frozen_total = bill.total_amount
    log.info("账单已冻结 version=%d total=%s", bill.version, frozen_total)

    late = dict(event_id="f2", account_id="acct-1", meter="api_calls",
                quantity="5000", occurred_at="2026-09-08T08:00:00+00:00")
    log.info("输入事件(冻结后才到达的 9 月用量): %s，接收时刻 10/3", late)
    with pytest.raises(BillingError) as ei:
        us.submit_usage(now=datetime(2026, 10, 3, 9, tzinfo=timezone.utc), **late)
    log.info("判定依据: 归属账期已冻结，拒绝改写历史 -> code=%s", ei.value.code.value)
    assert ei.value.code is ErrorCode.BILL_ALREADY_FINALIZED

    again = bs.finalize("acct-1", date(2026, 9, 1))
    log.info("历史账单复核: total=%s（期望仍为 %s）", again.total_amount, frozen_total)
    assert again.total_amount == frozen_total
    assert len(again.line_items) == 1
