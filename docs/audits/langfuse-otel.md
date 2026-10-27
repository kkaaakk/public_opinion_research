# Langfuse + OpenTelemetry 专项实施与审计

日期：2026-10-26（Asia/Shanghai）。从最新 `origin/main`
`b682ad5bf633f046df43101f012a5985f2768395`（PR #31）创建
`codex/langfuse-otel`。没有合并 main、部署生产、修改 `.env` / `CLAUDE.md`、
操作业务容器或删除本地资产。本次不调用真实付费模型，也不创建或写入密钥。

最终 Review 更新：后续独立复核发现并修复 tracestate 出口、Provider 注册竞态和
Agent 标签三个问题，并在官方本地 Langfuse 4.70.1 UI 完成真实验收。
最终原生回归另修正测试对归档后 receipt cursor 不再变化的假定；生产 CAS、
Finalizer 与授权未改，旧版本拒绝及执行次数断言保留。
下文首次实现的数字与“未验 UI”保留为原始阶段记录；当前结论、隔离测试凭据处理、
精确 HEAD 回归及合并门禁见 [最终 Review](langfuse-otel-final-review.md)。

## 1. 执行前审计与决策

先读 AGENTS.md、准备/迁移/Phase 0–5 审计、DBOS 部署和 PR #31 的 Studio
协议/审计，再审查实际 Runtime/Harness、DBOS Workflow/Step/Queue、RunFacts、
SDK hooks、operation receipts、Research Graph、预算、SSE 和 LangSmith。

| 事实来源 | 本次处理 |
| --- | --- |
| DBOS journal/status/原生控制消息 | 仍是执行、取消、重试、恢复权威；启用原生 OTel，不添加 journal 命令 |
| SDK RunHooks/Harness | 继续控制既有观察/清算边界；复用原身份，只附加安全属性 |
| operation receipts | 仍区分 started/returned/uncertain；Trace 不确认未知外部效果 |
| Research Graph | 保留 evidence、provenance、原始内容和写回执所有权；Trace 只收身份与计数 |
| Budget | 原有实际 usage capture/reducer 不变；Trace 不回写账本 |
| Studio/SSE | 只读投影，重连/回放不新建研究或业务 Span；没有新拓扑/时间线页面 |
| LangSmith | 保留独立 opt-in 的 LangChain/RAG 和历史导入功能 |

采用一个进程级 OTel Provider、Langfuse v4 原生 BatchSpanProcessor/OTLP 导出、
OpenInference 原生 SDK Span。自定义部分仅配置/初始化、身份与 usage 属性补充、
最终出口脱敏、有限关闭和少数业务边界。不创建 Trace 模型、存储、查询服务或数据库。

## 2. 固定版本与实际 API

| 组件 | 本次根锁 / 验证接口 |
| --- | --- |
| DBOS | **3.3.0** 保持，增加 `otel` extra；`enable_otlp`, `otel_attribute_format=semconv`, `PropagateOtelContext`, Client `otel_context`, native `step_id` |
| OpenAI Agents SDK | **0.24.0** 保持；RunConfig、Runner、公开 TracingProcessor、SpanData.usage |
| Langfuse | **4.18.0**；公开 `Langfuse(tracer_provider, span_exporter, should_export_span)`，不用旧版 trace()/span() |
| OpenInference Agents | **2.6.0**；原生 Instrumentor、TraceConfig、已有 OTel LLM/Tool Span |
| OTel | **1.45.1**；ParentBased ratio sampler、OTLP HTTP、标准 ReadableSpan/SpanProcessor/Exporter |

锁文件只新增所需的 15 个包条目（281 → 296），既有关键运行时不升级。
继承的 `markitdown-mcp` 要求 `mcp~=1.8.0`、实际 `mcp==1.23.0` 冲突仍存在；
`uv pip check` 非零，未当成成功，也没有扩大修复范围。

安装源码确认了两个不能仅凭文档假设的行为：

1. `tracing_disabled=True` 令 SDK tracing processor 收不到事件，OpenInference
   不会凭空补出 Span。现在仅在遥测初始化成功后启用；显式每次调用的 SDK tracing
   opt-out 仍保留，且强制 `trace_include_sensitive_data=False`。
2. SDK 0.24 的 Responses 在敏感数据关闭时仍提供 `ResponseSpanData.usage`，
   OI 2.6 却只从 withheld Response 读取 usage；Generation 映射也漏掉 total/details。
   小型 `SDKUsageEnrichment` 在 OI 结束 **同一个** LLM Span 前，补充 native 数值和
   已装配官方 Model 的模型名。
   它不创建 Span、调用记录、模型请求、第二份预算或重试授权。处理顺序是数值补充 →
   OI；默认 OpenAI 上传 processor 被移除。Responses/Chat Completions 都以真实 SDK
   Model + mock HTTP 验证实际 usage，无重复 LLM/Tool Span。

官方依据：[OpenAI tracing](https://developers.openai.com/api/docs/guides/agents/integrations-observability)、
[Langfuse OpenInference 集成](https://langfuse.com/integrations/frameworks/openai-agents)、
[Langfuse v4](https://github.com/langfuse/langfuse-python)、
[OpenInference compatibility](https://github.com/Arize-ai/openinference/tree/main/python/instrumentation/openinference-instrumentation-openai-agents)、
[DBOS tracing](https://docs.dbos.dev/python/tutorials/logging-and-tracing)、
[DBOS contexts](https://docs.dbos.dev/python/reference/contexts)。实际固定版运行结果优先于滚动文档。

## 3. 链路和修改位置

完整结构、身份字段、环境变量、Cloud/自部署步骤见
[observability.md](../architecture/observability.md)。核心文件：

| 文件 | 实现 |
| --- | --- |
| `observability/telemetry.py` | 唯一初始化、采样、标准导出过滤、关联属性、数值兼容补充、有限关闭 |
| `web/executor.py`, `web/server.py` | 在 DBOS launch / FastAPI lifespan 初始化，关闭时有界刷新 |
| `web/durable.py` | 仅新接纳 Run 的 `run_submit`；用 DBOSClient 官方字段传播上下文；重复创建请求不新建提交 Span |
| `workflow/dbos_workflow.py` | 原生工作流的 PropagateOtelContext；复用 unit/permit digest、operation ID 和既有代次事实 |
| `runtime/sdk_context.py` | SDK 配置、既有 hooks 的身份/状态，context_build、persist_tool_results、覆盖退出清算的 sdk_invocation |
| `runtime/sdk_runtime.py` | 保持唯一 Runner 和压缩算法，仅在实际压缩时包 rolling_compaction、提供前后估算 |
| `research_graph/workspace.py`, `strategy.py` | context_retrieval、research_graph_write，计数来自原子图/回执 |
| `workflow/business_steps.py` | research_review、report_generation；从既有 usage capture 附加阶段数值 |
| `.env.example`, 根锁、部署/观测文档、CI/smoke runner | opt-in 运维配置和可复现的验证入口 |

两 Producer 保持原生并行、独立 parent/span/invocation；补查保留 round；Risk →
Strategy → Reporting 串行路径不变。Planning/intake/Graph/Memory/report 的 LangChain
模型仍是单次服务，在 Langfuse 体现所属业务阶段与已有实际 usage；逐请求 LangChain
视图继续使用可选 LangSmith，没有为此重新引入 LangGraph 或重复工具记录器。

## 4. 隐私、故障与执行语义审查

- 双层防护：OI 隐藏 Prompt、messages、输出、参数、工具 schema；最终 Exporter 对
  全部 Span 做字段/数值/长度白名单。资源和 instrumentation scope 的任意属性也移除。
- DBOS 3.3 将原始异常写到 status.description；OI 也可能携带工具 schema。最终出口
  删除 description、事件 payload、异常栈、auth/owner、原始内容，只保留错误类别。
  DBOS 原生重试事件只保留 attempt 数字，不保留 error 文本。
- `run_id` 和 OTel trace_id 分开。Langfuse session.id / trace metadata.run_id 用于定位。
  没有持久化第二套 Trace ID 表、把 Trace 当恢复游标或生成替代 execution ID。
- 官方 `dbos.otelContext` 是现有 attributes 中的可选 W3C carrier。恢复/排队继续其
  parent，恢复段有新 Span ID；API 不接受传入 baggage/auth 作为 OTel 载荷。
- 没有新增 DBOS 命令、function ID、输入/数据库格式、重试分支或授权逻辑。因此应用
  版本保持 `dbos-phase5-studio-v1`，runtime 保持 `sdk-phase5-v1`。旧版本排空规则不变。
  没有 carrier 的原 journal 只能按稳定 Run ID 关联，不能伪造历史父节点。
- 代次只读查询位于原有清算保护内；查询失败省略代次，查询期间取消仍清算已取得单元。
  未知外部效果保护、Finalizer 唯一 retry grant、许可和资源占位不变。
- 无 auth probe；无凭据/初始化失败则关闭遥测。导出失败、超时或丢 Span 不会回调
  业务重试。正常关闭整个 SDK/provider 等待有上限，硬杀可能丢失尾 Span。
- 默认 LangSmith 与 Langfuse 都关闭。显式启用 LangSmith 时，它自己的内容上传策略
  独立于 Langfuse 出口；文档不把两者等同，也不从多个层级的 Span 重复累计费用。
- 未添加 Studio 深链接：现有单操作员 Token 不是 Langfuse 项目/多租户权限映射。
  后台通过现有 Run ID 查询授权项目，前端不接收追踪凭据。

## 5. 验证记录

全部推理为官方 ScriptedModel、Fake/已有固定服务或官方 Model 的 mock HTTP，
dotenv 和默认 tracing 关闭；测试仅在明确场景启用本地内存/loopback 出口。
本地输出在忽略的 `.tmp/otel-*.log`，不提交数据库、缓存、凭据或运行输出。

| 检查 | 实际结果 |
| --- | --- |
| 最终完整 Python 离线 | **841 passed / 103 deselected / 146 warnings，135.36s**；socket guard，仅 loopback/unix |
| 完整原生矩阵 | **96 passed / 25 deselected / 0 skipped / 0 xfailed，1413.13s**；包含既有故障/重试/归档合同和当时 7 项 tracing 合同 |
| 最终 tracing 原生回归 | **8 passed / 0 skipped / 0 xfailed，216.37s**；在最终代码上重跑上述 7 项，并新增代次查询期间取消的清算回归；与完整矩阵合计 **97 个不同原生用例**，不是 104 个不同用例 |
| 根锁同步 / Ruff E/F/I / compileall | 通过；最终最低 Langfuse API 版本为 4.18，包含新增测试/支持模块和受影响 smoke runner |
| 生产入口导入 | **32 passed**；禁 dotenv/tracing/非 loopback 网络 |
| Studio TypeScript / 单元交互 | typecheck 通过，**42 passed** |
| Classic 浏览器协议 | **7 passed** |
| 根 pip 一致性 | **未通过**；唯一继承 markitdown-mcp/mcp 冲突 |
| 本地合成开销 | 5 批 × 1000 business Span，关闭中位 **1.08µs/次**，启用中位 **85.57µs/次**，5000 个实际导出至内存；不是生产吞吐 benchmark |

本轮新增 **22 项离线 + 8 项原生** 合同。最终离线重跑覆盖 API 存储关闭失败仍执行
遥测关闭、显式 SDK opt-out、严格出口字段过滤和模型名补充。完整原生矩阵运行期间的
最终复核将代次查询移入原清算 try/finally，随后用全部 8 项 tracing 合同复验；没有把
此前矩阵冒称为新增用例也已在其中执行。CI 会对 Draft PR head 重跑完整矩阵。

原生设施为本次新建 `por-otel-test` Compose project：PostgreSQL 17.6、Neo4j 5.26.31。
每例仅创建/删除自己的 UUID PostgreSQL 数据库。物理故障只指向明确的测试容器；
没有使用既有 `public-opinion-neo4j` 或 MySQL 业务容器。

实际进程恢复验证使用 SQLite 独立工具调用/效果见证、真实 DBOS/PG/Neo4j 和
loopback OTLP protobuf 接收器。安全检查点恢复后 parent trace 与原提交一致，
旧工具调用数/效果数保持 2；回执未知场景维持 interrupted/unresolved、拒绝重试，
调用数/效果数不增加。接收器验证 v4 ingestion header、身份、Span ID 和 Trace ID。

迭代失败如实保留：固定 OI 在敏感数据关闭时未导出 Responses usage 的初始失败，
促成上面的数字兼容补充；取消 Span 状态需由覆盖 settlement 的业务 invocation 表达。
滚动压缩夹具初始预算包含 7 次历史模型调用，新增断言按原测试合同校正为 baseline +
本次增量，未改预算实现或降低费用断言。首次 native 连接失败是会话中断后测试容器
已退出；恢复独立容器后重跑通过。Windows 安装 PE resource 的沙箱权限错误经允许
正常安装根锁解决，没有更换 SDK/DBOS 或关闭 TLS 校验。
部分 Windows 进程退出时有继承的 pyreadline3 析构告警；pytest 汇总和退出码分别核对，
不将此告警当作另一个测试通过或失败。

## 6. 首次实现时未验证的事项与回滚

本次实际验证了 OTel/Langfuse SDK 的安全出口、标准 OTLP 传输、Span 层级、并行时间
重叠、实际模型 usage、重试和恢复；**没有登录 Langfuse Cloud/自部署 UI，没有真实
项目凭据或生产服务端验收**。不声称远程展示/数据新鲜度/价格统计已经通过。

运营方可按 observability.md 将现有 Langfuse 项目 URL/keys 注入两个进程，运行一项
有界批准研究，按 run_id/session 查找 native root、两 Producer、Review/Gap、Risk、
Strategy 和报告；核对真实 generation 的 token、无 Prompt/工具内容，模拟出口不可用
后确认 Run/History 不重做研究。此步骤属于明确列出的手动外部验收，不是自动测试。

未支持跨服务 exactly-once export、硬杀尾数据保证、Trace 查询 API、多租户深链接、
逐请求 LangChain spans in Langfuse、生产负载/SaaS/HA 验收。回滚是关闭
`POR_TRACING_ENABLED` 并用兼容版本重启 API/Executor；保留原 DBOS journals、业务
回执和 Graph。Trace 缺失不会触发业务补跑；旧 Phase 4 / pre-Studio 仍按原版本排空。

首次交付为独立功能分支和 Draft PR；后续最终验收/合并状态以最终 Review 和
PR head/checks 的冻结记录为准。生产部署与凭据始终不在本次范围。
