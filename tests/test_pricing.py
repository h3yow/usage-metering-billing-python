"""Unit tests for the pure pricing rules."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from billing.plans import get_plan
from billing.pricing import price_marginal, price_usage, prorate
from billing.service import _segment_days_in_period
from billing.models import PlanSegment


def test_free_allowance_consumed_first():
    pro_api = get_plan("pro").price_for("api_calls")
    free, charged, tiers = price_usage(pro_api, Decimal("5000"), Decimal("10000"))
    assert (free, charged, tiers) == (Decimal("5000"), Decimal("0"), ())


def test_charge_starts_after_allowance_and_rounds_half_up():
    pro_api = get_plan("pro").price_for("api_calls")
    _, charged, tiers = price_usage(pro_api, Decimal("50000"), Decimal("10000"))
    assert charged == Decimal("40000")
    assert tiers[0].amount == Decimal("40.00")
    # half-up: 0.0005 * 1 = 0.0005 -> rounds to 0.00 (sub-cent disappears)
    assert tiers[0].unit_price == Decimal("0.001")


def test_graduated_tiers_split_correctly():
    pro_api = get_plan("pro").price_for("api_calls")
    _, _, tiers = price_usage(pro_api, Decimal("160000"), Decimal("10000"))
    assert tiers[0].quantity == Decimal("100000") and tiers[0].amount == Decimal("100.00")
    assert tiers[1].quantity == Decimal("50000") and tiers[1].amount == Decimal("25.00")


def test_marginal_slices_sum_to_one_shot_price():
    """The core invariant: marginal pricing of individual events sums
    exactly to pricing the aggregate quantity once."""
    pro_api = get_plan("pro").price_for("api_calls")
    allowance = Decimal("10000")

    pieces = [
        price_marginal(pro_api, Decimal("6000"), allowance, Decimal("0")),
        price_marginal(pro_api, Decimal("90000"), allowance, Decimal("6000")),
        price_marginal(pro_api, Decimal("80000"), allowance, Decimal("96000")),
    ]
    marginal_total = sum(
        (t.amount for piece in pieces for t in piece[2]), Decimal("0")
    )
    _, _, tiers = price_usage(pro_api, Decimal("176000"), allowance)
    one_shot = sum((t.amount for t in tiers), Decimal("0"))
    assert marginal_total == one_shot == Decimal("133.00")


@pytest.mark.parametrize(
    "monthly,days,total,expected",
    [
        ("29.00", 0, 30, "0.00"),
        ("29.00", 15, 30, "14.50"),
        ("29.00", 16, 30, "15.47"),   # 15.466.. rounds half-up
        ("199.00", 16, 30, "106.13"),
        ("31.00", 1, 31, "1.00"),
    ],
)
def test_proration(monthly, days, total, expected):
    assert prorate(Decimal(monthly), days, total) == Decimal(expected)


def test_proration_rejects_bad_days():
    with pytest.raises(ValueError):
        prorate(Decimal("29.00"), 31, 30)


def test_segment_days_in_period_half_open():
    p0, p1 = date(2026, 9, 1), date(2026, 9, 30)
    # fully inside: 9/10..9/20 half-open = 10 days
    seg = PlanSegment("pro", date(2026, 9, 10), date(2026, 9, 20))
    assert _segment_days_in_period(seg, p0, p1) == 10
    # open-ended segment runs to period end
    seg = PlanSegment("pro", date(2026, 9, 15), None)
    assert _segment_days_in_period(seg, p0, p1) == 16
    # segment starting before the period is clipped
    seg = PlanSegment("pro", date(2026, 8, 25), date(2026, 9, 15))
    assert _segment_days_in_period(seg, p0, p1) == 14
