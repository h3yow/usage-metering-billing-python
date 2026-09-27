"""JSON-file backed local storage.

Layout under the configured directory::

    accounts.json     -> {account_id: {"created_at": ...}}
    events.json       -> {account_id: [event, ...]}        (accepted only)
    rejections.json   -> {account_id: [rejection, ...]}
    subscriptions.json-> {account_id: subscription}
    bills.json        -> {account_id: {period_key: bill}}
    credits.json      -> {account_id: [credit, ...]}

All mutations are protected by a per-instance re-entrant lock and are
persisted atomically (write temp file + os.replace) so concurrent
writers cannot corrupt or lose data.
"""

from __future__ import annotations

import threading
from pathlib import Path

from billing.models import (
    Bill,
    CreditLineItem,
    Rejection,
    Subscription,
    UsageEvent,
)


class Storage:
    """Thread-safe, JSON-file backed repository."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._lock = threading.RLock()

    # -- accounts ------------------------------------------------------
    def create_account(self, account_id: str, created_at) -> None:
        raise NotImplementedError

    def account_exists(self, account_id: str) -> bool:
        raise NotImplementedError

    # -- events --------------------------------------------------------
    def find_event(self, account_id: str, event_id: str) -> UsageEvent | None:
        raise NotImplementedError

    def append_event(self, event: UsageEvent) -> None:
        raise NotImplementedError

    def append_rejection(self, account_id: str, rejection: Rejection) -> None:
        raise NotImplementedError

    def events_for_period(self, account_id: str, period_start, period_end) -> list[UsageEvent]:
        raise NotImplementedError

    # -- subscriptions -------------------------------------------------
    def save_subscription(self, subscription: Subscription) -> None:
        raise NotImplementedError

    def get_subscription(self, account_id: str) -> Subscription | None:
        raise NotImplementedError

    # -- bills ---------------------------------------------------------
    def save_bill(self, bill: Bill) -> None:
        raise NotImplementedError

    def get_bill(self, account_id: str, period_key: str) -> Bill | None:
        raise NotImplementedError

    def billed_periods(self, account_id: str) -> list[str]:
        raise NotImplementedError

    # -- credits -------------------------------------------------------
    def append_credit(self, account_id: str, credit: CreditLineItem) -> None:
        raise NotImplementedError

    def credits_for_period(self, account_id: str, period_start, period_end) -> list[CreditLineItem]:
        raise NotImplementedError
