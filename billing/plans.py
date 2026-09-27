"""Static plan catalog used by the demo service.

All prices are in major currency units (Decimal).  ``free`` has no base
fee and a small free allowance; ``pro`` adds a base fee and graduated
tiers; ``enterprise`` is a flat higher tier with a large allowance.
"""

from __future__ import annotations

from decimal import Decimal

from billing.models import MetricPrice, Plan, PricingMode, Tier

_D = Decimal

_PLANS: dict[str, Plan] = {
    "free": Plan(
        code="free",
        name="Free",
        base_fee=_D("0.00"),
        prices=(
            MetricPrice(
                metric="api_calls",
                mode=PricingMode.GRADUATED,
                free_allowance=_D("1000"),
                tiers=(Tier(start=_D("0"), unit_price=_D("0.002")),),
            ),
        ),
    ),
    "pro": Plan(
        code="pro",
        name="Pro",
        base_fee=_D("29.00"),
        prices=(
            MetricPrice(
                metric="api_calls",
                mode=PricingMode.GRADUATED,
                free_allowance=_D("10000"),
                tiers=(
                    Tier(start=_D("0"), end=_D("100000"), unit_price=_D("0.001")),
                    Tier(start=_D("100000"), unit_price=_D("0.0005")),
                ),
            ),
            MetricPrice(
                metric="storage_gb",
                mode=PricingMode.GRADUATED,
                free_allowance=_D("5"),
                tiers=(
                    Tier(start=_D("0"), end=_D("100"), unit_price=_D("0.10")),
                    Tier(start=_D("100"), unit_price=_D("0.05")),
                ),
            ),
        ),
    ),
    "enterprise": Plan(
        code="enterprise",
        name="Enterprise",
        base_fee=_D("199.00"),
        prices=(
            MetricPrice(
                metric="api_calls",
                mode=PricingMode.GRADUATED,
                free_allowance=_D("1000000"),
                tiers=(Tier(start=_D("0"), unit_price=_D("0.0002")),),
            ),
            MetricPrice(
                metric="storage_gb",
                mode=PricingMode.GRADUATED,
                free_allowance=_D("100"),
                tiers=(Tier(start=_D("0"), unit_price=_D("0.03")),),
            ),
        ),
    ),
}


def get_plan(code: str) -> Plan | None:
    """Look up a plan by code; ``None`` if unknown."""
    return _PLANS.get(code)


def list_plans() -> list[Plan]:
    """Return all available plans."""
    return list(_PLANS.values())
