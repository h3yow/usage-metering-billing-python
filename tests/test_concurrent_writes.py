"""并发写入：同一账户多线程上报不丢数、不重复，账单总额正确。"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from decimal import Decimal

from app.billing_service import BillingService
from app.models import PlanStatus, Subscription, SubscriptionSegment
from app.usage_service import UsageService

log = logging.getLogger("test.concurrent")


def test_concurrent_submit_total_is_correct(store):
    store.put_subscription(Subscription("acct-1", [
        SubscriptionSegment("basic", datetime(2026, 9, 1, tzinfo=timezone.utc),
                            None, PlanStatus.ACTIVE)
    ]))
    svc = UsageService(store)
    bs = BillingService(store)

    n_threads = 10
    per_thread = 50
    barrier = threading.Barrier(n_threads)

    def worker(tid: int):
        barrier.wait()  # 尽量同时放行，制造竞争
        for i in range(per_thread):
            # 每笔 100 次（在免费额度内），另用 storage_gb 做可加总额校验
            svc.submit_usage(
                event_id=f"c-{tid}-{i}", account_id="acct-1",
                meter="storage_gb", quantity="1",
                occurred_at=f"2026-09-{(i % 28) + 1:02d}T00:00:00+00:00",
                now=datetime(2026, 9, 28, tzinfo=timezone.utc),
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    log.info("输入: %d 个线程各 %d 笔 storage_gb=1，期望共 %d 笔、总额 %s",
             n_threads, per_thread, n_threads * per_thread,
             Decimal(n_threads * per_thread) * Decimal("0.10"))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = store.list_events("acct-1", date(2026, 9, 1))
    log.info("判定依据: 存储中账期内已接受流水数=%d（期望 %d）",
             len(events), n_threads * per_thread)
    assert len(events) == n_threads * per_thread
    assert len({e.event_id for e in events}) == n_threads * per_thread  # 无重复

    bill = bs.build_draft("acct-1", date(2026, 9, 1))
    log.info("判定依据: 账单明细行=%d，逐行金额求和 total=%s",
             len(bill.line_items), bill.total_amount)
    assert len(bill.line_items) == n_threads * per_thread
    assert bill.total_amount == Decimal(n_threads * per_thread) * Decimal("0.10")
    # 交叉校验：逐行求和必须等于总额字段
    assert sum((li.amount for li in bill.line_items), Decimal("0")) == bill.total_amount


def test_concurrent_duplicate_submit_charged_once(store):
    """多个线程用同一个 event_id 同时提交，只能有一笔成功入账。"""
    store.put_subscription(Subscription("acct-1", [
        SubscriptionSegment("basic", datetime(2026, 9, 1, tzinfo=timezone.utc),
                            None, PlanStatus.ACTIVE)
    ]))
    svc = UsageService(store)
    n = 8
    barrier = threading.Barrier(n)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        ev = svc.submit_usage(
            event_id="same-id", account_id="acct-1", meter="storage_gb",
            quantity="1", occurred_at="2026-09-10T00:00:00+00:00",
            now=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )
        with lock:
            results.append(ev.status.value)

    log.info("输入: %d 线程并发提交同一 event_id=same-id", n)
    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = results.count("ACCEPTED")
    duplicate = results.count("DUPLICATE")
    log.info("判定依据: ACCEPTED=%d DUPLICATE=%d（期望 1/%d）", accepted, duplicate, n - 1)
    assert accepted == 1 and duplicate == n - 1
    assert len(store.list_events("acct-1", date(2026, 9, 1))) == 1
