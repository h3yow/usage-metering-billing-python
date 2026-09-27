"""周期中改套餐/退订：按段计价、退款折算、历史账单冻结、非法操作拒绝。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.billing_service import BillingService
from app.errors import BillingError, ErrorCode
from app.subscription_service import SubscriptionService
from app.usage_service import UsageService

log = logging.getLogger("test.plan")


def _services(store):
    return (SubscriptionService(store), UsageService(store), BillingService(store))


def test_mid_cycle_plan_change_prorated_by_segments(store):
    ss, us, bs = _services(store)
    sep = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ss.subscribe("acct-1", "basic", sep)

    # 变更前（basic / tiered）：5000 次 -> 4000*0.02 = 80.00
    us.submit_usage(event_id="b1", account_id="acct-1", meter="api_calls",
                    quantity="5000", occurred_at="2026-09-10T00:00:00+00:00",
                    now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    log.info("输入: 9/15 由 basic 变更为 pro（月中变更，按各自区间计价）")
    ss.change_plan("acct-1", "pro", datetime(2026, 9, 15, tzinfo=timezone.utc))
    # 变更后（pro / graduated）：110000 次 -> 100000*0.01 + 5000*0.005 = 1025
    us.submit_usage(event_id="p1", account_id="acct-1", meter="api_calls",
                    quantity="110000", occurred_at="2026-09-20T00:00:00+00:00",
                    now=datetime(2026, 9, 20, tzinfo=timezone.utc))

    bill = bs.finalize("acct-1", date(2026, 9, 1))
    by_id = {li.event_id: li for li in bill.line_items}
    log.info("判定依据: b1 按 basic 计价=%s；p1 按 pro 计价=%s",
             by_id["b1"].amount, by_id["p1"].amount)
    assert by_id["b1"].plan_id == "basic" and by_id["b1"].amount == Decimal("80.00")
    assert by_id["p1"].plan_id == "pro" and by_id["p1"].amount == Decimal("1025.00")
    assert bill.total_amount == Decimal("1105.00")

    # 下个月完全按 pro
    us.submit_usage(event_id="o1", account_id="acct-1", meter="api_calls",
                    quantity="110000", occurred_at="2026-10-02T00:00:00+00:00",
                    now=datetime(2026, 10, 2, tzinfo=timezone.utc))
    oct_bill = bs.finalize("acct-1", date(2026, 10, 1))
    assert oct_bill.line_items[0].plan_id == "pro"
    assert oct_bill.total_amount == Decimal("1025.00")


def test_finalized_bill_not_rewritten(store):
    ss, us, bs = _services(store)
    ss.subscribe("acct-1", "basic", datetime(2026, 9, 1, tzinfo=timezone.utc))
    us.submit_usage(event_id="v1", account_id="acct-1", meter="api_calls",
                    quantity="5000", occurred_at="2026-09-10T00:00:00+00:00",
                    now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    bill = bs.finalize("acct-1", date(2026, 9, 1))
    frozen = (bill.total_amount, bill.version)
    log.info("9 月账单冻结 total=%s version=%d，之后改套餐不应影响历史", *frozen)

    ss.change_plan("acct-1", "pro", datetime(2026, 9, 25, tzinfo=timezone.utc))
    rebuilt = bs.finalize("acct-1", date(2026, 9, 1))  # 幂等返回
    log.info("判定依据: 重复出账 total=%s version=%s（期望 %s/%s 原样）",
             rebuilt.total_amount, rebuilt.version, frozen[0], frozen[1])
    assert rebuilt.total_amount == frozen[0]
    assert rebuilt.version == frozen[1]
    assert rebuilt.line_items[0].plan_id == "basic"

    with pytest.raises(BillingError) as ei:
        bs.build_draft("acct-1", date(2026, 9, 1))
    assert ei.value.code is ErrorCode.BILL_ALREADY_FINALIZED


def test_cancel_and_refund_validation(store):
    ss, us, bs = _services(store)
    ss.subscribe("acct-1", "pro", datetime(2026, 9, 1, tzinfo=timezone.utc))
    ss.set_refund_credit("pro", Decimal("30.00"))  # 月费基数 30

    # 9/20 退订：9 月 30 天，剩余 11 天 -> 可退 11.00
    _sub, granted, refundable = ss.cancel(
        "acct-1", datetime(2026, 9, 20, tzinfo=timezone.utc))
    log.info("判定依据: 退订剩余天数 11/30，可退额度=%s 实退=%s", refundable, granted)
    assert refundable == Decimal("11.00") and granted == Decimal("11.00")

    # 退订后上报 -> 无有效套餐
    with pytest.raises(BillingError) as ei:
        us.submit_usage(event_id="z1", account_id="acct-1", meter="api_calls",
                        quantity="1", occurred_at="2026-09-25T00:00:00+00:00",
                        now=datetime(2026, 9, 25, tzinfo=timezone.utc))
    log.info("退订后上报被拒 code=%s", ei.value.code.value)
    assert ei.value.code is ErrorCode.NO_ACTIVE_PLAN

    # 另一账户：超额退款必须拒绝
    ss.subscribe("acct-2", "pro", datetime(2026, 9, 1, tzinfo=timezone.utc))
    with pytest.raises(BillingError) as ei:
        ss.cancel("acct-2", datetime(2026, 9, 20, tzinfo=timezone.utc),
                  refund_amount=Decimal("12.00"))
    log.info("超额退款被拒 code=%s（申请 12 > 可退 11）", ei.value.code.value)
    assert ei.value.code is ErrorCode.INVALID_REFUND
    # 合法的部分退款成功，且订阅仍为已退订
    _sub, granted, _ = ss.cancel("acct-2", datetime(2026, 9, 20, tzinfo=timezone.utc),
                                 refund_amount=Decimal("5.00"))
    assert granted == Decimal("5.00")


def test_invalid_lifecycle_ops_rejected(store):
    ss, _, _ = _services(store)
    # 未订阅直接变更/退订
    with pytest.raises(BillingError) as ei:
        ss.change_plan("acct-1", "pro", datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert ei.value.code is ErrorCode.NOT_SUBSCRIBED
    with pytest.raises(BillingError) as ei:
        ss.cancel("acct-1", datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert ei.value.code is ErrorCode.NOT_SUBSCRIBED

    ss.subscribe("acct-1", "basic", datetime(2026, 9, 1, tzinfo=timezone.utc))
    # 重复订阅
    with pytest.raises(BillingError) as ei:
        ss.subscribe("acct-1", "pro", datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert ei.value.code is ErrorCode.ALREADY_SUBSCRIBED
    # 变更为不存在的套餐
    with pytest.raises(BillingError) as ei:
        ss.change_plan("acct-1", "nonexistent",
                       datetime(2026, 9, 10, tzinfo=timezone.utc))
    assert ei.value.code is ErrorCode.PLAN_NOT_FOUND
    log.info("非法生命周期操作均按明确错误码拒绝")
