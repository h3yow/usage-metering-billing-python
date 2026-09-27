"""Static plan catalog used by the demo service."""

from __future__ import annotations

from billing.models import Plan


def get_plan(code: str) -> Plan | None:
    """Look up a plan by code; ``None`` if unknown."""
    raise NotImplementedError


def list_plans() -> list[Plan]:
    """Return all available plans."""
    raise NotImplementedError
