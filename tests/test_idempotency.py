"""Duplicate submission, idempotency conflicts and rejection reasons.

Every test logs the submitted event plus the verdict, as required.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from billing.errors import ReasonCode
from tests.conftest import log_decision, log_event, make_event


def _setup_account(svc) -> None:
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))


def test_identical_resubmission_is_not_billed_twice(svc, clock):
    _setup_account(svc)
    event = make_event("evt-1", "acme", "api_calls", "100", date(2026, 9, 10))
    log_event("submit-1", event_id="evt-1", payload="api_calls:100")
    first = svc.report_usage(event)
    log_decision(first, reason="fresh event")
    assert first == "accepted"

    # same event_id AND same payload, arriving again -> no-op
    log_event("submit-1-resubmit", event_id="evt-1", payload="api_calls:100")
    second = svc.report_usage(event)
    log_decision(second, reason="same id + same payload")
    assert second == "duplicate"

    stored = svc._storage.events_for_period("acme", date(2026, 9, 1), date(2026, 9, 30))
    assert len(stored) == 1

    bill = svc.close_period("acme", date(2026, 9, 1))
    assert len(bill.usage_lines) == 1
    log_decision("bill-verified", usage_lines=1, total=str(bill.total))


def test_same_id_with_different_payload_is_conflict(svc, clock):
    _setup_account(svc)
    log_event("submit-a", event_id="evt-2", payload="api_calls:100")
    assert svc.report_usage(make_event("evt-2", "acme", "api_calls", "100", date(2026, 9, 10))) == "accepted"

    changed = make_event("evt-2", "acme", "api_calls", "999", date(2026, 9, 10))
    log_event("submit-a-conflict", event_id="evt-2", payload="api_calls:999 (different!)")
    result = svc.report_usage(changed)
    log_decision(result, reason="same id, different payload")
    assert result == f"rejected:{ReasonCode.IDEMPOTENCY_CONFLICT}"

    rejections = svc._storage.rejections_for_account("acme")
    assert any(r.reason == ReasonCode.IDEMPOTENCY_CONFLICT for r in rejections)
    stored = svc._storage.events_for_period("acme", date(2026, 9, 1), date(2026, 9, 30))
    assert len(stored) == 1 and stored[0].quantity == Decimal("100")


@pytest.mark.parametrize(
    "event,expected_reason,why",
    [
        (make_event("x", "ghost", "api_calls", "1", date(2026, 9, 10)),
         ReasonCode.UNKNOWN_ACCOUNT, "account never created"),
        (make_event("x", "acme", "api_calls", "-5", date(2026, 9, 10)),
         ReasonCode.INVALID_VALUE, "negative quantity"),
        (make_event("x", "acme", "api_calls", "0", date(2026, 9, 10)),
         ReasonCode.INVALID_VALUE, "zero quantity"),
        (make_event("x", "acme", "api_calls", "1", date(2026, 10, 1)),
         ReasonCode.FUTURE_EVENT, "occurred_at after clock today"),
        (make_event("x", "acme", "unknown_metric", "1", date(2026, 9, 10)),
         ReasonCode.UNKNOWN_METRIC, "metric not in plan"),
    ],
)
def test_rejection_reasons_are_distinguishable(svc, clock, event, expected_reason, why):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    log_event(
        "submit-invalid",
        event_id=event.event_id,
        account=event.account_id,
        metric=event.metric,
        quantity=str(event.quantity),
        occurred_at=str(event.occurred_at),
    )
    result = svc.report_usage(event)
    log_decision(result, expected=expected_reason, why=why)
    assert result == f"rejected:{expected_reason}"

    rejections = svc._storage.rejections_for_account(event.account_id)
    assert [r.reason for r in rejections] == [expected_reason]


def test_event_without_active_plan_is_rejected(svc, clock):
    svc.create_account("acme")  # no subscription
    event = make_event("x", "acme", "api_calls", "1", date(2026, 9, 10))
    log_event("submit-no-plan", account="acme", date="2026-09-10")
    result = svc.report_usage(event)
    log_decision(result, expected=ReasonCode.NO_ACTIVE_PLAN)
    assert result == f"rejected:{ReasonCode.NO_ACTIVE_PLAN}"


def test_event_before_subscription_is_rejected(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 10))
    event = make_event("x", "acme", "api_calls", "1", date(2026, 9, 5))
    log_event("submit-before-plan", occurred="2026-09-05", plan_start="2026-09-10")
    result = svc.report_usage(event)
    log_decision(result, expected=ReasonCode.NO_ACTIVE_PLAN)
    assert result == f"rejected:{ReasonCode.NO_ACTIVE_PLAN}"
