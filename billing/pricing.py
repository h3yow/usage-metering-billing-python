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


def price_marginal(
    price: MetricPrice,
    quantity: Decimal,
    free_allowance: Decimal,
    prior_consumed: Decimal,
) -> tuple[Decimal, Decimal, tuple[TierCharge, ...]]:
    """Price ``quantity`` units given ``prior_consumed`` units earlier in
    the same billing segment.

    The free allowance and the graduated tiers are shared resources of
    the whole segment: earlier events may already have consumed part of
    them.  The returned :class:`TierCharge` entries still describe *this*
    event's slice of each tier (its start/end refer to the tier, not to
    the event), so every bill line is independently traceable while the
    sum of all lines equals pricing the period's total quantity at once.

    Returns ``(free_portion, charged_portion, tier_breakdown)``.
    """
    if quantity <= 0:
        return _ZERO, _ZERO, ()

    prior_consumed = max(prior_consumed, _ZERO)
    allowance_left = max(free_allowance - prior_consumed, _ZERO)
    free_portion = min(quantity, allowance_left)
    charged_quantity = quantity - free_portion

    # Position within the *charged* axis before/after this event.
    charged_start = max(prior_consumed - free_allowance, _ZERO)
    charged_end = charged_start + charged_quantity

    breakdown: list[TierCharge] = []
    if charged_quantity > 0:
        for tier in price.tiers:
            overlap_start = max(charged_start, tier.start)
            tier_end = tier.end if tier.end is not None else charged_end
            overlap_end = min(charged_end, tier_end)
            in_tier = overlap_end - overlap_start
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

    return free_portion, charged_quantity, tuple(breakdown)


def price_usage(
    price: MetricPrice,
    quantity: Decimal,
    free_allowance: Decimal,
) -> tuple[Decimal, Decimal, tuple[TierCharge, ...]]:
    """Price ``quantity`` units of usage under ``price`` as the first
    usage in a segment (i.e. no prior consumption).

    Returns ``(free_portion, charged_portion, tier_breakdown)``; only
    tiers with non-zero quantity are included.
    """
    return price_marginal(price, quantity, free_allowance, _ZERO)


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
