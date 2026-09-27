"""Concurrent writes to one account must not corrupt or lose totals.

The service serializes all mutations on a per-process storage lock.
These tests hammer one account from many threads and then verify that:

1. every uniquely-identified event is accepted and stored exactly once;
2. the bill total equals a single-threaded recomputation;
3. the tier breakdown per line reconciles with the line amount.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

from billing.plans import get_plan
from billing.pricing import price_usage
from tests.conftest import make_event

logger = logging.getLogger("billing.test")


def test_concurrent_unique_events_all_counted(svc, clock):
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    # 100 threads, one event each on a unique day (9/1..9/28, unique ids),
    # 1000 units apiece -> total 100,000 api_calls.
    events = [
        make_event(f"evt-{i:03d}", "acme", "api_calls", "1000", date(2026, 9, 1 + i))
        for i in range(28)
    ]
    n = len(events)
    # clock sits at month end the whole time, so every event is in the past
    clock.set(date(2026, 9, 30))
    logger.info("[concurrent] submitting %d unique events from 32 threads", n)

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(svc.report_usage, events))

    accepted = results.count("accepted")
    logger.info("decision=concurrent-accept accepted=%d expected=%d", accepted, n)
    assert accepted == n, f"only {accepted}/{n} unique events accepted"

    stored = svc._storage.events_for_period("acme", date(2026, 9, 1), date(2026, 9, 30))
    assert len(stored) == n
    assert len({e.event_id for e in stored}) == n

    bill = svc.close_period("acme", date(2026, 9, 1))
    assert len(bill.usage_lines) == n
    # total qty 28,000 within free allowance 10,000 -> charged 18,000 @ 0.001
    logger.info("decision=concurrent-bill lines=%d total_usage=%s", n, bill.total_usage)
    assert bill.total_usage == Decimal("18.00"), bill.total_usage
    assert sum((l.amount for l in bill.usage_lines), Decimal("0")) == bill.total_usage
    for line in bill.usage_lines:
        assert sum((t.amount for t in line.tier_breakdown), Decimal("0")) == line.amount


def test_concurrent_unique_events_cross_tier_boundary(svc, clock):
    """Bigger volume from many threads crosses the 100k tier boundary;
    the marginal aggregation must still equal one-shot pricing."""
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))
    clock.set(date(2026, 9, 30))

    # 100 events x 2,000 = 200,000 on unique days
    events = [
        make_event(f"v-{i:03d}", "acme", "api_calls", "2000", date(2026, 9, 1 + (i % 28)))
        for i in range(100)
    ]
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(svc.report_usage, events))
    assert results.count("accepted") == 100

    bill = svc.close_period("acme", date(2026, 9, 1))
    pro_api = get_plan("pro").price_for("api_calls")
    _, _, tiers = price_usage(pro_api, Decimal("200000"), Decimal("10000"))
    expected = sum((t.amount for t in tiers), Decimal("0"))
    logger.info(
        "decision=tier-boundary billed=%s one_shot=%s", bill.total_usage, expected
    )
    assert bill.total_usage == expected
    assert sum((l.amount for l in bill.usage_lines), Decimal("0")) == bill.total_usage


def test_concurrent_duplicate_submissions_billed_once(svc, clock):
    clock.set(date(2026, 9, 30))
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    same = [
        make_event("only-one", "acme", "api_calls", "50000", date(2026, 9, 20))
        for _ in range(100)
    ]
    logger.info("[concurrent-dup] 100 threads submit one shared event_id")
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(svc.report_usage, same))

    accepted = results.count("accepted")
    duplicates = results.count("duplicate")
    logger.info("decision=concurrent-dup accepted=%d duplicate=%d", accepted, duplicates)
    assert accepted == 1
    assert duplicates == 99

    stored = svc._storage.events_for_period("acme", date(2026, 9, 1), date(2026, 9, 30))
    assert len(stored) == 1
    bill = svc.close_period("acme", date(2026, 9, 1))
    assert len(bill.usage_lines) == 1
    assert bill.total_usage == Decimal("40.00"), bill.total_usage


def test_concurrent_plan_change_and_usage(svc, clock):
    clock.set(date(2026, 9, 16))
    svc.create_account("acme")
    svc.subscribe("acme", "pro", effective=date(2026, 9, 1))

    # 50 usage events for Sep-16 race against the pro->enterprise change.
    events = [
        make_event(f"mix-{i:02d}", "acme", "api_calls", "1000", date(2026, 9, 16))
        for i in range(50)
    ]
    with ThreadPoolExecutor(max_workers=16) as pool:
        usage_futures = [pool.submit(svc.report_usage, e) for e in events]
        change_future = pool.submit(svc.change_plan, "acme", "enterprise", date(2026, 9, 15))
        change_future.result()
        results = [f.result() for f in usage_futures]

    logger.info(
        "decision=concurrent-mix accepted=%d other=%s",
        results.count("accepted"),
        {r for r in results if r != "accepted"},
    )
    assert set(results) == {"accepted"}  # both plans meter api_calls

    bill = svc.close_period("acme", date(2026, 9, 1))
    assert len(bill.usage_lines) == 50
    # events are dated 9/16 -> enterprise segment regardless of race order
    plans = {line.plan_code for line in bill.usage_lines}
    assert plans == {"enterprise"}, plans
    assert sum((l.amount for l in bill.usage_lines), Decimal("0")) == bill.total_usage

    enterprise = get_plan("enterprise")
    allowance = Decimal("1000000") * Decimal(16) / Decimal(30)
    _, _, tiers = price_usage(enterprise.price_for("api_calls"), Decimal("50000"), allowance)
    expected = sum((t.amount for t in tiers), Decimal("0"))
    logger.info("decision=concurrent-mix-reconcile billed=%s recomputed=%s", bill.total_usage, expected)
    assert bill.total_usage == expected
