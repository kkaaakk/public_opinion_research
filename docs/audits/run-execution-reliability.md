# 请求与执行可靠性审查

基线为 GitHub `main` 的 `11b23847b391ab62a30343d9b9e756cae331d62a`。依赖锁定 DBOS 3.3.0、DBOSRunner 0.3.0 和 OpenAI Agents SDK 0.24.0。

## 调用链

- `web/runs.py` 校验身份、参数和 Session ID 格式，`RunClient.create` 在一笔 PostgreSQL 事务中检查所有者和容量、比较请求与 Session 的 fingerprint、写请求记录、以 Run ID 派生的稳定 ID 入队 `por.run`、`por.events`、`por.finalize`，并发送排队事件。数据库的 `UNIQUE(owner,request_key)` 和事务锁约束并发提交。
- `por.run` 通过 DBOS 等待澄清和 Brief 审批，分段排队规划和研究。研究 Workflow 为两个并行 Producer 分配稳定的角色及轮次 ID，通过 `wait_first_async` 和子 Workflow 结果汇合，再执行 Review、后续角色和报告。DBOS journal 决定返回已提交的结果或执行未提交的 Step。
- `business_step` 在 Workflow 上下文重建阶段。DBOSRunner 的 `_model_call_step` 保存 SDK 模型响应，`sdk_context.py` 以 `por.agent_tool` 保存工具结果并恢复 Harness 局部状态，`durability.py` 将普通模型与有限 Context I/O 接入原生 Step。实际调用前后由 MeteredModel、工具适配器和普通模型适配器写 `operations`。该账本记录准入和实际费用，旧许可或未知回执继续阻止外部重放。
- `por.stage_result` 保存完整阶段结果、费用和交付 ID。此前费用在 Step 外汇总，`finish_unit` 写投影时再次查账。现在结果 Step 保存一次账单，成功重放直接使用其中的费用。未持久化的账单汇总可重算，已保存的模型和工具结果继续由原 Step 返回。
- `RunClient.get` 将原生状态与业务清算、Brief、成果和 SSE 游标合成 API。Finalizer 用阶段投影组装报告归档和 SSE 终态 manifest。Execution Studio 消费原生 Stream 的阶段事件、报告和状态，Classic 也读取 API 的 `completed_steps` 和 `current_step`。

[GitHub 基线](https://github.com/kkaaakk/public_opinion_research/commit/11b23847b391ab62a30343d9b9e756cae331d62a) · [DBOS Steps](https://docs.dbos.dev/python/tutorials/step-tutorial) · [DBOSRunner 集成](https://docs.dbos.dev/integrations/openai-agents)

## RunFacts 方法

| 方法 | 调用方与职责 | 本次处理 |
| --- | --- | --- |
| `__init__`、`facts`、`close` | API、Executor、测试夹具共享配置、连接和 DBOSClient 生命周期 | 保留 |
| `initialize`、`policy`、`verify_policy` | 初始化命令、Executor、RunClient 校验共享的容量、审批及期限策略 | 保留现有 DDL 和策略合同 |
| `migration_inventory`、`require_matching_journals` | Executor 启动、迁移检查、新 Run 准入盘点旧 journal 与未清算占位 | 保留版本切换门禁 |
| `now`、`lock` | 请求及审批、预留及释放、人工对账、SSE 使用数据库时钟和事务锁 | 保留业务序列化 |
| `request`、`publish`、`validate` | RunClient 和 Workflow 校验归属与冻结配置，在业务事务内发原生消息 | 保留 |
| `reserve` | `reserve_step`、`business_step` 获取全局、owner 和 Session 业务容量 | 复用事务已读取的请求，取消及版本校验仍在入口执行 |
| `unresolved`、`release` | Run API、阶段退出、Finalizer、重试检查未清算与未决效果并释放占位 | 保留。规划退出仍用 owned release，覆盖释放后、Workflow 回执前的进程中断 |
| `pending_settlement`、`quarantine_pending` | 并行子 Workflow 退出和 Finalizer 等待实际清算，逾期撤销许可并隔离。重试等待已启动的清算 | 用 SQL `EXISTS` 查询清算，无需加载报告、费用和错误 JSON |
| `expire_approval` | Finalizer 比较当前 Brief 版本、修改代次与期限，事务内写审批回执 | 保留 |
| `acquire` | `business_step` 刷新许可与 pending 清算，取回历史费用 | 新行直接用 INSERT 的返回。原有行一次写入清算和错误字段。保留非原生调用的保守拒绝分支 |
| `check`、`effect_guard` | 取消监听、SDK 观察、外部操作、Graph/Memory 提交和 `finish_unit` 校验许可；写入期间持行锁 | 保留原边界和锁 |
| `operation` | 普通模型、DBOSRunner 代理模型、工具和 Context 提交准入、fingerprint、结束回执 | 保留事务、未知结果拒绝和精确回执确认 |
| `failed_usage`、`physical_usage`、`accounted_usage` | 物理调用前核查预算，阶段恢复携带未入账差额，正常及失败阶段结算 | 空历史用量跳过补差查询。成功账单归入原有结果 Step，失败仍按实际回执清算 |
| `finish_unit`、`units` | 阶段退出写清算和公开投影，RunClient、Finalizer 及诊断读取 | 去掉第二次账单查询和投影中的 `completed` 键。合并投影比较与阶段身份读取，成果与 SSE 消息仍同事务提交 |

## Schema 和保留理由

| 表与字段 | 责任与依赖 |
| --- | --- |
| `policy.id,value` | 持久容量和期限策略的唯一记录，API 和 Executor 在各自进程校验 |
| `requests.id,owner,request_key,session,value` | API 幂等、授权及 Session 归属。 `value` 保存请求 fingerprint、冻结配置与初始状态、运行版本、创建时间和期限 |
| `requests.occupied,cancel_requested,released` | 全局、owner、Session 业务占位，原生取消 RPC 丢失后可继续交付的取消意图，人工核验释放记录 |
| `units.id,run_id,name` | 阶段、公开观察、报告来源及 `operations.unit` 外键定位 |
| `units.permit` | 旧 Executor、取消清算及手动撤销的业务围栏。DBOS journal 的提交权限与 Graph/Memory 写入和回执事务之间有跨系统边界 |
| `units.settled` | 实际清算 pending、已清算、需核对三种事实。DBOS 可先发布终态，仍需为 owned cleanup 或未知效果保留资源 |
| `units.usage` | API 与归档累计费用及重试的历史补差。原生模型结果和物理回执有各自的费用边界 |
| `units.artifact` | 用户报告、角色报告、交付 ID 和 API 阶段完成投影。保留投影相等性检查，以免数据库已提交但 ACK 丢失后重复发布成果 |
| `units.error` | 失败分类、费用完整性与交付 ID。RunClient 的安全重试及人工清算界面依赖这些事实 |
| `operations.id,unit,fingerprint,status,name,usage,admission_permit` | 已准入操作身份、父单元、参数绑定、已知或未知效果、操作种类、实际用量及原许可 |
| `retries.run_id,key,version,generation,outcome` | 幂等授权和失败代次绑定。只有 Finalizer 可以对 grant 中的原生目标执行 rewind |
| `approvals.run_id,version,decision` | 澄清、审批和 AI 修改的版本及决定回执，事务内绑定人机交互与原生消息 |

六张表和已有数据兼容。新版只省略新投影中的 `completed` 键，历史 JSON 原样可读。由于费用查询的失败现在可能成为原生结果 Step 的失败记录，切换采用应用版本 `dbosrunner-stage-settlement-v1`。已提交模型可保留，安全重试从失败的结果 Step 开始。旧版未完成 journal 由匹配发行版清算。[部署与回滚](../architecture/dbos-deployment.md#阶段清算版本切换)

许可轮换、取消探测、外部操作账本和投影提交仍各有开销。保留原生产者并行拓扑与两个释放边界。真实供应商账单、生产数据排空和跨主机自动接管仍需要各自的运行环境验证。

## PostgreSQL 查询样本

在同一套隔离 PostgreSQL 17.6 和 Neo4j 上，分别用基线和本版源码完成一个受控综合研究。通过 SQLAlchemy 的 `after_cursor_execute` 统计五个方法调用期间实际执行的 SQL，按最外层方法归类。两次均完成九个阶段，归档和终态事件均交付，Run 使用量均为 26 次模型调用、291 tokens 和 2 次工具调用，含夹具的初始费用。

| 方法 | 基线 | 本版 |
| --- | ---: | ---: |
| `reserve` | 26 | 26 |
| `acquire` | 36 | 18 |
| `failed_usage` | 9 | 0 |
| `accounted_usage` | 9 | 9 |
| `finish_unit` | 81 | 63 |
| 合计 | 161 | 116 |

计数范围为表内方法，`finish_unit` 包含同事务发出的业务事件。此样本衡量被改动路径的 SQL 开销。生产延迟、吞吐和长时间保留成本仍需独立测量。

## 验证

- 离线 Python 1094 项通过。Windows 跳过两项 POSIX 信号测试。
- 本机原生 CI 选集共 170 项通过，包含 69 项 PostgreSQL 合同、52 项 Brief/预算/回执/归档与追踪合同、47 项进程恢复和版本兼容合同，以及 2 项真实数据库停启合同。进程终止及重启用真实 DBOS journal，模型和工具使用受控夹具。
- 阶段结果提交前终止 Executor 会重算费用，提交后终止会复用费用，两种情况下均只调用一次模型。四项并行 Producer 恢复用例核对已完成子 Workflow 的结果和身份，覆盖原生模型、工具与未知 Context 批次。
- 实际停启 PostgreSQL 时提交返回 503，恢复后只保留原请求和对应三个稳定 Workflow。Neo4j 停启验证报告保留和归档补偿。
- React 单测 50 项，Classic 控制器 8 项，Chrome 端到端 7 项通过。首次并行运行 Chrome 时有一个终态 SSE 等待超时，独立重跑沿用原断言并通过。Ruff、Python 编译、前端 lint、类型检查和构建通过。
