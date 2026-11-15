# DBOS 架构第四批收尾与验收

本批以 PR #47 合并后的 `main`（`32f3cba3`）为基线。生产执行协议沿用 `dbos-events-v1`。 [当前架构与可靠性](../architecture/agent-loop.md) · [数据库、部署与回滚](../architecture/dbos-deployment.md) · [面试讲解](../interview/index.md)

## 清理结果

| 范围 | 处理及依据 |
| --- | --- |
| 生产残留 | 清除描述旧 LangGraph 状态、调度和中断的过时注释。Executor 启动与初始化共用 DBOS schema 常量，历史阶段报告复用现行 `role_deliverables` 的文本提取。修正 History 测试名称，使其对应归档后可重开报告的断言。 |
| 历史读路径 | 保留旧 relay 的 `projection/cursor/done/history` 读取、`por_archive_native_recovery` 读回、匹配版本排空与迁移检查、旧 Role patch 形状、归档 owner 和 LangSmith 导入。`get_workflow` 及旧配置值的拒绝合同仍服务于旧调用方迁移。 |
| 业务数据 | 接纳、策略、重试授权、物理模型尝试和受保护效果继续由五张业务表保存。Graph、Memory、History 稳定键依旧用于远端写入。原生 journal 负责执行顺序和恢复。 |
| 历史评测 | 旧 Langfuse 基准和 SQL/Event 诊断使用已退役的 RunFacts 工作负载及冻结指标。测量入口现在在连接服务前检查所需版本。基线、原脚本和纯统计测试均有复现路径。 [评测说明](../../evaluation/observability/README.md) |
| 文档与 CI | 更新原执行、部署、Brief、Studio 与观测文档，迁移记录按历史版本引用。完整 `dbos-postgresql-contracts` 作业移到独立手动工作流，自动 CI 的质量、离线和前端入口沿用原触发规则。 [手动工作流](../../.github/workflows/postgresql-contracts.yml) |

## 四批效果

按 `src/open_deep_research/**/*.py` 的 Git 跟踪文件统计。行数包含空行和注释，新增/删除用每段合并提交的树差异计算，累计净值相对 PR #45 的直接 main 基线。

| 批次 | 范围 | 变更文件 | 新增 / 删除行 | 净行数 | 合计文件 / 行数 | 执行边界变化 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| 初始 | `7a3ef0cc` | — | — | — | 119 / 35,029 | 三批改造前 |
| 一 | `7a3ef0cc → 26bd6b57` | 19 | 1,151 / 1,214 | −63 | 116 / 34,966 | DBOS 原生业务边界和官方 Runner。移除 `business_steps.py`、`durability.py`、`durable_agent.py`。 |
| 二 | `26bd6b57 → ab68c3c9` | 19 | 1,948 / 2,047 | −99 | 116 / 34,867 | 移除 `run_facts.py` 和运行期 `units`、通用许可。新增 `domain_schema.py`，分离物理模型用量与受保护工具回执。 |
| 三 | `ab68c3c9 → 32f3cba3` | 7 | 674 / 562 | +112 | 116 / 34,979 | 单 Stream 单写者与一个 `delivery` Event。报告与阶段通过原生结果和 Event 读取，归档与重试仍由 Finalizer 处理。 |
| 四 | `32f3cba3 → 本批` | 9 | 22 / 37 | −15 | 116 / 34,964 | 小范围清理、历史读取去重、文档统一和按需 CI。 |
| 累计 | `7a3ef0cc → 本批` | 33 | 2,730 / 2,795 | −65 | −3 个文件 | 运行期执行职责集中到 DBOS，应用保留必要的业务回执与控制。 |

[第一批 PR #45](https://github.com/kkaaakk/public_opinion_research/pull/45) · [第二批 PR #46](https://github.com/kkaaakk/public_opinion_research/pull/46) · [第三批 PR #47](https://github.com/kkaaakk/public_opinion_research/pull/47)

## 验证记录

- 本批通过 107 项定向离线测试，7 项 integration 用例被筛出。范围为 `test_dbos_unit.py`、`test_phase5_entrypoints.py`、`test_web_history.py`、`test_observability_benchmark.py`、`test_sql_server_delta.py`。复用上一批锁定依赖的虚拟环境，通过导入位置确认使用本 worktree 源码。测试关闭 dotenv、外部 socket、追踪和缓存。
- Ruff `E,F,I` 检查通过。内存编译 118 个 Python 文件通过，含 116 个生产模块和两个评测脚本。生产入口 smoke 在阻断网络与旧引擎时成功导入 32 个模块。
- 工作流 YAML 解析通过。拆分后自动 CI 作业、原完整 PostgreSQL 作业内容和 Studio 路径规则分别与基线核对一致。GitHub 查询显示 `main` 在本批检查时没有启用必需状态检查，分支保护设置沿用现状。
- 本批未运行 PostgreSQL Contracts、全量测试、进程故障矩阵、浏览器 E2E、真实付费模型或性能压测。隔离测试中的历史通过记录适用其对应提交。 [第二批验证记录](dbos-domain-simplification.md)
- 可见历史 CI 中，`ab68c3c9` 的数据库作业通过，`26bd6b57` 的数据库作业曾失败，#47 的 PR head `dea85b8f` 的数据库作业被取消。`32f3cba3` 的普通 CI 与 Studio 浏览器检查已通过。 [#46 数据库作业](https://github.com/kkaaakk/public_opinion_research/actions/runs/43699136216/job/132766150671) · [#45 数据库作业](https://github.com/kkaaakk/public_opinion_research/actions/runs/43667445883/job/132669861393) · [#47 PR 数据库作业](https://github.com/kkaaakk/public_opinion_research/actions/runs/43721932202/job/132835616507) · [#47 合并后 CI](https://github.com/kkaaakk/public_opinion_research/actions/runs/43723058428) · [#47 合并后 Studio](https://github.com/kkaaakk/public_opinion_research/actions/runs/43723058409)

## 故障验收清单

在隔离 PostgreSQL/Neo4j 与受控模型、工具上执行。先保存 Run ID、Executor ID、应用版本和起始 SSE 游标，在目标边界加屏障后停止测试拥有的 Executor，再以匹配发行版和原 Executor ID 重启。用 `list_workflows`、`list_workflow_steps`、`get_event`、Stream 当前代次、外部夹具调用计数及业务账本核对。每次记录实际注入点和对应结果。历史证据只覆盖相应夹具和提交，下表所列故障注入本批均未执行。 [隔离配置](../architecture/dbos-deployment.md#verification)

| 场景及注入位置 | 预期与可观察证据 | 现有测试和验证状态 |
| --- | --- | --- |
| 模型 Step 前后终止 Executor。分别在 attempt 插入前及模型结果已记录后停进程。 | 前者可继续发起一次调用，后者直接读取响应。对比 `_model_call_step` 或 `por.llm`、`model_attempts` 的调用 ID 及 mock provider 次数。 | `test_dbosrunner.py::test_official_runner_process_reuses_model_and_tool[model-committed]`、`test_business_runner_process_restores_harness_and_fences_unknown_batch[runner-model]` 有历史已提交结果样本。首次 intent 前终止仍需同版本逐点核对。 |
| Agent 工具结束后终止。分别在 `por.agent_tool` 提交前和提交后停进程。 | 已提交工具结果可恢复引用。同一受保护调用只得到一条操作记录，结果丢失时进入待核对。比对工具 Step、`operations`、调用日志及 Graph 引用。 | `test_dbosrunner.py::test_business_runner_process_restores_harness_and_fences_unknown_batch[runner-tool]`、`test_dbos_process.py::test_process_kill_journal_recovery_never_replays_unknown_tool` 有历史隔离覆盖。 |
| 两个 Producer 中一个成功、另一个运行时终止。两种快慢顺序各试一次。 | 已完成 role Workflow 保持同 ID 和输出，另一侧按原检查点恢复。Review 在当前轮子结果齐备后接收固定顺序的 State。比较两个子状态、任务 ID、用量和 tool effects。 | `test_dbos_process.py::test_gap_replay_keeps_child_order_and_committed_rounds` 有历史 Gap 中断覆盖。`test_dbos_contracts.py::test_research_results_are_joined_after_both_native_children` 覆盖两种完成顺序，首轮两种杀进程组合待验证。 |
| Plan、Review、Report 之间恢复。分别在 Plan 完成后、Review 返回后、合成写入前后终止。 | 父 Workflow 复用已提交子结果，匹配原 Brief、Review/Gap 和已完成角色。报告重试只重进允许的后缀。检查原生子 ID、保存的模型 Step、`report` Event 和用量。 | `test_dbos_process.py::test_planning_recovers_before_waiting_for_clarification`、`test_gap_replay_keeps_child_order_and_committed_rounds` 及 `test_dbosrunner_report.py::test_incomplete_synthesis_retries_last_response_without_repeating_roles` 有历史分段覆盖。完整跨阶段进程矩阵待验证。 |
| 澄清或 Brief 审批等待中重启。包括消息已提交但尚未消费。 | 同一 Run 显示原挑战/Brief 版本及截止时间。只消费匹配消息，过期可取消。查 `clarification`/`plan`、`phase`、`approval_end`、native receive Step 与规划次数。 | `test_dbos_process.py::test_clarification_wait_survives_executor_kill_and_same_run_answer`、`test_approval_process_restart_uses_committed_plan` 有历史验证。 |
| AI 修改 Brief 期间重启。在队列、原生结果 ACK、未确认 provider 响应窗口注入。 | 原请求键派生的修改子 ID 保持稳定，审批 deadline 保持原值。最多发布一份新 Brief，未确认调用保留旧 Brief 及收据。查 `por.plan` 子结果、`plan` Event、`model_attempts`。 | `test_dbos_process.py::test_brief_revision_executor_recovery` 有历史排队、到期、结果前、ACK、未知及重试夹具。 |
| 外部成功但回执丢失。在远端 fixture 写成功之后、SQL 回执落库之前停止进程，另测提交后丢 ACK。 | 短暂 ACK 丢失允许同键补写。存在意图且缺原生结果时保留未知状态并人工核对。比对 fixture 的操作数、`operations.outcome` 与 native Step，确认没有第二次远端写入。 | `test_dbosrunner_receipts.py::test_finished_receipt_retries_control_only` 和 `test_dbos_process.py::test_process_kill_journal_recovery_never_replays_unknown_tool[lost-receipt]` 有历史隔离覆盖。真实供应商回执核对仍待验证。 |
| 模型真实用量缺失或尚未结算。在 provider 返回前停进程，另用省略 usage 的可解析响应。 | `model_attempts.usage` 仍为 `NULL` 或标有 `usage_complete=false`。保留已知调用数和 Token，预算可以拒绝新的付费尝试。查 Run usage、账本和下一次 provider 请求计数。 | `test_dbos_contracts.py::test_cancel_during_safe_retry_preserves_inflight_model_bill`、`test_dbosrunner_receipts.py::test_parallel_models_share_run_limit_and_final_report_reservation` 有历史结算预算覆盖。真实 provider 缺用量与发票核对待验证。 |
| PostgreSQL 暂不可用。暂停隔离容器并尝试创建或控制 Run，恢复后重发同键。 | 存储不可用时返回 `503`，无伪造终态。复查请求、根/relay/Finalizer 的稳定 ID 和同一游标的补发行为。 | `test_dbos_process.py::test_actual_postgresql_outage_fails_closed` 有历史容器停启验证。生产拓扑与跨主机接管另验。 |
| 用户取消与重试并发。分别在 grant 提交前后以及失败叶子已 rewind 时触发取消。 | 独立请求键最多授权同一失败代次一次。Finalizer 校验取消意图及原生目标，已发出的模型用量仍结算。查 `cancel_requested`、`por.cancel`、`retries`、`retry_applied` 和 native 状态。 | `test_dbos_retry.py::test_concurrent_distinct_keys_authorize_one_native_failure`、`test_old_key_cannot_bypass_cancel_or_unknown_effect`、`test_dbos_contracts.py::test_cancel_during_safe_retry_preserves_inflight_model_bill` 有历史单项验证。最终版两种提交顺序与进程中断的组合待验证。 |
| SSE 断线续传。收到 N 条后断开，在人机等待、普通事件或同毫秒尾帧时重连。 | 用 `Last-Event-ID=N` 从 N+1 补发，确认 seq 连续、无跨代次的终态，且 DBOS Step 和模型次数没有增加。比对 `delivery.offset` 和 `events_complete`。 | `test_dbos_contracts.py::test_sse_cursor_replay_is_read_only_and_redacted`、`test_sse_terminal_receipt_cannot_close_over_an_older_cursor` 有历史覆盖。`test_dbos_retry.py::test_terminal_delivery_drains_reordered_committed_messages` 覆盖新版 relay 的定向场景，等待本版本的按需验收。 |
| History 归档失败后恢复。在终稿后停隔离 Neo4j，恢复后用同一 Run 验证。 | 终稿仍在 `por.report` 的 Event。`archive_pending` 后可看到 `history_saved`、同一个稳定归档 ID 和正确代次，模型与工具调用数稳定。 | `test_dbos_process.py::test_actual_neo4j_outage_keeps_report_and_compensates_without_sdk_replay`、`test_dbos_contracts.py::test_archive_compensates_independently_without_rerunning_research` 有历史验证。 |

生产升级和回滚仍需使用目标数据的隔离副本演练。真实供应商账单、跨主机自动接管和长 Stream 保留成本各需针对部署环境验收。当前版本进入稳定维护，后续故障验收可从上述单项开始。 [部署与回滚](../architecture/dbos-deployment.md#事件与归档版本切换)
