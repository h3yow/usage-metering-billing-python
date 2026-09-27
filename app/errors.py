"""业务错误码与错误类型。

所有可预期的拒绝都返回明确的错误码，便于调用方按原因区分处理，
而不是静默丢弃。
"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """可区分的拒绝/失败原因。"""

    # 上报相关
    MISSING_FIELD = "MISSING_FIELD"              # 必填字段缺失
    INVALID_FIELD = "INVALID_FIELD"              # 字段值不合法（类型/格式）
    NEGATIVE_USAGE = "NEGATIVE_USAGE"            # 用量为负
    UNKNOWN_METER = "UNKNOWN_METER"              # 未知计量项
    USAGE_TOO_OLD = "USAGE_TOO_OLD"              # 用量时间过早，无法归集
    USAGE_IN_FUTURE = "USAGE_IN_FUTURE"          # 用量时间在未来
    DUPLICATE_EVENT = "DUPLICATE_EVENT"          # 幂等重复（同一 event_id）
    DUPLICATE_CONFLICT = "DUPLICATE_CONFLICT"    # event_id 重复但负载不一致
    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"      # 账户不存在

    # 套餐/生命周期相关
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    ALREADY_SUBSCRIBED = "ALREADY_SUBSCRIBED"
    NOT_SUBSCRIBED = "NOT_SUBSCRIBED"
    NO_ACTIVE_PLAN = "NO_ACTIVE_PLAN"            # 用量发生时该账户无有效套餐
    INVALID_CHANGE_TIME = "INVALID_CHANGE_TIME"  # 变更时间不在当前周期内
    BILL_ALREADY_FINALIZED = "BILL_ALREADY_FINALIZED"  # 历史账单已出账，禁止改写
    INVALID_REFUND = "INVALID_REFUND"            # 退款额度不合法/超出可退

    # 出账相关
    BILL_NOT_FOUND = "BILL_NOT_FOUND"
    CONFLICT = "CONFLICT"                        # 并发冲突


class BillingError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code: ErrorCode, message: str, *, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(f"[{code.value}] {message}")
