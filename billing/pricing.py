"""Pure pricing functions: tiered usage pricing and proration."""

from __future__ import annotations

from decimal import Decimal

from billing.models import MetricPrice, TierCharge


def price_usage(
    price: MetricPrice,
    quantity: Decimal,
    free_allowance: Decimal,
) -> tuple[Decimal, Decimal, tuple[TierCharge, ...]]:
    """Price ``quantity`` units of usage under ``price``.

    ``free_allowance`` is the remaining free quota for this metric in the
    current period (already prorated if the plan changed mid-period).

    Returns (free_portion, charged_portion, tier_breakdown) where the
    tier breakdown covers only the charged portion.  All amounts are
    rounded to cents with ROUND_HALF_UP.
    """
    raise NotImplementedError


def prorate(monthly_amount: Decimal, days_active: int, period_days: int) -> Decimal:
    """Prorate a monthly amount to ``days_active`` of ``period_days``."""
    raise NotImplementedError
