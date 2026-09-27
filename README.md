# 按用量计费服务（本地模拟版）

一个基于 FastAPI 的按用量计费（usage-based billing）最小实现：用**进程内本地存储**
模拟用量流水与账本，重点保证重复上报、乱序到达、周期中途改套餐三类场景下
**账对得上、可复现、可拒绝**。

## 1. 目录结构

```
app/
  errors.py               错误码枚举与 BillingError
  models.py               领域模型（Plan / UsageEvent / Subscription / Bill / LineItem）
  pricing.py              免费额度 + 分段/阶梯计价（Decimal，逐档拆解）
  storage.py              本地存储（内存表 + RLock，账单带 version，可存/取 JSON 快照）
  usage_service.py        用量校验、幂等查重、账期归集（按 occurred_at）
  subscription_service.py 订阅/变更/退订 + 按剩余周期折算的退款额度
  billing_service.py      草稿重算、出账冻结、逐笔明细与总额对账
  schemas.py / api.py     HTTP 接口
  bootstrap.py            装配 + 示例套餐目录
scripts/demo.py           本地演示脚本
tests/                    14 个单测（重复/乱序/并发/改套餐/快照）
main.py                   uvicorn 入口
```

## 2. 计价规则

所有金额/数量一律 `Decimal`（入参接受整数或**字符串**，拒绝 float/NaN），
金额按 `ROUND_HALF_UP` 保留两位小数。

- **账期**：自然月，`period_start = 每月 1 日`；账期归属只看
  **`occurred_at`（用量发生时间）**，与服务端接收时间 `received_at` 无关。
- **免费额度**：`chargeable = max(quantity - free_allowance, 0)`。
- **分段计价 `tiered`**：每一个计费单位只落一档，按其所属区间单价计费，各档求和。
- **阶梯计价 `graduated`**：从低档开始填满，超出部分进入更高档。
- 每条账单明细行都带：`quantity / free_allowance / charge_mode /
  tier_breakdown[[档内数量, 单价, 金额], ...] / amount`，可逐项对回流水。
- **账单总额 = 所有明细行金额之和**（`_recompute_total` 是唯一总额来源）。

内置目录（`app/bootstrap.py:seed_catalog`）：

| 套餐 | 计量项 | 模式 | 免费额度 | 档位 |
|---|---|---|---|---|
| basic | api_calls | tiered | 1000 | 0–10000 @0.02；10000+ @0.01 |
| pro   | api_calls | graduated | 5000 | 0–100000 @0.01；100000+ @0.005 |
| basic/pro | storage_gb | tiered | 0 | 全部 @0.10 |

计算示例（basic / api_calls，20000 次）：计费量 = 20000−1000 = 19000；
10000×0.02 + 9000×0.01 = **290.00**。

## 3. 幂等与并发约定

### 3.1 幂等
- 每笔上报必须带**字符串 `event_id`**。`event_id` 是唯一幂等键：
  - 同 `event_id` + **负载完全一致** → 不二次计费，返回 `status=DUPLICATE`，
    存储中原始事件保持 `ACCEPTED`（只计一次费）。
  - 同 `event_id` + 负载不一致（账户/计量项/数量/发生时间任一不同）→
    HTTP 409 `DUPLICATE_CONFLICT`，且**不污染**原账。
- 查重与落库在 `LocalStore.lock`（RLock）内原子完成；并发用同一 event_id
  提交时恰有一笔 ACCEPTED，其余为 DUPLICATE（见
  `tests/test_concurrent_writes.py::test_concurrent_duplicate_submit_charged_once`）。

### 3.2 并发
- 跨表的“读—改—写”全部在同一把进程内锁内完成（上报、变更套餐、出账）。
- 账单带 `version`，写回支持乐观版本校验，冲突返回 `CONFLICT`。
- 出账时从流水**整体重算**（不做增量累加），从根上避免并发漏加/重复加。

### 3.3 乱序与迟到
- 归集只依赖 `occurred_at`，迟到、乱序事件与正常事件走同一规则，天然落回正确账期。
- **出账前**到达：计入该账期草稿，草稿可任意次重算，结果确定可复现。
- **出账后**才到达、且归属账期已 `FINALIZED`：返回 409
  `BILL_ALREADY_FINALIZED`，**不改写历史账单**（也不会静默丢弃，错误体里带
  event_id 与账期）；需要调整时走补退/下期流程。已入账事件的重放仍按幂等处理。

## 4. 套餐生命周期与折算

- 订阅段为半开区间 `[effective_from, effective_to)`，同一账户各段互不重叠。
- 周期内 `change-plan`：在 `effective_at` 结束旧段、开启新段。出账时每笔用量
  按其 `occurred_at` 时刻生效的套餐取价——**月中改套餐即按时间分段计价，
  无需额外折算用量**。
- `cancel` 退订：
  - 可退额度 = 套餐月费基数 × 剩余天数 / 账期总天数
    （剩余天数含退订当天；9 月共 30 天，9/20 退订为 11/30）。
  - 纯按量套餐月费基数为 0；可用 `set_refund_credit(plan_id, 月费基数)` 登记。
  - 申请退款为负或**超过可退额度** → `INVALID_REFUND`；不传则按可退额度全额退。
  - 退订后该时刻再上报 → `NO_ACTIVE_PLAN`。
- 已 `FINALIZED` 的历史账单不受之后任何变更影响（重复 finalize 幂等返回同一版本）。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/usage` | 上报一笔用量 |
| POST | `/accounts/{id}/subscribe` | 订阅 `{plan_id, effective_from?}` |
| POST | `/accounts/{id}/change-plan` | 周期内变更 `{new_plan_id, effective_at?}` |
| POST | `/accounts/{id}/cancel` | 退订 `{effective_at?, refund_amount?}` |
| POST | `/accounts/{id}/bills/{period}/finalize` | 出账（period=YYYY-MM-DD） |
| GET  | `/accounts/{id}/bills/{period}` | 查账单（含逐行明细） |

错误响应统一结构（冲突类 409，其余校验类 422）：

```json
{"error": {"code": "NEGATIVE_USAGE", "message": "quantity 不能为负: -3",
           "details": {"field": "quantity"}}}
```

错误码（`app/errors.py`）：`MISSING_FIELD`、`INVALID_FIELD`、`NEGATIVE_USAGE`、
`UNKNOWN_METER`、`USAGE_TOO_OLD`、`USAGE_IN_FUTURE`、`DUPLICATE_EVENT`、
`DUPLICATE_CONFLICT`、`ACCOUNT_NOT_FOUND`、`PLAN_NOT_FOUND`、
`ALREADY_SUBSCRIBED`、`NOT_SUBSCRIBED`、`NO_ACTIVE_PLAN`、
`INVALID_CHANGE_TIME`、`BILL_ALREADY_FINALIZED`、`INVALID_REFUND`、
`BILL_NOT_FOUND`、`CONFLICT`。

## 6. 本地验证

```bash
uv sync                                  # 安装依赖（含 pytest）
.venv/bin/python -m pytest               # 全部单测
.venv/bin/python -m pytest -s            # 单测日志打印“输入事件/判定依据”
.venv/bin/python scripts/demo.py         # 端到端演示，产物 data/demo-state.json
.venv/bin/python main.py                 # 启动 HTTP 服务（127.0.0.1:8000）
```

服务启动后试用：

```bash
curl -s localhost:8000/accounts/acct-1/subscribe -H 'content-type: application/json' \
  -d '{"plan_id":"basic","effective_from":"2026-09-01T00:00:00Z"}'
curl -s localhost:8000/usage -H 'content-type: application/json' -d '{
  "event_id":"c1","account_id":"acct-1","meter":"api_calls",
  "quantity":"20000","occurred_at":"2026-09-10T08:00:00Z"}'
curl -s -X POST localhost:8000/accounts/acct-1/bills/2026-09-01/finalize
```

## 7. 测试覆盖

| 文件 | 覆盖点 |
|---|---|
| `tests/test_usage_idempotency.py` | 重复不计费、冲突拒绝、各类缺字段/非法值原因可区分、无套餐上报 |
| `tests/test_out_of_order.py` | 迟到/乱序按 occurred_at 归集、明细排序、草稿可复现、冻结后迟到拒绝 |
| `tests/test_concurrent_writes.py` | 10×50 并发写入不丢数/不重复；同 event_id 并发仅一笔入账 |
| `tests/test_plan_change.py` | 月中改套餐按段计价、跨月、历史账单冻结、退订折算、超额退款与非法操作拒绝 |
| `tests/test_snapshot.py` | JSON 快照落盘/重载后账单流水一致（可复现对账） |

> 说明：本地存储是单进程内存实现（+ JSON 快照），锁与 version 用于模拟数据库事务
> 与乐观并发控制；多实例部署需替换为共享数据库。
