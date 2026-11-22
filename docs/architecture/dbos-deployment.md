# DBOS 部署、数据库和恢复

当前应用版本为 `por-agent-context-v1`，由 `workflow/dbos_config.py:VERSION` 固定，并同时用于入队与 Executor 配置。SDK Runtime 版本为 `sdk-vertical-v1`。新环境使用 `--initialize-only`，旧环境先盘点并排空。 [执行架构](agent-loop.md) · [可靠性验收](../audits/dbos-architecture-closeout.md#故障验收清单)

## Local setup

下面使用隔离开发库。PostgreSQL 17.6 的 Compose 端口只绑定 loopback，示例密码供本机隔离环境使用。部署时在 API、Executor 和运维命令的各自进程环境提供同一逻辑数据库的 `DBOS_DATABASE_URL`（`postgresql+psycopg://…`）及相同业务配置。需要入队事务的两个 schema 必须位于同一 PostgreSQL 数据库。

```powershell
docker compose -p por-dbos-phase4 -f compose.dbos.yaml up -d postgres --wait
uv sync --extra dev --locked --python 3.11
$env:DBOS_DATABASE_URL = 'postgresql+psycopg://por_execution:local-dbos-development-only@127.0.0.1:15434/por_execution'
.venv/Scripts/python.exe -m open_deep_research.web.executor --initialize-only
```

初始化账号需有 schema 安装权限。命令先使用 DBOS 公共 `DBOS.migrate(..., schema="por_dbos")`，再安装 `por_domain` 表和共享策略。常规数据库角色需访问两个 schema 及其表、序列和原生队列所需对象。迁移前在隔离副本验证角色授权。Compose 项目名和 `por-integration=dbos-phase4` 标签也供隔离停启夹具定位。

在独立终端运行 Executor；CLI 是主要准入入口，Web 是可选入口。每个进程提供同一逻辑数据库的 `DBOS_DATABASE_URL`。业务设置只来自仓库根目录的 `config.yaml`（`por smoke` 使用 `config.smoke.yaml`）；`.env` 只提供凭据和部署连接。Executor 为每个 Run 读取其冻结快照，不重新读取修改后的配置文件。Langfuse/OTel 默认由 `config.yaml` 的 `tracing_enabled` 决定；它是进程级初始化，`POR_TRACING_ENABLED` 不再覆盖业务配置。正式 Run 与 smoke Run 共用一个 Executor 时共享该进程级追踪状态，逐 Run 快照不会重新初始化追踪。 [业务配置](../../README.md) · [可观测性配置](observability.md)

```powershell
# Executor
.venv/Scripts/python.exe -m open_deep_research.web.executor --executor-id workstation-1
# CLI（主要入口）
por doctor
por run "调研某品牌近期舆情"
# 可选 Web / API
.venv/Scripts/python.exe -m open_deep_research.web.server
# PostgreSQL 连接和原生目录读检查
.venv/Scripts/python.exe -m open_deep_research.web.executor --health
```

`/api/health` 可检查 Web 入口，Executor `--health` 只检查数据库及原生目录可读。进程存活由运行环境监督。
`por doctor` 复用同一套启动前检查，覆盖配置、模型凭据、检索、RAG、插件/MCP、预算/超时和
PostgreSQL/DBOS。Web 的 SDK 准入也不再接受模型或 RAG 覆盖字段，业务设置同样来自 `config.yaml`。
旧输入别名 `workflow_backend=python/dbos` 和 `business_agent_runtime=openai_agents` 校验后丢弃，
退役值（包括环境配置）会被拒绝。启用令牌后 History 遵循现有 owner 过滤；无归属的旧归档保留原策略。

## Executor、队列和并发

| 队列 | 职责 |
| --- | --- |
| `por-control` | 根 `por.run`、relay `por.events`、`por.cancel`。持久澄清和审批在根等待，期间不占 `por-business` 槽位。当前 Run 仍有 Session 互斥。 |
| `por-business` | 规划、AI Brief 修改、研究段及安全 rewind 的业务目标。DBOS 使用 `global_concurrency` 和按 owner 的 `partition_concurrency`；同一个研究段内可并行执行它的角色子 Workflow。 |
| `por-archive` | `por.finalize` 独立等待根终态，清算、发布终态、补偿归档，接收重试授权。 |
| `por-recovery` | 原生运维恢复保留队列，`worker_concurrency` 与 `RUN_MAX_ACTIVE` 一致。常规用户重试继续使用其原生 Control / Business 目标队列。 |

| 配置 | 默认值与作用 |
| --- | --- |
| `RUN_MAX_ACTIVE` / `RUN_OWNER_ACTIVE` | `4` / `2`，Business Queue 跨 Executor 的全局及 owner 分区并发 |
| `RUN_QUEUE_CAPACITY` | `1000`，准入时按活跃 `por.run` 计数限制积压 |
| `RUN_APPROVAL_SECONDS` | `86400`，每次澄清及 Brief 审批的期限；Brief 编辑和 AI 修改沿用同一审批期限 |
| `RUN_TIMEOUT_SECONDS` | `3600`，规划或研究子 Workflow 自出队起的时限；有审批截止的修改同时受截止约束 |
| `DBOS_EXECUTOR_ID` 或 `--executor-id` | 每个同时存活的实例使用独立 ID |
| `RUN_SDK_SESSION_PATH` | 可选的私有 SQLite SDK transcript。启用前准备父目录和 Executor 文件权限；恢复读取 DBOS journal |

共享策略持久化到 `por_domain.policy`，API 与 Executor 核对同一份值。业务段时限、Stage 自身时限和人工等待有独立边界。API 通过 DBOSClient 写入队列并读取状态，队列消费和 Workflow 恢复由 DBOS 完成。

## 数据库设计

以下六张表由 `workflow/domain_schema.py:initialize_domain` 建立。`value`、`usage`、`outcome` 中承载 JSON 的字段使用应用的 `RunJSON` 序列化约定，具体 SQL 类型为 `text`。

| PostgreSQL 业务表 | 关键字段 | 职责 |
| --- | --- | --- |
| `por_domain.requests` | `id` 主键，`owner`、`request_key` 唯一组合，`session` 索引，`value`、`cancel_requested`、`reconciliation` | 接纳身份与审计。`value` 保存请求、指纹、冻结配置与初始 State、版本、创建时间与时限。取消是请求意图，核对记录用于 Session 准入。 |
| `por_domain.policy` | `id=1`、`value` | 共享并发、容量和期限设置，启动及首次取得 RunClient 时校验。 |
| `por_domain.retries` | `(run_id, key)` 主键，`version`、`generation`、`outcome`，`(run_id, generation)` 唯一 | `version` 绑定用户看到的状态游标，`outcome` 保存含源代次、应用版本和具体原生目标的授权数据。应用结果由 DBOS `retry_applied` Event 记录。 |
| `por_domain.model_attempts` | `(run_id, id)` 主键，`stage_id`、可空 `usage` | 执行前登记物理尝试。`NULL` 表示尝试尚无结算回执，已返回用量含实际调用与已知 Token。 |
| `por_domain.tool_calls` | `(run_id, id)` 主键，`search_calls` | Run 级工具与搜索准入。`id` 由 Role Workflow 与 Tool Call ID 派生；相同身份表示 DBOS 重放，不再扣减。 |
| `por_domain.operations` | `(run_id, id)` 主键，`name`、`fingerprint`、`executor_id`、可空 `outcome` | 保护需核对的外部工具。准入时 outcome 为 `NULL`，后续写 `returned`、`unknown` 或 `not_executed`。 |

| 其他存储 | 所有权与可读内容 |
| --- | --- |
| PostgreSQL `por_dbos` | DBOS 维护原生 Workflow 状态、队列、输入输出、Step 结果、消息、Events 与 Streams。`workflow_status`、`operation_outputs` 等表由库管理。应用执行写入使用原生 API，运维按 API 查询和恢复。 |
| PostgreSQL `por_archive_native_recovery` | 仅迁移安装存在。原 `units`、`operations` 连同数据、索引和约束归档到此；`web/run_data.py:legacy_projection` 读取旧产物、用量与未确认效果。 |
| PostgreSQL History | `web/history.py:HistoryStore` 写 `por_domain.history`。稳定 ID 由 Run ID 派生，`run_state_version = generation + 1` 阻止旧重试代次覆盖新归档。存报告、阶段产物、用量、状态和 owner。 |
| 长期 Memory / RAG | 配置 MySQL 时，原始记忆在 `rag_chat_memories`（可配置表名），`memory_id` 唯一，包含用户、会话、记录类型、内容与索引状态。RAG 维护知识及可索引记忆的向量副本和独立知识图。 [Memory](memory.md) · [RAG](rag.md) |

现有安装升级到含 `por_domain.tool_calls` 的版本前，先运行 `--initialize-only`；该命令使用 `CREATE TABLE IF NOT EXISTS` 只补齐缺失表。

## 事件与归档版本切换

1. 停止旧版新建 Run，保留原版的控制入口与匹配的 Executor。处理排队、执行、澄清、审批、AI 修改、已授权重试、未知效果及待归档的旧 Run。先确认业务和 History 已结算，再停止仅剩等待旧消息的 `por.events` / `por.finalize`。
2. 保存同一切换点的 `por_dbos`、`por_domain` 与业务数据备份，包括需要保留的原生 Stream。运行新版 `python -m open_deep_research.web.executor --migration-check`。其输出为 JSON；上线前确认盘点为空，包含超出恢复尝试次数的旧 journal 也需核查。
3. `dbos-domain-v1` 已有五张业务表，从该版切到 `dbos-events-v1` 沿用业务 schema。若仍有旧 `por_domain.units`，在排空、备份并演练后显式运行 `--migrate-domain`。事务将旧 `units` 和 `operations` 移到 `por_archive_native_recovery`，将 `requests.released` 更名为 `reconciliation` 并移除 `occupied`，再创建当前表。需要历史读取的运行角色保留归档 schema 的 `USAGE` 和表 `SELECT`。
4. 同步切换 API、Studio/Classic 与 Executor，核对版本、请求创建、澄清/审批、SSE/状态与 History。对已终止旧 Run，`RunClient` 读取保留的旧投影/事件/Stream、旧账单及操作记录。旧 journal 和旧回执继续保留。

`por-agent-context-v1` 精简 `por.agent_context` 和 `por.agent_settlement` 的检查点。Agent 模型预算拒绝发生在 DBOSRunner 的模型 Step。切换前使用匹配的 API 和 Executor 排空 `por-inline-report-v1` 的 journal、人工交互与归档。已有 PostgreSQL 业务 schema、History 和调用回执继续使用。

`por-inline-report-v1` 将终稿的模型 Step 和事件移入 `por.research`，改变了原生 journal 命令序列。切换前，使用 `por-research-agents-v1` 对应的 API 和 Executor 排空其 journal、人工交互及归档。`por-sdk-models-v1` 和 `por-vertical-v1` 也须由匹配的发行版排空。
旧版已成功 Run 的终稿可从根 Workflow 结果读取，已归档报告仍可从 History 读取。旧 Run 的原生 rewind 由其匹配版本执行。
更新自定义 YAML 时移除旧的章节模式与章节模型字段。本轮沿用现有业务 schema 和 `por_domain.model_attempts`。

历史 Phase 4 DBOS、`dbos-phase5-v1`、`dbos-phase5-studio-v1`、`dbos-brief-spec-v1`、`dbosrunner-unified-v1`、后续规划/审批版本（含 `dbosrunner-brief-idempotency-v1` 预览）、`dbosrunner-native-interaction-v1` 与 `dbos-native-recovery-v1` 均由各自的发行版处理原生执行。旧 SQL 队列也由原 SQL 发行版排空，保留原 History。数据库中可能存在的旧 `approvals` 与 `brief_revision_receipts` 供历史或回滚保留。 [版本与恢复](https://docs.dbos.dev/python/tutorials/upgrading-workflows) · [原生交互变更](research-brief.md) · [旧阶段清算记录](../audits/run-execution-reliability.md) · [业务 schema 迁移审计](../audits/dbos-domain-simplification.md)

### 回滚

停止新版准入，保留匹配的新版 Executor 直到已接纳工作、已入队命令和归档清算完毕。保留该版本所有 journal、外部凭证和归档。完成 schema 变更的安装用迁移前一致备份在隔离数据库恢复旧版 schema，再让旧版读取其数据。切换后新增 Run 及其效果从新版存储对账。版本回滚需演练并核对这一时间窗内的两版数据去向。

## 故障恢复和用户重试

- 同机恢复使用相同的 `application_version` 和已停止 Executor 的原 ID。多机部署需要 Conductor 或外部编排器协调故障检测与接管；本仓库的 Executor 启动命令只配置 ID、队列和匹配版本。关闭进程与提交业务取消属于两个入口。 [DBOS Executor 恢复](https://docs.dbos.dev/production/workflow-recovery)
- PostgreSQL 短时故障会让准入和控制失败，API 存储错误返回 `503`。恢复后用原请求和键确认是否提交。SSE EOF 后从最后完整游标重连并读取当前状态。连续归档失败时，已有报告仍可从 Run 读取；Finalizer 独立重试 History。
- 用户重试只适用于当前终态交付完毕且标记安全的失败。根据 GET 返回的 `state_version` 提交新键；相同键只确认同一代次授权。Finalizer 校验原生失败身份和回执，再按叶子到根 rewind。相关结果与代次以 `retry_applied` 为准。 [执行和外部调用边界](agent-loop.md#可靠性设计)
- 未确认外部效果先核对供应商或存储系统回执、确认旧 Executor 已停止，并记录证据。操作命令 `--reconcile-run RUN_ID --state-version VERSION --worker-stopped --note '…'` 只记录人工核验并解除 Session 准入隔离。

## Verification

通用 PR 继续运行 `CI` 的 Python 离线测试、Ruff、编译、入口导入和 Classic 控制器合同，Studio 仍按自己的路径规则运行。完整数据库合同由 GitHub Actions 的 `PostgreSQL Contracts` 工作流手动触发（工作流文件需先进入默认分支）。发布前选择要验证的分支或 ref。 [手动触发工作流](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)

历史和后续故障验证仅使用隔离测试服务。`TEST_DBOS_DATABASE_URL`、`TEST_DBOS_NEO4J_URI`、`TEST_DBOS_NEO4J_PASSWORD` 指定测试端点，停启夹具使用 `TEST_DBOS_POSTGRES_CONTAINER` 和 `TEST_DBOS_NEO4J_CONTAINER` 定位带标签的测试容器。测试使用临时 `por_test_<uuid>` 数据库和受控推理。精确测试入口以 [按需工作流](../../.github/workflows/postgresql-contracts.yml) 为准。 [已验证范围与后续清单](../audits/dbos-architecture-closeout.md)
