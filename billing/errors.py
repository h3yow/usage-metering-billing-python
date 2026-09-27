"""Typed errors with machine-readable reason codes.

Every rejection (invalid usage report, illegal lifecycle operation) maps
to one of these codes so callers can distinguish causes instead of
silently dropping data.
"""

from __future__ import annotations


class ReasonCode:
    # usage report validation
    MISSING_FIELD = "MISSING_FIELD"
    INVALID_VALUE = "INVALID_VALUE"
    UNKNOWN_ACCOUNT = "UNKNOWN_ACCOUNT"
    UNKNOWN_METRIC = "UNKNOWN_METRIC"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"
    FUTURE_EVENT = "FUTURE_EVENT"
    PERIOD_ALREADY_BILLED = "PERIOD_ALREADY_BILLED"
    # lifecycle
    ACCOUNT_EXISTS = "ACCOUNT_EXISTS"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    REFUND_EXCEEDS_REFUNDABLE = "REFUND_EXCEEDS_REFUNDABLE"
    BILL_NOT_FOUND = "BILL_NOT_FOUND"


class BillingError(Exception):
    """Base error carrying a machine-readable reason code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ValidationError(BillingError):
    """A usage report failed validation."""


class LifecycleError(BillingError):
    """A subscription lifecycle operation was rejected."""
