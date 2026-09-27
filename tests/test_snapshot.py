"""JSON 快照：落盘后重新加载，账单与流水可复现（支持对账）。"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from app.billing_service import BillingService
from app.storage import LocalStore
from app.subscription_service import SubscriptionService
from app.usage_service import UsageService

log = logging.getLogger("test.snapshot")


def test_snapshot_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    store = LocalStore(path)
    from app.bootstrap import seed_catalog
    seed_catalog(store)
    ss, us, bs = SubscriptionService(store), UsageService(store), BillingService(store)
    ss.subscribe("acct-1", "basic", datetime(2026, 9, 1, tzinfo=timezone.utc))
    us.submit_usage(event_id="s1", account_id="acct-1", meter="api_calls",
                    quantity="5000", occurred_at="2026-09-10T00:00:00+00:00",
                    now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    bill = bs.finalize("acct-1", date(2026, 9, 1))
    store.save_snapshot()
    log.info("快照已写入 %s，账单总额=%s", path, bill.total_amount)

    restored = LocalStore.load_snapshot(path)
    loaded = restored.get_bill("acct-1", date(2026, 9, 1))
    log.info("重载账单: status=%s total=%s 明细=%d",
             loaded.status.value, loaded.total_amount, len(loaded.line_items))
    assert loaded.total_amount == bill.total_amount == Decimal("80.00")
    assert loaded.status.value == "FINALIZED"
    assert len(loaded.line_items) == 1
    assert restored.get_event("s1").period_start == date(2026, 9, 1)
    events = restored.list_events("acct-1", date(2026, 9, 1))
    assert sum((li.amount for li in loaded.line_items), Decimal("0")) == loaded.total_amount
    assert len(events) == 1
