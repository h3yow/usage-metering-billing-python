"""领域模型（内存存储用的可序列化数据结构）。

金额一律用 Decimal（字符串构造），避免浮点误差；
时间一律用 timezone-aware datetime（UTC）。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from decimal import Decimal
from enum import Enum


class ChargeMode(str, Enum):
    """计费方式。"""

    TIERED = "tiered"        # 分段：各段分别计价后求和
    GRADUATED = "graduated"  # 阶梯：超出部分进入更高档


class UsageStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"


class PlanStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"


class BillStatus(str, Enum):
    DRAFT = "DRAFT"
    FINALIZED = "FINALIZED"


@dataclass(frozen=True)
class PriceTier:
    """价格档：[from_qty, to_qty)，to_qty 为 None 表示无上限。"""

    from_qty: Decimal
    unit_price: Decimal
    to_qty: Decimal | None = None


@dataclass(frozen=True)
class MeterDef:
    """计量项定义。"""

    meter: str
    unit: str
    charge_mode: ChargeMode
    free_allowance: Decimal
    tiers: tuple[PriceTier, ...]


@dataclass(frozen=True)
class Plan:
    """套餐：一个周期性订阅，包含若干计量项定价。"""

    plan_id: str
    name: str
    period_days: int
    meters: dict[str, MeterDef]


@dataclass
class UsageEvent:
    """一笔用量流水。"""

    event_id: str
    account_id: str
    meter: str
    quantity: Decimal
    occurred_at: datetime          # 用量实际发生时间（决定账期归属）
    received_at: datetime          # 服务端收到时间
    period_start: date             # 归集到的账期起点
    status: UsageStatus
    reject_reason: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quantity"] = str(self.quantity)
        d["occurred_at"] = self.occurred_at.isoformat()
        d["received_at"] = self.received_at.isoformat()
        d["period_start"] = self.period_start.isoformat()
        d["status"] = self.status.value
        return d


@dataclass
class SubscriptionSegment:
    """账期内的一段套餐生效区间 [effective_from, effective_to)。"""

    plan_id: str
    effective_from: datetime
    effective_to: datetime | None
    status: PlanStatus
    prorated_charge: Decimal = Decimal("0")  # 该段周期费折算（本设计为 0，留口）


@dataclass
class Subscription:
    """账户的订阅状态与历史。"""

    account_id: str
    segments: list[SubscriptionSegment] = field(default_factory=list)

    def active_segment_at(self, at: datetime) -> SubscriptionSegment | None:
        """返回 at 时刻生效（含起点，不含终点）且未退订的最新一段。"""
        for seg in reversed(self.segments):
            if seg.effective_from <= at and (
                seg.effective_to is None or at < seg.effective_to
            ):
                if seg.status is PlanStatus.ACTIVE:
                    return seg
                return None
        return None


@dataclass
class LineItem:
    """账单明细行：一笔用量对应一项费用，可逐项对账。"""

    event_id: str
    meter: str
    quantity: Decimal
    period_start: date
    occurred_at: datetime
    plan_id: str
    charge_mode: ChargeMode
    free_allowance: Decimal
    tier_breakdown: list[tuple[Decimal, Decimal, Decimal]]  # (档内数量, 单价, 金额)
    amount: Decimal

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "meter": self.meter,
            "quantity": str(self.quantity),
            "period_start": self.period_start.isoformat(),
            "occurred_at": self.occurred_at.isoformat(),
            "plan_id": self.plan_id,
            "charge_mode": self.charge_mode.value,
            "free_allowance": str(self.free_allowance),
            "tier_breakdown": [
                [str(q), str(p), str(a)] for q, p, a in self.tier_breakdown
            ],
            "amount": str(self.amount),
        }


@dataclass
class Bill:
    """一个账户一个账期的账单。"""

    account_id: str
    period_start: date
    period_end: date
    status: BillStatus
    line_items: list[LineItem] = field(default_factory=list)
    total_amount: Decimal = Decimal("0")
    created_at: datetime | None = None
    finalized_at: datetime | None = None
    version: int = 0  # 乐观并发版本

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "status": self.status.value,
            "total_amount": str(self.total_amount),
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            "line_items": [li.to_dict() for li in self.line_items],
        }
