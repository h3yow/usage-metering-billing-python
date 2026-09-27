"""Billing service: orchestrates validation, idempotency, pricing and
subscription lifecycle on top of :class:`billing.storage.Storage`.

The service takes an injectable ``clock`` (defaults to
``datetime.date.today``) so tests can control "now" deterministically.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from billing.models import Bill, UsageEvent
from billing.storage import Storage


class BillingService:
    def __init__(self, storage: Storage, clock: Callable[[], date] = date.today) -> None:
        self._storage = storage
        self._clock = clock

    # -- accounts / subscriptions --------------------------------------
    def create_account(self, account_id: str) -> None:
        raise NotImplementedError

    def subscribe(self, account_id: str, plan_code: str, effective: date | None = None) -> None:
        raise NotImplementedError

    def change_plan(self, account_id: str, plan_code: str, effective: date | None = None) -> None:
        raise NotImplementedError

    def cancel(self, account_id: str, effective: date | None = None) -> None:
        raise NotImplementedError

    # -- usage ----------------------------------------------------------
    def report_usage(self, event: UsageEvent) -> str:
        """Ingest one usage event. Returns the resulting status string."""
        raise NotImplementedError

    # -- billing --------------------------------------------------------
    def close_period(self, account_id: str, period: date | None = None) -> Bill:
        """Generate the immutable bill for the period containing ``period``."""
        raise NotImplementedError

    def get_bill(self, account_id: str, period_key: str) -> Bill:
        raise NotImplementedError
