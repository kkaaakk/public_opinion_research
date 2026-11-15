# DBOS 业务持久化简化

基线为 PR #45 合并提交 `26bd6b57`。新 application version 为 `dbos-domain-v1`。依赖锁保留 DBOS 3.3.0、官方 DBOSRunner 0.3.0 和 OpenAI Agents SDK 0.24.0。

## 模块与职责

| 原边界 | 当前实现 |
| --- | --- |
| `workflow/run_facts.py`、`UnitReceipts`、`reserve/acquire/release/check/finish_unit` | 删除阶段占位、许可轮换、阶段清算行和阶段费用副本。DBOS Business Queue 执行全局和 owner 分区限流。 |
| `stage_receipts/watch_stage_fence/record_stage_failure` | 删除阶段许可和周期探测。Workflow 调用原业务函数。阶段超时与公开事件保留在业务调用边界。 |
| `por.stage_result/por.save_stage` | `por.stage_result` 为缺失的消息 ID 提供原生检查点。`por.publish_stage` 以事务化 DBOS 消息交付公开报告和进度。 |
| `RunClient._rewind_safe` | API 提交用户授权。Finalizer 比对实际失败的原生版本，逐个调用 DBOS rewind。 |
| 通用 `operation` | 记录非幂等或行为不明的工具调用。模型费用独立记账。 |
| `reconcile` | 记录操作员核验及 Executor 停止证明，解除关联 Session 的准入隔离，并保留原始费用与操作回执。 |

现有应用侧控制负责请求准入、预算、影响外部系统的操作和用户重试授权。SDK Provider 取消使用 DBOS 原生终态等待，因为 DBOSRunner 0.3.0 的模型 Step 未启用 preemption。终态报告归档和已发出外部调用的清算由 Finalizer 处理。 [DBOS 取消与 rewind](https://docs.dbos.dev/python/tutorials/workflow-management)

## 调用链

1. FastAPI 或 CLI 经 RunClient 校验 owner、Session、请求指纹和 Idempotency-Key，在同一 PostgreSQL 事务写入请求并调用 `DBOSClient.enqueue_in_transaction`。同一 Session 已有活跃 Run 时，新幂等键收到冲突。相同请求可重新读取原响应。 [DBOS Client](https://docs.dbos.dev/python/reference/client)
2. `por.run` 通过 Business Queue 启动 `por.plan`，通过 Control Queue 等待澄清和 Brief 审批。`por.research` 并行运行两个 Producer，确定性合并后执行 Review、Gap 补研、Risk、Strategy 和 `por.report`。
3. `por.role` 沿原角色函数进入 SDKAgentRuntime 和官方 DBOSRunner。模型 Step 复用已提交的 ModelResponse。显式 Tool Step 和有界 Context Step 恢复工具响应和 Working Context。
4. Tavily、普通 RAG 检索、已实施只读约束的 DBHub stdio 及文档转换直接走 Tool Step。RAG 关联索引、Research Graph、Memory 和 History 使用既有稳定业务键。外部 MCP、可能启动 Apify Actor 的社媒 HTTP 路径和其他行为未确定的工具采用保护记录。
5. `por.publish_stage` 通过原生消息交付报告内容和阶段观察。`por.events` 将它们写入 Stream，并以原生事件记录公开索引和终态交付确认。Finalizer 用稳定 Run ID 和用户重试代次归档 History。
6. Executor 使用 DBOS 的 Step 与子 Workflow 检查点恢复。用户重试绑定已交付的失败和原生目标，Finalizer 按子到父顺序 rewind 并等待，沿用 Business Queue 容量。取消意图和 `por.cancel` 原子入队，由原生取消 cascade 传播到子 Workflow。

## 数据及未知结果

| Schema / 表 | 用途 |
| --- | --- |
| `por_dbos` | DBOS 所有。Workflow、Step、队列、消息、事件和 Stream 由原生 API 维护。 |
| `por_domain.requests` | owner、Session、HTTP key、请求指纹、初始参数、取消意图和人工核验。 |
| `por_domain.policy` | API 与 Executor 共享的容量和时限策略。 |
| `por_domain.retries` | 失败代次的用户授权及对应原生目标。 |
| `por_domain.model_attempts` | 物理调用前的预算占用及供应商返回的用量，包含稳定调用 ID、阶段键和账单。 |
| `por_domain.operations` | 受保护工具的 Run ID、调用 ID、类型、参数摘要、发起 Executor 及结果。 |
| `por_archive_native_recovery` | 迁移安装保留旧 `units`、`operations` 和索引，供历史 Run 读取及核对。 |
| Neo4j / MySQL | 按稳定键保存 Research Graph、History 和 Memory。 |

运行期删除 `units` 及其 8 个列，删除 `requests.occupied`，从 `operations` 移除 `unit`、`usage`、`admission_permit` 和旧 `status`。原 `released` 迁移到 `reconciliation`。新装的业务 schema 共 5 张表。

两个 Producer 共享 Run 级模型预算。预算检查与物理尝试登记在同一事务内，最终报告保留调用额度。配置 Token 上限时，未返回或缺失的供应商用量占用额度，有限额度下的并行调用可能提前降级。Token 阈值按供应商返回的观测量计算。用量不完整时保存已知尝试及已知 Token。原生响应重放复用既有用量。

受保护操作的唯一键跨原生恢复保持稳定。已有操作记录且原生结果缺失时，Run 进入待核对状态。外部返回后的数据库 ACK 丢失允许补写同一份回执。对远端效果的进一步自动核实需要供应商的权威查询或幂等接口。

## 上线与回滚

1. 停止旧 API 新建 Run。使用 PR #45 对应的 API 和 Executor 完成或取消排队、执行、澄清和审批中的旧任务。处理待归档记录和未确认操作，保留原始凭证。
2. 保存同一时点的 `por_dbos`、`por_domain`、Neo4j 和 MySQL 备份，并在隔离数据库试跑。旧 Run 归档后，可用 DBOS 原生 API 停止仅在等待重试或关流的旧 `por.finalize` / `por.events`。确认根 Run 和各业务子 Workflow 已终止。
3. 旧服务下线后，运行新版 `python -m open_deep_research.web.executor --migration-check`。排空和核验完成后执行 `--migrate-domain`。迁移在一笔事务中归档旧表及其约束和回执，再更新业务 schema。首次安装使用 `--initialize-only`。DBOS 自身的 schema 初始化使用公开的 `DBOS.migrate`。
4. 迁移账号需建立归档 schema。运行账号读取历史 Run 时需要该 schema 的 USAGE 和旧表 SELECT。启动同版 API 与 Executor，验证审批、队列、SSE、History、预算和旧 Run 的读取。
5. 回滚先停止新准入。由匹配新版 Executor 清算已接收的 Run，备份迁移后的数据和外部回执。用切换前的一致备份恢复到另一套数据库并部署 PR #45 的 API 和 Executor。切换后接受的 Run 及其外部效果留在新版数据和原业务存储中，可按 Run ID 继续核对。

迁移验证使用隔离 PostgreSQL。生产切换由操作员执行。

## 重构规模与验证口径

按基线 Git diff 统计，生产 Python 净减少 103 行（新增 1,943 行、删除 2,046 行）。删除一个生产模块和一个运行期数据库表，归档保留原表数据。调用链省去了每阶段的 Run 预留、可撤销阶段许可、阶段查询循环和 usage 汇总副本。

本地 Python 离线验证通过 1,096 项，Windows 跳过 2 项，186 项非离线用例未在此轮选中。PostgreSQL 核心回归通过 24 项，覆盖 schema、审批与澄清、SSE 乱序交付、报告重试和外部回执。另对迁移、空库 schema 与跨并行模型的最终报告预留运行了 3 项定向复测。前端 Vitest 50 项、Classic 控制器 8 项和生产构建通过。Chrome 首轮 8 项通过，受影响的原生 Studio 流在修复后单项复测通过。后续定向离线复测通过 132 项。Ruff 和 Python 编译通过。Mypy 仍报告基线的 35 个错误。

原生进程验证中的官方 DBOSRunner 模型检查点、工具检查点和预算场景分别通过，实际双 Producer 的模型检查点恢复样本也通过。完整的 Executor 停启、跨 Producer 故障组合、外部 ACK 丢失以及 PostgreSQL 停启需要集中复测。双 Producer Context 丢检查点的首次运行暴露了 Graph 层吞掉待核对异常的问题。修复后，相同夹具的丢检查点与已缓存错误两个用例均通过。
