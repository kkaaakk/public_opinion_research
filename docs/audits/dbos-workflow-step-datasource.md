# DBOS Workflow、Step 与 Datasource Transaction 重构

本次重构重新划分 Workflow、Step、Datasource Transaction、DBOS 原生通信与普通
Python 函数的职责。目标是让可靠性机制只出现在真正需要持久化的边界上：业务
顺序由 Workflow 表达，外部调用由 Step 表达，数据库提交由 Datasource
Transaction 表达，其余代码在重放时确定性重算。改造前应用版本为
`por-call-ledger-v1`，改造后为 `por-datasource-stage-v1`。

## 改造后的四层

| 层 | 归属 | 判据 |
| --- | --- | --- |
| Workflow | `por.run`、`por.plan`、`por.research`、`por.role`、`por.events`、`por.finalize`、`por.cancel` | 业务控制流、并行分派、人工交互等待、原生 Event/Stream |
| Step | `por.llm`、`por.agent_tool`、`por.role_tools`、`por.load_request`、`por.clock`、`por.completion_facts`、`por.retry_execution`、`por.wait_for_revision` | 一次外部调用或一次非确定性读取 |
| Datasource Transaction | `por.commit_stage`、`por.archive` | 一次业务数据库提交 |
| 普通 Python | 其余函数（`observe_stage`、`publish_stage`、`review_round`、`business_config`、`failure_target` 等） | 可确定性重算 |

Agent Loop 仍由 `DBOSRunner` 管理，没有被包装成 Step。模型调用账本、工具调用
账本、预算原子准入、外部 operations 副作用记录、RunClient 请求创建与原子入队、
取消与重试准入事务都保持在原有层级，没有迁入 Workflow 或 Datasource。

## 清单对比

`scripts/dbos_inventory.py` 现在同时统计 Workflow、装饰器 Step、动态 Step 与
Datasource Transaction，基线为本次改造起点。

### Workflow：8 → 7

| 保留 | 说明 |
| --- | --- |
| `por.run` | 根编排、澄清与审批交互 |
| `por.plan` | 图片上下文、`plan_research`、`revise_brief` |
| `por.research` | 角色分派、Review、风险、策略、终稿 |
| `por.role` | 单个研究角色 |
| `por.events` | 单写 Stream |
| `por.finalize` | 清算、归档补偿、重试授权 |
| `por.cancel` | 原生级联取消 |

删除 `por.review`：Review 是一轮有界模型调用加普通业务函数，现在是
`por.research` 内的 `review_round`。它只读取已提交的角色报告，失败时只重跑本轮
评审，不会重复已经完成的研究。

### Step：19 → 11（编排 8 + SDK Harness 3）

| 改造前 | 改造后 |
| --- | --- |
| `por.stage_started`、`por.stage_failed`、`por.publish_stage` | 合并为 Datasource Transaction `por.commit_stage` |
| `por.stage_result` | 删除；消息 ID 由 `python_state.message_identity` 确定性生成 |
| `event_timestamp` | 删除；事件时间戳在产生事件时写入消息 |
| `por.sdk_observation` | 删除；观察改为确定性身份 + 幂等写入 |
| `approval_deadline` + `interaction_clock` | 合并为 `por.clock` |
| `archive_step` | 改为 Datasource Transaction `por.archive` |
| `por.llm` / `por.llm_sync` 两个名字 | 统一为 `por.llm` |
| `por.load_request`、`por.completion_facts`、`por.retry_execution`、`por.wait_for_revision` | 保留 |

`por.agent_context`、`por.agent_settlement`、`por.sdk_transcript` 属于 SDK Context
Harness，本次改造范围外，保持不变。`por.wait_for_revision` 条件保留，原因见下文。

### Datasource Transaction：0 → 2

`por.commit_stage`（`running`/`completed`/`failed` 三种阶段转换）与 `por.archive`
（History 归档）。两者都在同一个 SQL 事务中写入业务行与 checkpoint。

### 代码量

`src` 下相对本次改造起点净增 52 行 SLOC（`workflow/transactions.py` +133，
`workflow/dbos_workflow.py` −96，其余为小幅增删）。相对 Phase 4 冻结基线仍为
−6300 SLOC。

## 关键实现

### 阶段事务

`por.commit_stage` 用 `SQLAlchemyDatasource`，与业务引擎共用同一 PostgreSQL 和
同一个 `Session`。事务内按状态写入 `step_started`/`step_failed`/`step_completed`、
`agent_report`、`research_review`、`report` 等业务事件，并用既有
`digest((execution_id, kind, role))` 作为消息幂等键。隔离级别为
`READ COMMITTED`：这些写入不做读取回验，串行化只会增加重试。

Workflow 以 `asyncio.to_thread` 调用同步事务，因为业务仓储层、`HistoryStore` 和
`publish_event` 都是同步 SQLAlchemy。真实 PostgreSQL 契约测试验证了 DBOS 上下文
在跨线程后仍然有效。

**DBOS 3.3.0 的实际约束**：`DBOS.register_datasource` 要求数据源在
`DBOS.launch()` 之前创建（它负责 rewind 时的 checkpoint 生命周期）。因此新增
`transactions.prepare()`，由 `web/executor.py:launch` 在 `DBOS.launch()` 前调用。
懒加载会在运行时直接抛 `DBOSException`。

**checkpoint 的生命周期**：DBOS 在 Workflow 进入任意终态（返回或抛错）时删除它的
全部 datasource checkpoint。因此已失败或已完成的 Run 再被 rewind 时，事务体会重新
执行；重复写入由业务幂等键消除，而不是由 checkpoint 消除。崩溃时未提交的事务不留
任何行，已提交的事务保留 checkpoint 供恢复重放。

### 归档事务

`por.archive` 在同一事务里写 `por_domain.history` 与 checkpoint，
`HistoryStore.save(records, db=...)` 因此接受外部事务。归档失败仍与业务终态解耦，
由 Finalizer 独立重试。

### 事件时间戳与消息 ID

事件产生时（`send_event`、`publish_event`）写入 `observed_at`，relay
（`por.events`）直接使用已有时间戳，不再单独调用 Step 取时间。消息 ID 由
`message_identity(source, position)` 生成：`source` 携带 Run、阶段、角色/轮次与
generation，`position` 是该消息在列表中的下标，重放时得到同一组 ID。

### 研究日期冻结

`RunClient.create` 把研究日期写入冻结快照 `data["research_date"]`，
`business_config` 把它注入每个阶段的配置，`get_today_str(config)` 因此返回同一个
日期。阶段不再各自读取时钟。

### 工具目录确定性

`por.role_tools` 的目录按工具名排序后再比较，恢复时不再因 MCP/插件发现顺序不同而
误判“工具定义已变更”。

### 失败恢复与重试

`failure_target` 按当前阶段的 journal 后缀决定 rewind 起点，顺序为：中断的
`por.llm`/`por.agent_tool` 重跑该调用；本地校验失败的模型回答重新请求；失败的
`por.commit_stage` 只重跑该事务；已发布的阶段只重跑其后的普通 Python。判断
“已发布”读取事务记录的状态（`por.commit_stage` 的返回值随 Step 一起入账），因此
统一的 Step 名仍然可区分阶段转换。

`retry_targets` 的叶子集合改为 `por.plan` 与 `por.research`（Review 不再是叶子）。
崩溃自动恢复与用户主动重试的区别保持不变：前者由 DBOS 从 journal 继续，后者必须
经过准入事务、失败版本校验与 `rewind_workflow`。

## 验证结果

### 离线契约

`pytest tests -m "not integration"`：982 passed, 2 skipped。

### 真实 PostgreSQL 契约（新增 `tests/test_dbos_datasource.py`）

- 阶段事务的四个业务事件与两次 checkpoint 在同一事务提交；
- 事务体抛错时不留下任何业务行或 checkpoint；
- 终态 Workflow 不保留 checkpoint；两次 rewind 重跑同一事务三次，业务事件仍只有
  一份（幂等键生效）；
- `list_workflow_steps` 中统一事务的状态可读，`failure_target` 据此把重试定位到
  事务之后的普通 Python。

### 集成与故障场景

`tests/test_dbos_contracts.py` 的 76 条集成用例中，57 条通过。19 条失败里 3 条在本次
改造之前的 `main` 上同样失败（`test_every_run_api_control_requires_auth[-post]`
返回 400 而非 401、`test_approval_expiry_gap_before_recv_is_still_enforced`、
`test_operator_approval_cannot_be_bypassed_or_replanned`），已用基线 worktree 复现，
不是本次引入。

其余失败多数是测试仍在使用本次删除的内部钩子，已改为新边界并定点复验：
`_publish_stage`/`_announce_stage` 改为阶段事务，`python_state.uuid4` 改为确定性
身份，`por.stage_result` 改为读取事务记录。复验通过的场景包括：阶段发布失败后用户
重试仍复用已提交的模型调用、分析阶段发布失败时按失败关闭且不重复写报告、两个角色
子 Workflow 的并行归并与发布顺序、取消在事务提交前夺走所有权时不产生 `step_failed`。

**未复验**：`test_native_vision_attempt_and_usage_are_settled_once` 仍超时停在
`por.plan`，尚未定位；`tests/test_dbos_process.py`、`tests/test_brief_dbos.py` 的
断言已按新边界改写但未重跑；完整集成套件与完整离线套件在最后一轮改动后没有重跑。

## 未验证与限制

- 真实 provider 网络调用（DeepSeek/Ark/Tavily/MCP）没有在本次验证中执行，推理由
  脚本化模型替代；这沿用了现有测试约定，不是新增缺口。
- `por.wait_for_revision` 保留：原生 `recv`/`wait_first` 的超时从等待被求值的那一刻
  开始计时，无法表达“包含排队时间的绝对截止时间”，仍需一次有界 Step 读取数据库
  时钟后与原生等待赛跑。
- History 归档的外部读取接口（`GET /api/history`）与前端没有在本次改造中手工回归。
