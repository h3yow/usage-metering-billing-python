"""Billing service: orchestrates validation, idempotency, pricing and
subscription lifecycle on top of :class:`billing.storage.Storage`.

Ledger model
------------
* When a plan segment starts (subscribe / change_plan), the plan's base
  fee for the *remaining days of the current period* is charged
  immediately into the charges ledger.
* When a segment ends early (change_plan / cancel), the unused days of
  the old plan are refunded into the credits ledger.
* At period close the bill is ``charges + usage - credits``, every
  component itemized.  Credits in a period may never exceed charges in
  that period (``REFUND_EXCEEDS_REFUNDABLE``).

The service takes an injectable ``clock`` (defaults to
``datetime.date.today``) so tests can control "now" deterministically.
All public methods are serialized by the storage lock, so concurrent
requests cannot corrupt totals.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal

from billing.errors import LifecycleError, ReasonCode, ValidationError
from billing.models import (
    BaseFeeLineItem,
    Bill,
    CreditLineItem,
    PlanSegment,
    Rejection,
    Subscription,
    SubscriptionStatus,
    UsageEvent,
    UsageLineItem,
)
from billing.periods import days_in_period, period_key, period_of
from billing.plans import get_plan
from billing.pricing import price_marginal, prorate
from billing.storage import Storage

logger = logging.getLogger("billing.service")

_ZERO = Decimal("0")


def _plan_at(subscription: Subscription | None, day: date) -> str | None:
    """Plan code covering ``day`` (segments are half-open [start, end))."""
    if subscription is None:
        return None
    for segment in subscription.segments:
        if segment.start <= day and (segment.end is None or day < segment.end):
            return segment.plan_code
    return None


def _segment_index_at(subscription: Subscription | None, day: date) -> int | None:
    """Index of the segment covering ``day`` (half-open [start, end))."""
    if subscription is None:
        return None
    for index, segment in enumerate(subscription.segments):
        if segment.start <= day and (segment.end is None or day < segment.end):
            return index
    return None


def _segment_days_in_period(segment: PlanSegment, period_start: date, period_end: date) -> int:
    """Number of days a segment is active inside a closed period."""
    lo = max(segment.start, period_start)
    # An open segment intersecting a closed period ends at the period end.
    seg_end = segment.end or (period_end + timedelta(days=1))
    hi = min(seg_end, period_end + timedelta(days=1))
    return max((hi - lo).days, 0)


class BillingService:
    def __init__(self, storage: Storage, clock: Callable[[], date] = date.today) -> None:
        self._storage = storage
        self._clock = clock

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def create_account(self, account_id: str) -> None:
        if not account_id or not account_id.strip():
            raise ValidationError(ReasonCode.MISSING_FIELD, "account_id is required")
        with self._storage.lock():
            if self._storage.account_exists(account_id):
                raise LifecycleError(ReasonCode.ACCOUNT_EXISTS, f"account {account_id!r} exists")
            self._storage.create_account(account_id, self._clock())
            logger.info("account created", extra={"account_id": account_id})

    # ------------------------------------------------------------------
    # subscription lifecycle
    # ------------------------------------------------------------------
    def subscribe(self, account_id: str, plan_code: str, effective: date | None = None) -> None:
        effective = effective or self._clock()
        plan = get_plan(plan_code)
        if plan is None:
            raise LifecycleError(ReasonCode.PLAN_NOT_FOUND, f"unknown plan {plan_code!r}")
        with self._storage.lock():
            self._require_account(account_id)
            sub = self._storage.get_subscription(account_id)
            if sub is not None and sub.status == SubscriptionStatus.ACTIVE:
                raise LifecycleError(
                    ReasonCode.ALREADY_SUBSCRIBED,
                    "already subscribed; use change_plan instead",
                )
            if sub is None:
                sub = Subscription(account_id, SubscriptionStatus.ACTIVE, [])
            else:
                sub.status = SubscriptionStatus.ACTIVE
            sub.segments.append(PlanSegment(plan_code=plan_code, start=effective, end=None))
            self._charge_for_new_segment(account_id, plan_code, effective)
            self._storage.save_subscription(sub)
            logger.info(
                "subscribed",
                extra={"account_id": account_id, "plan": plan_code, "effective": str(effective)},
            )

    def change_plan(self, account_id: str, plan_code: str, effective: date | None = None) -> None:
        effective = effective or self._clock()
        plan = get_plan(plan_code)
        if plan is None:
            raise LifecycleError(ReasonCode.PLAN_NOT_FOUND, f"unknown plan {plan_code!r}")
        with self._storage.lock():
            sub = self._require_active_subscription(account_id)
            last = sub.segments[-1]
            if last.plan_code == plan_code:
                raise LifecycleError(
                    ReasonCode.INVALID_VALUE, f"already on plan {plan_code!r}"
                )
            self._validate_effective(sub, effective)
            self._refund_unused(account_id, last.plan_code, effective, reason_to=plan_code)
            last.end = effective
            if last.start == effective:
                sub.segments.pop()  # zero-length segment: same-day switch
            sub.segments.append(PlanSegment(plan_code=plan_code, start=effective, end=None))
            self._charge_for_new_segment(account_id, plan_code, effective)
            self._storage.save_subscription(sub)
            logger.info(
                "plan changed",
                extra={
                    "account_id": account_id,
                    "plan": plan_code,
                    "effective": str(effective),
                },
            )

    def cancel(self, account_id: str, effective: date | None = None) -> None:
        effective = effective or self._clock()
        with self._storage.lock():
            sub = self._require_active_subscription(account_id)
            last = sub.segments[-1]
            self._validate_effective(sub, effective)
            self._refund_unused(account_id, last.plan_code, effective, reason_to=None)
            last.end = effective
            if last.start == effective:
                sub.segments.pop()
            sub.status = SubscriptionStatus.CANCELLED
            self._storage.save_subscription(sub)
            logger.info(
                "cancelled",
                extra={"account_id": account_id, "effective": str(effective)},
            )

    def refund(self, account_id: str, amount: Decimal, reason: str) -> None:
        """Record a manual goodwill refund in the current period.

        Refunds in a period may never exceed what was charged in that
        period (minus refunds already given).
        """
        if amount is None or amount <= 0:
            raise ValidationError(ReasonCode.INVALID_VALUE, "refund amount must be positive")
        today = self._clock()
        period_start, period_end = period_of(today)
        with self._storage.lock():
            self._require_account(account_id)
            self._check_refund_capacity(account_id, period_start, period_end, amount)
            self._storage.append_credit(
                account_id, CreditLineItem(reason=reason or "manual_refund", amount=amount, created_at=today)
            )

    # ------------------------------------------------------------------
    # usage ingestion
    # ------------------------------------------------------------------
    def report_usage(self, event: UsageEvent) -> str:
        """Ingest one usage event.

        Returns ``"accepted"``, ``"duplicate"`` (same event_id + payload,
        a no-op) or ``"rejected:<CODE>"``.  Rejections are persisted with
        their reason so nothing is silently dropped.
        """
        today = self._clock()
        with self._storage.lock():
            if event.quantity is None or event.quantity <= 0:
                return self._reject(
                    event, ReasonCode.INVALID_VALUE, "quantity must be a positive number"
                )
            if not self._storage.account_exists(event.account_id):
                return self._reject(
                    event, ReasonCode.UNKNOWN_ACCOUNT, f"unknown account {event.account_id!r}"
                )

            existing = self._storage.find_event(event.account_id, event.event_id)
            if existing is not None:
                if existing == event:
                    logger.info(
                        "duplicate event ignored",
                        extra={"event_id": event.event_id, "account_id": event.account_id},
                    )
                    return "duplicate"
                return self._reject(
                    event,
                    ReasonCode.IDEMPOTENCY_CONFLICT,
                    f"event_id {event.event_id!r} already used with a different payload",
                )

            if event.occurred_at > today:
                return self._reject(
                    event,
                    ReasonCode.FUTURE_EVENT,
                    f"occurred_at {event.occurred_at} is in the future (today={today})",
                )
            if period_key(event.occurred_at) in self._storage.billed_periods(event.account_id):
                return self._reject(
                    event,
                    ReasonCode.PERIOD_ALREADY_BILLED,
                    f"period {period_key(event.occurred_at)} is already billed",
                )

            sub = self._storage.get_subscription(event.account_id)
            plan_code = _plan_at(sub, event.occurred_at)
            if plan_code is None:
                return self._reject(
                    event,
                    ReasonCode.NO_ACTIVE_PLAN,
                    f"no active plan on {event.occurred_at}",
                )
            plan = get_plan(plan_code)
            if plan is None or plan.price_for(event.metric) is None:
                return self._reject(
                    event,
                    ReasonCode.UNKNOWN_METRIC,
                    f"metric {event.metric!r} is not metered by plan {plan_code!r}",
                )

            self._storage.append_event(event)
            logger.info(
                "usage accepted",
                extra={
                    "event_id": event.event_id,
                    "account_id": event.account_id,
                    "metric": event.metric,
                    "quantity": str(event.quantity),
                    "occurred_at": str(event.occurred_at),
                },
            )
            return "accepted"

    # ------------------------------------------------------------------
    # billing
    # ------------------------------------------------------------------
    def close_period(self, account_id: str, period: date | None = None) -> Bill:
        """Generate the immutable bill for the period containing ``period``.

        Closing is idempotent: an existing bill is returned unchanged, so
        history is never rewritten by later changes.

        Usage pricing within a period
        -----------------------------
        Events are priced against the plan segment in force at their
        occurrence time (out-of-order arrival is handled because events
        are sorted by ``(occurred_at, event_id)``).  A metric's free
        allowance for a segment is prorated to the segment's days inside
        the period; graduated tiers are shared across the whole segment
        via marginal pricing, so line items sum exactly to the aggregate
        tiered charge.
        """
        day = period or self._clock()
        period_start, period_end = period_of(day)
        key = period_key(day)
        with self._storage.lock():
            self._require_account(account_id)
            existing = self._storage.get_bill(account_id, key)
            if existing is not None:
                logger.info(
                    "period close idempotent",
                    extra={"account_id": account_id, "period": key},
                )
                return existing

            sub = self._storage.get_subscription(account_id)
            period_days = days_in_period(day)
            events = sorted(
                self._storage.events_for_period(account_id, period_start, period_end),
                key=lambda e: (e.occurred_at, e.event_id),
            )

            usage_lines: list = []
            # consumed[segment_index][metric] = total quantity seen so far
            consumed: dict[int, dict[str, Decimal]] = {}
            for event in events:
                seg_index = _segment_index_at(sub, event.occurred_at)
                if seg_index is None:
                    # Should never happen: events without a covering plan are
                    # rejected at ingestion.  Skip defensively rather than
                    # corrupting the bill.
                    continue
                plan_code = sub.segments[seg_index].plan_code
                plan = get_plan(plan_code)
                price = plan.price_for(event.metric) if plan else None
                if price is None:
                    continue

                active_days = _segment_days_in_period(
                    sub.segments[seg_index], period_start, period_end
                )
                allowance = (
                    price.free_allowance * Decimal(active_days) / Decimal(period_days)
                ).quantize(Decimal("0.000001"))
                prior = consumed.setdefault(seg_index, {}).get(event.metric, _ZERO)

                free_portion, charged_portion, tiers = price_marginal(
                    price, event.quantity, allowance, prior
                )
                consumed[seg_index][event.metric] = prior + event.quantity
                amount = sum((t.amount for t in tiers), _ZERO)

                usage_lines.append(
                    UsageLineItem(
                        event_id=event.event_id,
                        metric=event.metric,
                        occurred_at=event.occurred_at,
                        plan_code=plan_code,
                        quantity=event.quantity,
                        free_portion=free_portion,
                        charged_portion=charged_portion,
                        tier_breakdown=tiers,
                        amount=amount,
                    )
                )

            base_fee_lines = sorted(
                self._storage.charges_for_period(account_id, period_start, period_end),
                key=lambda c: (c.start, c.plan_code),
            )
            credits = sorted(
                self._storage.credits_for_period(account_id, period_start, period_end),
                key=lambda c: (c.created_at, c.reason),
            )

            total_base_fee = sum((c.amount for c in base_fee_lines), _ZERO)
            total_usage = sum((c.amount for c in usage_lines), _ZERO)
            total_credits = sum((c.amount for c in credits), _ZERO)
            total = total_base_fee + total_usage - total_credits

            bill = Bill(
                account_id=account_id,
                period_start=period_start,
                period_end=period_end,
                base_fee_lines=base_fee_lines,
                usage_lines=usage_lines,
                credits=credits,
                total_base_fee=total_base_fee,
                total_usage=total_usage,
                total_credits=total_credits,
                total=total,
                generated_at=self._clock(),
            )
            self._storage.save_bill(bill)
            logger.info(
                "period closed",
                extra={
                    "account_id": account_id,
                    "period": key,
                    "base_fee": str(total_base_fee),
                    "usage": str(total_usage),
                    "credits": str(total_credits),
                    "total": str(total),
                },
            )
            return bill

    def get_bill(self, account_id: str, period: str) -> Bill:
        bill = self._storage.get_bill(account_id, period)
        if bill is None:
            raise LifecycleError(
                ReasonCode.BILL_NOT_FOUND, f"no bill for {account_id!r} in {period}"
            )
        return bill

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------
    def _require_account(self, account_id: str) -> None:
        if not self._storage.account_exists(account_id):
            raise LifecycleError(ReasonCode.UNKNOWN_ACCOUNT, f"unknown account {account_id!r}")

    def _require_active_subscription(self, account_id: str) -> Subscription:
        self._require_account(account_id)
        sub = self._storage.get_subscription(account_id)
        if sub is None or not sub.segments:
            raise LifecycleError(ReasonCode.NOT_SUBSCRIBED, "account has no subscription")
        if sub.status == SubscriptionStatus.CANCELLED:
            raise LifecycleError(ReasonCode.ALREADY_CANCELLED, "subscription is cancelled")
        return sub

    def _validate_effective(self, sub: Subscription, effective: date) -> None:
        today = self._clock()
        if effective > today:
            raise ValidationError(
                ReasonCode.INVALID_VALUE, f"effective {effective} is in the future"
            )
        last = sub.segments[-1]
        if effective < last.start:
            raise ValidationError(
                ReasonCode.INVALID_VALUE,
                f"effective {effective} is before current segment start {last.start}",
            )
        if period_key(effective) in self._storage.billed_periods(sub.account_id):
            raise LifecycleError(
                ReasonCode.PERIOD_ALREADY_BILLED,
                f"period {period_key(effective)} is already billed and cannot be changed",
            )

    def _charge_for_new_segment(self, account_id: str, plan_code: str, start: date) -> None:
        """Prepay the plan's base fee for the rest of the current period."""
        plan = get_plan(plan_code)
        if plan is None or plan.base_fee <= 0:
            return
        period_start, period_end = period_of(start)
        period_days = days_in_period(start)
        remaining_days = (period_end - start).days + 1  # inclusive of start day
        amount = prorate(plan.base_fee, remaining_days, period_days)
        if amount <= 0:
            return
        self._storage.append_charge(
            account_id,
            BaseFeeLineItem(
                plan_code=plan_code,
                start=start,
                end=period_end,
                days_active=remaining_days,
                period_days=period_days,
                monthly_fee=plan.base_fee,
                amount=amount,
            ),
        )

    def _refund_unused(
        self, account_id: str, plan_code: str, effective: date, reason_to: str | None
    ) -> None:
        """Refund the unused days of ``plan_code`` in the current period.

        The automatic refund is clamped to what was actually prepaid in
        this period (charges minus earlier credits).  A segment carried
        over from a previous period was never prepaid for the current
        period, so the refund there is simply zero — not an error.
        """
        plan = get_plan(plan_code)
        if plan is None or plan.base_fee <= 0:
            return
        period_start, period_end = period_of(effective)
        period_days = days_in_period(effective)
        unused_days = (period_end - effective).days + 1
        amount = prorate(plan.base_fee, unused_days, period_days)
        capacity = self._refundable(account_id, period_start, period_end)
        amount = min(amount, capacity)
        if amount <= 0:
            return
        verb = "plan_change_refund" if reason_to else "cancel_refund"
        to_part = f"->{reason_to}" if reason_to else ""
        self._storage.append_credit(
            account_id,
            CreditLineItem(
                reason=f"{verb}:{plan_code}{to_part}", amount=amount, created_at=effective
            ),
        )

    def _refundable(self, account_id: str, period_start: date, period_end: date) -> Decimal:
        charged = sum(
            (c.amount for c in self._storage.charges_for_period(account_id, period_start, period_end)),
            _ZERO,
        )
        credited = sum(
            (c.amount for c in self._storage.credits_for_period(account_id, period_start, period_end)),
            _ZERO,
        )
        return charged - credited

    def _check_refund_capacity(
        self, account_id: str, period_start: date, period_end: date, amount: Decimal
    ) -> None:
        charged = sum(
            (c.amount for c in self._storage.charges_for_period(account_id, period_start, period_end)),
            _ZERO,
        )
        credited = sum(
            (c.amount for c in self._storage.credits_for_period(account_id, period_start, period_end)),
            _ZERO,
        )
        if credited + amount > charged:
            raise LifecycleError(
                ReasonCode.REFUND_EXCEEDS_REFUNDABLE,
                f"refund {amount} exceeds refundable balance {charged - credited}",
            )

    def _reject(self, event: UsageEvent, code: str, detail: str) -> str:
        self._storage.append_rejection(
            event.account_id, Rejection(event_id=event.event_id, reason=code, detail=detail)
        )
        logger.info(
            "usage rejected",
            extra={
                "event_id": event.event_id,
                "account_id": event.account_id,
                "reason": code,
                "detail": detail,
            },
        )
        return f"rejected:{code}"
