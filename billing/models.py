"""Domain models for usage metering and billing.

Money convention
----------------
* All monetary amounts are represented as ``Decimal`` in major currency
  units (e.g. yuan/dollar), rounded to 2 fractional digits with
  ``ROUND_HALF_UP``.
* Usage quantities are ``Decimal`` as well so that fractional usage
  (e.g. GB-hours) is supported.

Time convention
---------------
* All dates are naive ``date`` values interpreted in the billing
  timezone.  A billing period is a natural calendar month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum


# ---------------------------------------------------------------------------
# Pricing / plan models
# ---------------------------------------------------------------------------

class PricingMode(str, Enum):
    """How usage is priced once the free allowance is consumed."""

    GRADUATED = "graduated"  # tiered: each unit is priced by the tier it falls in


@dataclass(frozen=True)
class Tier:
    """A single pricing tier.

    ``start`` is inclusive, ``end`` is exclusive.  An ``end`` of ``None``
    means open-ended (infinity).
    """

    start: Decimal
    unit_price: Decimal
    end: Decimal | None = None


@dataclass(frozen=True)
class MetricPrice:
    """Pricing rule for one metered metric inside a plan."""

    metric: str
    mode: PricingMode
    free_allowance: Decimal
    tiers: tuple[Tier, ...]


@dataclass(frozen=True)
class Plan:
    """A billable plan.

    ``base_fee`` is the monthly subscription fee.  Metered prices are
    declared per metric.
    """

    code: str
    name: str
    base_fee: Decimal
    prices: tuple[MetricPrice, ...] = field(default_factory=tuple)

    def price_for(self, metric: str) -> MetricPrice | None:
        for p in self.prices:
            if p.metric == metric:
                return p
        return None


# ---------------------------------------------------------------------------
# Subscription lifecycle
# ---------------------------------------------------------------------------

class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class PlanSegment:
    """One contiguous slice of a subscription that uses a given plan.

    Segments are half-open: [start, end); ``end=None`` means the segment
    runs to the end of the current billing period (or until another
    segment replaces it).
    """

    plan_code: str
    start: date
    end: date | None = None


@dataclass
class Subscription:
    """A customer's subscription, made of ordered, non-overlapping segments."""

    account_id: str
    status: SubscriptionStatus
    segments: list[PlanSegment] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Usage events
# ---------------------------------------------------------------------------

class EventStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class UsageEvent:
    """A single usage report submitted by a client."""

    event_id: str
    account_id: str
    metric: str
    quantity: Decimal
    occurred_at: date


@dataclass(frozen=True)
class Rejection:
    """A rejected usage report with a machine-readable reason."""

    event_id: str
    reason: str
    detail: str


# ---------------------------------------------------------------------------
# Billing output
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageLineItem:
    """One metered line on a bill, traceable back to a single event."""

    event_id: str
    metric: str
    occurred_at: date
    plan_code: str
    quantity: Decimal
    free_portion: Decimal
    charged_portion: Decimal
    tier_breakdown: tuple["TierCharge", ...]
    amount: Decimal


@dataclass(frozen=True)
class TierCharge:
    """How much quantity was charged in one tier."""

    start: Decimal
    end: Decimal | None
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class BaseFeeLineItem:
    """Subscription base fee for one plan segment inside a period."""

    plan_code: str
    start: date
    end: date
    days_active: int
    period_days: int
    monthly_fee: Decimal
    amount: Decimal


@dataclass(frozen=True)
class CreditLineItem:
    """A refund / credit applied within a period."""

    reason: str
    amount: Decimal
    created_at: date


@dataclass
class Bill:
    """An immutable, itemized bill for one account + billing period."""

    account_id: str
    period_start: date
    period_end: date  # inclusive
    base_fee_lines: list[BaseFeeLineItem] = field(default_factory=list)
    usage_lines: list[UsageLineItem] = field(default_factory=list)
    credits: list[CreditLineItem] = field(default_factory=list)
    total_base_fee: Decimal = Decimal("0.00")
    total_usage: Decimal = Decimal("0.00")
    total_credits: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")
    generated_at: date | None = None

    @property
    def period_key(self) -> str:
        return self.period_start.strftime("%Y-%m")
