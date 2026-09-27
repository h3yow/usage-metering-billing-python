"""Mid-period plan changes, cancellation refunds and bill immutability."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from billing.errors import BillingError, LifecycleError, ReasonCode
from tests.conftest import log_decision, log_event, make_event

logger = logging.getLogger("billing.test")


def test_mid_period_change_prorates_fee_and_allowance(svc, clock):
    svc.create_account("acme")
    log_event("subscribe", plan="pro", effective="2026-09-01")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    clock.set(date(2026, 9, 15))
    log_event("change-plan", from_plan="pro", to_plan="enterprise", effective="2026-09-15")
    svc.change_plan("acme", "enterprise", effective=date(2026, 9, 15))

    bill = svc.close_period("acme", date(2026, 9, 1))

    # base fee: pro full 29.00 prepaid, enterprise 16 remaining days
    assert [(l.plan_code, l.amount) for l in bill.base_fee_lines] == [
        ("pro", Decimal("29.00")),
        ("enterprise", Decimal("106.13")),
    ]
    # unused pro days (15..30 = 16 days): 29*16/30
    assert bill.credits[0].reason.startswith("plan_change_refund:pro->enterprise")
    assert bill.credits[0].amount == Decimal("15.47")
    log_decision(
        "prorated",
        pro_fee="29.00",
        enterprise_fee="106.13",
        refund="15.47",
        net_base=str(bill.total_base_fee - bill.total_credits),
    )
    # net base fee == 14 days pro + 16 days enterprise
    # 29*14/30 = 13.53 ; 199*16/30 = 106.13 ; sum = 119.66
    assert bill.total_base_fee - bill.total_credits == Decimal("119.66")


def test_usage_priced_against_plan_in_force_at_occurrence(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    svc.report_usage(make_event("u1", "acme", "api_calls", "50000", date(2026, 9, 10)))

    clock.set(date(2026, 9, 15))
    svc.change_plan("acme", "enterprise", effective=date(2026, 9, 15))
    clock.set(date(2026, 9, 20))
    svc.report_usage(make_event("u2", "acme", "api_calls", "600000", date(2026, 9, 20)))

    bill = svc.close_period("acme", date(2026, 9, 1))
    by_id = {l.event_id: l for l in bill.usage_lines}
    log_decision(
        "plan-per-event",
        u1_plan=by_id["u1"].plan_code,
        u1_amt=str(by_id["u1"].amount),
        u2_plan=by_id["u2"].plan_code,
        u2_amt=str(by_id["u2"].amount),
    )
    # u1 pro: 14-day segment allowance 4666.67; charged 45333.33 @0.001
    assert by_id["u1"].plan_code == "pro"
    assert by_id["u1"].amount == Decimal("45.33"), by_id["u1"].amount
    # u2 enterprise: allowance 533333.33; charged 66666.67 @0.0002 = 13.33
    assert by_id["u2"].plan_code == "enterprise"
    assert by_id["u2"].amount == Decimal("13.33"), by_id["u2"].amount


def test_cancel_refunds_unused_days(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    svc.report_usage(make_event("u1", "acme", "api_calls", "5000", date(2026, 9, 3)))

    clock.set(date(2026, 9, 20))
    log_event("cancel", effective="2026-09-20")
    svc.cancel("acme", effective=date(2026, 9, 20))

    # usage after cancellation has no active plan
    clock.set(date(2026, 9, 21))
    late = make_event("u2", "acme", "api_calls", "5000", date(2026, 9, 21))
    result = svc.report_usage(late)
    log_decision(result, expected=ReasonCode.NO_ACTIVE_PLAN)
    assert result == f"rejected:{ReasonCode.NO_ACTIVE_PLAN}"

    bill = svc.close_period("acme", date(2026, 9, 1))
    cancel_credits = [c for c in bill.credits if c.reason.startswith("cancel_refund")]
    assert len(cancel_credits) == 1
    # unused days 20..30 = 11: 29*11/30 = 10.63
    assert cancel_credits[0].amount == Decimal("10.63"), cancel_credits[0].amount
    # only the pre-cancel usage is billed
    assert [l.event_id for l in bill.usage_lines] == ["u1"]
    log_decision(
        "cancelled-bill",
        refund=str(cancel_credits[0].amount),
        usage_lines=1,
        total=str(bill.total),
    )


def test_resubscribe_after_cancel_starts_new_segment(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    clock.set(date(2026, 9, 10))
    svc.cancel("acme", effective=date(2026, 9, 10))

    clock.set(date(2026, 9, 20))
    svc.subscribe("acme", "enterprise", effective=date(2026, 9, 20))
    clock.set(date(2026, 9, 22))
    assert svc.report_usage(make_event("u1", "acme", "api_calls", "100", date(2026, 9, 22))) == "accepted"

    sub = svc._storage.get_subscription("acme")
    assert [s.plan_code for s in sub.segments] == ["pro", "enterprise"]
    log_decision("resubscribe", segments=[s.plan_code for s in sub.segments])


def test_illegal_lifecycle_operations_are_rejected(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    for fn, code, label in [
        (lambda: svc.subscribe("acme", "pro"), ReasonCode.ALREADY_SUBSCRIBED, "double subscribe"),
        (lambda: svc.change_plan("acme", "pro"), ReasonCode.INVALID_VALUE, "same plan"),
        (lambda: svc.change_plan("acme", "nope"), ReasonCode.PLAN_NOT_FOUND, "unknown plan"),
        (lambda: svc.cancel("ghost"), ReasonCode.UNKNOWN_ACCOUNT, "cancel unknown acct"),
        (lambda: svc.refund("acme", Decimal("-1"), "x"), ReasonCode.INVALID_VALUE, "negative refund"),
    ]:
        try:
            fn()
            raise AssertionError(f"{label} should have been rejected")
        except BillingError as exc:
            log_decision(f"rejected:{exc.code}", case=label)
            assert exc.code == code, (label, exc.code)

    svc.cancel("acme", effective=date(2026, 9, 10))
    try:
        svc.cancel("acme")
        raise AssertionError("double cancel should fail")
    except LifecycleError as exc:
        assert exc.code == ReasonCode.ALREADY_CANCELLED


def test_refund_never_exceeds_charged_balance(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    # refundable balance this period is 29.00 minus any automatic credits
    svc.refund("acme", Decimal("10.00"), "goodwill")
    try:
        svc.refund("acme", Decimal("20.00"), "greedy")
        raise AssertionError("over-cap refund should fail")
    except LifecycleError as exc:
        log_decision(f"rejected:{exc.code}", charged="29.00", already="10.00", asked="20.00")
        assert exc.code == ReasonCode.REFUND_EXCEEDS_REFUNDABLE
    # the valid refund stays on the bill
    bill = svc.close_period("acme", date(2026, 9, 1))
    assert any(c.amount == Decimal("10.00") and c.reason == "goodwill" for c in bill.credits)


def test_history_bill_is_immutable_after_later_changes(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    svc.report_usage(make_event("u1", "acme", "api_calls", "50000", date(2026, 9, 5)))
    september = svc.close_period("acme", date(2026, 9, 1))
    frozen_total = september.total
    logger.info("september closed total=%s", frozen_total)

    # October activity: new plan + new usage; September must not move
    clock.set(date(2026, 10, 5))
    svc.change_plan("acme", "enterprise", effective=date(2026, 10, 1))
    svc.report_usage(make_event("u2", "acme", "api_calls", "999", date(2026, 10, 4)))
    october = svc.close_period("acme", date(2026, 10, 1))

    reread_september = svc.get_bill("acme", "2026-09")
    log_decision(
        "immutability",
        sep_at_close=str(frozen_total),
        sep_after_october=str(reread_september.total),
        october=str(october.total),
    )
    assert reread_september.total == frozen_total
    assert [l.event_id for l in reread_september.usage_lines] == ["u1"]
    assert october.total != frozen_total
