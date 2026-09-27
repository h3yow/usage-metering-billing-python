"""Late and out-of-order events must land in the right billing period and
be ordered deterministically at close time.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from billing.errors import ReasonCode
from tests.conftest import log_decision, log_event, make_event


def test_late_event_lands_in_correct_period(svc, clock):
    """An event arriving in October for usage in September is billed on
    September's bill (which is still open), not October's."""
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    # close September first... no: simulate October arrival while September
    # is still open.  Move clock to Oct 3, report a Sep usage.
    clock.set(date(2026, 10, 3))
    late = make_event("late-1", "acme", "api_calls", "20000", date(2026, 9, 20))
    log_event("late-arrival", arrival="2026-10-03", occurred="2026-09-20", qty="20000")
    result = svc.report_usage(late)
    log_decision(result, reason="open past period accepts late event")
    assert result == "accepted"

    sep = svc.close_period("acme", date(2026, 9, 1))
    assert len(sep.usage_lines) == 1
    log_decision("september-bill", lines=1, total=str(sep.total))

    oct_bill = svc.close_period("acme", date(2026, 10, 1))
    assert len(oct_bill.usage_lines) == 0
    log_decision("october-bill", lines=0, total=str(oct_bill.total))


def test_event_for_already_billed_period_is_rejected(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    svc.close_period("acme", date(2026, 9, 1))

    clock.set(date(2026, 10, 3))
    event = make_event("too-late", "acme", "api_calls", "1", date(2026, 9, 25))
    log_event("after-close", occurred="2026-09-25", billed_period="2026-09")
    result = svc.report_usage(event)
    log_decision(result, expected=ReasonCode.PERIOD_ALREADY_BILLED)
    assert result == f"rejected:{ReasonCode.PERIOD_ALREADY_BILLED}"
    # and the historical bill is untouched
    bill = svc.get_bill("acme", "2026-09")
    assert bill.total_usage == Decimal("0.00")


def test_bill_lines_sorted_by_occurrence_not_arrival(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    clock.set(date(2026, 9, 30))
    arrivals = [
        ("e-15", "20000", date(2026, 9, 15)),
        ("e-05", "30000", date(2026, 9, 5)),   # late
        ("e-25", "5000", date(2026, 9, 25)),
        ("e-10", "7000", date(2026, 9, 10)),   # late
    ]
    for event_id, qty, when in arrivals:
        log_event("arrival", event_id=event_id, occurred=str(when), qty=qty)
        assert svc.report_usage(make_event(event_id, "acme", "api_calls", qty, when)) == "accepted"

    bill = svc.close_period("acme", date(2026, 9, 1))
    ids = [line.event_id for line in bill.usage_lines]
    log_decision("ordered", order=" -> ".join(ids))
    assert ids == ["e-05", "e-10", "e-15", "e-25"]

    # aggregate consistency: marginal tier charges == pricing the total
    # total qty = 62000; free 10000; charged 52000 @ 0.001 = 52.00
    summed = sum((line.amount for line in bill.usage_lines), Decimal("0"))
    assert summed == bill.total_usage == Decimal("52.00")
    log_decision("aggregate", total_usage=str(bill.total_usage))


def test_tier_position_follows_occurrence_order_not_arrival(svc, clock):
    """The free allowance is consumed by the earliest-occurring event
    even though it arrived last."""
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    clock.set(date(2026, 9, 30))
    svc.report_usage(make_event("late", "acme", "api_calls", "20000", date(2026, 9, 20)))
    # earliest event arrives last
    svc.report_usage(make_event("early", "acme", "api_calls", "10000", date(2026, 9, 1)))

    bill = svc.close_period("acme", date(2026, 9, 1))
    by_id = {line.event_id: line for line in bill.usage_lines}
    log_decision(
        "allowance-allocation",
        early_free=str(by_id["early"].free_portion),
        late_free=str(by_id["late"].free_portion),
    )
    assert by_id["early"].free_portion == Decimal("10000")
    assert by_id["late"].free_portion == Decimal("0")
