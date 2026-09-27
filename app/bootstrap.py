"""应用装配：创建 store、注入 FastAPI、写入示例套餐目录。"""
from __future__ import annotations

from decimal import Decimal

from fastapi import FastAPI, Request

from .api import billing_error_response, router
from .errors import BillingError
from .models import ChargeMode, MeterDef, Plan, PriceTier
from .storage import LocalStore


def seed_catalog(store: LocalStore) -> None:
    """写入两套示例套餐与两个示例账户。

    计量项 api_calls（次）：
    - basic：分段 tiered，1000 次免费；0~10,000 单价 0.02，以上 0.01；
    - pro：  阶梯 graduated，5000 次免费；0~100,000 单价 0.01，以上 0.005。
    另含 storage_gb（GB）：tiered，无免费额度，单价 0.10/GB。
    """
    api_basic = MeterDef(
        meter="api_calls",
        unit="count",
        charge_mode=ChargeMode.TIERED,
        free_allowance=Decimal("1000"),
        tiers=(
            PriceTier(from_qty=Decimal("0"), to_qty=Decimal("10000"),
                      unit_price=Decimal("0.02")),
            PriceTier(from_qty=Decimal("10000"), to_qty=None,
                      unit_price=Decimal("0.01")),
        ),
    )
    api_pro = MeterDef(
        meter="api_calls",
        unit="count",
        charge_mode=ChargeMode.GRADUATED,
        free_allowance=Decimal("5000"),
        tiers=(
            PriceTier(from_qty=Decimal("0"), to_qty=Decimal("100000"),
                      unit_price=Decimal("0.01")),
            PriceTier(from_qty=Decimal("100000"), to_qty=None,
                      unit_price=Decimal("0.005")),
        ),
    )
    storage = MeterDef(
        meter="storage_gb",
        unit="GB",
        charge_mode=ChargeMode.TIERED,
        free_allowance=Decimal("0"),
        tiers=(
            PriceTier(from_qty=Decimal("0"), to_qty=None,
                      unit_price=Decimal("0.10")),
        ),
    )
    basic = Plan(plan_id="basic", name="基础版", period_days=30,
                 meters={"api_calls": api_basic, "storage_gb": storage})
    pro = Plan(plan_id="pro", name="专业版", period_days=30,
               meters={"api_calls": api_pro, "storage_gb": storage})
    store.upsert_plan(basic)
    store.upsert_plan(pro)
    store.create_account("acct-1")
    store.create_account("acct-2")


def create_app(store: LocalStore | None = None, *, seed: bool = True) -> FastAPI:
    app = FastAPI(title="usage-metering-billing")
    store = store or LocalStore()
    app.state.store = store
    if seed:
        seed_catalog(store)
    app.include_router(router)

    @app.exception_handler(BillingError)
    async def _on_billing_error(_request: Request, exc: BillingError):
        return billing_error_response(exc)

    return app
