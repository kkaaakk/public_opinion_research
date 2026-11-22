# 第四轮：调用记录与预算收敛

本轮把「一次真实调用记在哪里」固定为三张表，删掉重复的预算前置判断，并放宽了深度研究的检索额度。执行仍属 DBOS，准入策略仍在 `budget.py`，业务结果仍在 State。

## 1. 三张表分别负责什么

```text
requests (Run)
├── model_attempts   一次真实模型请求一条，模型次数与 Token 的唯一权威
├── tool_calls       一次经准入的工具调用一条，工具/搜索次数的唯一权威
│      └── operations  受保护外部副作用，(run_id, tool_call_id) 外键
└── por_dbos         DBOS 原生 journal，唯一执行权威
```

| 表 | 本轮字段 | 含义 |
| --- | --- | --- |
| `por_domain.model_attempts` | `(run_id, id)`、`stage_id`、可空 `usage` | 未改。`usage IS NULL` = 已准入未结算；`usage.usage_complete=false` = 供应商未报告用量。 |
| `por_domain.tool_calls` | `(run_id, id)`、`name`、`search`、`status` | 新增 `name`、`status`；`search_calls smallint` 折算为 `search boolean` 后删除。 |
| `por_domain.operations` | `(run_id, id)`、`tool_call_id`、`name`、`fingerprint`、`executor_id`、可空 `outcome` | 新增 `tool_call_id` 与 `operations_tool_call_fkey`。 |

没有新建 `model_calls` 表：`model_attempts` 已经表达 Run、调用身份、阶段、结算状态（`usage` 是否为空）、实际调用次数（行数）与用量完整性，改名只会增加迁移面。

## 2. 迁移

`initialize_domain()` 在同一事务（`pg_advisory_xact_lock`）内先 `CREATE TABLE IF NOT EXISTS`，再按 `information_schema` 检查执行增量 DDL：

1. `tool_calls.name text NOT NULL DEFAULT ''`
2. `tool_calls.search boolean NOT NULL DEFAULT false`，从 `search_calls > 0` 折算后 `DROP COLUMN search_calls`
3. `tool_calls.status text NOT NULL DEFAULT 'admitted'` 与 `tool_calls_status_check`
4. `operations.tool_call_id text`
5. `operations_tool_call_fkey FOREIGN KEY(run_id, tool_call_id) REFERENCES tool_calls(run_id, id)`

全部语句可重复执行（`tests/test_domain_schema_upgrade.py` 连续执行两次）。旧 `operations` 行保留空 `tool_call_id`；外键允许 `NULL`，所以历史回执不受影响，新行必须指向真实工具调用。不复制表、不重建 DBOS schema、不删除 History。

## 3. 模型调用链

```text
generate_text / generate_structured ─┐
DBOSRunner → MeteredModel.get_response ┴→ metered_model_response
   → begin_model_call()   stage_context.call_id("model")
   → start_model_attempt  require_live_effect + 行锁 + model_bill + INSERT(model_attempts)
   → 供应商请求
   → end_model_call       finish_model_attempt(usage)  失败也写已发生的用量
```

- 准入：PostgreSQL 在 `requests` 行锁内读完本 Run 全部 `model_attempts`，因此两个并行 Agent 共享一个额度，不会各自从相同 State 快照出发。
- 结算：`usage` 含 `model_calls` 与已知 Token；失败请求也写回执。供应商未报告用量时 `physical_model_usage` 标记 `usage_complete=false`，不伪装成零消耗。
- 未结算：带 Token 上限时等待在途账单结算（最多 `MODEL_BILL_WAIT_SECONDS`），等待期间不持有事务与连接。已结算但无用量的一次尝试不再永久阻断后续调用，只作为「1 次调用 + 用量不完整」计数。
- 重放：`por.llm` Step 命中 journal 即返回原 `(text, delta, invalid)`；同一 Step 内再次准入会因 `id` 已存在而 fail closed。`MeteredModel` 位于 DBOSRunner 模型 Step 内，同样由 journal 去重。

## 4. 工具调用链

```text
SDK Tool Call
→ ToolExecutor.perform: 权限校验 → _admit
→ admit_tool_call: require_live_effect + 行锁 + count/搜索计数 + INSERT(tool_calls)
→ 工具体（受保护工具在参数校验后 start_operation，写入 tool_call_id）
→ 结果或失败 → _settle_call → finish_tool_call(status)
→ DBOS por.agent_tool 提交
```

- 写入条件只剩 `stage_context`（正式 Run）。预算开关只决定 `can_spend_tool_call` 是否拒绝，不再决定是否记账；`budget_enabled=false` 或未设上限时同样记账。
- 额度在准入时消耗，`returned`/`failed`/`cancelled` 都不返还；同一 `id` 再次准入直接返回 `True`，不重复扣减。被拒绝的调用不写行。
- 搜索工具同时计 `tool_calls` 与 `search_calls`（`search=true`），其他工具只计 `tool_calls`。
- `status` 从 `admitted` 单向落到终态，重复结算写 0 行并返回已有行，因此重放幂等。状态写失败只告警：`por.agent_tool` 的提交结果仍是该调用的权威。
- 顺序不可调换：`start_operation` 只在 `_admit` 之后、参数校验之后执行，外键因此不可能指向未准入的调用。

## 5. 用量汇总

`RunClient._outcome` 现在按字段归属组合，`web/observations.py` 不再把阶段投影里的计数并入用量：

| 来源 | 提供 |
| --- | --- |
| `model_attempts` | `model_calls`、全部 Token 与明细 |
| `tool_calls` | `tool_calls`、`search_calls` |
| `operations` | 结算状态（`pending`/`unresolved`），不参与计数 |
| 阶段事件 / State | `budget_warnings`、`degradation_reasons`、阶段进度与报告 |
| `requests.value.initial` | 接纳时的业务基线（新 Run 为空） |

同一模型调用不会既算进阶段投影又算进账单；工具次数只有一处来源。`usage_complete` 仍由模型账单完整性决定，缺失时写入 History 与 SSE。

## 6. 预算配置

| 配置 | 旧 | 新 | 依据 |
| --- | --- | --- | --- |
| `config.yaml: max_search_calls` | 60 | 120 | 两个研究角色 × 10 轮 × 2 轮，每轮通常至少一次检索；60 会在拿到有效证据前触顶。 |
| `config.yaml: max_tool_calls` | 120 | 160 | 工具额度必须覆盖搜索额度并为非检索工具留余量，否则搜索额度不可用。 |
| `config.yaml: max_model_calls` | 120 | 120 | 轮次上限 40 + 规划/复核/分析/报告/压缩约 15，120 已有余量，不盲目扩大。 |
| `config.smoke.yaml: max_react_tool_calls` | 2 | 6 | 该值限制 SDK 轮次而非搜索次数；2 轮无法完成「检索 + 阅读 + 回答」。 |
| `config.smoke.yaml: max_tool_calls` | 16 | 20 | 覆盖 6 轮内的并行调用。 |
| `config.smoke.yaml: max_search_calls` | 8 | 10 | 同上，保持单轮低成本。 |

`max_model_calls`、Token 上限、`reserve_final_report_call` 与告警比例未改。

## 7. 预算不足时的行为

- 模型额度不足：研究角色停止继续推理，`_ReasoningStopped` 保留已收集证据；Risk/Strategy 写入「预算耗尽」降级说明；终稿仍需一次预留调用，否则 `generate_final_report` 明确失败。
- 工具额度不足：只拒绝新的工具调用并记录降级原因，不影响已经获准的模型推理。
- 模型额度耗尽也不拒绝已获准的工具调用（工具准入不看模型计数）。
- 阶段事件里不再出现统一的 `model_calls` 计数：Stage Usage 只剩业务提示，模型次数以账单为准。

## 8. 版本与切换

`VERSION` 从 `por-agent-context-v1` 升为 `por-call-ledger-v1`。本轮不改变 journal 命令序列，但改变了工具准入的写库条件与 Run 用量口径，因此旧版仍在执行的 Run 必须由匹配发行版排空；`por-agent-context-v1` 已加入 `NATIVE_RESULT_VERSIONS`，旧版已完成的 Run 仍可读取报告与用量。升级只运行 `--initialize-only`，不重建数据库。

## 9. 验证

离线定向测试覆盖：准入写入与拒绝、重复身份不扣减、状态结算与重放、Operation 归属、缺用量不阻断、工具/搜索额度独立、用量组合不重复、Run 用量口径。PostgreSQL 合同测试覆盖：schema 升级与外键、并发共享最后一份额度、重复身份不重复计费、预算关闭仍记账。结果见 PR 描述。

## 10. 留给后续轮次

- 第五轮：ToolExecutor/ToolManager 与工具恢复架构；`por.agent_tool` 在「状态已写、journal 未提交」窗口内仍会重新执行外部检索（额度不重复扣，但供应商调用会重复）。
- 第六轮：真实供应商与 MCP 的最终验收；本轮未运行 Executor 杀进程恢复、真实模型或真实 Tavily/MCP Smoke。
