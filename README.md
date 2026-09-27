# 用量计量与按用量计费（本地模拟）

基于 FastAPI 的本地计费服务：上报用量 -> 归集到正确账期 -> 按套餐计价 ->
月末逐项出账。用量流水、预收费用、退款和账单都以 JSON 文件落盘，可对账、
可复现。

## 快速开始

```bash
uv sync                       # 安装依赖（Python 3.14）
uv run pytest                 # 全部 38 个单测，日志打印输入事件与判定依据
uv run uvicorn main:app --reload   # 启动服务，默认 http://127.0.0.1:8000
```

数据默认写入 `./billing_data/`，可用环境变量 `BILLING_DATA_DIR` 覆盖。
交互式文档：`http://127.0.0.1:8000/docs`。

## 模块结构

| 文件 | 职责 |
| --- | --- |
| `billing/models.py` | 领域模型：套餐、阶梯价、套餐段、用量事件、账单及明细行 |
| `billing/pricing.py` | 纯函数：阶梯边际计价、按天折算（无副作用） |
| `billing/periods.py` | 自然月账期工具 |
| `billing/plans.py` | 内置套餐目录（free / pro / enterprise） |
| `billing/storage.py` | JSON 文件存储，进程内可重入锁 + 临时文件原子替换 |
| `billing/service.py` | 核心服务：校验、幂等、生命周期、预收/退款、出账 |
| `billing/api.py` | HTTP 路由、请求字段校验、错误码映射、响应序列化 |
| `tests/` | 38 个测试：重复上报、乱序、并发写入、周期中改套餐等 |

存储文件：`accounts.json` / `events.json` / `rejections.json` /
`subscriptions.json` / `charges.json` / `credits.json` / `bills.json`。

## 计价规则

### 账期

* 账期是**自然月**（`YYYY-MM`），由用量的发生日期 `occurred_at` 决定，
  与上报到达时间无关。
* 所有金额用 `Decimal` 表示，统一 `ROUND_HALF_UP` 保留到分；用量数量也是
  `Decimal`（支持小数用量）。每个阶梯段金额独立取整，因此**每行金额之和
  恒等于账单汇总金额**，可逐项对账。

### 用量计价

1. 每个套餐为每种指标配置**免费额度**和若干**分段（阶梯）价**；
2. 免费额度先消耗，超出部分按阶梯计价；
3. 阶梯是"对全账期共享"的资源，采用**边际计价**：出账时先按
   `(occurred_at, event_id)` 排序，再按"此前该套餐段内已累计用量"定位每笔
   事件落在阶梯的哪一段。这样：
   * 每一笔事件的金额都可独立追溯（明细行带 free/charged/阶梯拆分）；
   * 所有明细行之和恰好等于"把账期总量一次性计价"的结果，不会因为事件多
     而各自从阶梯起点重新计算导致多收。

### 套餐中途变更（按剩余周期折算）

订阅/变更时，基础费进入**预收台账**：

* 订阅生效：预收当前账期**剩余天数**的基础费
  `月费 × 剩余天数 / 账期天数`；
* 变更套餐（`d` 日生效）：
  * 旧套餐退回 `d` 日起到月末未使用天数的费用（credit）；
  * 新套餐预收其剩余天数的基础费；
* 退订：同样按未使用天数退回；
* 免费额度随套餐段在账期内的实际天数折算：
  `额度 × 段内天数 / 账期天数`（例：9/15 生效的 16 天段，额度 × 16/30）；
* 用量始终按**发生时所在套餐段**计价，与上报到达顺序无关。

净效果示例（9 月 30 天，pro 29.00/月，9/15 升级到 enterprise 199.00/月）：
pro 14 天 13.53 + enterprise 16 天 106.13 = 119.66；账单上体现为预收
29.00 + 106.13，退款 15.47。

### 退款额度与上限

* 自动退款（改套餐/退订）只退回**当前账期实际预收且尚未退还**的部分；
  跨账期变更时旧套餐在新账期没有预收，退款为 0（不报错）。
* 手工退款 `POST /billing/accounts/{id}/refund` 不得超过
  `本账期预收 - 已退`，超额返回 `400 REFUND_EXCEEDS_REFUNDABLE`。

### 出账与不可变性

* `POST .../bills/close` 对指定账期生成账单：基础费明细（来自预收台账）+
  用量明细（逐事件）+ 退款明细，汇总
  `总费用 = 基础费 + 用量费 - 退款`。
* 出账**幂等**：重复关账返回同一张账单；已关账的账期之后：
  * 迟到事件被拒绝（`PERIOD_ALREADY_BILLED`），不会改写历史账单；
  * 生效日落入该账期的变更/退订同样被拒绝。

## 幂等与并发约定

### 幂等

* 每笔用量带 `event_id`；同一账户下以 `(account_id, event_id)` 去重。
* 相同 `event_id` + **完全相同内容**重复上报：返回 `duplicate`，不重复
  计费、不报错（网络重试场景）。
* 相同 `event_id` 但内容不一致：返回 `422 IDEMPOTENCY_CONFLICT`，保留首次
  事件，防止标识复用。
* 所有被拒绝的上报连同原因码写入 `rejections.json`，
  `GET /billing/accounts/{id}/rejections` 可查，**绝不静默丢弃**。

### 拒绝原因码（可区分、可分支处理）

| code | 含义 |
| --- | --- |
| `MISSING_FIELD` | 必填字段缺失或为空 |
| `INVALID_VALUE` | 日期格式错、数量非数字/NaN/Inf/非正、生效日非法等 |
| `UNKNOWN_ACCOUNT` | 账户不存在 |
| `UNKNOWN_METRIC` | 当前套餐不计量该指标 |
| `FUTURE_EVENT` | `occurred_at` 晚于当前日期 |
| `NO_ACTIVE_PLAN` | 发生日没有生效中的套餐（含退订之后） |
| `IDEMPOTENCY_CONFLICT` | 同 event_id 内容冲突 |
| `PERIOD_ALREADY_BILLED` | 账期已出账（迟到事件 / 回溯变更） |
| `ACCOUNT_EXISTS` / `ALREADY_SUBSCRIBED` / `NOT_SUBSCRIBED` / `ALREADY_CANCELLED` | 生命周期状态冲突 |
| `PLAN_NOT_FOUND` | 套餐代码不存在 |
| `REFUND_EXCEEDS_REFUNDABLE` | 退款超过可退额度 |
| `BILL_NOT_FOUND` | 账单不存在 |

### 并发

* 服务实例的所有写操作（上报、变更、退款、出账）都在**同一把进程内
  可重入锁**内完成：校验 + 写入是一个临界区，并发上报同账户不会重复
  计费或漏数；
* 文件写入采用"同目录临时文件 + `os.replace`"原子替换，崩溃/并发都不会
  产生半截文件；
* 适用于单进程多线程/多协程（FastAPI 默认）部署。多进程/多机需把
  `Storage` 换成带行锁/事务的数据库实现。

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /billing/accounts` | 建户 `{account_id}` |
| `POST /billing/accounts/{id}/subscription` | 订阅 `{plan_code, effective?}` |
| `POST /billing/accounts/{id}/plan-change` | 账期内改套餐 |
| `POST /billing/accounts/{id}/cancel` | 退订 `{effective?}` |
| `POST /billing/accounts/{id}/refund` | 手工退款 `{amount, reason}` |
| `POST /billing/usage` | 上报用量（显式字段校验，原因码区分） |
| `POST /billing/accounts/{id}/bills/close?period=YYYY-MM-DD` | 出账（幂等） |
| `GET  /billing/accounts/{id}/bills/{YYYY-MM}` | 查询账单 |
| `GET  /billing/accounts/{id}/rejections` | 查询拒绝流水 |

错误响应统一为 `4xx` + `{"error": {"code": "...", "detail": "..."}}`。

### curl 示例

```bash
curl -s -X POST localhost:8000/billing/accounts -d '{"account_id":"acme"}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/billing/accounts/acme/subscription \
  -d '{"plan_code":"pro","effective":"2026-09-01"}' -H 'Content-Type: application/json'
curl -s -X POST localhost:8000/billing/usage -H 'Content-Type: application/json' -d '{
  "event_id":"evt-1","account_id":"acme","metric":"api_calls",
  "quantity":50000,"occurred_at":"2026-09-10"}'
# 重试同一笔 -> {"status":"duplicate"}，不会重复计费
curl -s -X POST localhost:8000/billing/accounts/acme/bills/close?period=2026-09-15 | python -m json.tool
```

## 本地验证

```bash
uv run pytest                       # 全量；-s 可看到每条输入事件/判定日志
uv run pytest tests/test_concurrency.py -v
uv run pytest tests/test_out_of_order.py -v
uv run pytest tests/test_plan_change.py -v
uv run pytest tests/test_idempotency.py -v
```

测试用例覆盖：

1. **重复上报**：同标识同内容幂等、同标识异内容 `IDEMPOTENCY_CONFLICT`，
   各类非法输入原因码互不相同；
2. **乱序/迟到事件**：跨月到达仍归入发生月账期、出账行按发生时间排序、
   免费额度按发生顺序消耗；已关账期的迟到事件被拒；
3. **并发写入**：32 线程并发提交唯一事件（含跨阶梯边界）全部计入、100 线程
   并发提交同一 event_id 只计费一次、用量上报与改套餐并发后账目仍可对账；
4. **周期中改套餐**：基础费按天折算、旧套餐未用天数退回、免费额度按段
   折算、用量按发生时套餐计价、退订/重订、退款超额拒绝、历史账单不可变。

单测日志会打印每个输入事件（id/账户/指标/数量/发生时间）以及判定依据
（accepted / duplicate / rejected:CODE / 账单各项汇总），失败时可直接从
日志复盘。
