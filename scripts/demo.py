"""本地演示：覆盖正常/重复/乱序/改套餐/出账/快照的完整流程。

运行：.venv/bin/python scripts/demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.billing_service import BillingService
from app.bootstrap import seed_catalog
from app.storage import LocalStore
from app.subscription_service import SubscriptionService
from app.usage_service import UsageService
from app.errors import BillingError

SNAPSHOT = Path("data/demo-state.json")


def main() -> None:
    SNAPSHOT.parent.mkdir(exist_ok=True)
    store = LocalStore(SNAPSHOT)
    seed_catalog(store)
    ss, us, bs = SubscriptionService(store), UsageService(store), BillingService(store)
    dt = lambda d: datetime(2026, 9, d, 8, tzinfo=timezone.utc)

    ss.subscribe("acct-1", "basic", datetime(2026, 9, 1, tzinfo=timezone.utc))
    us.submit_usage(event_id="demo-1", account_id="acct-1", meter="api_calls",
                    quantity="5000", occurred_at=dt(10), now=dt(10))
    dup = us.submit_usage(event_id="demo-1", account_id="acct-1", meter="api_calls",
                          quantity="5000", occurred_at=dt(10), now=dt(11))
    print("重复上报判定:", dup.status.value)

    # 乱序：9/5 的事件 9/12 才到
    late = us.submit_usage(event_id="demo-2", account_id="acct-1",
                           meter="api_calls", quantity="5000",
                           occurred_at=dt(5), now=dt(12))
    print("迟到事件归集账期:", late.period_start.isoformat())

    ss.change_plan("acct-1", "pro", datetime(2026, 9, 15, tzinfo=timezone.utc))
    us.submit_usage(event_id="demo-3", account_id="acct-1", meter="api_calls",
                    quantity="110000", occurred_at=dt(20), now=dt(20))

    bill = bs.finalize("acct-1", date(2026, 9, 1))
    print(json.dumps(bill.to_dict(), ensure_ascii=False, indent=2, default=str))
    store.save_snapshot()
    print(f"快照已写入: {SNAPSHOT}")

    # 冻结后迟到事件被拒
    try:
        us.submit_usage(event_id="demo-4", account_id="acct-1", meter="api_calls",
                        quantity="1", occurred_at=dt(6),
                        now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    except BillingError as exc:
        print("冻结后迟到事件被拒:", exc.code.value, "-", exc.message)


if __name__ == "__main__":
    main()
