"""JSON-file backed local storage.

Layout under the configured directory::

    accounts.json      -> {account_id: {"created_at": iso}}
    events.json        -> {account_id: [event, ...]}        (accepted only)
    rejections.json    -> {account_id: [rejection, ...]}
    subscriptions.json -> {account_id: subscription}
    bills.json         -> {account_id: {period_key: bill}}
    charges.json       -> {account_id: [base-fee charge, ...]}
    credits.json       -> {account_id: [credit, ...]}

Concurrency
-----------
* One process-wide :class:`~billing.storage.Storage` instance serves all
  requests; all mutations are guarded by a re-entrant lock.
* Every file write is atomic: data is written to a temporary file in
  the same directory and then ``os.replace``-d into place.  A crash or
  a concurrent writer therefore never leaves a half-written file.

Persistence format
------------------
``date`` is stored as ``YYYY-MM-DD``; ``Decimal`` as its canonical
string; enums as their value.  Everything else is plain JSON types.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from billing.models import (
    BaseFeeLineItem,
    Bill,
    CreditLineItem,
    PlanSegment,
    Rejection,
    Subscription,
    SubscriptionStatus,
    TierCharge,
    UsageEvent,
    UsageLineItem,
)


def _enc(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"not JSON serializable: {obj!r}")


def _date(value: str) -> date:
    return date.fromisoformat(value)


class Storage:
    """Thread-safe, JSON-file backed repository."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # low level helpers
    # ------------------------------------------------------------------
    def _path(self, name: str) -> Path:
        return self._root / f"{name}.json"

    def _load(self, name: str) -> dict:
        path = self._path(name)
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write(self, name: str, data: dict) -> None:
        path = self._path(name)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=self._root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, default=_enc)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    def lock(self) -> threading.RLock:
        """The re-entrant guard used for multi-step atomic operations."""
        return self._lock

    def reset(self) -> None:
        """Remove every persisted file (used by tests)."""
        with self._lock:
            for path in self._root.glob("*.json"):
                path.unlink()

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def create_account(self, account_id: str, created_at: date) -> None:
        with self._lock:
            accounts = self._load("accounts")
            accounts[account_id] = {"created_at": created_at.isoformat()}
            self._write("accounts", accounts)

    def account_exists(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self._load("accounts")

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------
    @staticmethod
    def _event_to_dict(event: UsageEvent) -> dict:
        return {
            "event_id": event.event_id,
            "account_id": event.account_id,
            "metric": event.metric,
            "quantity": str(event.quantity),
            "occurred_at": event.occurred_at.isoformat(),
        }

    @staticmethod
    def _event_from_dict(raw: dict) -> UsageEvent:
        return UsageEvent(
            event_id=raw["event_id"],
            account_id=raw["account_id"],
            metric=raw["metric"],
            quantity=Decimal(raw["quantity"]),
            occurred_at=_date(raw["occurred_at"]),
        )

    def find_event(self, account_id: str, event_id: str) -> UsageEvent | None:
        with self._lock:
            for raw in self._load("events").get(account_id, []):
                if raw["event_id"] == event_id:
                    return self._event_from_dict(raw)
            return None

    def append_event(self, event: UsageEvent) -> None:
        with self._lock:
            events = self._load("events")
            bucket = events.setdefault(event.account_id, [])
            bucket.append(self._event_to_dict(event))
            self._write("events", events)

    def append_rejection(self, account_id: str, rejection: Rejection) -> None:
        with self._lock:
            rejections = self._load("rejections")
            rejections.setdefault(account_id, []).append(
                {
                    "event_id": rejection.event_id,
                    "reason": rejection.reason,
                    "detail": rejection.detail,
                }
            )
            self._write("rejections", rejections)

    def events_for_period(
        self, account_id: str, period_start: date, period_end: date
    ) -> list[UsageEvent]:
        with self._lock:
            result = []
            for raw in self._load("events").get(account_id, []):
                event = self._event_from_dict(raw)
                if period_start <= event.occurred_at <= period_end:
                    result.append(event)
            # arrival-ordered storage is preserved; bill lines sort
            # explicitly by (occurred_at, event_id) for reproducibility.
            return result

    def rejections_for_account(self, account_id: str) -> list[Rejection]:
        with self._lock:
            return [
                Rejection(
                    event_id=raw["event_id"],
                    reason=raw["reason"],
                    detail=raw["detail"],
                )
                for raw in self._load("rejections").get(account_id, [])
            ]

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------
    @staticmethod
    def _segment_from_dict(raw: dict) -> PlanSegment:
        return PlanSegment(
            plan_code=raw["plan_code"],
            start=_date(raw["start"]),
            end=_date(raw["end"]) if raw.get("end") else None,
        )

    def save_subscription(self, subscription: Subscription) -> None:
        with self._lock:
            subs = self._load("subscriptions")
            subs[subscription.account_id] = {
                "account_id": subscription.account_id,
                "status": subscription.status.value,
                "segments": [
                    {
                        "plan_code": s.plan_code,
                        "start": s.start.isoformat(),
                        "end": s.end.isoformat() if s.end else None,
                    }
                    for s in subscription.segments
                ],
            }
            self._write("subscriptions", subs)

    def get_subscription(self, account_id: str) -> Subscription | None:
        with self._lock:
            raw = self._load("subscriptions").get(account_id)
            if raw is None:
                return None
            return Subscription(
                account_id=raw["account_id"],
                status=SubscriptionStatus(raw["status"]),
                segments=[self._segment_from_dict(s) for s in raw["segments"]],
            )

    # ------------------------------------------------------------------
    # charges (prepaid base-fee ledger)
    # ------------------------------------------------------------------
    @staticmethod
    def _base_fee_line_to_dict(line: BaseFeeLineItem) -> dict:
        return {
            "plan_code": line.plan_code,
            "start": line.start.isoformat(),
            "end": line.end.isoformat(),
            "days_active": line.days_active,
            "period_days": line.period_days,
            "monthly_fee": str(line.monthly_fee),
            "amount": str(line.amount),
        }

    @staticmethod
    def _base_fee_line_from_dict(raw: dict) -> BaseFeeLineItem:
        return BaseFeeLineItem(
            plan_code=raw["plan_code"],
            start=_date(raw["start"]),
            end=_date(raw["end"]),
            days_active=raw["days_active"],
            period_days=raw["period_days"],
            monthly_fee=Decimal(raw["monthly_fee"]),
            amount=Decimal(raw["amount"]),
        )

    def append_charge(self, account_id: str, line: BaseFeeLineItem) -> None:
        """Record a prepaid base-fee charge for a plan segment."""
        with self._lock:
            charges = self._load("charges")
            charges.setdefault(account_id, []).append(self._base_fee_line_to_dict(line))
            self._write("charges", charges)

    def charges_for_period(
        self, account_id: str, period_start: date, period_end: date
    ) -> list[BaseFeeLineItem]:
        with self._lock:
            result = []
            for raw in self._load("charges").get(account_id, []):
                line = self._base_fee_line_from_dict(raw)
                if period_start <= line.start <= period_end:
                    result.append(line)
            return result

    # ------------------------------------------------------------------
    # bills
    # ------------------------------------------------------------------
    @staticmethod
    def _tier_to_dict(tier: TierCharge) -> dict:
        return {
            "start": str(tier.start),
            "end": None if tier.end is None else str(tier.end),
            "quantity": str(tier.quantity),
            "unit_price": str(tier.unit_price),
            "amount": str(tier.amount),
        }

    @staticmethod
    def _tier_from_dict(raw: dict) -> TierCharge:
        return TierCharge(
            start=Decimal(raw["start"]),
            end=None if raw["end"] is None else Decimal(raw["end"]),
            quantity=Decimal(raw["quantity"]),
            unit_price=Decimal(raw["unit_price"]),
            amount=Decimal(raw["amount"]),
        )

    def _bill_to_dict(self, bill: Bill) -> dict:
        return {
            "account_id": bill.account_id,
            "period_start": bill.period_start.isoformat(),
            "period_end": bill.period_end.isoformat(),
            "base_fee_lines": [
                {
                    "plan_code": line.plan_code,
                    "start": line.start.isoformat(),
                    "end": line.end.isoformat(),
                    "days_active": line.days_active,
                    "period_days": line.period_days,
                    "monthly_fee": str(line.monthly_fee),
                    "amount": str(line.amount),
                }
                for line in bill.base_fee_lines
            ],
            "usage_lines": [
                {
                    "event_id": line.event_id,
                    "metric": line.metric,
                    "occurred_at": line.occurred_at.isoformat(),
                    "plan_code": line.plan_code,
                    "quantity": str(line.quantity),
                    "free_portion": str(line.free_portion),
                    "charged_portion": str(line.charged_portion),
                    "tier_breakdown": [self._tier_to_dict(t) for t in line.tier_breakdown],
                    "amount": str(line.amount),
                }
                for line in bill.usage_lines
            ],
            "credits": [
                {
                    "reason": c.reason,
                    "amount": str(c.amount),
                    "created_at": c.created_at.isoformat(),
                }
                for c in bill.credits
            ],
            "total_base_fee": str(bill.total_base_fee),
            "total_usage": str(bill.total_usage),
            "total_credits": str(bill.total_credits),
            "total": str(bill.total),
            "generated_at": bill.generated_at.isoformat() if bill.generated_at else None,
        }

    def _bill_from_dict(self, raw: dict) -> Bill:
        return Bill(
            account_id=raw["account_id"],
            period_start=_date(raw["period_start"]),
            period_end=_date(raw["period_end"]),
            base_fee_lines=[
                BaseFeeLineItem(
                    plan_code=line["plan_code"],
                    start=_date(line["start"]),
                    end=_date(line["end"]),
                    days_active=line["days_active"],
                    period_days=line["period_days"],
                    monthly_fee=Decimal(line["monthly_fee"]),
                    amount=Decimal(line["amount"]),
                )
                for line in raw["base_fee_lines"]
            ],
            usage_lines=[
                UsageLineItem(
                    event_id=line["event_id"],
                    metric=line["metric"],
                    occurred_at=_date(line["occurred_at"]),
                    plan_code=line["plan_code"],
                    quantity=Decimal(line["quantity"]),
                    free_portion=Decimal(line["free_portion"]),
                    charged_portion=Decimal(line["charged_portion"]),
                    tier_breakdown=tuple(self._tier_from_dict(t) for t in line["tier_breakdown"]),
                    amount=Decimal(line["amount"]),
                )
                for line in raw["usage_lines"]
            ],
            credits=[
                CreditLineItem(
                    reason=c["reason"],
                    amount=Decimal(c["amount"]),
                    created_at=_date(c["created_at"]),
                )
                for c in raw["credits"]
            ],
            total_base_fee=Decimal(raw["total_base_fee"]),
            total_usage=Decimal(raw["total_usage"]),
            total_credits=Decimal(raw["total_credits"]),
            total=Decimal(raw["total"]),
            generated_at=_date(raw["generated_at"]) if raw.get("generated_at") else None,
        )

    def save_bill(self, bill: Bill) -> None:
        with self._lock:
            bills = self._load("bills")
            bills.setdefault(bill.account_id, {})[bill.period_key] = self._bill_to_dict(bill)
            self._write("bills", bills)

    def get_bill(self, account_id: str, period_key: str) -> Bill | None:
        with self._lock:
            raw = self._load("bills").get(account_id, {}).get(period_key)
            return self._bill_from_dict(raw) if raw else None

    def billed_periods(self, account_id: str) -> list[str]:
        with self._lock:
            return sorted(self._load("bills").get(account_id, {}).keys())

    # ------------------------------------------------------------------
    # credits
    # ------------------------------------------------------------------
    def append_credit(self, account_id: str, credit: CreditLineItem) -> None:
        with self._lock:
            credits = self._load("credits")
            credits.setdefault(account_id, []).append(
                {
                    "reason": credit.reason,
                    "amount": str(credit.amount),
                    "created_at": credit.created_at.isoformat(),
                }
            )
            self._write("credits", credits)

    def credits_for_period(
        self, account_id: str, period_start: date, period_end: date
    ) -> list[CreditLineItem]:
        with self._lock:
            result = []
            for raw in self._load("credits").get(account_id, []):
                created = _date(raw["created_at"])
                if period_start <= created <= period_end:
                    result.append(
                        CreditLineItem(
                            reason=raw["reason"], amount=Decimal(raw["amount"]), created_at=created
                        )
                    )
            return result

    def total_credits(self, account_id: str) -> Decimal:
        with self._lock:
            return sum(
                (Decimal(raw["amount"]) for raw in self._load("credits").get(account_id, [])),
                Decimal("0"),
            )
