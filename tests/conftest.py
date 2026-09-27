"""Shared test fixtures and logging helpers.

Every test logs:
* the input events/requests it submits, and
* the decision basis (returned status / reason code / bill totals),
so failures can be diagnosed from the unit-test log alone.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import billing.api as api_module
from billing.service import BillingService
from billing.storage import Storage

logger = logging.getLogger("billing.test")


class MutableClock:
    """A controllable clock; advance it to simulate time passing."""

    def __init__(self, day: date) -> None:
        self.day = day

    def __call__(self) -> date:
        return self.day

    def set(self, day: date) -> None:
        self.day = day

    def advance(self, days: int = 1) -> date:
        self.day += timedelta(days=days)
        return self.day


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock(date(2026, 9, 15))


@pytest.fixture
def service(clock: MutableClock) -> BillingService:
    tmp = Path(tempfile.mkdtemp(prefix="billing-test-"))
    return BillingService(Storage(tmp), clock=clock)


@pytest.fixture
def svc(service: BillingService) -> BillingService:
    """Alias: a ready-to-use service."""
    return service


@pytest.fixture
def client(clock: MutableClock) -> TestClient:
    tmp = Path(tempfile.mkdtemp(prefix="billing-http-test-"))
    api_module._service = BillingService(Storage(tmp), clock=clock)
    from main import app

    with TestClient(app) as test_client:
        yield test_client
    api_module._service = BillingService(
        Storage(Path("billing_data")), clock=date.today
    )


def log_event(step: str, **fields) -> None:
    """Log an input event and the decision context for a test step."""
    payload = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("[%s] %s", step, payload)


def log_decision(decision: str, **fields) -> None:
    payload = " ".join(f"{k}={v}" for k, v in fields.items())
    logger.info("decision=%s %s", decision, payload)


def make_event(event_id, account_id, metric, quantity, occurred_at):
    from billing.models import UsageEvent

    return UsageEvent(
        event_id=event_id,
        account_id=account_id,
        metric=metric,
        quantity=Decimal(str(quantity)),
        occurred_at=occurred_at
        if isinstance(occurred_at, date)
        else date.fromisoformat(occurred_at),
    )
