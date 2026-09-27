"""Pure pricing functions: tiered usage pricing and proration.

Rounding rule
-------------
Every monetary result is rounded to 2 fractional digits with
``ROUND_HALF_UP``.  Tier amounts are rounded individually, so a bill
line equals the sum of the tier amounts printed next to it — the bill
is reproducible and item-by-item verifiable.

Tier semantics
--------------
Tiers are relative to the *charged* quantity (i.e. usage left after the
free allowance).  Tier i covers charged units in ``[start, end)``;
``end=None`` is the open-ended top tier.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from billing.models import MetricPrice, TierCharge

_CENT = Decimal("0.01")
_ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def price_usage(
    price: MetricPrice,
    quantity: Decimal,
    free_allowance: Decimal,
) -> tuple[Decimal, Decimal, tuple[TierCharge, ...]]:
    """Price ``quantity`` units of usage under ``price``.

    ``free_allowance`` is the free quota available for this metric in the
    current segment/period (already prorated if the plan changed
    mid-period).

    Returns ``(free_portion, charged_portion, tier_breakdown)``; only
    tiers with non-zero quantity are included.
    """
    if quantity <= 0:
        return _ZERO, _ZERO, ()

    free_portion = min(quantity, max(free_allowance, _ZERO))
    charged_portion = quantity - free_portion

    breakdown: list[TierCharge] = []
    if charged_portion > 0:
        for tier in price.tiers:
            if charged_portion <= tier.start:
                break
            tier_end = tier.end if tier.end is not None else charged_portion
            in_tier = min(charged_portion, tier_end) - tier.start
            if in_tier <= 0:
                continue
            amount = _money(in_tier * tier.unit_price)
            breakdown.append(
                TierCharge(
                    start=tier.start,
                    end=tier.end,
                    quantity=in_tier,
                    unit_price=tier.unit_price,
                    amount=amount,
                )
            )

    return free_portion, charged_portion, tuple(breakdown)


def prorate(monthly_amount: Decimal, days_active: int, period_days: int) -> Decimal:
    """Prorate a monthly amount to ``days_active`` out of ``period_days``.

    ``monthly_amount * days_active / period_days`` rounded half-up.
    """
    if not 0 <= days_active <= period_days:
        raise ValueError(
            f"days_active {days_active} out of range for period of {period_days} days"
        )
    if period_days <= 0 or days_active == 0:
        return _money(_ZERO)
    return _money(monthly_amount * Decimal(days_active) / Decimal(period_days))
