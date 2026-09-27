"""用量上报与归集。

职责：
1. 校验上报（字段缺失 / 非法值 / 未知计量项 / 时间异常），拒绝原因可区分；
2. 幂等：同一 event_id 的重复提交不重复计费；负载不一致按冲突拒绝；
3. 归集：按 occurred_at（而非 received_at）落入正确账期，迟到/乱序安全；
4. 并发：写入在 store.lock 内完成“查重 -> 落库”，保证不漏数。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .errors import BillingError, ErrorCode
from .models import BillStatus, UsageEvent, UsageStatus
from .storage import LocalStore

logger = logging.getLogger("billing.usage")

# 允许的未来时间偏移（时钟漂移容差）
FUTURE_SKEW = timedelta(minutes=1)
# 超过该时长的历史用量视为无法归集
MAX_AGE = timedelta(days=366)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UsageService:
    def __init__(self, store: LocalStore):
        self.store = store

    def period_for(self, plan_period_days: int, occurred_at: datetime,
                   anchor: date | None = None) -> date:
        """账期按自然月归集：账期起点为 occurred_at 所在月的 1 日。

        迟到/乱序事件与正常事件使用同一归属规则，因此总能落回正确账期。
        """
        return date(occurred_at.year, occurred_at.month, 1)

    def submit_usage(
        self,
        *,
        event_id,
        account_id,
        meter,
        quantity,
        occurred_at,
        now: datetime | None = None,
    ) -> UsageEvent:
        """提交一笔用量。

        - 成功：落库 ACCEPTED 事件并返回（status=ACCEPTED）；
        - 同一 event_id 完全相同的重放：不落第二次账，返回原事件（status=DUPLICATE）；
        - event_id 相同但负载不一致：抛 DUPLICATE_CONFLICT；
        - 其它非法输入：抛 BillingError，code 区分原因。
        """
        now = now or _utcnow()
        logger.info(
            "收到用量上报 event_id=%r account=%r meter=%r qty=%r occurred_at=%r",
            event_id, account_id, meter, quantity, occurred_at,
        )
        qty, ts = self._validate(event_id, account_id, meter, quantity,
                                 occurred_at, now)

        # 幂等查重与落库放在同一把锁内，避免并发双写
        with self.store.lock:
            existing = self.store.get_event(event_id)
            if existing is not None:
                same = (
                    existing.account_id == account_id
                    and existing.meter == meter
                    and existing.quantity == qty
                    and existing.occurred_at == ts
                )
                if not same:
                    logger.warning(
                        "幂等冲突 event_id=%s：新负载(%s/%s/%s/%s) 与原负载(%s/%s/%s/%s)",
                        event_id, account_id, meter, qty, ts.isoformat(),
                        existing.account_id, existing.meter, existing.quantity,
                        existing.occurred_at.isoformat(),
                    )
                    raise BillingError(
                        ErrorCode.DUPLICATE_CONFLICT,
                        f"event_id={event_id} 已存在但负载不一致",
                        details={
                            "existing": {
                                "account_id": existing.account_id,
                                "meter": existing.meter,
                                "quantity": str(existing.quantity),
                                "occurred_at": existing.occurred_at.isoformat(),
                            },
                        },
                    )
                logger.info("判定=DUPLICATE event_id=%s 依据=event_id 已存在且负载一致，"
                            "不重复计费", event_id)
                # 返回副本承载本次响应语义，存储中的事件保持 ACCEPTED（只计一次费）
                dup = UsageEvent(
                    event_id=existing.event_id, account_id=existing.account_id,
                    meter=existing.meter, quantity=existing.quantity,
                    occurred_at=existing.occurred_at,
                    received_at=existing.received_at,
                    period_start=existing.period_start,
                    status=UsageStatus.DUPLICATE,
                )
                return dup

            # 账户与套餐/计量项在锁内再次确认（防止与退订等并发）
            if account_id not in self.store.accounts:
                raise BillingError(ErrorCode.ACCOUNT_NOT_FOUND,
                                   f"账户不存在: {account_id}")
            sub = self.store.get_subscription(account_id)
            segment = sub.active_segment_at(ts) if sub else None
            if segment is None:
                raise BillingError(
                    ErrorCode.NO_ACTIVE_PLAN,
                    f"账户 {account_id} 在 {ts.isoformat()} 没有有效套餐",
                )
            plan = self.store.get_plan(segment.plan_id)
            if meter not in plan.meters:
                raise BillingError(ErrorCode.UNKNOWN_METER,
                                   f"套餐 {plan.plan_id} 下无计量项: {meter}")

            period_start = self.period_for(plan.period_days, ts)

            # 已出账账期不允许再补写：历史账单冻结，迟到事件明确拒绝
            bill = self.store.get_bill(account_id, period_start)
            if bill is not None and bill.status is BillStatus.FINALIZED:
                logger.warning(
                    "拒绝迟到事件 event_id=%s：账期 %s 账单已冻结（不改写历史）",
                    event_id, period_start.isoformat(),
                )
                raise BillingError(
                    ErrorCode.BILL_ALREADY_FINALIZED,
                    f"账期 {period_start.isoformat()} 已出账，迟到事件 "
                    f"{event_id} 不能改写历史账单",
                    details={"event_id": event_id,
                             "period_start": period_start.isoformat()},
                )

            event = UsageEvent(
                event_id=event_id, account_id=account_id, meter=meter,
                quantity=qty, occurred_at=ts, received_at=now,
                period_start=period_start, status=UsageStatus.ACCEPTED,
            )
            self.store.put_event(event)
            logger.info(
                "判定=ACCEPTED event_id=%s 依据=校验通过且首次见到该 event_id；"
                "按 occurred_at 归集账期 %s（received_at=%s）",
                event_id, period_start.isoformat(), now.isoformat(),
            )
            return event

    def _validate(self, event_id, account_id, meter, quantity, occurred_at,
                  now: datetime):
        """字段级与业务级校验，返回规整后的 (qty: Decimal, ts: datetime)。"""

        def require(name, value):
            if value is None or (isinstance(value, str) and not value.strip()):
                raise BillingError(ErrorCode.MISSING_FIELD,
                                   f"必填字段缺失: {name}",
                                   details={"field": name})

        require("event_id", event_id)
        require("account_id", account_id)
        require("meter", meter)
        if quantity is None:
            raise BillingError(ErrorCode.MISSING_FIELD, "必填字段缺失: quantity",
                               details={"field": "quantity"})
        if occurred_at is None:
            raise BillingError(ErrorCode.MISSING_FIELD,
                               "必填字段缺失: occurred_at",
                               details={"field": "occurred_at"})

        if not isinstance(event_id, str):
            raise BillingError(ErrorCode.INVALID_FIELD, "event_id 必须是字符串",
                               details={"field": "event_id"})
        if not isinstance(account_id, str) or not isinstance(meter, str):
            raise BillingError(ErrorCode.INVALID_FIELD,
                               "account_id/meter 必须是字符串")

        # quantity：接受 int / 数字字符串；拒绝 bool/float/NaN/非数字
        if isinstance(quantity, bool):
            raise BillingError(ErrorCode.INVALID_FIELD,
                               "quantity 不能是布尔值", details={"field": "quantity"})
        if isinstance(quantity, float):
            raise BillingError(ErrorCode.INVALID_FIELD,
                               "quantity 不接受浮点数，请用整数或字符串以避免精度问题",
                               details={"field": "quantity"})
        try:
            qty = Decimal(str(quantity))
        except (InvalidOperation, ValueError):
            raise BillingError(ErrorCode.INVALID_FIELD,
                               f"quantity 不是合法数字: {quantity!r}",
                               details={"field": "quantity"})
        if not qty.is_finite():
            raise BillingError(ErrorCode.INVALID_FIELD,
                               "quantity 必须是有限数字",
                               details={"field": "quantity"})
        if qty < 0:
            raise BillingError(ErrorCode.NEGATIVE_USAGE,
                               f"quantity 不能为负: {qty}",
                               details={"field": "quantity"})

        # occurred_at：ISO8601；naive 视为 UTC
        if isinstance(occurred_at, datetime):
            ts = occurred_at
        elif isinstance(occurred_at, str):
            try:
                ts = datetime.fromisoformat(occurred_at)
            except ValueError:
                raise BillingError(ErrorCode.INVALID_FIELD,
                                   f"occurred_at 不是合法 ISO8601 时间: "
                                   f"{occurred_at!r}",
                                   details={"field": "occurred_at"})
        else:
            raise BillingError(ErrorCode.INVALID_FIELD,
                               "occurred_at 必须是 ISO8601 字符串",
                               details={"field": "occurred_at"})
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if ts > now + FUTURE_SKEW:
            raise BillingError(ErrorCode.USAGE_IN_FUTURE,
                               f"用量时间在未来: {ts.isoformat()}",
                               details={"occurred_at": ts.isoformat()})
        if ts < now - MAX_AGE:
            raise BillingError(ErrorCode.USAGE_TOO_OLD,
                               f"用量时间过早（超过 {MAX_AGE.days} 天），无法归集",
                               details={"occurred_at": ts.isoformat()})
        return qty, ts
