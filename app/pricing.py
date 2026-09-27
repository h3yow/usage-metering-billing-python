"""定价计算：免费额度 + 分段(tiered)/阶梯(graduated)计价。

规则（金额单位由调用方约定，内部全部 Decimal，金额保留两位小数）：
- 先扣除免费额度 free_allowance；
- tiered（分段单价）：每个计费单位只落一档，各档金额求和；
- graduated（阶梯累进）：用量从低档往高档跨越，低档用满后超出部分才进高档。
计算结果同时返回每一档的拆解 (档内数量, 单价, 金额)，供账单逐项对账。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import ChargeMode, MeterDef, PriceTier

TWO_PLACES = Decimal("0.01")


def money(x: Decimal) -> Decimal:
    return x.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def charge_quantity(quantity: Decimal, free_allowance: Decimal) -> Decimal:
    """扣除免费额度后的计费量（不小于 0）。"""
    remaining = quantity - free_allowance
    if remaining < 0:
        return Decimal("0")
    return remaining


def calc_tiered(chargeable: Decimal, tiers: tuple[PriceTier, ...]):
    """分段计价：每个单位按其落在的区间取价，各档分别汇总。

    返回 (breakdown, total)，breakdown 仅包含档内数量 > 0 的档。
    """
    breakdown: list[tuple[Decimal, Decimal, Decimal]] = []
    total = Decimal("0")
    for tier in tiers:
        lo = tier.from_qty
        hi = tier.to_qty
        if chargeable <= lo:
            break
        in_tier = chargeable - lo if hi is None else min(chargeable, hi) - lo
        if in_tier > 0:
            amount = money(in_tier * tier.unit_price)
            breakdown.append((in_tier, tier.unit_price, amount))
            total += amount
    return breakdown, money(total)


def calc_graduated(chargeable: Decimal, tiers: tuple[PriceTier, ...]):
    """阶梯计价：先填满低档，超出部分进入高档。"""
    breakdown: list[tuple[Decimal, Decimal, Decimal]] = []
    total = Decimal("0")
    remaining = chargeable
    for tier in tiers:
        if remaining <= 0:
            break
        lo = tier.from_qty
        hi = tier.to_qty
        width = None if hi is None else hi - lo
        in_tier = remaining if width is None else min(remaining, width)
        if in_tier > 0:
            amount = money(in_tier * tier.unit_price)
            breakdown.append((in_tier, tier.unit_price, amount))
            total += amount
        if width is not None:
            remaining -= in_tier
    return breakdown, money(total)


def price_meter(meter_def: MeterDef, quantity: Decimal):
    """对单个计量项的一笔用量计价。

    返回 (tier_breakdown, amount)：
    tier_breakdown 为 [(档内数量, 单价, 金额), ...]。
    免费额度内的用量返回 ([], 0.00)。
    """
    chargeable = charge_quantity(quantity, meter_def.free_allowance)
    if chargeable == 0:
        return [], Decimal("0.00")
    if meter_def.charge_mode is ChargeMode.TIERED:
        return calc_tiered(chargeable, meter_def.tiers)
    return calc_graduated(chargeable, meter_def.tiers)


def zero_amount() -> Decimal:
    return Decimal("0.00")
