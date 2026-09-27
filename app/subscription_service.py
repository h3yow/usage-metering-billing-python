"""套餐生命周期：订阅、周期内变更、退订与退款额度。

关键约定：
1. 账期按自然月（与用量归集一致）；订阅段 [effective_from, effective_to) 互不重叠；
2. 周期内变更/退订只影响生效时刻之后的用量计价（出账时按每笔用量 occurred_at
   时刻生效的套餐取价），因此同账期天然“按段计价”；
3. 退订按剩余整月天数比例折算可退额度：refund = base_refund * 剩余天数/账期总天数；
   调用方申请的退款不得超过可退额度；
4. 已 FINALIZED 的历史账单不受影响（变更不回改旧账单，只结束/开启订阅段）；
5. 非法操作（重复订阅、无订阅变更、时间越界、超额退款）均带错误码拒绝。
"""
from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone
from decimal import Decimal

from .errors import BillingError, ErrorCode
from .models import (
    PlanStatus,
    Subscription,
    SubscriptionSegment,
)
from .storage import LocalStore
from .pricing import money

logger = logging.getLogger("billing.subscription")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def month_bounds(at: datetime) -> tuple[datetime, datetime, int]:
    """返回 at 所在自然月的 [起, 止)（UTC, aware）与总天数。"""
    first = datetime(at.year, at.month, 1, tzinfo=timezone.utc)
    days = calendar.monthrange(at.year, at.month)[1]
    if at.month == 12:
        nxt = datetime(at.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        nxt = datetime(at.year, at.month + 1, 1, tzinfo=timezone.utc)
    return first, nxt, days


def remaining_day_ratio(at: datetime) -> tuple[Decimal, int, int]:
    """剩余天数占比（含当天剩余时间按整天计），返回 (ratio, 剩余天数, 总天数)。"""
    first, nxt, days = month_bounds(at)
    # 剩余整天数：从 at 当天到月末（含当天）
    remaining_days = days - at.day + 1
    ratio = Decimal(remaining_days) / Decimal(days)
    return ratio, remaining_days, days


class SubscriptionService:
    def __init__(self, store: LocalStore):
        self.store = store
        # 套餐月费基数（可退额度来源）；纯按量套餐为 0。
        # 放实例上，避免多个 store/服务实例之间串数据。
        self.refund_credits: dict[str, Decimal] = {}

    def subscribe(self, account_id: str, plan_id: str,
                  effective_from: datetime | None = None) -> Subscription:
        effective_from = effective_from or _utcnow()
        if effective_from.tzinfo is None:
            effective_from = effective_from.replace(tzinfo=timezone.utc)
        logger.info("订阅 account=%s plan=%s from=%s",
                    account_id, plan_id, effective_from.isoformat())
        with self.store.lock:
            if account_id not in self.store.accounts:
                raise BillingError(ErrorCode.ACCOUNT_NOT_FOUND,
                                   f"账户不存在: {account_id}")
            plan = self.store.get_plan(plan_id)  # 校验套餐存在
            sub = self.store.get_subscription(account_id)
            if sub is not None and sub.active_segment_at(effective_from) is not None:
                raise BillingError(
                    ErrorCode.ALREADY_SUBSCRIBED,
                    f"账户 {account_id} 在该时刻已有有效套餐，请走变更",
                )
            if sub is None:
                sub = Subscription(account_id=account_id)
            sub.segments.append(SubscriptionSegment(
                plan_id=plan.plan_id, effective_from=effective_from,
                effective_to=None, status=PlanStatus.ACTIVE,
            ))
            self.store.put_subscription(sub)
            return sub

    def change_plan(self, account_id: str, new_plan_id: str,
                    effective_at: datetime | None = None) -> Subscription:
        effective_at = effective_at or _utcnow()
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=timezone.utc)
        logger.info("变更套餐 account=%s new_plan=%s at=%s",
                    account_id, new_plan_id, effective_at.isoformat())
        with self.store.lock:
            sub = self._require_active_sub(account_id, effective_at)
            current = sub.active_segment_at(effective_at)
            if current.plan_id == new_plan_id:
                raise BillingError(
                    ErrorCode.ALREADY_SUBSCRIBED,
                    f"账户已在使用套餐 {new_plan_id}，无需变更",
                )
            self.store.get_plan(new_plan_id)  # 校验新套餐存在
            _, month_end, _ = month_bounds(effective_at)
            current.effective_to = effective_at  # 旧段半开区间结束
            sub.segments.append(SubscriptionSegment(
                plan_id=new_plan_id, effective_from=effective_at,
                effective_to=None, status=PlanStatus.ACTIVE,
            ))
            self.store.put_subscription(sub)
            logger.info(
                "变更生效：旧段 %s 截止于 %s；新段 %s 生效；月内用量按各自区间计价",
                current.plan_id, effective_at.isoformat(), new_plan_id,
            )
            return sub

    def cancel(self, account_id: str, effective_at: datetime | None = None,
               refund_amount: Decimal | None = None):
        """退订；refund_amount 为申请退款额度，必须 <= 按剩余周期折算的可退额度。

        当前套餐目录为 0 月费（纯按量），默认可退额度为 0；
        可通过 set_refund_credit 为套餐段登记可退基数（见下）。
        返回 (subscription, granted_refund, refundable_credit)。
        """
        effective_at = effective_at or _utcnow()
        if effective_at.tzinfo is None:
            effective_at = effective_at.replace(tzinfo=timezone.utc)
        with self.store.lock:
            sub = self._require_active_sub(account_id, effective_at)
            current = sub.active_segment_at(effective_at)
            ratio, remaining_days, days = remaining_day_ratio(effective_at)
            base = self.refund_credits.get(current.plan_id, Decimal("0"))
            refundable = money(base * ratio)
            logger.info(
                "退订 account=%s plan=%s at=%s 剩余天数=%d/%d 比例=%s 可退额度=%s",
                account_id, current.plan_id, effective_at.isoformat(),
                remaining_days, days, ratio, refundable,
            )
            if refund_amount is not None:
                refund_amount = money(Decimal(str(refund_amount)))
                if refund_amount < 0:
                    raise BillingError(ErrorCode.INVALID_REFUND,
                                       "退款额度不能为负")
                if refund_amount > refundable:
                    raise BillingError(
                        ErrorCode.INVALID_REFUND,
                        f"申请退款 {refund_amount} 超过可退额度 {refundable}"
                        f"（剩余 {remaining_days}/{days} 天）",
                        details={"requested": str(refund_amount),
                                 "refundable": str(refundable)},
                    )
            granted = refund_amount if refund_amount is not None else refundable
            current.effective_to = effective_at
            current.status = PlanStatus.CANCELLED
            self.store.put_subscription(sub)
            return sub, granted, refundable

    def active_plan_at(self, account_id: str, at: datetime):
        """返回某时刻生效的 (segment, plan)；无有效套餐抛 NO_ACTIVE_PLAN。"""
        sub = self.store.get_subscription(account_id)
        seg = sub.active_segment_at(at) if sub else None
        if seg is None:
            raise BillingError(ErrorCode.NO_ACTIVE_PLAN,
                               f"账户 {account_id} 在 {at.isoformat()} 无有效套餐")
        return seg, self.store.get_plan(seg.plan_id)

    def _require_active_sub(self, account_id: str, at: datetime) -> Subscription:
        if account_id not in self.store.accounts:
            raise BillingError(ErrorCode.ACCOUNT_NOT_FOUND,
                               f"账户不存在: {account_id}")
        sub = self.store.get_subscription(account_id)
        if sub is None or sub.active_segment_at(at) is None:
            raise BillingError(ErrorCode.NOT_SUBSCRIBED,
                               f"账户 {account_id} 在 {at.isoformat()} 无有效套餐")
        # 变更时间必须落在当前有效段内（不能对过去或下周期直接操作）
        cur = sub.active_segment_at(at)
        if at < cur.effective_from:
            raise BillingError(ErrorCode.INVALID_CHANGE_TIME,
                               "变更时间早于当前套餐生效时间")
        return sub

    def set_refund_credit(self, plan_id: str, monthly_base: Decimal) -> None:
        self.store.get_plan(plan_id)
        self.refund_credits[plan_id] = money(Decimal(str(monthly_base)))
