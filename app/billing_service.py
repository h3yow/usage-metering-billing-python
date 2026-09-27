"""出账：把账期内已接受用量按每笔发生时生效的套餐逐笔计价生成账单。

可对账 / 可复现：
- 账单总额 = 明细行金额之和（_recompute_total 为唯一总额来源）；
- 每行带计量项、数量、免费额度与档位拆解，可逐项对到用量流水；
- 幂等去重由上报层保证，账内每个 event_id 至多一行；
- FINALIZED 后冻结：重复 finalize 返回同一版本；已冻结账单不能再重建/改写；
- 出账前到达（含乱序/迟到）的用量都会计入草稿，重算结果可复现；
- 出账后才到达的旧账期用量：账单已冻结，按账期不可改写原则拒绝，并提示其
  归属账期（需要时走补退/下期调整流程，而非改写历史）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from .errors import BillingError, ErrorCode
from .models import Bill, BillStatus, LineItem
from .pricing import price_meter, zero_amount
from .storage import LocalStore

logger = logging.getLogger("billing.billing")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def period_end(start: date) -> date:
    """自然月账期的右开边界（下月 1 日）。"""
    if start.month == 12:
        return date(start.year + 1, 1, 1)
    return date(start.year, start.month + 1, 1)


class BillingService:
    def __init__(self, store: LocalStore):
        self.store = store

    def build_draft(self, account_id: str, period_start: date,
                    now: datetime | None = None) -> Bill:
        """（重新）生成某账期草稿账单。

        每次都从流水重算，保证可复现；FINALIZED 账单禁止重建。
        """
        now = now or _utcnow()
        with self.store.lock:
            existing = self.store.get_bill(account_id, period_start)
            if existing is not None and existing.status is BillStatus.FINALIZED:
                raise BillingError(
                    ErrorCode.BILL_ALREADY_FINALIZED,
                    f"账单 {account_id}/{period_start.isoformat()} 已出账，"
                    "历史账单不可改写",
                )
            events = self.store.list_events(account_id, period_start)
            events.sort(key=lambda e: (e.occurred_at, e.event_id))
            line_items: list[LineItem] = []
            for ev in events:
                line_items.append(self._line_for_event(ev))
            bill = Bill(
                account_id=account_id, period_start=period_start,
                period_end=period_end(period_start),
                status=BillStatus.DRAFT, line_items=line_items,
                created_at=existing.created_at if existing else now,
                version=(existing.version + 1) if existing else 0,
            )
            bill.total_amount = self._recompute_total(bill)
            self.store.put_bill(bill)
            logger.info(
                "生成草稿账单 account=%s period=%s 明细=%d 行 总额=%s（按 occurred_at "
                "时刻生效套餐逐笔计价）",
                account_id, period_start.isoformat(), len(line_items), bill.total_amount,
            )
            return bill

    def finalize(self, account_id: str, period_start: date,
                 now: datetime | None = None) -> Bill:
        """冻结账单。已冻结则原样返回（出账幂等）。"""
        now = now or _utcnow()
        with self.store.lock:
            bill = self.store.get_bill(account_id, period_start)
            if bill is None:
                bill = self.build_draft(account_id, period_start, now)
            if bill.status is BillStatus.FINALIZED:
                logger.info("账单已冻结，重复出账原样返回 account=%s period=%s version=%d",
                            account_id, period_start.isoformat(), bill.version)
                return bill
            # 冻结前再重算一次，确保与最新流水一致
            bill = self.build_draft(account_id, period_start, now)
            bill.status = BillStatus.FINALIZED
            bill.finalized_at = now
            bill.version += 1
            self.store.put_bill(bill)
            logger.info("账单冻结 account=%s period=%s version=%d 总额=%s",
                        account_id, period_start.isoformat(),
                        bill.version, bill.total_amount)
            return bill

    def get_bill(self, account_id: str, period_start: date) -> Bill:
        bill = self.store.get_bill(account_id, period_start)
        if bill is None:
            raise BillingError(
                ErrorCode.BILL_NOT_FOUND,
                f"账单不存在: {account_id}/{period_start.isoformat()}",
            )
        return bill

    def _line_for_event(self, event) -> LineItem:
        """单笔用量 -> 明细行（按 occurred_at 时刻生效套餐中的该计量项计价）。"""
        sub = self.store.get_subscription(event.account_id)
        seg = sub.active_segment_at(event.occurred_at) if sub else None
        if seg is None:
            # 数据一致性兜底：已接受事件在发生时刻必有套餐
            raise BillingError(
                ErrorCode.NO_ACTIVE_PLAN,
                f"事件 {event.event_id} 发生时无有效套餐，无法计价",
            )
        plan = self.store.get_plan(seg.plan_id)
        meter_def = plan.meters.get(event.meter)
        if meter_def is None:
            raise BillingError(
                ErrorCode.UNKNOWN_METER,
                f"套餐 {plan.plan_id} 下无计量项 {event.meter}",
            )
        breakdown, amount = price_meter(meter_def, event.quantity)
        return LineItem(
            event_id=event.event_id, meter=event.meter,
            quantity=event.quantity, period_start=event.period_start,
            occurred_at=event.occurred_at, plan_id=plan.plan_id,
            charge_mode=meter_def.charge_mode,
            free_allowance=meter_def.free_allowance,
            tier_breakdown=breakdown, amount=amount,
        )

    @staticmethod
    def _recompute_total(bill: Bill) -> Decimal:
        """总额 = 明细金额之和（单一事实来源，防止并发/重算漂移）。"""
        total = zero_amount()
        for li in bill.line_items:
            total += li.amount
        return total
