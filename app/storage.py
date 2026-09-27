"""本地存储：内存表 + 一把可重入锁模拟事务，可选 JSON 快照落盘。

并发约定：所有跨表的读改写操作都必须在 self.lock 下完成，
保证同一账户的用量并发写入不会漏数或算错总额。
账单带 version 字段，写回时校验版本（乐观锁兜底）。
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .errors import BillingError, ErrorCode
from .models import (
    Bill,
    BillStatus,
    ChargeMode,
    LineItem,
    MeterDef,
    Plan,
    PlanStatus,
    PriceTier,
    Subscription,
    SubscriptionSegment,
    UsageEvent,
    UsageStatus,
)


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


class LocalStore:
    """进程内本地存储（单实例；测试与本地验证足够）。"""

    def __init__(self, snapshot_path: str | Path | None = None):
        self.lock = threading.RLock()
        self.plans: dict[str, Plan] = {}
        self.events: dict[str, UsageEvent] = {}                 # event_id -> event
        self.account_events: dict[str, list[str]] = {}          # account_id -> [event_id]
        self.subscriptions: dict[str, Subscription] = {}        # account_id -> sub
        self.bills: dict[tuple[str, date], Bill] = {}           # (account, period) -> bill
        self.accounts: set[str] = set()
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None

    # ---- 账户 / 套餐目录 ----
    def create_account(self, account_id: str) -> None:
        with self.lock:
            self.accounts.add(account_id)
            self.account_events.setdefault(account_id, [])

    def upsert_plan(self, plan: Plan) -> None:
        with self.lock:
            self.plans[plan.plan_id] = plan

    def get_plan(self, plan_id: str) -> Plan:
        with self.lock:
            plan = self.plans.get(plan_id)
            if plan is None:
                raise BillingError(ErrorCode.PLAN_NOT_FOUND, f"套餐不存在: {plan_id}")
            return plan

    # ---- 用量流水 ----
    def get_event(self, event_id: str) -> UsageEvent | None:
        with self.lock:
            return self.events.get(event_id)

    def put_event(self, event: UsageEvent) -> None:
        with self.lock:
            if event.event_id not in self.events:
                self.account_events.setdefault(event.account_id, []).append(
                    event.event_id
                )
            self.events[event.event_id] = event

    def list_events(self, account_id: str, period_start: date) -> list[UsageEvent]:
        """返回该账户归集到指定账期、且已接受（幂等去重后）的全部流水。"""
        with self.lock:
            ids = self.account_events.get(account_id, [])
            return [
                self.events[eid]
                for eid in ids
                if (e := self.events.get(eid)) is not None
                and e.period_start == period_start
                and e.status is UsageStatus.ACCEPTED
            ]

    # ---- 订阅 ----
    def get_subscription(self, account_id: str) -> Subscription | None:
        with self.lock:
            return self.subscriptions.get(account_id)

    def put_subscription(self, sub: Subscription) -> None:
        with self.lock:
            self.subscriptions[sub.account_id] = sub

    # ---- 账单 ----
    def get_bill(self, account_id: str, period_start: date) -> Bill | None:
        with self.lock:
            return self.bills.get((account_id, period_start))

    def put_bill(self, bill: Bill, expected_version: int | None = None) -> Bill:
        """写回账单；提供 expected_version 时做乐观并发校验。"""
        with self.lock:
            key = (bill.account_id, bill.period_start)
            old = self.bills.get(key)
            if expected_version is not None and old is not None:
                if old.version != expected_version:
                    raise BillingError(
                        ErrorCode.CONFLICT,
                        "账单已被其他写操作修改，请重试",
                        details={"expected": expected_version,
                                 "actual": old.version},
                    )
            if old is not None and expected_version is None:
                bill.version = old.version  # 调用方未走读改写时保留版本
            self.bills[key] = bill
            return bill

    # ---- 快照（可复现 / 对账）----
    def save_snapshot(self) -> None:
        if self.snapshot_path is None:
            return
        with self.lock:
            self.snapshot_path.write_text(
                json.dumps(self._to_jsonable(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _to_jsonable(self) -> dict:
        plans = {}
        for pid, p in self.plans.items():
            plans[pid] = {
                "plan_id": p.plan_id,
                "name": p.name,
                "period_days": p.period_days,
                "meters": {
                    m: {
                        "meter": d.meter,
                        "unit": d.unit,
                        "charge_mode": d.charge_mode.value,
                        "free_allowance": str(d.free_allowance),
                        "tiers": [
                            {"from_qty": str(t.from_qty),
                             "to_qty": str(t.to_qty) if t.to_qty is not None else None,
                             "unit_price": str(t.unit_price)}
                            for t in d.tiers
                        ],
                    }
                    for m, d in p.meters.items()
                },
            }
        events = {eid: e.to_dict() for eid, e in self.events.items()}
        subs: dict[str, list[dict]] = {}
        for aid, sub in self.subscriptions.items():
            subs[aid] = [
                {
                    "plan_id": s.plan_id,
                    "effective_from": s.effective_from.isoformat(),
                    "effective_to": s.effective_to.isoformat()
                    if s.effective_to else None,
                    "status": s.status.value,
                    "prorated_charge": str(s.prorated_charge),
                }
                for s in sub.segments
            ]
        bills = []
        for (aid, ps), b in self.bills.items():
            bills.append({
                "account_id": aid,
                "period_start": ps.isoformat(),
                "period_end": b.period_end.isoformat(),
                "status": b.status.value,
                "version": b.version,
                "total_amount": str(b.total_amount),
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "finalized_at": b.finalized_at.isoformat() if b.finalized_at else None,
                "line_items": [li.to_dict() for li in b.line_items],
            })
        return {
            "accounts": sorted(self.accounts),
            "plans": plans,
            "events": events,
            "account_events": {a: ids for a, ids in self.account_events.items()},
            "subscriptions": subs,
            "bills": bills,
        }

    @classmethod
    def load_snapshot(cls, path: str | Path) -> "LocalStore":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        s = cls(path)
        for aid in data.get("accounts", []):
            s.create_account(aid)
        for pid, p in data.get("plans", {}).items():
            meters = {}
            for m, d in p["meters"].items():
                tiers = tuple(
                    PriceTier(
                        from_qty=Decimal(t["from_qty"]),
                        to_qty=Decimal(t["to_qty"]) if t["to_qty"] is not None else None,
                        unit_price=Decimal(t["unit_price"]),
                    )
                    for t in d["tiers"]
                )
                meters[m] = MeterDef(
                    meter=d["meter"], unit=d["unit"],
                    charge_mode=ChargeMode(d["charge_mode"]),
                    free_allowance=Decimal(d["free_allowance"]), tiers=tiers,
                )
            s.upsert_plan(Plan(plan_id=p["plan_id"], name=p["name"],
                               period_days=p["period_days"], meters=meters))
        for eid, e in data.get("events", {}).items():
            s.put_event(UsageEvent(
                event_id=eid, account_id=e["account_id"], meter=e["meter"],
                quantity=Decimal(e["quantity"]), occurred_at=_dt(e["occurred_at"]),
                received_at=_dt(e["received_at"]),
                period_start=date.fromisoformat(e["period_start"]),
                status=UsageStatus(e["status"]), reject_reason=e.get("reject_reason"),
            ))
        # account_events 顺序恢复（put_event 已重建，这里对齐快照顺序）
        for aid, ids in data.get("account_events", {}).items():
            s.account_events[aid] = list(ids)
        for aid, segs in data.get("subscriptions", {}).items():
            s.put_subscription(Subscription(
                account_id=aid,
                segments=[
                    SubscriptionSegment(
                        plan_id=x["plan_id"], effective_from=_dt(x["effective_from"]),
                        effective_to=_dt(x["effective_to"]) if x["effective_to"] else None,
                        status=PlanStatus(x["status"]),
                        prorated_charge=Decimal(x["prorated_charge"]),
                    )
                    for x in segs
                ],
            ))
        for b in data.get("bills", []):
            items = [
                LineItem(
                    event_id=li["event_id"], meter=li["meter"],
                    quantity=Decimal(li["quantity"]),
                    period_start=date.fromisoformat(li["period_start"]),
                    occurred_at=_dt(li["occurred_at"]), plan_id=li["plan_id"],
                    charge_mode=ChargeMode(li["charge_mode"]),
                    free_allowance=Decimal(li["free_allowance"]),
                    tier_breakdown=[
                        (Decimal(x[0]), Decimal(x[1]), Decimal(x[2]))
                        for x in li["tier_breakdown"]
                    ],
                    amount=Decimal(li["amount"]),
                )
                for li in b["line_items"]
            ]
            bill = Bill(
                account_id=b["account_id"],
                period_start=date.fromisoformat(b["period_start"]),
                period_end=date.fromisoformat(b["period_end"]),
                status=BillStatus(b["status"]), line_items=items,
                total_amount=Decimal(b["total_amount"]),
                created_at=_dt(b["created_at"]) if b.get("created_at") else None,
                finalized_at=_dt(b["finalized_at"]) if b.get("finalized_at") else None,
                version=b.get("version", 0),
            )
            s.put_bill(bill)
        return s
