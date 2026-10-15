# LangGraph Agent Server 原生能力复用与重复实现审计

审计日期：2026-10-14（Asia/Shanghai）。仓库：`kkaaakk/public_opinion_research`。

本报告以当前工作区源码为主要审计对象，同时核对本地 HEAD、本地远端跟踪引用、GitHub 实际 main 和开放 PR。**本轮只交付审计与改造建议，没有修改生产代码、依赖、数据库或 Git 历史，没有启动业务研究或调用真实付费模型。** 下文的目标架构、文件修改和验收项均为建议，尚未实施。

## 1. 执行摘要

发现 **24 项审计结论**，其中 19 项涉及改进或待确认事项，5 项为明确应保留的业务能力。不能将这 24 项理解为 24 个已发生的线上故障。

| 分布维度 | 数量 | 说明 |
| --- | ---: | --- |
| P0 | 2 | 启用人工审核时的恢复一致性；启用多用户认证时的跨用户创建 Run |
| P1 | 11 | 运行入口、排队、取消、流协议、身份边界、归档可靠性、资源管理、请求限制、部署基线 |
| P2 | 11 | 局部简化、保留业务实现、未合并分支和兼容入口评估 |
| A：明确重复实现 | 2 | Web 进程内研究请求并发调度；无动态需求的逐次 Graph Factory 编译 |
| B：部分能力重叠 | 14 | 基础运行能力可复用，业务适配、安全和数据职责仍需保留 |
| C：合理业务自定义 | 5 | 业务记忆、证据/RAG、预算与观测边界、多 Agent 策略、离线评测入口 |
| D：无法完全确认 | 3 | 生产部署与版本基线、PR #14 合并后的边界、兼容导出是否仍有外部消费者 |

最值得优先处理的五项：

| 顺序 | 编号 | 优先级 | 处理内容 | 涉及模块 |
| ---: | --- | --- | --- | --- |
| 1 | F07 | P0（多用户认证启用时） | `threads.create_run` 返回 owner 过滤条件，并验证跨用户创建、恢复和流读取 | `src/security/auth.py` |
| 2 | F05 | P0（人工审核启用时） | 拆开规划与审核节点，让用户批准的正是已保存的计划，避免恢复重复规划和漏记预算 | `workflow/intake.py`、`workflow/graph.py`、`state.py` |
| 3 | F01 | P1 | 生产 Web 研究统一提交给有持久化后端的 Agent Server | `web/server.py`、`langgraph.json`、部署配置 |
| 4 | F02 | P1 | 用官方 Run 队列和 worker 配置替代 Web 请求内 Semaphore，显式设定同 Thread 策略 | `web/server.py`、前端、worker 配置 |
| 5 | F03 | P1 | 把停止请求、停止 Run、连接断开和恢复分别建模，提供按 Run ID 的取消接口 | `web/server.py`、`web/static/app.js`、SDK 适配层 |

推荐方向是 **业务前端 → 薄业务 API → Agent Server → 现有 Workflow / 多 Agent / Research Graph / RAG / MCP**。保留薄 API 有实际理由：输入与模型白名单、受信身份、业务参数转换、报告归档和兼容现有 UI 协议。不要把这些职责全部转交给浏览器。

不建议本轮迁移或删除：四角色协作与补充研究策略、ResearchWorkspace、证据溯源图、RAG 检索/索引、业务长期记忆、业务预算策略、模型供应商适配、工具领域权限、报告档案，以及 LangSmith 未自动覆盖的 RAG Span。**Agent Server 不会自动替代这些业务能力，也不会把普通 Python ReAct 循环自动变成逐模型调用可恢复的状态机。**

两个 P0 均有离线证据，但发生条件不同：F05 默认关闭，本轮没有证据表明已产生真实重复账单；F07 在实际启用 Supabase 自定义认证并有多个用户时构成授权缺口。本地 `.env` 未配置 Supabase，不能宣称当前本机已遭到跨用户访问。

## 2. 审计基线、版本与扫描范围

### 2.1 Git 与工作区快照

| 对象 | 审计时实际状态 | 如何使用此证据 |
| --- | --- | --- |
| 当前分支 / HEAD | `main` / `c54323bac38a2d437f86705f39df33a1b109256c` | 当前工作区的提交基线 |
| 本地 `origin/main` | `50f4d7dc34699873ac840ec228fdeb2b78516e5c` | `HEAD...origin/main` 为 ahead 0 / behind 2 |
| GitHub 实际 main | `b1aa74889b4df55c500d3814c2623f1112efc0b4` | `git ls-remote` 与 GitHub Compare 核对，实际比 HEAD 多 4 个提交 |
| 远端新增功能 | PR #15、#16 已合并：用户图片直接 Vision 识别、独立 query-image prompt | 7 个变更文件集中在 configuration、图片 loader、intake 和图片/RAG 测试；不是新的运行队列或 Checkpointer 实现 |
| 未提交修改 | 145 个已跟踪文件修改，39 个未跟踪条目；暂存区无修改 | 保留原样；没有 checkout、reset、stash、fetch、merge 或 push |
| 新增业务历史 | `web/history.py`、`tests/test_web_history.py`、`tests/test_web_research.py` 等未跟踪文件 | 是当前工作区能力，不能称为 main 已合并能力 |
| 当前聊天附属工件 | `list_artifacts` 返回空 | Git worktree 清单不等于当前聊天已附属 worktree |

源码 AST 对比显示，145 个已跟踪修改中，9 个生产 Python 模块存在 AST 差异：`configuration.py`、`public_opinion_agents/response_strategy.py`、`rag/loaders/__init__.py`、`rag/loaders/query_images.py`、`rag/query_images.py`、`research_graph/schema.py`、`runtime/business_agent.py`、`web/server.py`、`workflow/intake.py`；另外存在新增 `web/history.py`。大量其他源码修改是注释等非 AST 变化，但仍不能覆盖或丢弃。

**并发工作区变化记录**：交付前核对发现，审计期间 `budget.py`、`workflow/intake.py`、`workflow/reporting.py` 在 10:50（Asia/Shanghai）另有修改，并新增 `tests/test_report_section_budget.py`。本轮未写入这些文件，也未回滚它们。已按最新源码补核：新增 `remaining_model_calls`、章节写作前分配调用额度、规划保留完整章节；最终有 AST 差异的已有生产模块为 11 个（初始 9 个加 budget/reporting）。HITL 仍在同一规划节点内，位置更新为 intake.py:399；报告中的该处证据采用末次版本。基线变化后补跑全量离线套件和专项探针，结果在第 11 节单列。

以下核心事实在 HEAD 与工作区都成立：Factory 逐次编译、Web 直接 `.astream()` 且无 Checkpointer、请求级 Semaphore、规划模型与 `interrupt()` 在同一节点、`create_run` 回调未返回 owner 过滤条件、业务 ReAct 循环、LangSmith 与预算分工。报告归档和保留失败阶段输出则以工作区为准。

| 相关 worktree | HEAD / 状态 | 结论 |
| --- | --- | --- |
| 当前工作区 | `c54323ba`，`main` | 主要审计对象 |
| `C:/codex_workspace/POR-after` | `5989480e`，detached，prunable | 目录已不存在的记录；未清理 |
| `C:/codex_workspace/POR-before` | `d0270b6b`，detached，prunable | 同上 |
| 当前仓库 `.tmp/trajectory-ledger` | `41e3ef16`，detached | 与 PR #14 实际 head 相同，用只读 Git 对象核对运行相关实现 |
| Codex worktree `ci-baseline-check` | `7ccbce8f`，detached | 历史基线，不视为当前代码 |
| `dsh-trajectory-ui` | `96dd19bb`，本地分支落后对应跟踪引用 2 个提交 | 不能用本地 worktree head 代替 PR #14 head |
| `rag-config-consolidation` | `74cf166c` | PR #12 实际 head 为 `c65eeaf2`，不是此 worktree head |
| `unified-model-factory-deepseek-structured` | `f68bde29` | 相关 PR #5 已合并，不等于该 worktree 的全部后续差异已合并 |
| `workflow-utils-architecture-consolidation` | `a5a060e1` | PR #13 已合并到 HEAD；仍按当前源码判断，不按分支名推定功能状态 |

GitHub 审计时开放 PR 共 2 个：[PR #12](https://github.com/kkaaakk/public_opinion_research/pull/12)（RAG 配置整合）和 [PR #14](https://github.com/kkaaakk/public_opinion_research/pull/14)（durable DSH trajectory ledger / replay，294 个变更文件）。`git branch --no-merged` 还会列出已经通过其他提交方式合入功能的分支；它不是开放 PR 清单。历史 Agent Observer 的 PR #8 已移除相关实现；不能根据旧分支或旧文档建议再次移除当前已不存在的 Observer。

### 2.2 依赖声明、锁定值和实测环境

本轮实际执行器是 `C:/develop/python311/python.exe`，Python 3.11.9。工作区没有 `.venv/Scripts/python.exe`；没有运行 `uv sync` 或升级/降级任何包。以下“已安装”仅代表这个 Python 环境，**不等于 `uv run`、CLI 镜像或线上容器使用的版本**。

| 包 | pyproject 约束 / 来源 | `uv.lock` | 实测已安装 |
| --- | --- | --- | --- |
| langgraph | `>=0.5.4` | `1.0.10rc1` | `1.2.0` |
| langgraph-sdk | 间接依赖，认证模块直接导入 | `0.3.5` | `0.3.14` |
| langgraph-cli | `[inmem]>=0.3.1` | `0.3.1` | `0.4.26` |
| langgraph-api | CLI/inmem 间接依赖 | `0.7.31` | `0.8.7` |
| langgraph-runtime-inmem | 间接依赖 | `0.25.0` | `0.28.1` |
| langgraph-checkpoint | 间接依赖 | `4.0.0` | `4.1.0` |
| langgraph-prebuilt | 间接依赖 | `1.0.8` | `1.1.0` |
| langgraph-checkpoint-postgres | 未声明 | 未锁定 | 未安装 |
| langchain | 间接依赖 | `1.2.10` | `1.3.1` |
| langchain-core | `>=1.2.22,<2.0.0` | `1.2.22` | `1.4.0` |
| langchain-openai | `>=0.3.28` | `1.1.9` | `1.2.1` |
| langchain-anthropic | `>=0.3.15` | `1.3.3` | `1.4.3` |
| langchain-mcp-adapters | `>=0.1.6` | `0.2.1` | `0.2.2` |
| langsmith | `>=0.3.37` | `0.7.1` | `0.8.5` |
| mcp | `>=1.9.4`，UV override | `1.23.0` | `1.8.1` |
| openai | `>=1.99.2` | `2.20.0` | `3.3.1` |
| fastapi | `>=0.138.0`，web extra 另有较宽约束 | `0.138.0` | `0.136.1` |
| uvicorn | `>=0.34.3` | `0.34.3` | `0.40.0` |
| pytest | 未限定 | `8.4.0` | `8.4.2` |

`pip check` 实测不通过，包含 MCP、OpenAI provider 和测试工具的依赖冲突。运行时导入实际打印：`MCP adapters unavailable ... cannot import name 'ElicitationFnT' ... MCP tools will be skipped`。因此 454 个离线测试通过不能证明真实 MCP 连接可用。实测环境还缺少 Neo4j 包的 distribution metadata；没有进行真实 Neo4j 连通或默认研究运行验证。

为校验锁定版本的 API，本轮从 PyPI 只读获取 wheel 并解读源码，**没有安装**：`langgraph==1.0.10rc1`、`langgraph-sdk==0.3.5`、`langgraph-api==0.7.31`、`langgraph-runtime-inmem==0.25.0`。关键核对位置：

| 精确版本源码 | 核实结果 |
| --- | --- |
| `langgraph/types.py:420-440`，1.0.10rc1 | `interrupt()` 恢复会从节点开头重跑；需要 Checkpointer |
| `langgraph_api/graph.py:get_graph`，0.7.31 | Server 给图副本注入 Checkpointer / Store；加载器接受已编译 Pregel |
| `langgraph_runtime_inmem/ops.py:2330-2349`，0.25.0 | Run 创建只在返回非空过滤条件时检查现有 Thread metadata |
| `langgraph_sdk/client.py:RunsClient`，0.3.5 | 有 `create/stream/join_stream/cancel`、`command`、`stream_resumable`、`last_event_id` 等参数 |
| `langgraph_api/config/__init__.py`，0.7.31 | 有 `N_JOBS_PER_WORKER`、`BG_JOB_MAX_RETRIES`、`BG_JOB_ISOLATED_LOOPS`、`RESUMABLE_STREAM_TTL_SECONDS`；`BG_JOB_TIMEOUT_SECS` 默认 3600 |
| 本机同名 config，0.8.7 | `BG_JOB_TIMEOUT_SECS` 默认 86400，与锁定版不同；不能无版本地引用默认超时 |
| `langgraph_api/models/run.py`，两个版本 | HTTP Run 创建未指定策略时均使用 `enqueue`；底层 `Runs.put` 的 Python 参数默认 `reject` 不代表 HTTP 默认行为 |

### 2.3 扫描方法和覆盖面

对生产 100 个 Python 文件（`open_deep_research` 99 个、`security` 1 个）做了全量文件清单、AST 结构、执行/存储/网络调用定位和引用检查；对关键链路逐函数阅读。另纳入初始 37 个、交付前 38 个测试 Python 文件，以及原有 42 个 docs Markdown 文件、静态 HTML/JS/CSS、`.github`、根目录配置、2 个 scripts、15 个 evaluation Python 文件、examples、`locustfile.py`、`tools/code_graph_analysis.py`。历史报告、数据样本和生成的演示文稿只作为背景，不作为能力成立的证据。

| 区域 | 实际定义 → 调用方 → 运行入口 | 审计结论 / 验证 |
| --- | --- | --- |
| graph / workflow | `deep_researcher.py` 转导出 → `workflow/graph.py` → langgraph.json / Web / Python 评测 | 两种生产候选入口；编译、HITL、子图传播已做离线探针 |
| runtime / 四角色 | `run_business_agent` → `AgentRuntime.run` → 四角色节点 → `research_phase` | 逐次模型/工具循环在普通 Python 局部变量内；业务上下文策略不可直接删除 |
| state / config | 六个状态域、reducer、Configuration、环境优先级 | State 是业务数据结构，不是重复 Run 数据库；环境值优先于 configurable |
| web / 前端 | POST `/api/research` → Semaphore → `.astream(updates)` → JSON SSE → `fetch` parser | 缺乏独立 Run 资源、恢复接口及连接重建语义 |
| security | langgraph.json → Supabase authenticate → resource hooks | `create_run` 过滤缺口已离线验证；其他部署/认证条件另列 |
| history / memory | SSE 消费者 → Neo4j 归档；报告节点 → MySQL 写入 → RAG 索引 | Checkpoint、档案、原始对话与检索记忆互不等价 |
| research_graph | Workspace → strategy/extractor/context manager → scoped Neo4j store | 有稳定键、run scope 和写入后压缩机制；仍需异步 I/O 与资源释放 |
| RAG 全目录 | tool → pipeline → indexer / embedding / BM25 / vector / graph expansion / reranker / citations / loaders | 是检索与证据业务，不是 Agent Server 的运行存储；已纳入默认离线测试 |
| MCP / tools / search | registry → adapters / 域过滤 → tool wrapper / tool 调用 → 预算捕获 | 使用官方 MCP adapters；保留权限、故障隔离、原始搜索来源边界 |
| 其他服务 | RAG MCP、Social MCP、Apify HTTP 服务 | 提供领域工具/采集服务，没有发现它们另行执行研究图；不迁成 Run 管理服务 |
| observability / budget | 官方自动 Trace、RAG Span、官方 usage callback + 业务 reducer | 未发现当前主链重复自建完整 tracing 系统 |
| tests / evaluation | MemorySaver / 直接图调用 / case 超时 / 压测信号量 | 是验证隔离与测量职责，不误判为生产队列 |
| PR #14 | `web/research.py:ResearchExecution`、trajectory lifecycle / JSONL ledger / React adapter | 未合并；单独讨论其未来与 Run 管理的重叠 |

## 3. 官方能力核验与运行模式边界

### 3.1 分层能力表

| 层次 | 已核实 API / 能力 | 不应推导出的能力 | 版本与官方依据 |
| --- | --- | --- | --- |
| LangGraph Python | `StateGraph.compile`、Pregel `ainvoke/astream`、reducer、`Send`、`interrupt`、`Command`、Checkpointer | 单靠 `.astream()` 和 thread_id 不会得到服务端 Run 队列、持久化或 HTTP 授权 | 锁定源码 1.0.10rc1；[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)、[Streaming](https://docs.langchain.com/oss/python/langgraph/streaming) |
| Agent Server | Thread / Run / Assistant 资源；受部署配置管理的 Checkpointer、Store；队列和流接口 | 不自动定义业务租户、预算、证据 schema 或外部工具的幂等规则 | API 0.7.31 与本机 0.8.7 加载源码；[Agent Server](https://docs.langchain.com/langsmith/agent-server) |
| LangGraph SDK | `get_client`、`threads.create/get_state/get_history`、`runs.create/stream/get/list/join_stream/cancel` | SDK 是客户端；安装 SDK 不会启动 Server 或建立数据库 | wheel 0.3.5 已核对；[Background runs](https://docs.langchain.com/langsmith/background-run) |
| Server 的并发策略 | `multitask_strategy=enqueue/reject/interrupt/rollback` 管同一 Thread 的重入；worker 参数管 Run 并发 | 同 Thread 策略不是全局限流，也不是工具/供应商 QPS 或多角色共享预算 | [Enqueue concurrent](https://docs.langchain.com/langsmith/enqueue-concurrent)、[Scale](https://docs.langchain.com/langsmith/agent-server-scale) |
| Server 取消 | `runs.cancel(thread_id, run_id, action="interrupt", wait=True)` 等 | 取消不撤销已付费的模型调用、外部写入或 MCP 副作用；rollback 也不是跨库事务回滚 | SDK 0.3.5；[Cancel a run](https://docs.langchain.com/langsmith/cancel-run) |
| 服务端流 | `stream_mode`、`stream_subgraphs`、`stream_resumable`、`Last-Event-ID` | 普通 join 不保证收到连接前事件；临时流缓存不等于永久轨迹档案 | SDK 0.3.5；[Streaming API](https://docs.langchain.com/langsmith/streaming)、[当前 OpenAPI](https://docs.langchain.com/langsmith/agent-server-openapi.json) |
| Server HITL | 同 Thread 再创建 Run，`command={"resume": ...}` | 不会自动拆分模型调用与 interrupt 节点，也不会自动提供业务审核 UI | [Server HITL](https://docs.langchain.com/langsmith/add-human-in-the-loop) |
| Server Auth | `Auth.authenticate`、`auth.on.threads.*`、`auth.on.assistants.*`、`auth.on.store` | 身份校验不等于所有业务库自动隔离；None 返回并非默认拒绝 | SDK 源码、[Custom auth](https://docs.langchain.com/langsmith/custom-auth)、[Resource auth](https://docs.langchain.com/langsmith/resource-auth)、[Store auth](https://docs.langchain.com/langsmith/store-auth) |
| LangSmith | 自动图/模型/工具 Trace，手工逻辑 Span，trace/run 查询 | Trace 保留策略和采样不等于永久报告存储、成本硬限制或用户进度协议 | 当前项目已使用官方集成；[LangSmith observability](https://docs.langchain.com/langsmith/observability) |
| LangChain / prebuilt | `create_agent`、`ToolNode`、`Runnable.with_retry`、usage callbacks、summarization / retry middleware | 通用循环不自动保持本项目生产者/消费者、证据写入回执、上下文保护和角色记忆语义 | 当前安装中存在；[Agents](https://docs.langchain.com/oss/python/langchain/agents)、[Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)；新特性须再次对照锁定版 |

当前文档是滚动更新页面。原生库 `astream(version="v2")` 的统一 `StreamPart` 格式要求 LangGraph 1.1+，新的 event streaming 页面还涉及 1.2 能力；**不能把这些例子直接用于锁定的 1.0.10rc1**。本文推荐先使用已经在 SDK 0.3.5 中确认的 Run API 和现有 graph stream modes，不要求同时升级库。[版本说明](https://docs.langchain.com/oss/python/langgraph/streaming)

OpenAPI 的 `info.version` 为 `0.1.0`，这是 schema 文档字段，不是正在运行的 `langgraph-api` 包版本。上线时应保存实际镜像/API 版本及其 OpenAPI，不能用这个字段判定运行兼容性。

### 3.2 四种运行模式

| 模式 | 谁执行图 | 状态 / 队列 | 本项目现状与限制 |
| --- | --- | --- | --- |
| 自行 Python 编译执行 | 调用进程 | 需要调用方显式配置 Checkpointer / Store；没有 Agent Server 的资源/队列 API | Web factory 结果无 Checkpointer / Store；评测有部分 MemorySaver，仅进程内 |
| `langgraph dev` | 开发版 Agent Server | inmem 后端；当前文档说明有本地目录持久化快照 | 用于开发验证；不是生产 PostgreSQL/Redis、高可用或故障恢复承诺。要实测当前版本、数据目录及关机/崩溃恢复 |
| `langgraph up` | 本地容器化 Agent Server | PostgreSQL / Redis 等生产相似组件 | 适合队列、连接断开、重启、worker 行为验收；仓库未提供已运行的该栈证据 |
| 托管或 standalone 部署 | 正式 Agent Server | 按部署配置的持久化、worker、认证、容量和保留策略 | 云端与自托管的运维、许可、网络、备份责任不同，不能直接推定本项目已拥有 |

官方将 dev 定位为快速开发、up 定位为生产相似验证；当前文档列出 dev 的内存/本地快照与 up 的 PostgreSQL 差异。[Local development & testing](https://docs.langchain.com/langsmith/local-dev-testing)

standalone 当前文档明确列出数据库、Redis、许可校验与相关网络要求；项目采用 MIT 开源库，不能据此推导 Agent Server 所有生产模式也没有许可或运维成本。本轮未确认账户权益和生产部署，故把正式部署选择列为 F21 待验证。[Standalone servers](https://docs.langchain.com/langsmith/deploy-standalone-server)

### 3.3 必须分开的可靠性语义

| 操作/故障 | 当前 Web | 官方可复用部分 | 仍需项目决定/验证 |
| --- | --- | --- | --- |
| HTTP 断开 | 浏览器 abort 或网络故障均结束读取；可能取消响应任务 | background Run 与 HTTP 分离；stream disconnect policy | 断开继续运行还是主动取消；UI 如何重新定位 Run |
| 取消运行中 Run | 无独立 Run 取消接口 | cancel + interrupt，保留已有 Checkpoint | 外部工具停止能力、迟到结果、归档和状态确认 |
| 取消排队 Run | Semaphore 等待者无资源 ID | 可取消 pending Run | 多实例排队、取消与 worker 领取的竞争 |
| 人工暂停后恢复 | 有图 interrupt，Web 没有恢复契约 | 同 Thread + `command.resume` | 审核载荷校验、计划版本、一致性和用户权限 |
| 模型失败重试 | 已用 `with_retry`，另有上下文缩减重试 | LangChain retry、可选节点 RetryPolicy | 应重试的异常、次数、费用和副作用策略 |
| worker 瞬时故障重试 | 无跨进程执行恢复 | Server 对指定可重试故障重新入队并读取 Checkpoint | 不是所有业务异常都会重试；要核对部署版本的异常集合 |
| 进程/机器重启 | 局部变量和请求队列丢失；已有业务库资料可能仍在 | 持久化 Run + Checkpoint + worker 恢复机制 | 数据卷/数据库可用、未完成节点的重放、外部工具幂等 |
| 完整任务幂等 | 每次 POST 新 UUID，重复提交是新研究 | Thread / Run 标识、同 Thread 策略 | 请求去重和工具幂等键仍是应用责任 |

官方 data-plane 页面对重试范围的文字较窄，而环境变量页描述了可配置重试；0.7.31 worker 源码实际按 `ALL_RETRIABLE_EXCEPTIONS` 分支处理，普通异常走 error、超时走 timeout、用户取消走 interrupted。本文按**具体版本源码和故障类型**划界，不把文档中的“重试”解释为任意失败自动重跑。[Data plane](https://docs.langchain.com/langsmith/data-plane)、[运行参数](https://docs.langchain.com/langsmith/env-var-cloud)

## 4. 当前实际架构

实线表示源码可定位的调用/数据流；虚线标明部署配置或本轮未执行的运行验证。

### 4.1 Agent Server 执行链路

```mermaid
flowchart TD
    CLI["langgraph dev / up / 托管：实际运行模式待验证"] -.-> CFG["langgraph.json"]
    CFG --> AUTH["security/auth.py：Supabase + resource hooks"]
    CFG --> EXPORT["deep_researcher.py：兼容导出"]
    EXPORT --> FACTORY["workflow/graph.py：每次重新构建并 compile"]
    SDK["Studio / SDK / HTTP 客户端"] --> API["Agent Server Thread / Run API"]
    AUTH --> API
    API --> QUEUE["部署后端队列 / worker"]
    QUEUE --> FACTORY
    FACTORY --> INJECT["Server 注入 Checkpointer / Store"]
    INJECT --> WF["研究 Workflow"]
    INJECT -.-> DB["dev 本地快照或正式持久化后端：待实测"]
    WF --> RESULT["官方 Run 状态与流"]
    RESULT --> SDK
```

### 4.2 自建 FastAPI 执行链路与关键业务依赖

```mermaid
flowchart TD
    UI["static/index.html + app.js"] --> REQ["POST /api/research"]
    REQ --> VALIDATE["请求大小头 / Pydantic / 可选共享 Token"]
    VALIDATE --> SEM["进程内 Semaphore：2 个研究请求"]
    SEM --> LOCAL["生成 thread_id；factory.astream updates"]
    LOCAL --> INTAKE["图片上下文 → 澄清 → 简报 → 章节规划"]
    INTAKE --> PHASE["research_phase：调用已编译子图"]
    PHASE --> SIGNAL["public_signal：生产证据"]
    PHASE --> INTERNAL["internal_knowledge：内部证据"]
    SIGNAL --> REVIEW["research_review：证据缺口复核"]
    INTERNAL --> REVIEW
    REVIEW --> SEND["按角色 Send 补充研究，受 max_research_rounds 约束"]
    SEND --> SIGNAL
    SEND --> INTERNAL
    REVIEW --> RISK["risk_assessment"]
    RISK --> RESPONSE["response_strategy"]
    RESPONSE --> WRITE["研究章节 → 非研究章节 → 汇编/兜底"]
    SIGNAL --> RUNTIME["BusinessAgent 装配 → AgentRuntime ReAct"]
    INTERNAL --> RUNTIME
    RISK --> RUNTIME
    RESPONSE --> RUNTIME
    RUNTIME --> TOOLS["Tavily / social / RAG / MCP + 域权限"]
    RUNTIME --> WORKSPACE["ResearchWorkspace + 上下文策略"]
    WORKSPACE --> EVIDENCE["Neo4j 证据库 / scoped retrieval"]
    TOOLS --> RAG["RAG 索引、召回、重排、引用"]
    WRITE --> RETURN["SSE 消费者归档并返回报告"]
    RETURN --> UI
```

未启用的角色节点仍在图中注册，但 `run_business_agent` 先检查 enabled roles 并返回空增量；当前 Web 在 `rag_enabled=False` 时从配置中去掉 `internal_knowledge`。不能把该节点注册误判为每次必然初始化内部 RAG。

### 4.3 Checkpoint、业务状态与持久化

```mermaid
flowchart LR
    DOM["State：messages / workflow / agents / research / report / runtime"] --> CP["仅在配置 Checkpointer 时：图步骤快照"]
    SERVER["Agent Server"] --> CP
    WEB["当前 Web 直接执行"] -.-> NONE["没有 Checkpointer；thread_id 只是传入标识"]
    EVAL["部分评测 MemorySaver"] --> RAM["进程内检查点"]
    DOM --> WORK["WorkingContext 序列化状态"]
    WORK --> CP
    DOMAIN["研究工具与 Workspace"] --> NEO["Neo4j Source / Evidence / Claim / Finding"]
    FINAL["报告完成节点"] --> MYSQL["可选 MySQL：原始对话 / 摘要 / 长期记忆"]
    MYSQL --> INDEX["pending → RAG 索引；后台线程 + 查询时补偿"]
    CONSUMER["Web SSE 消费者"] --> HISTORY["当前工作区新增：Neo4j ResearchHistory"]
    LS["LangSmith Trace"] --> IMPORT["用户发起的旧历史导入"]
    IMPORT --> HISTORY
```

业务历史保存成功不代表任务可恢复；Neo4j 证据保留不代表 ReAct 局部消息、计划审批点和 worker 队列已持久化。反过来，有 Checkpoint 也不意味着报告已进入业务历史索引。

### 4.4 SSE、前端与执行进度

```mermaid
sequenceDiagram
    participant B as 浏览器 app.js
    participant W as FastAPI
    participant G as 本进程 Graph
    participant H as 业务历史库
    B->>W: POST topic / model / mode / rag_enabled
    W->>W: 等待 Semaphore
    W->>G: factory().astream(updates, subgraphs=True)
    G-->>W: 节点增量；子图角色输出
    W->>W: 局部收集 report / budget / stages
    W-->>B: data JSON：status / stream
    W->>H: 完成、异常或捕获取消时尝试归档
    W-->>B: report / usage / history_saved / done
    Note over B,W: Stop 仅 AbortController.abort；没有独立 cancel Run 请求
    Note over B,G: 没有重连定位、Last-Event-ID、暂停审核或恢复 API
```

### 4.5 单个角色的耐久边界

```mermaid
flowchart LR
    STEP["LangGraph 角色节点开始"] --> INIT["装配 tools / model / Workspace"]
    INIT --> LOOP["Python for 循环：局部 messages、budget、receipts"]
    LOOP --> MODEL["模型调用，官方 callbacks 计数"]
    MODEL --> TOOL["asyncio.gather 执行工具"]
    TOOL --> GRAPH["提取证据 → 写库回执 → 上下文更新/压缩"]
    GRAPH --> LOOP
    LOOP --> REPORT["finalize / compression：角色报告"]
    REPORT --> PATCH["返回 State patch"]
    PATCH --> CHECKPOINT["父/子图步骤 Checkpoint：仅有配置时"]
    LOOP -.-> LOST["中途失败时未提交的普通 Python 局部状态不能自动恢复"]
```

对子图不能作同样的无持久化推断：在配置父图 Checkpointer 的离线测试中，函数式调用的已编译子图继承了持久化上下文，成功子节点执行 1 次、失败子节点重试后共 2 次。**需要整改的是角色节点内部循环的粒度，不能因为子图 `.compile()` 未传 Checkpointer 就认定整个子图没有持久化。**

## 5. 完整问题与处置清单

本表中的 `workflow/`、`web/` 等缩写均相对于 `src/open_deep_research/`；`security/auth.py` 相对于 `src/`。行号对应末次核对的工作区快照，函数名用于后续行号变化时定位。C 类列为保留决策，不代表缺陷；D 类不批准删除。

| 编号 | 问题 | 分类 | 优先级 | 源码位置 | 当前实现 | 官方替代方案 | 建议 | 风险 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| F01 | Web 执行没有持久化 Run / Checkpoint | B | P1 | `workflow/graph.py:95-107`；`web/server.py:285-367` | UUID 仅放入 config，本进程直接执行 | Server Thread / Run / Checkpointer | Web 提交官方 stateful Run，保留请求适配 | 仅替换调用名不能保留重启恢复 |
| F02 | 请求级 Semaphore 与临时等待队列 | A | P1 | `web/server.py:57,75,459-473` | 每进程 2 个请求，等待者无 Run 资源 | Server 队列、worker 容量、同 Thread 策略 | 迁移后删除该研究调度 Semaphore | 限制维度与总并发发生变化 |
| F03 | Stop、断线、取消与恢复混为一体 | B | P1 | `web/static/app.js:125-166`；`web/server.py:444-457` | abort fetch；捕获取消后尝试写库和向已断开连接 yield | `runs.cancel`、background Run、disconnect policy | 显式 Run 取消并等待结果，断线只改变订阅 | 外部调用不一定立即停止，不能保证回滚 |
| F04 | SSE 只包节点更新，缺乏原生流语义 | B | P1 | `web/server.py:176-210,365-442`；`web/static/app.js:133-217` | 单行 JSON data、无 event/id/重连；非 token 流 | Run stream / join / resumable stream | 官方 SDK 消费，保留 UI 展示适配 | 不能直接把官方 SSE 喂给旧 parser |
| F05 | 审核恢复重跑规划，审批对象和预算不一致 | B | P0 | `workflow/intake.py:286-429`，尤其 348、399、402 | 规划结果和 usage 尚未提交就 interrupt | 原生 interrupt + 已提交 State | 拆规划/审核节点 | 开启后可能重复请求、批准不同计划、漏记首轮消耗 |
| F06 | HITL 有代码但 Web 无暂停/恢复契约 | B | P1 | `configuration.py:1447`；`web/server.py:334-442` | 默认关闭；未处理 interrupt，也没有 resume endpoint | 同 Thread 的 `command.resume`、state API | UI/业务 API 显式支持审核状态、反馈和 resume | 不能将暂停报为“研究结束但无报告” |
| F07 | create_run 未执行现有 Thread owner 过滤 | B | P0 | `security/auth.py:81-122` | 写 metadata 后返回 None；read hook 另有 owner filter | Auth resource filter | create_run 返回受信 owner filter；覆盖所有 Run 入口 | 多用户开启时已知他人 Thread 可被写入/执行 |
| F08 | 业务身份与 Server 身份/资源隔离不统一 | B | P1 | `memory/context.py:14-42`；`mcp/tools.py:144-200`；`web/history.py:39-55,248-291` | 可选共享 Token；config/metadata 身份；档案无 owner 字段 | Server Auth user + Store namespace | 统一受信身份，业务库强制租户/用户范围 | Server Thread 授权不能保护直接 Neo4j/MySQL 查询 |
| F09 | MySQL 长期记忆与 Checkpoint 不同 | C | P2 | `memory/writer.py:28-156`；`memory/store.py:54-183`；`rag/indexer.py:139-238` | 原始对话、摘要、长期记忆与 pending 索引 | Store 可承接部分 KV，但不替代整个链路 | 保留；可单独评估 KV 子集 | 删除会失去原文、引用、索引增量和业务 schema |
| F10 | Research Graph / RAG 被误当运行存储的风险 | C | P2 | `research_graph/workspace.py:63-225`；`store.py:327,483`；`rag/service.py:287-425` | 来源、证据、冲突、覆盖、检索和引用 | Server 不提供这些业务语义 | 保留并强化 run/tenant 边界 | 用 Checkpoint/Store 代替会破坏证据关系和检索 |
| F11 | 角色内部 ReAct 局部状态不可逐步恢复 | B | P1 | `runtime/agent_runtime.py:214-415`；`business_agent.py:393-510` | 循环、模型、工具、回执与预算在单节点内 | 图节点 / tasks + Checkpointer | 明确耐久粒度；按需拆循环步骤，保留架构 | 重放未完成节点可能重复外部调用或费用 |
| F12 | 业务归档依赖 SSE 消费者且两个入口不一致 | B | P1 | `web/server.py:294-311,408-457`；`web/history.py:155-239,541-591` | Web 结束时存档；Server 入口仅有 Trace/业务记忆路径 | Run 状态/Checkpoint、run webhook | 归档独立于 HTTP，服务端幂等投影及补偿 | 断线/崩溃可能未存档；Trace 导入不能作为恢复协议 |
| F13 | Trace、usage 与预算已有合理分工 | C | P2 | `observability/langsmith.py:1-18,169-374`；`budget.py:231-448` | 官方 callbacks + 项目策略；仅 RAG 手工 Span | 已复用 LangSmith / LangChain | 保留，不把预算改成查询 Trace 汇总 | 回放和失败节点仍需防重复/漏记 |
| F14 | 多 Agent 业务编排不是 Server 队列 | C | P2 | `workflow/research.py:74-127,240-416`；`public_opinion_agents/base.py:1-134` | 两取证角色、复核、Send 补充、风险和策略 | LangGraph 已执行这些图语义 | 保留业务路由、reducer 和角色契约 | 改为通用 agent 容易丢失业务质量约束 |
| F15 | Runtime 通用工具循环与 prebuilt 部分重叠 | B | P2 | `agent_runtime.py:276-378`；`business_agent.py:194-349` | 自行分发、gather、ToolMessage、异常转观察 | ToolNode / create_agent / middleware | 先保留；只在合同测试证明等价后替换小段 | 工具顺序、异常、预算、native search 和回执语义不同 |
| F16 | 同步 I/O 与按 run 缓存资源妨碍稳定复用 | B | P1 | `workflow/research.py:147-150`；`research_graph/strategy.py:275,332`；`store.py:593-635` | async 节点内同步 Neo4j；每 run 缓存 store/driver，无生产释放调用 | 官方异步执行建议和应用生命周期扩展 | 异步/线程边界；按连接复用池，按 run 隔离数据 | 阻塞 worker、累积驱动/连接；Server 不自动回收项目全局缓存 |
| F17 | Graph Factory 无动态需求却重复编译 | A | P2 | `workflow/graph.py:94-107`；`langgraph.json:3-4` | 全局编译图与逐次重编译并存；factory 丢弃 config | 已编译 Pregel 导出 | 校验并发和 loader 后注册 compiled export | 改测试契约，不能误删业务初始化 |
| F18 | research/thread/run/trace 标识语义混用 | B | P2 | `web/server.py:286-303`；`workflow/state_utils.py:183-205`；`observability/langsmith.py:147-187` | history.run_id 实为 Thread UUID；另有随机日志 trace_id | 官方 Run ID / Thread ID；LangSmith tracing | 明确四类标识与映射，一 Thread 对应一次研究会话 | 多次 resume 和同 Thread 新研究容易串档/误查 |
| F19 | 请求大小限制可被分块 body 绕过 | B | P1 | `web/server.py:129-160`；`ResearchRequest:100-126` | 仅检查 Content-Length；额外字段默认忽略 | Server/ASGI/网关限制须按实际环境配置 | 保留业务校验并计实际 body bytes | 不是换用 Server 就自动继承 64 KiB 策略 |
| F20 | 原始 transcript 与 Trace 重叠但用途不同 | B | P2 | `research_graph/transcript.py:17-34`；`strategy.py:231,450` | JSONL 保存原始工具/压缩信息；未见去敏和保留策略 | LangSmith 可保留技术追踪 | 先确定原始证据/审计用途，再缩减重复载荷 | 敏感材料落盘、多实例本地文件和隐私保留问题 |
| F21 | 正式部署、锁定运行基线及恢复保证未核实 | D | P1 | `pyproject.toml`、`uv.lock`、`README.md:66`、`langgraph.json`、`.github/workflows/ci.yml` | 版本漂移；仅见 Neo4j Compose；测试不启动正式 Server 栈 | dev / up / standalone / cloud 分模式验收 | 固定并记录执行基线后再迁移 | 许可、数据库、运行参数或故障恢复假设不成立 |
| F22 | PR #14 新执行管理将与 Run 管理重叠 | D | P2 | PR #14 `41e3ef16`：`web/research.py:ResearchExecution`、`trajectory/lifecycle.py`、`persistence.py` | 独立 task、显式 cancel、订阅、JSONL writer lease / replay | 官方 Run lifecycle + 流事件 | 合并前评审，保留必要业务轨迹投影 | 尚未合并，不能算当前 main 的重复代码或批准删除 |
| F23 | 评测/压测直调图和并发限制属合理测试能力 | C | P2 | `tests/run_evaluate.py:35-55`；`tests/debug_run.py:9-19`；`tests/agent_stress.py:186`；`evaluation/cost_profile/run_cost_profile.py:742-757` | MemorySaver、case timeout、client 并发、结果快照 | Server E2E 可补充，不能代替单测 | 保留离线/评测入口；增加 Server 集成测试 | 强制所有测试联网会降低隔离与可复现性 |
| F24 | 部分旧兼容 helper 无内部实际调用方 | D | P2 | `workflow/intake.py:64,111`；`tools/registry.py:213,356`；`deep_researcher.py` / `utils.py` 导出 | helper 定义或兼容导出，未见生产使用 | 无需官方替代；可直接退役的前提尚不足 | 标记弃用候选，核对外部导入和动态使用 | 静态无调用不能证明公共导出可安全删除 |

## 6. 逐项技术分析

### F01：统一运行入口，但保留业务准备与验证

**定义、调用与启用。** `workflow/graph.py:99` 的 factory 返回没有 Checkpointer/Store 的 compiled graph。`web/server.py:365` 直接调用它的 `.astream()`；`configurable.thread_id` 只是字符串，没有创建 Agent Server Thread。图通过 `deep_researcher.py` 兼容导出，也被 langgraph.json 注册。在 Server 路径中，持久化由 Server 加载器注入，因此两个入口具有不同运行保证。

**处置与边界。** Web 改用 SDK 创建 Thread、提交 stateful Run，返回官方 ID；现有模式映射、模型白名单、RAG 角色开关仍在薄 API 内。无需为 Server 导出图手工添加 PostgreSQL Checkpointer；直接 Python 测试入口可自行用 MemorySaver。Server 能承接基础运行设施，但 RAG/MCP 工具装配、Workspace 创建和供应商模型配置继续在业务节点发生。[Server 图加载与持久化](https://docs.langchain.com/langsmith/agent-server)

**涉及文件与测试。** 替换 `web/server.py:research` 的图执行部分，新增 `web/agent_client.py`、`web/research_service.py`；更新 Web fake SDK 测试，增加真实 Server 正常完成、状态读取和 worker 重启测试。当前 `test_web_research.py` 只证明 fake graph 收到正确 RAG 参数，不证明 Run 持久化。

### F02：可替换的是生产研究调度 Semaphore

`event_stream` 从 `async with _RESEARCH_SEMAPHORE` 到响应结束一直占槽。等待者仍是 HTTP 请求，没有 pending Run、排队时间或可取消的 Run ID；排队期间尚未进入 `_research_event_stream`，连 thread_id 和 started_at 都未生成。多个 Web worker/实例各自限制 2 个，不能形成全局 2 个研究任务的上限。现有单测强制 Semaphore(1) 后并发消费两个生成器，只验证一个事件循环内的限制。

迁移 stateful Run 后，官方队列负责接纳和 worker 领取，`N_JOBS_PER_WORKER` 控制每 worker 的 Run 容量。同 Thread 的 `multitask_strategy` 另行显式设定：推荐初次提交防重入用 reject，确需序列追问时用 enqueue；不是用 enqueue 实现所有用户共享的全局限额。若要维持全局并发 2，初期可采用单 worker、每 worker 2 槽的配置，多 worker 后需重新计算容量。供应商 QPS、租户公平性或硬预算仍需要专门策略。[官方容量边界](https://docs.langchain.com/langsmith/agent-server-scale)

删除范围限于 `web/server.py` 的研究请求 Semaphore；不删除 ReAct 工具批次、章节写作、搜索或压测客户端的并发。新增多个 Thread 排队、同 Thread 冲突、pending 取消、多实例容量测试。避免迁移后同时留两层独立研究队列，造成请求已等待但 Server 尚无 Run 的不可见状态。

### F03：取消是 Run 操作，断线是订阅事件

当前 `stopResearch()` 仅调用 `AbortController.abort()`。服务端捕获取消后在同一响应生成器内 `await archive_run()`，随后继续 yield 错误/保存事件；真实断线时这些事件无法再供客户端确认，取消作用域也可能阻止清理等待完成。没有显式 re-raise、独立 cancel endpoint、Run ID 或“已确认停止”的状态查询。不能从源码断言每一种 ASGI/代理版本下都会漏归档，但可以确认没有独立于连接的取消与确认契约。

`test_cancelled_run_still_creates_a_history_record` 让 fake graph 主动抛出 `CancelledError`，客户端仍完整接收响应；这与真实 socket 断开、AnyIO 持续取消、进程被杀不是同一种测试。推荐后台 Run + 独立取消接口：Stop 调用官方 cancel 并等待终态；网络断开按明确策略继续运行并允许重连。保留 partial state 和阶段档案，不把 rollback 作为“停止但保留证据”的默认行为。[取消与 rollback](https://docs.langchain.com/langsmith/cancel-run)

改 `app.js`、`server.py`、研究服务/归档适配层；增加 ASGI disconnect、真实 TCP 断开、pending cancel、running cancel、取消与完成竞争、不可取消同步工具迟到完成测试。外部已发生的副作用无法由 Server 自动撤销。

### F04：流式传输可复用，展示协议需要保留

当前输出是 `_event({type: ...})` 的 JSON data 行。`updates` 在节点返回时发送状态增量，`_extract_text` 再截断为少量展示文本；不是 token 流。嵌套 namespace 仅用于收集角色档案，普通 UI 不广播子图事件。前端逐行找 `data: ` 并 JSON.parse，忽略 event/id、跨行 data 和解析错误；遇到 EOF 也没有要求已收到 done 才算有效结束。无断线游标或历史事件重放。

官方 SDK 解决服务端 stream 协议和 Run 关联，但旧的 `status/report/usage/history_saved/warning` 是业务显示语义，不能直接删除。建议 SDK → 小型 stream adapter → 现有 UI 事件；首先保持节点级进度和最终报告行为。若后续增加 token 流，再选 `messages-tuple`、按节点/标签过滤并检查 `langsmith:nostream`：当前多个模型配置主动带此标签，换 API 不会自动得到所需 token 展示。[官方流模式](https://docs.langchain.com/langsmith/streaming)

重连使用 `stream_resumable=True` 和 Last-Event-ID 的前提是已启用临时流缓存且尚未过期；普通 join 的过往事件可能不可得。缓存过期则读取 Thread/Run 状态与业务报告恢复页面，不能声称完整原始 token 永久回放。改 `app.js`、新增 `stream_adapter.py`；测试 SSE 分块、事件去重、子图过滤、错误/终态、缓存过期恢复和重连不得创建第二个 Run。

### F05：规划与审核同节点的风险已离线复现

调用链为 `write_research_brief → plan_report_sections → planner LLM → interrupt → Command(update=sections,budget)`。人工审核开关 `allow_plan_feedback` 默认 False；本地 `.env` 未设置该项。预算不足或上下文错误降级分支也可能绕过审核。因此问题是**到达 interrupt 分支并恢复时**的确定性行为，不是所有研究默认都会重复规划。

原生 `interrupt` 通过恢复同一节点重新执行逻辑。第一次 planner 的 sections 和 `total_budget_update` 只是局部变量，还未提交给 State。离线假规划器每次返回不同计划，实测：展示 `Plan-1`，`Command(resume=True)` 后又调用一次规划器，最终 State 采用 `Plan-2`；实际假模型调用共 2 次，State 仅计 1 次、10 tokens。这个测试没有实际账单，但证明了重复请求、审批错位和记账遗漏的条件。[官方节点重执行语义](https://docs.langchain.com/oss/python/langgraph/interrupts)

建议拆为 `plan_report_sections`（模型 + 预算 + draft/version 入 State）→ `review_report_plan`（只读 draft 并 interrupt）→ research。True 批准已保存版本；字符串反馈先入 `plan_feedback`，再回规划节点明确产生下一版计划。保留原有 True / str 合同和预算降级策略，拒绝未知反馈类型；不能用在同节点临时字典里缓存结果来替代 Checkpoint。

改 `workflow/intake.py`、`graph.py`、`state.py`（如新增 plan version/status），必要时调整 prompt。增加真实图 + MemorySaver 的 approve/revise/重复 resume/进程重启审核测试，断言纯批准不再调用 planner、反馈只导致一次新规划、预算包括所有已发生调用、规划内容 hash 与被批准版本一致。Server API 不能替代此节点边界修复。

### F06：完整 HITL 是 UI、API、状态和持久化的合同

仓库唯一动态 `interrupt()` 在规划节点；没有生产 `Command(resume=...)` 调用。`clarify_with_user` 是返回问题并 goto END 的会话澄清，不能当成同一种中断恢复。Web 强制 `allow_clarification=False`，也没有暴露 plan-feedback 输入，但 `Configuration.from_runnable_config` 的环境优先级仍可启用审核。

离线测试显示无 Checkpointer 的初次调用可返回 interrupt 信息，但恢复抛 `RuntimeError: Cannot use Command(resume=...) without checkpointer`。因此不能只依据初次收到 interrupt 就称 Web 支持 HITL。目前 Web 也没有识别 interrupt，流结束无 final report 时会归档 incomplete 并报错。

统一到 Server 后，薄 API 增加授权的 state / feedback / resume 操作，前端呈现 interrupted、计划及审核版本；同一个 Thread 下提交带 command 的后续 Run，不重新发起新 topic。Run ID 随调用变化，业务研究 ID 保持稳定。修改配置说明、前端、服务层和 HITL 测试；覆盖默认关闭、预算降级、True、非空字符串、非法类型、越权 resume、重复按钮和断线后的审核恢复。[服务端 HITL API](https://docs.langchain.com/langsmith/add-human-in-the-loop)

### F07：认证成功不等于 create_run 获得资源授权

`on_thread_create` 同时装饰 threads.create 和 threads.create_run，写入 `value.metadata.owner` 后返回 None。读取、删除、更新、搜索的 `on_thread_read` 则返回 owner filter。SDK/Server 对这些 action 分别选择 handler，读取过滤不会自动叠加到 create_run。两个动作共用回调不是问题本身，**缺少返回过滤条件才是问题**。

精确锁定后端 0.25.0 的 `Runs.put` 在 `existing_thread and filters` 时才检查 owner。本机 0.28.1 同样如此。离线使用真实项目回调和官方内存 `Runs.put`，只用虚构 Alice/Bob 与系统 Assistant：Bob 的 create_run filter 为 None，成功在 owner=Alice 的已知 Thread 上创建一个 Run；Thread owner 仍是 Alice。没有真实用户、数据库或网络访问。它证明授权合同缺口，而不是一次线上入侵。[官方 owner filter 示例](https://docs.langchain.com/langsmith/resource-auth)

建议创建时设置 owner 并返回 filter；同时审查 cron、batch/stateless、Run stream/wait、cancel/resume、Thread copy/history/state、Assistant 创建/版本/读取和 StudioUser 例外的实际路由。锁定版与当前版对非系统 Assistant 的 Run 授权实现也有差异，不能假定 assistants.read hook 自动覆盖所有版本的执行路径。按运行版本增加私有 Assistant 交叉执行测试。

只改安全回调和对应测试即可先独立修复，不必等待架构迁移；保持 Supabase 验证与业务授权规则。真实 Server 双用户端到端授权仍未运行，必须作为发布门禁。Server 原生 Auth 是扩展框架，不会替项目补上漏写的返回值。

### F08：统一受信身份及业务库的访问范围

Web 的可选常量 Token 是本地单操作者入口，不提供用户/租户区分；默认 host 是 127.0.0.1，对外无 Token 仅打印警告。工作区 history schema 和查询没有 owner/tenant 过滤，导入使用服务端 LangSmith project 凭据读取该项目记录。不能把它直接升级成多用户接口并假定 Server Thread 隔离会保护这些档案。

`memory/context.py` 接受 configurable.user_id/conversation_id 和 metadata.owner；MCP OAuth KV 使用 metadata.owner 作为 namespace。Server 明确注入 `langgraph_auth_user_id` 等受信身份，但 helper 并不直接读取它们。注意：内存后端还会构造 `configurable.user_id`，所以“只给 auth 字段 helper 返回 None”的离线结果**不代表所有真实 Server Run 都没有 user_id**。进一步查锁定后端发现，user_id 合并时调用配置优先于已验证身份；它不是可直接当作安全边界的业务身份来源。

建议新增统一 identity adapter，从已验证 Server auth context 取得身份，拒绝客户端覆盖 owner/tenant/user_id；运行参数仅接受白名单。MySQL 读写、RAG memory 过滤、Neo4j evidence/history、MCP token namespace 必须在服务端带受信范围。Auth Store hook 只覆盖其约定资源，不能保护直接 SQL/Cypher。现有 MCP tokens 已在使用官方 Store，应该修身份传递和密钥保留政策，不再新建另一个 token 数据库。[Store 用户隔离](https://docs.langchain.com/langsmith/store-auth)

修改 `security/auth.py`、`memory/context.py`、`mcp/tools.py`、history/store/RAG 配置边界和研究服务。多用户模式下增加字段及索引属于未来数据迁移，本轮未执行；无法可靠确定 owner 的旧档案进入管理员隔离区。测试伪造 user_id/owner/conversation_id、跨租户研究 ID、LangSmith 导入范围、私有 MCP token、缺少身份时 fail closed，不能只验证 JWT 签名成功。

### F09：保留 MySQL 对话与检索记忆

报告节点调用 `maybe_persist_chat_memory`，提取原始对话、报告摘要、长期记忆，写 MySQL 的稳定 memory_id、record_type、index_status；后台 daemon 线程触发索引，查询时 indexer 再补偿 pending 记录。默认记忆写入关闭。Checkpoint 是图执行快照，Store 是跨 Thread KV 接口；它们没有自动实现原文引用、MySQL 表迁移、pending 批次、向量更新和记忆提取策略。

保留 `memory/`、相关 loader / indexer / citations。需要的身份修复归 F08，后台索引的进程退出风险通过 pending 记录与补偿测试处理；不用新增研究任务队列。若将用户偏好等小型 KV 子集迁到 Store，应单独列 schema、命名空间、检索语义和迁移/回滚方案，不能删除 MySQL 原始档案。测试保持原文/摘要类型区分、稳定 ID、pending 批次标记、查询时补偿、图片临时上下文不写原始记忆。

### F10：保留 Research Graph 与完整 RAG 数据面

Workspace scope 含 research run、角色、轮次和 task；Producer 提取来源/证据/主张并取得 WriteReceipt 后再压缩原始消息；Consumer 与最终报告按范围召回相关子图。Neo4j 查询显式带 run_id，稳定 node/edge ID 与内容版本检查提供一定去重能力。RAG 另有文档切分、结构化 metadata、BM25/向量/图扩展、重排、权威状态过滤、引用和图片解析。

这些是证据质量和上下文控制，不是重复 Checkpoint。官方 Store 可存 JSON，但不自动提供这些关系、搜索、索引和溯源不变量。保留 `research_graph/`、`rag/`、`tools/rag_tool.py` 及领域采集服务。测试继续要求跨 run 不混证据、缺失来源不得编造、写入失败不得压缩掉原文、Consumer 无 RAG 时仍能利用已有图证据；资源生命周期问题见 F16，用户范围见 F08。不要把未来多实例数据隔离要求当作删除图存储的理由。

### F11：Server 的耐久边界止于已保存的业务步骤

一个角色节点内部创建 `messages`、budget_update、Workspace receipts，然后循环模型 → 工具 gather → ingest，最后才返回 patch。中途未提交的普通 Python 变量无法仅靠 Thread Checkpoint 还原到某次模型调用之后。重启/节点重试可重新执行该节点内前面的模型/工具；工具安全包装把 Exception 转成观察文本，意味着部分工具失败在图层仍可能被视作成功运行，不能依赖 Server 自动重试它们。

现有图存储的稳定键、source version 去重、MySQL upsert 降低重复写风险，但不能撤销模型账单、保证外部 MCP 写操作幂等，也不覆盖章节 `asyncio.gather` 中成功结果尚未提交便遭其他章节失败的情况。不是所有内层循环都必须马上拆，先明确可接受的重放范围。

实施建议是保留四角色和 Workspace 接口，逐步把确需耐久的模型步骤/工具批次结果作为序列化子图 State 或受框架支持的 task 结果提交；以实际锁定版验证任务 API。副作用使用 research_id/task_id/tool_call_id/operation_id 的稳定幂等键，不能只依赖随机新 tool call ID。改 `agent_runtime.py`、`business_agent.py` 和必要的子状态定义，隔离为独立阶段实施。增加模型后、工具后、图写后、patch 前的故障注入，断言重复调用次数和预算处理均符合显式合同。任何 exactly-once 的宣称必须包含外部系统验证。

### F12：报告档案不能绑在读取 SSE 的请求上

当前工作区新增的 archive_run 有实际业务价值：完成、失败、取消、未生成报告时保留主题、参数、usage 和已完成角色输出；save 使用稳定 HISTORY ID 和 Neo4j MERGE，且不让后续失败版本覆盖已有完整报告。测试验证了嵌套子图后续失败时保留前面已结束角色结果。不能因 Server 有 Run history 就删除这套档案。

问题是触发点属于 Web 流消费者。Agent Server 原生入口不经过它，HTTP 断开、进程崩溃或 archive 失败后的再投递也没有统一可靠协议。LangSmith import 是历史补救，并依赖 Trace 保留/项目权限，不应成为主执行恢复来源。

建议把档案写入改为独立于客户端连接的服务端幂等投影：成功报告可在图侧已保存结果后触发；所有终态通过受保护的 Run webhook / 状态对账补齐，并从 Checkpoint 的已完成角色 state 取资料。不要只依赖 webhook 送达或只保存最终报告；中断审核不能归为失败，partial stages 需要独立键和合并规则。[Run webhook](https://docs.langchain.com/langsmith/use-webhooks) 支持通知，但业务归档接收、授权与幂等仍由项目实现。

保留并重整 `web/history.py`，新增 archive adapter / reconciler，原 UI warning 降级仍保留。测试归档故障后报告可读、无 HTTP 订阅仍归档、重复通知不重复、不同 resume 的阶段合并、跨用户导入、部分成功后失败、取消与完成竞争；真实 Neo4j 和 Server 对账需要集成环境。

### F13：保留官方 usage 事实上的业务预算策略

当前 `budget.py` 已用 `UsageMetadataCallbackHandler`、`AIMessage.usage_metadata`、`add_usage` 和官方模型 start 生命周期，而非另写 provider token parser。项目 `ModelAttemptCounter` 是自定义回调子类，统计框架发出的真实 attempt 事件；不是另一个 retry 引擎。`structured_output_chain` 使用 include_raw envelope + validation + `with_retry`，让解析失败重试覆盖真实模型调用，且能统计此前响应的 reported usage。现有失败/重试/并行/嵌套用量单测已通过。

业务层仍需决定 max_model_calls、tool/search 预算、最终报告预留、降级理由和 reducer 增量语义。LangSmith 可以展示 usage，但不能替代执行中的预算决策，采样/上传失败更不能让策略失效。`observability/langsmith.py` 只为自动追踪以外的 RAG 阶段建立 Span，并不再次包装每个模型/工具节点；本次未发现应删除的完整自建 Trace 数据库。[LangSmith](https://docs.langchain.com/langsmith/observability)

保留这些边界，补 F05/F11 的中断与失败状态记账测试。末次源码已在章节 writer 批次启动前使用 `remaining_model_calls` 分配额度，并保留完整章节计划；这属于合理的业务预算控制，不应删除。并行角色可能仍在同一预算快照上获准，默认预算也关闭，因此不能无条件称为跨角色原子硬限额。若业务要求绝对上限，另设计预留/结算语义，而不是把 `N_JOBS_PER_WORKER` 或通用单 Agent call-limit middleware 当成等价替代。没有 provider usage 时不得用估算数伪装真实计费事实。

### F14：保留多 Agent 业务协作和状态所有权

`public_opinion_builder` 同时启动公众信号、内部知识，汇合后复核；缺口任务仅发给允许的两个取证角色，按角色分组 Send，过滤重复/已完成任务和无效任务，并受 research rounds 安全上限限制；随后风险评估、回应策略执行。角色 memory、rolling_summary、正式 report 和 Research Graph 上下文各有所有权。runtime/state reducers 提交的是增量，不是另一套 Run 调度数据库。

LangGraph 的节点、Send、reducer 已经是执行这些策略的原生能力；Agent Server 排队的是外部 Run，不能替代“证据足够才进入风险分析”的业务决策。保留 `workflow/research.py`、`public_opinion_agents/`、State schema、角色域白名单、Research Graph strategy。继续通过复核充分/不足、单角色/双角色补充、轮次上限、任务去重、上下游正式报告完整性测试，不把业务 task_id 与官方 Run ID 合并。

### F15：通用工具 primitive 可评估，整个 Runtime 不宜替换

工具循环确实包含 ToolNode/create_agent 常见职责：解析 tool_calls、并发执行、未知工具反馈、ToolMessage 回填、异常观察。项目已直接复用模型 `bind_tools` 和 `with_retry`；不能重复建议实现这些基础能力。`business_agent._execute_tool_safely` 捕获 Exception、让 CancelledError 等 BaseException 传播，并在 finally 恢复预算 ContextVar，这与任何候选 ToolNode 的默认 handle_tool_errors 都不应假定相同。

本项目额外依赖：Producer 原始批次边界、native provider web search 检测、逐工具成功标记、跳过工具的合成消息、Workspace ingest 顺序、WriteReceipt 后压缩、保护上下文、最终角色报告协议和独立 memory。通用 SummarizationMiddleware 不能直接替代“材料落图后才能裁剪”的证据不变量。[Agents](https://docs.langchain.com/oss/python/langchain/agents)、[Middleware](https://docs.langchain.com/oss/python/langchain/middleware/built-in)

目前没有证明直接替换整个 Runtime 能保持全部语义，结论是 B/P2、先保留。若将纯工具分发抽成 ToolNode，文件范围限于 runtime 两文件及适配器，先在精确锁定版验证 error、顺序、return_direct、并行预算隔离、未知工具、native search、取消和 ingest 回执合同；差异不能消除则不迁移。没有理由借此重写四角色架构。

### F16：处理业务 I/O 与全局缓存的生命周期

`research_review`、报告证据读取和 Workspace hook 在 async 节点内执行同步 Neo4j 查询/写入；Neo4j store 初始化还执行 schema queries。README 明确用 `langgraph dev --allow-blocking`，说明当前开发命令允许这类行为，并不证明正式 worker 不受阻塞影响。RAG query 和 memory 写入已有 `asyncio.to_thread`，这些现有正确边界应保留。[官方同步阻塞建议](https://docs.langchain.com/langsmith/agent-server-scale)

`create_research_graph_store` 全局 `_STORE_CACHE` 按连接信息和 run_id 永久缓存，每个 Neo4j store 建立 driver；不同研究持续增加缓存项。`clear_research_graph_store_cache` 未见生产调用，只见测试/导出；Web lifespan 仅释放 history pool。RAG pipeline cache 同样无容量边界，包含会话配置的 cache key 可能持续增多。Server 重用编译图不会自动关闭这些模块全局资源。

建议把连接池按受信连接配置复用、数据查询仍强制 run/tenant scope；run-local Workspace 和 receipts 随执行结束释放，缓存设容量/TTL。同步数据库边界选择 async driver 或 `to_thread`，不能仅靠调大超时/允许阻塞来掩盖。`BG_JOB_ISOLATED_LOOPS` 是可选运行缓解且改变并发/线程模型，应先验证 shared locks、数据库池容量和全局资源安全。改 store / strategy / reporting / research 与应用生命周期，新增大量不同 run 后缓存和 driver 数有界、关闭只执行一次、两个租户并行不串状态、阻塞检测测试。

### F17：已编译图可替代无动态用途的 Factory

factory 丢弃 config，每次重建相同节点与边，且模块加载时已创建 `deep_researcher_graph`；模型、工具和 Workspace 的实际选择发生在节点内，不依赖 factory 动态改图。当前存在额外编译开销，代码量不大，优先级 P2；没有测量证据证明这是主要延迟来源。

0.7.31 loader 源码和本机 0.8.7 离线探针都接受 compiled Pregel。官方推荐静态图导出，factory 用于确需每次动态构图的场景。[Graph loading](https://docs.langchain.com/langsmith/agent-server)、[Runtime rebuild](https://docs.langchain.com/langsmith/graph-rebuild)

建议 langgraph.json 注册稳定 compiled export；factory 先保留兼容入口，再按公开 API 政策退役。更新 `test_langgraph_runtime_compat.py` 中“必须是 factory”的实现形状断言，改测原生 Pregel、loader 加载、并行不同配置、注入 Checkpointer/Store 和 tracing 行为。不能看到测试锁定 factory 就把它当成产品必需功能，也不能直接删除深层节点的业务准备。

### F18：明确研究会话、Run 和 Trace 的映射

Web 的 thread_id 同时作为 `HistoryRecord.run_id`；Research Graph 优先用 state.research.run_id，再退到 configurable.thread_id；logical_run_id 则从 thread_id 派生。另有 12 位 trace_id 只用于错误日志，既非 Server Run UUID，也没有被设置为 LangSmith root trace ID。这在“一 POST 一研究”的当前入口可工作，但名称会让用户和运维错误查找；多次 resume、同 Thread 新研究时风险更明显。

建议保留兼容的业务 `research_id`（原 history.run_id 可暂作 legacy alias），一个 Thread 对应一次可多次恢复的研究会话，每次调用有独立 server_run_id；LangSmith trace ID 从实际 trace context 关联。State、Neo4j、history、UI 只使用明确的 ID 类型，不把随机日志 ID 对外标为可点击的 LangSmith trace。改 state_utils、correlation helpers、history schema/导入和 UI；测试多次 resume 同研究、重新开始生成新 Thread、旧档案 ID 稳定和关联查询。没有必要为此建立第二套技术 Run 主表。

### F19：请求限制必须依据实际接收字节

middleware 只读取 Content-Length，缺失时按 0；Pydantic 限制 topic/org_context 但默认忽略额外字段。离线 TestClient 向真实 FastAPI app 发送 70,041 bytes 的分块 JSON，内容包含合法 topic 与无用大字段：没有 Content-Length 的请求返回 200；相同固定长度请求返回 413。Graph、history 全部替身，未访问数据库或模型。

建议明确由网关或 ASGI 层计实际接收字节、限制未知字段和模型/配置输入，保留现有不回显敏感输入的 validation response、constant-time token 比较和 DOMPurify fail-closed 渲染。若依赖反向代理，需版本化配置并端到端验收，不宣称应用 middleware 已独立保证该限额。跨域 UI 迁移时再配置精确 CORS origin，当前同源模式未配置宽松 CORS，不构成发现。[Server HTTP 安全配置范围](https://docs.langchain.com/langsmith/custom-auth)

改 server middleware/网关、ResearchRequest 和负向请求测试；覆盖 chunked、缺失 Content-Length、超大未知字段、边界长度和合法请求的 body 可重读。用户图片本地路径与运行参数中的知识库/数据库/MCP URL 也应限定到受信配置；当前 loader 明确拒绝远端图片 URL，但未提供多租户本地路径 allowlist，公共部署不能放开任意配置输入。

### F20：原始 transcript 需要明确用途、权限和保留期限

`ResearchTranscript(directory=None, run_id)` 仍使用 `.tmp/runtime/transcripts`；graph-enabled strategy 下会创建 sink 并 append 原始材料/压缩事件。它既非 Checkpoint，也非前端完整 replay；本地 JSONL 没有事件资源授权、保留周期或多实例共享约定。LangSmith 已自动追踪模型/工具，部分正文可能重复，但原始采集材料可承担业务溯源、调试和压缩前证据保存职责。

先规定哪些材料必须长期保留、哪些仅诊断，新增显式开关、大小/期限限制、去敏、受控目录和访问规则；仅需技术诊断时优先用现有 Trace，业务证据则用稳定 source ID/reference 避免四处复制。注意 correlation metadata 不含用户正文，不能据此推导自动 Trace 的 inputs/outputs 也已去敏。

涉及 transcript、strategy、observability 配置与文档；测试工具结果含模拟密钥、个人信息、长内容时的保留/去敏规则，删除或禁用诊断 sink 不损坏模型上下文和图证据。此项不会建议直接删掉原始证据；PR #14 的 durable trajectory 另见 F22。

### F21：生产恢复与许可必须有可复现部署证据

仓库有 langgraph.json 和开发命令；`compose.neo4j.yaml` 只证明领域数据库配置，不证明 Agent Server PostgreSQL/Redis/worker 已部署。CI 用 `uv sync --locked`，本轮使用漂移且 `pip check` 失败的系统环境。末次 454 个离线测试是有效的本机回归证据，但覆盖不了正式数据库恢复、MCP 可用性、用户 JWT 或许可配置。

上线前保存经过选择的 Python/CLI/API/SDK/镜像版本、运行模式、数据库/Redis卷、Checkpointer/Store 后端、worker 数、Run 超时/重试、stream TTL、checkpoint/thread TTL、认证和网关配置；在干净隔离环境按现有 lock 重建，不默默把审计结果当作批准升级依赖。若锁定版不支持目标需求，升级应成为独立提案和回归任务。

当前官方文档包含比锁定 API 更新的部署模式、后端替换和双栈配置，不能整份照搬；库 1.2 的流例子也不适用于 lock。正式 Server 的许可、备份与容量若不符合项目约束，可暂保留直接执行架构并明确它的本地单操作者保证；但满足生产耐久目标时，还需显式 Checkpointer、可靠任务执行器和授权，不是保留现状即已达标。[dev/up 边界](https://docs.langchain.com/langsmith/local-dev-testing)、[部署要求](https://docs.langchain.com/langsmith/deploy-standalone-server)、[TTL](https://docs.langchain.com/langsmith/configure-ttl)

### F22：PR #14 需要独立的合并前架构决策

只读核对 PR head `41e3ef16`：`ResearchExecution` 使用独立 asyncio task 运行图，HTTP 读取者订阅结果；显式 Stop 取消该 task；仍持有本进程 Semaphore，并用 `astream_events` 采集事件。trajectory lifecycle 自行关联模型 attempt、tool call/result、step/turn 终态；JSONL persistence 具有 writer lease、损坏修复/读取和保留等职责。WriterLease 是文件写者互斥，不是用户授权。当前工作区不存在这些模块，不能把它们计入现有 Web 的功能。

其 task 生命周期、队列、取消、Run 状态与官方 Run 管理高度相近，合并前应与统一入口方案协调。持久化 UI 轨迹、token 片段重放和业务事件则不一定被短期流缓存或采样 Trace 覆盖，不能全部删除。建议由官方 Run/Thread ID 和流事件驱动投影，只保留已确认有长期展示价值的事件与存储；特别验证 attempt/嵌套 tool 配对和服务器重试事件是否可获得。

处置属于 D/P2：评审 PR 内 `web/research.py`、trajectory service/session/recorder/persistence/lifecycle 和 React reducer 的合同，再决定替换哪部分。原分支文件锁/单进程 task 不等于多实例 Run 所有权和崩溃继续执行；展示“durable ledger”也不能直接称任务已可恢复。本轮没有测试该 PR，也没有合并或改动它。[PR #14](https://github.com/kkaaakk/public_opinion_research/pull/14)

### F23：保留离线图、评测与压测专用执行器

`tests/run_evaluate.py` / debug 重新 compile MemorySaver 是为了独立测试会话；benchmark/cost_profile 的 wait_for 是 case 时间限制，round2 的 checkpoint_loop 写结果文件，是评测进度快照，不是 LangGraph checkpoint 或线上 job queue；Locust/agent_stress 的并发是流量产生方式。它们不经 Server 是合理的测试隔离。

保留这些入口，同时标清哪些会访问真实模型/搜索/数据库并保持显式 integration 门禁；生产 UI 统一入口不要求单元测试依赖网络。新增 Server E2E 专项覆盖运行保证，保留无 Server 的业务合同测试和故障注入。源码 `.ainvoke()` 出现在评测里不能直接算重复运行基础设施。本轮未运行实际 evaluate/benchmark/provider smoke，它们可能产生付费请求。

### F24：兼容导出仅标记弃用候选

静态引用和图注册检查未发现 intake 的 `_messages_without_query_image_context`、`budget_skip_tool_message`、registry 的 `get_research_tool_prompt`、`get_notes_from_tool_calls` 被生产链调用；其中若干通过 `deep_researcher.py` 或 `utils.py` 兼容导出，静态 prompt helper 还明确标注 deprecated。memory/writer 的同名图片过滤 helper 有实际调用和测试，不能因名字相同顺手删除。

图节点均由 builder 注册，不能仅用普通函数引用数量判死代码；本次上述候选没有节点注册。仍缺少仓库外调用者/发布 API 合同证据，所以 D/P2、先记录弃用候选。未来删除需更新 package boundary 测试、示例与发布说明，并确认配置动态加载和外部导入不依赖它们；无需为这些 helper 引入官方替代层。

## 7. 保留能力清单

| 能力 | 保留理由 | 可优化的边界 |
| --- | --- | --- |
| 四角色 AgentSpec、领域权限、行业/组织上下文 | 定义舆情分析业务、输入输出契约和可用工具 | 使用受信身份、统一运行关联 ID |
| 复核与缺口驱动补充研究 | 决定取证质量、轮次、任务去重与角色路由 | 用原生 Send/reducer，保持现有业务顺序 |
| Research Graph 的 Source/Evidence/Claim/Finding/冲突/覆盖 | 证据关系和溯源无法由 Run 状态替代 | 幂等、租户隔离、异步 I/O、连接复用 |
| WorkingContext、Producer/Consumer、写入回执后压缩 | 保证有效证据留存和上下文可控 | 单独证明通用压缩器满足不变量才替换 |
| RAG、BM25/向量/图扩展、重排、权威调整和 citations | 负责知识检索与答案依据 | 索引 lifecycle、缓存容量、用户 scope |
| MySQL 原始聊天/摘要/长期记忆与 pending 索引 | 跨研究历史和原文引用；区别于 Checkpoint | 小型偏好 KV 可另评估官方 Store |
| 报告业务历史、阶段输出与参数复用 | 用户档案和追踪过期后的结果保留 | 与 HTTP 解耦，以 Run 状态/Checkpoint 为执行事实来源 |
| 业务预算、最终报告预留、工具/搜索成本规则 | 是成本和质量策略，不是 Trace 的重复 | 官方 callbacks 为事实；明确并行硬限制和重放结算 |
| LangSmith 自动 Trace 与手工 RAG Span | 已复用官方系统，补充无法自动追踪的逻辑段 | 去敏、身份、完整父子链和失败可降级 |
| MCP OAuth、命名冲突、领域过滤和单服务器故障隔离 | 是工具集成和业务授权 | 复用已有 Store，修身份；恢复锁定 adapter 兼容 |
| Tavily 原始批次、社交/投诉/传播采集、Apify 服务 | 领域研究与证据转换 | 保持工具层超时/权限与幂等，勿迁成 Run scheduler |
| 模型 provider factory | Ark/DeepSeek 等参数、响应与密钥路由适配 | 与运行调度解耦，保留 provider 合同测试 |
| UI 托管、参数验证、Markdown 净化、业务展示事件 | 是面向用户的产品协议与安全边界 | 薄 API + SDK；明确连接和 Run 状态 |
| 独立评测和压测工具 | 隔离、可复现与模型成本评测 | 增加 Server 集成层，保留离线测试层 |

## 8. 推荐目标架构与决策记录

### 8.1 可实施的统一架构

```mermaid
flowchart TD
    UI["业务前端：提交、进度、审核、Stop、重连、历史"] --> BFF["薄业务 API：身份、参数白名单、ID 映射、展示适配"]
    BFF --> SDK["官方 LangGraph SDK"]
    SDK --> API["唯一生产运行入口：Agent Server"]
    API --> AUTH["Supabase / 企业身份 + 资源 owner/tenant hooks"]
    API --> QUEUE["Run 队列、worker、同 Thread 策略、取消"]
    QUEUE --> WF["现有研究 Workflow + 独立 plan review 节点"]
    WF --> ROLES["现有四角色、ReAct / Workspace / 研究复核"]
    ROLES --> TOOLS["RAG / MCP / Social / Search"]
    ROLES --> GRAPH["业务 Neo4j 证据库"]
    WF --> MEMORY["业务 MySQL 记忆及索引"]
    QUEUE --> CP["Server 管理 Checkpointer / Store：持久化后端"]
    WF --> TRACE["LangSmith；官方 usage callbacks + 业务预算"]
    API --> EVENTS["官方 Run 流；可重连的临时事件缓存"]
    EVENTS --> BFF
    API --> ARCHIVE["授权的幂等档案投影 / Run 终态对账"]
    CP --> ARCHIVE
    ARCHIVE --> HISTORY["业务报告/阶段历史；明确用户与保留策略"]
    BFF --> HISTORY
    OFFLINE["离线测试/评测：直接调用图"] -.-> WF
```

推荐以一个 Thread 对应一次研究会话；澄清与审核恢复继续该 Thread，重新开始研究创建新 Thread。研究业务 ID 稳定，Run ID 每次调用不同。跨研究的用户记忆在业务记忆/Store 中传递，不依赖把多个独立研究硬塞进同一个证据 scope。

薄 API 保留 UI 托管、请求白名单、用户身份、业务组织配置、研究 ID / 幂等请求映射、受保护历史查询/导入、流展示适配和归档接收/对账。它**不**再编译图、持有正在运行的 graph task、维护权威 pending/running 状态或研究执行 Semaphore。技术状态以 Server 为准，业务 API 只保留业务引用与投影，避免新建第二套 Run 数据库。

原生 REST/SDK 可直接用于可信 Studio 或管理端；面向业务用户不开放任意 configurable 数据库 URI、MCP endpoint、文件路径或身份字段。若 BFF 使用服务凭据，它必须将已验证身份安全传递给 Server 并保留资源授权，不能让所有用户都变成同一管理员。优先按请求转发用户 JWT 或采用经过明确设计的可信身份交换。

### 8.2 数据所有权与故障语义

| 数据 / 操作 | 唯一执行事实来源 | 业务侧保留 |
| --- | --- | --- |
| pending/running/success/error/timeout/interrupted Run | Agent Server Run API | UI 状态翻译与关联引用，不能自行宣布 Run 成功 |
| Thread state、暂停点、可恢复图任务 | Server Checkpointer | 计划版本和业务 State schema；不把报告 JSON 当 Checkpoint |
| 跨 Thread 小型 KV / MCP token | 官方 Store（若采用） | 用户 namespace、权限、过期、密钥政策 |
| 原始记忆、知识索引、证据关系 | MySQL / RAG / Neo4j | 全部领域模型、引用和隔离规则 |
| 可下载报告、失败阶段、业务检索 | 业务 history | 长期档案、搜索索引、业务状态映射、旧记录兼容 |
| 临时重连事件 | Server stream cache | UI 游标和去重；缓存过期读 state/history |
| 长期原始轨迹（若确有需求） | 专门业务轨迹投影 | 去敏、保留和 replay；不能冒充运行恢复器 |
| Trace / Span | LangSmith | 受限关联 metadata、非自动捕获的逻辑 Span |
| 模型尝试与 reported tokens | 官方 callbacks | 业务预算增量、预留/结算、缺失 usage 标识 |

Run interrupted 应再区分“计划等待审核”和“用户停止”：前者 UI 显示审核并允许反馈，后者允许用户选择从 checkpoint 继续；不要把任一状态自动映射成 completed。服务端 error/timeout 的 partial state 可供业务档案保存，但不等于一次成功研究。

外部工具调用采用可接受的至少一次/幂等合同。Run cancel 只停止后续执行，rollback 只回滚 Server 管理的相应状态，不能清除已经进入业务证据库的所有外部副作用。是否保留取消前证据按业务档案规则决定。

### 8.3 拟议 ADR（本报告内记录，状态均为 Proposed）

| 决策 | 背景和建议 | 替代方案与取舍 | 落地门槛 |
| --- | --- | --- | --- |
| ADR-A：Server 作为生产唯一运行入口 | 两个入口的 Run/Checkpoint/取消保证不同；统一到原生服务 | 继续自建可减少部署依赖，但需自己承担可靠队列、持久化、授权和运维成本 | 精确版本与许可符合约束；无真实模型的正式后端验收通过 |
| ADR-B：保留薄业务 API | 保留输入/身份白名单、档案和兼容 UI | 前端直连更少代理，但增加配置/权限暴露，并不能替代档案后端 | 身份链完整；API 不维护第二套运行权威状态 |
| ADR-C：业务存储与执行存储分离 | Checkpoint、证据、原文记忆和报告生命周期不同 | 全迁 Store 表面统一，但破坏关系查询、索引和档案兼容 | 各库 scope、备份、保留、ID 映射明确 |
| ADR-D：保留业务 Harness，分阶段加耐久点 | ReAct 与通用 agent 部分相似，但业务证据不变量不等价 | 直接用 create_agent 会减少通用代码，代价是大范围行为重证 | 等价合同、故障注入、usage 与工具权限回归通过后才局部替换 |

这些决策供后续实现评审，不将报告建议伪装为已批准上线。若正式 Server 的许可、运维成本与项目约束冲突，ADR-A 可不接受；其他安全、HITL 和身份修复依然可以独立进行。

## 9. 具体改造方案：文件、接口、部署、迁移与回滚

### 9.1 文件级清单

以下“新增/修改/删除”都是后续实施清单；本轮没有执行。

| 文件 / 目录 | 动作 | 明确修改内容与保留内容 | 前置依赖 / 验证 |
| --- | --- | --- | --- |
| `src/security/auth.py` | 先修改 | create_run 返回 owner filter；受信 tenant/user；明确 Studio 管理员及共享系统 Assistant 策略；防止客户端重写安全字段 | 独立安全测试，可先于所有迁移 |
| `workflow/intake.py` | 先修改 | planner 返回 draft/usage 后结束节点；新增纯审核节点；保留降级、反馈 prompt 与 True/str 合同 | F05 的真实图离线 fixture |
| `workflow/graph.py` | 修改 | 注册审核节点和路由；验证后使用 compiled export；保留与旧脚本兼容的 builder/factory 过渡导出 | 老 Checkpoint 处理策略先确定 |
| `state.py` | 按需增加字段 | plan version/hash/approval、明确 research_id；保持六个域和 delta reducers | 序列化、旧状态兼容、并行 reducer |
| `deep_researcher.py`、`workflow/__init__.py` | 小范围修改 | 保留外部入口；导出新审核节点/compiled graph；不重复实现节点 | package boundary、CLI loader |
| `web/agent_client.py` | 新增 | 官方 SDK 客户端与生命周期；按受信请求传 JWT/身份；不自行实现 Run HTTP 协议、SSE decoder 或队列 | 连接、认证、超时策略；fake SDK 合同 |
| `web/research_service.py` | 新增 | 参数映射、单次提交、Thread/Run/research ID 关联、授权的 get/cancel/resume；请求幂等引用 | ID、身份和 SDK API 基线 |
| `web/stream_adapter.py` | 新增 | 官方事件映射为现有 status/report/usage/warning/history_saved/done；识别 interrupted；游标/去重；数据白名单 | 记录上游 API/事件格式，不盲用库 v2/v3 样例 |
| `web/server.py` | 替换并删除局部代码 | 路由调用 service/adapter；删除 `_deep_researcher_factory(...).astream()`、本地研究 Semaphore 和响应生成器内的权威执行 lifecycle；保留 UI、请求校验、业务历史路由、稳定错误响应 | 新旧路径只能选择一个，不能双执行 |
| `web/history.py` | 保留并修改 | 分清 research/thread/server_run/trace；owner/tenant；幂等阶段合并；独立归档与兼容旧 ID | 现有文件未跟踪，实施前先归入正常版本管理流程 |
| `web/archive_service.py` | 新增 | 限定字段的档案投影、成功/失败/取消/interrupted 映射、Run 特定 checkpoint 读取、Webhook 接收与对账 | Server 终态与业务 DB 权限；不建立另一套执行队列 |
| `workflow/archive.py` | 按需新增 | 在有已保存结果的图侧触发成功报告/阶段档案；写入幂等、失败可补偿，不能令报告因历史故障不可读 | 与 archive_service 共享业务写入合同 |
| `web/static/app.js` | 修改 | 提交获取 ID；订阅/重连；Stop 调 cancel；审核 UI/resume；EOF/错误状态；历史仍可查询/下载/沿用参数 | 单一 active Run、请求竞争和 DOMPurify 回归 |
| `web/static/index.html`、`style.css` | 必要修改 | 排队、等待审核、取消中、恢复与重连状态控件 | 保持 RAG 开关、组织上下文、已有报告交互 |
| `memory/context.py`、`memory/writer.py`、`memory/store.py` | 保留并修边界 | 从受信 identity 取 scope；原始记忆、stable ID、pending 索引不变 | 双用户读写、旧记录归属 |
| `mcp/tools.py`、`domain_filter.py`、`tool_wrapper.py` | 保留并小改 | 修 token namespace 身份；继续官方 Store、权限和故障隔离 | exact adapter/MCP 组合；模拟 token 过期/越权 |
| `research_graph/store.py`、`workspace.py`、`strategy.py` | 保留并修改 | async/to_thread；连接配置级 pool；run/tenant 查询；有界缓存；副作用幂等和回执 | 并发、关闭、失败写入、重启重放 |
| `workflow/research.py`、`reporting.py` | 保留并修改边界 | 移除 async 路径上的同步数据库阻塞；报告/复核逻辑不变；必要时将写作批次细化耐久点 | 多角色顺序、完整正式报告与成本回归 |
| `runtime/agent_runtime.py`、`business_agent.py` | 首轮保留；后续分阶段修改 | 不直接换整个 ReAct；在确需耐久的边界序列化消息/工具结果/预算/回执；小 primitive 替换须证明等价 | 故障注入、工具副作用与 usage 合同 |
| `budget.py`、`observability/`、`models/` | 保留，必要关联调整 | 官方 usage / tracing 不重写；修标识、重放与失败结算 | 无 Trace 时预算不变；callback attempt 对账 |
| `research_graph/transcript.py` | 修改政策 | 显式启用、去敏、大小/保留、受控访问；必要时以证据引用取代重复全文 | 不损坏证据留存和模型上下文 |
| `langgraph.json` | 修改 | stable compiled export；按选定版本配置认证、HTTP/生命周期、Webhook headers/destination；保留 graph ID 兼容策略 | 实际 loader/config schema 验证 |
| `compose.agent-server.yaml` / Helm values 等部署文件 | 新增一种选定方案 | 固定 API image/version、Postgres/Redis、数据卷、worker、健康检查、备份、许可、网络；不只添加 Neo4j | 正式后端无模型 E2E、故障恢复 |
| `.env.example`、README、运维文档 | 修改 | BFF Server URL、认证、并发/超时/重试/stream TTL、运行模式、保留期限；使用占位值 | 不写入真实密钥；明确 dev 不作生产承诺 |
| `pyproject.toml` / `uv.lock` | 首轮不升版本 | 如 BFF 直接依赖 SDK，可在独立实施提交中显式声明现有已选版本；需要升级则另立提案 | 本轮不改；锁定环境全量回归 |
| 现有 `tests/test_web_*`、runtime / token / graph tests | 修改与保留 | Web 从 fake graph 改 fake SDK；继续测业务字段/档案；factory 形状断言改行为合同 | 不因迁移删除现有业务回归 |
| `tests/test_auth_resource_isolation.py` | 新增 | 双用户 Thread / Run / Assistant / Store 资源矩阵；伪造 identity/config | 离线 handler + 真实 Server 两层 |
| `tests/test_plan_feedback_resume.py` | 新增 | 实际 planner 节点 + MemorySaver/持久化后端，批准/修改/重启不重复 | 无真实 LLM |
| `tests/integration/test_agent_server_lifecycle.py` 等 | 新增 | 正式后端排队、cancel、重连、worker kill/restart、归档和 auth | integration 标记；fake model/tool graph |
| `tests/run_evaluate.py`、`evaluation/`、Locust | 保留，补 Server 场景 | 继续直接图的离线/模型评测；压测增加返回 ID、显式停止、终态对账 | 实际模型评测仍需单独付费授权 |
| PR #14 / #12 文件 | 暂不改动 | 分支合并前对齐新 service 和事件合同；不把其他 worktree 改动带入本次审计 | 独立 PR 评审、功能/数据冲突分析 |

**明确删除范围**：新路径验证后，删除生产 Web 的研究调度 Semaphore、本地 graph 执行循环和以客户端连接代表任务状态的逻辑；后续确认无消费者再退役无用 factory/helper。没有证据支持整目录删除 runtime、memory、research_graph、observability 或 history。不会先删除旧功能再用后续研究填空。

### 9.2 推荐 API 合同与运行参数

| 业务 API | SDK / 官方资源 | 行为合同 |
| --- | --- | --- |
| POST `/api/research-runs` | `threads.create` + `runs.create` | 一次接纳返回 research_id/thread_id/run_id；请求 ID 防重；只接受受信业务参数 |
| GET `/api/research-runs/{id}` | `runs.get` + 指定 checkpoint/state | 返回 Run 技术状态与业务展示状态；暂停不是失败 |
| GET `/api/research-runs/{id}/events` | `runs.join_stream` | 订阅已存在 Run；`cancel_on_disconnect=False`；不可因为重连再次 create |
| POST `/api/research-runs/{id}/cancel` | `runs.cancel(..., wait=True, action="interrupt")` | 支持 pending/running；重复/终态请求有明确响应；保留 partial state |
| POST `/api/research-runs/{id}/resume` | 同 Thread `runs.create/stream(command=...)` | 校验 owner、暂停原因和 plan version；新 Run ID 仍属于同一研究 |
| `/api/history*` | 业务 Neo4j + 归档服务 | 继续搜索、阶段查看、参数复用；强制 owner/tenant；不能任意导入整个他人项目 |
| POST 受保护的 Run webhook endpoint | Server webhook + 状态读取核验 | 外来 payload 仅作为通知，核验 Run/Thread/身份并幂等投影，不信任任意配置字段 |

旧 POST `/api/research` 可在过渡期继续返回现有 SSE：内部调用同一个 submit service **一次**，再订阅该 Run。新前端切换至接纳 + 订阅模式后退役旧协议；不能在超时后再启动本地 graph 作为 fallback。UI 的业务参数及报告内容保持兼容，协议变更需版本化说明。

SDK 0.3.5 的 `runs.stream` 使用 `on_disconnect`，`runs.join_stream` 则使用 `cancel_on_disconnect`；两者参数名不同，不能复制错位置。创建 Run 时显式设置 stream modes、`stream_subgraphs=True`、`stream_resumable=True`，以已有版本实测最早事件缓冲和游标传递；若 SDK 事件模型不暴露所需游标，先验证官方协议/客户端组合，不编造本地游标去请求上游重放。

初期 worker 容量可保持单 worker 2 个 Run 槽；工具/角色内部并发不由该值控制。Run 超时、最大重试、stream TTL 和 checkpoint/thread TTL 均显式配置，不用漂移环境默认值。需保证用户审核允许等待的时间不会被 Thread/Checkpoint TTL 提前删除；流缓存可以短期保留，业务档案另设期限。[TTL 配置](https://docs.langchain.com/langsmith/configure-ttl)

模型 retry 继续使用已覆盖的 `with_retry`；上下文超限后的缩减属于业务恢复，不直接换成同输入通用重试。可选节点 RetryPolicy 只用于已经证明安全的 transient 错误，认证、参数错误和不可幂等写入不得盲目重放。Server 自身 worker 重试与模型 retry 分别统计，不混为一次成功模型调用。

### 9.3 实施顺序与依赖

1. **固定审计/实现基线**：保全当前修改并在单独实现提交中纳入 history 等新增文件；确定以当前工作区、远端 main 还是其他已批准分支为改造基线。核对 PR #12/#14 的整合顺序；不用 worktree 名称自动择代码。建立锁定的隔离运行环境，保存 API/SDK/镜像版本及正式部署选择。
2. **先修独立正确性**：F07 授权过滤、F05 规划/审核拆分、F19 实际 body 限制；完善 F08 受信身份。离线复现变成防回归用例，不依赖 Server 迁移才修安全问题。
3. **建立无模型的正式 Server 验证栈**：用测试专用 graph/model/tool，确认原生 Thread/Run、Checkpointer/Store、流缓存、队列、取消、JWT 与 Run 故障恢复。许可或配置不满足则暂不切生产。
4. **引入薄 SDK 适配和归档投影**：service/stream/archive 模块先以 fake SDK 与正式测试栈覆盖，再接入现有业务 graph；保留业务参数映射，显式 IDs 和档案 scope。归档从具体 Run checkpoint 取值，不能在后续 Run 已执行后无条件拿 Thread 最新状态冒充前一 Run 结果。
5. **修资源边界并切流量**：处理同步 Neo4j 与全局缓存；新研究统一走 Server，客户端断线继续订阅；Stop 真实取消。旧直调请求先排空；同一个 research_id 永远只能有一个执行后端。
6. **删除已替代的局部基础设施**：去除研究 Semaphore、直调循环、冗余 factory 形状约束；保留兼容 API 的只转发部分。确认 UI、业务档案、权限、usage 和 Trace 对等。
7. **后续细化耐久与 Harness**：根据故障注入结果逐步细化 ReAct/写作节点；只有语义等价时采用 prebuilt primitive。与 PR #14 对齐长期轨迹需求，避免再建另一套 task lifecycle。

依赖顺序为：基线与身份 → 授权/HITL 正确性 → Server 运行栈 → SDK/事件/归档 → 业务切流 → 删除 → 内层耐久优化。节点拆分与 graph export 修改涉及旧 Checkpoint 的 pending node 名/interrupt 次序；必须先处理旧 Thread。可选择排空旧运行，或保留固定旧图/部署供其完成、让新研究使用新版本；不能将旧暂停任务直接交给新节点结构恢复。

### 9.4 数据迁移和配置变化

| 数据/配置 | 迁移要求 | 不迁移/不假定的部分 |
| --- | --- | --- |
| 原 Web history.run_id | 保留旧 ID，补 research_id/legacy 字段；新记录保存 Thread 与 Run 引用 | 旧 UUID 对应的 Server Thread 未曾创建，不能声称可 resume |
| 旧档案 owner/tenant | 有可审计映射才回填；其他记录管理员隔离 | 不按用户名猜测、也不把无 owner 的全部历史开放给新用户 |
| Research Graph | 继续 legacy run scope；新研究使用明确 research_id/tenant scope；索引和引用兼容 | 不删除旧 Source/Evidence，也不强制转 Store |
| MySQL memory | 保留 memory_id/source 引用；评审 user/conversation 的可信映射 | 不把所有 checkpoint messages 导入长期记忆 |
| Checkpoint / Thread | 新接纳任务从新后端开始；旧持久化图状态按版本处理 | 当前 Web 局部变量不能无损转换为待运行 checkpoint |
| LangSmith 历史导入 | 限制项目与用户范围，保存原 trace 引用和业务 ID | 不能保证已过期/未采样 Trace 可补回 |
| Secret / MCP token | 按受信用户 namespace，过期和权限核验 | 不将服务密钥放前端、历史 request 或原始回放 |
| 正式部署 | Postgres/Redis/volume、worker、API URL、认证/许可、备份与 TTL | Neo4j Compose 不能代替执行持久化组件 |

迁移前备份及 schema 变更是未来实施环节，需独立可回滚脚本和验收；本轮未连接或改动任何数据库。

### 9.5 回滚办法

- 分阶段发布，保留旧 API 读取与报告下载；新 SDK 路径用受控切换只影响**新研究接纳**。切换值和已接纳 research_id/backend 映射持久化，禁止失败后自动换后端再次执行。
- 回滚时停止新 Server 任务接纳，但让既有 Run 在原图/原部署继续完成或按明确指令停止；新旧 UI 均可查询这些 Run/档案。不能把运行中 Server state 搬回无 Checkpointer 的旧 Web 并称为恢复。
- 业务 schema 先做向后兼容的增加字段/索引与双读，再退役 legacy 字段；不先删旧数据。新档案保留与旧 ID 的映射；旧 owner 不明记录继续隔离。
- F05/F07/F19 等安全/正确性修复应独立提交，回滚运行入口时继续保留。若必须保留旧图供暂停 Thread 完成，阻止新增不安全用法并设排空计划。
- 恢复基线镜像、配置和数据备份不等于撤销外部副作用；工具幂等键、usage 对账和档案补偿作为回滚后的独立核对项。

## 10. 验收标准与测试策略

L = 可本地离线，用假模型/工具、MemorySaver、fake SDK、临时文件完成；S = 必须真实启动 Agent Server（仍可完全用假模型，不产生 LLM 费用）；D = 需要隔离的实际业务数据库/索引或身份/观测服务。S 的重启/多实例项目必须用正式持久化后端，不能仅以 dev 进程代替。

| 场景 | 明确验收标准 | 测试层次 | 本轮状态 |
| --- | --- | --- | --- |
| 正常研究完成 | 一次提交只有一个初始 Run；完整报告、参数、usage 可查询/下载/归档；标识一致 | L + S + D | 既有离线回归通过；完整 Server 链未运行 |
| 多 Agent 完整执行 | 并行取证、汇合、缺口 Send、风险、策略及正式报告上下文不丢失；禁用角色不装配工具 | L + S | 既有离线图测试通过；Server 待验 |
| SSE / 节点进度 | 分块、空帧、error/end/interrupted、子图事件正确；EOF 不误报成功 | L + S | 旧 SSE 部分回归通过；新适配未实现 |
| token 流（如启用） | 选定节点 token 有序；不泄漏内部提示/私有材料；nostream 策略明确 | L + S | 未运行；当前 Web 不是 token 流 |
| 人工审核 | 展示计划 hash=批准版本；True 不再调用 planner；str 只生成一次新计划；默认关闭与降级一致 | L + S | 旧代码缺陷已复现；修复验收未运行 |
| 审核恢复与重启 | 同 Thread 新 Run 恢复；身份/版本校验；重启仍可批准原计划，无重复规划/计数 | S | 未运行 |
| 并发任务 | 两个/多个 Thread 并行，身份、预算、Workspace、图证据不串；全局容量等于配置合同 | L + S | 单进程业务/预算部分通过；多 worker 待验 |
| 任务排队 | 超容量得到可查询 pending Run；同 Thread reject/enqueue 按显式策略；等待期间可取消 | S | 未运行 |
| 用户取消 | pending/running 都有明确终态；停止确认与连接独立；已完成阶段保留，不伪装 external rollback | L + S + D | 仅 fake CancelledError 归档回归通过；真实取消待验 |
| 客户端断线 | worker 按策略继续；重新订阅同 Run，不能再次 create；缓存过期能恢复 state/报告 | S | 未运行 |
| BFF 重启 | 已接纳 Run 不依赖 BFF 内存；恢复 ID 映射和用户授权后可继续订阅 | S + D | 未运行 |
| worker / API 重启 | 在可定位的已完成步骤后 kill/restart；任务与 Checkpoint 保留，已完成步骤不多执行 | S | 子图内存恢复探针通过；正式后端未运行 |
| 内层故障恢复 | 模型后/工具后/图写后/patch 前注入故障；副作用幂等，重复尝试和未报 usage 不隐匿 | L + S + D | 部分 callback 故障用例通过；完整 ReAct 重放未验 |
| 失败重试 | transient、认证失败、解析失败、上下文超限、timeout 分别命中正确策略和计数 | L + S | 模型/解析/上下文单测通过；worker retry 待验 |
| 状态与业务历史 | 无订阅也归档；失败角色之前的报告可查；同研究多次恢复不覆盖完整报告；存储故障可补偿 | L + S + D | 工作区历史 fake store 回归通过；真实 DB 未验 |
| 长期记忆/RAG | 原文、摘要、记忆类型与引用不变；pending 批次补偿；不索引临时图片上下文 | L + D | 默认离线测试通过；实际 MySQL/向量库未验 |
| 身份认证 | JWT 无效/过期/缺失拒绝；Studio 例外明确；服务 Token 不可冒充用户 | L + S + 身份服务 | handler 合同部分已验；真实 Supabase 未运行 |
| 用户/租户隔离 | Alice/Bob 对 Thread/Run/state/history/cancel/resume/Assistant/Store/业务库交叉测试全拒绝越权；伪造 config 无效 | L + S + D | create_run 授权缺口已复现；完整矩阵未验 |
| Token 统计 | fake model attempt 数与 callbacks 相等；重试/解析失败、并行 delta、工具嵌套不重计；审核包含首轮 | L + S | 既有用量用例通过；审核漏计已复现；新架构未验 |
| LangSmith Trace | Workflow → 子图 → 角色 → 模型/工具/RAG 父子链完整；Run/Thread 关联准确；trace 失效不改变预算/结果 | L + S + 观测服务 | mock 合同通过；真实 Trace 上传未运行 |
| 安全输入/显示 | 真实 body 限额、模型/路径/URL 白名单、配置身份不可伪造；Markdown 净化保留 | L + S/网关 | chunked 限额绕过已复现；现有模型/渲染回归通过 |
| 资源生命周期 | 大量 run 后缓存/driver 有界，worker shutdown 释放，阻塞检测无预期外告警 | L + S + D | 源码风险确认；负载/正式生命周期未运行 |
| 业务整体回归 | fast/normal/deep、RAG on/off、query images、graph/standard context、缺失章节兜底、参数复用、错误脱敏 | L + S + D | 当前默认离线套件通过；切换后的整体回归待验 |
| PR #14 轨迹兼容（若采用） | attempt/tool 嵌套配对、显式 Stop、回放和去敏不丢；运行权威只有 Server | L + S | 未合并、未运行其测试 |

建议 CI 分三层：快速离线合同；无付费模型的 Server + PostgreSQL/Redis 集成；隔离业务 DB/身份/Trace 集成。真实搜索、Vision、付费模型和端到端质量 benchmark 继续单独授权运行，不能因使用 Server 就自动执行付费回归。

## 11. 本轮验证证据、限制与复现

### 11.1 已实际执行

| 验证 | 实测结果 | 不能推导出的结论 |
| --- | --- | --- |
| Git / worktree / GitHub PR / main 比较 | 已核对 commit、差异、开放 PR 和基线 | 不代表其他 worktree 的未提交内容已审计或可合入 |
| 包 metadata / lock / pip check | 版本表已记录；pip check 不通过，MCP adapter 实际被跳过 | 不代表项目 lock 也不兼容或线上使用相同环境 |
| 锁定 wheel 源码只读核验 | interrupt、loader、Auth filter、SDK、运行参数及 worker 分支已核实 | 未在完整 lock 环境运行集成测试 |
| 相关定向离线回归 | 188 passed，1 deselected，153 warnings | 是全量套件子集，不能和全量数相加 |
| 全仓默认离线套件（末次工作区补验） | **454 passed，6 deselected，157 warnings，101.21 秒，exit 0** | 被排除 integration 未执行；实际 MCP/数据库/Server/模型能力未验 |
| 并发修改前的全量快照 | 424 passed，6 deselected，157 warnings，66.25 秒，exit 0 | 此数保留为历史证据，不替代末次结果，也不累加 |
| 五组审计专项探针 | 均执行成功，exit 0；结果见下表 | 模型计数来自替身，无真实账单；Auth 使用后端 primitive，无真实 JWT |

本机定向测试结束时曾打印 pyreadline3 清理句柄诊断，不影响 pytest 通过计数；两次全量运行均正常 exit 0。审计工具的第一轮 socket 全封锁误拦截了 Windows asyncio 内部 loopback socketpair，产生 80 个工具导致的失败、116 个通过。修正为只放行 socketpair 创建期间的内部连接后重跑得到上述结果。**这轮无效隔离结果没有算作仓库缺陷，也没有隐藏为通过。**

测试命令（执行目录为仓库根；临时脚本从下方附录重建）：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONPATH = "$PWD/src;$PWD/.tmp/langgraph-overlap-audit-20261014"
python -m pytest -q -p offline_guard -p no:cacheprovider --basetemp=.tmp/langgraph-overlap-audit-20261014/pytest-full --disable-warnings --tb=short
python -u .tmp/langgraph-overlap-audit-20261014/offline_probe.py
```

`offline_guard` 禁用 dotenv / Trace 并阻断测试进程对外 socket 连接；只允许 Windows asyncio 自身 socketpair 的内部 loopback。所有专项探针用虚构身份、假模型、MemorySaver、mock history 或纯内存 conn，不连接 Neo4j/MySQL/Supabase/Agent Server，也不会运行 provider smoke / evaluate / benchmark。临时验证文件在交付前清理，本次新增交付文件只有本报告；附录保存实际执行脚本供复现。末次补验后，对 243 个已跟踪文件逐个 SHA-256 核对，与末次源码核对基线一致；审计期间另有的三处源码和章节预算测试变化保留如第 2.1 节所述。

| 探针 | 观察结果 |
| --- | --- |
| 真实 planner 节点 + MemorySaver + `resume=True` | calls=2；展示 Plan-1，采用 Plan-2；中断前 report={}；State accounted_calls=1、accounted_tokens=10 |
| 相同节点无 Checkpointer | 初次有 interrupt；resume 抛 `RuntimeError: Cannot use Command(resume=...) without checkpointer` |
| 项目 Auth callback + 官方 inmem Runs.put | create_run filter=None；read filter=Bob；在 Alice Thread 成功生成 Run，Thread owner 仍为 Alice |
| compiled export / 子图继承 | loader 接受 compiled Pregel；factory 确实返回新对象且 Checkpointer=None；父有 Checkpointer 时子图 a 执行1次、失败 b 共2次后恢复成功 |
| 真实 FastAPI body 限额 | 70,041 bytes；缺少 Content-Length 的分块请求 200，相同固定长度请求 413；上限 65,536 bytes |

### 11.2 未执行及待补证据

没有启动真实 Agent Server、PostgreSQL/Redis worker 集群或托管部署；没有实际断开 TCP / kill worker / 数据卷恢复；没有连接真实业务数据库、MCP 服务、Supabase 或 LangSmith 上传 Trace；没有运行付费模型、搜索、Vision、质量评测或 PR #14 测试。当前不具备断言正式生产可靠性已通过的证据。

必须补齐的具体证据为：目标部署/许可与实际镜像版本；完整 lock 环境结果和 MCP 可用性；Thread/Run 的真实双用户授权矩阵；正式持久化后端重启/取消/重连；归档故障补偿与租户迁移；内层 ReAct 外部副作用重放。对应文件、实施顺序和验收合同已在第 9、10 节给出，不以“以后再研究”代替方案。

### 11.3 审计专项复现脚本

在隔离目录保存下面两个文件，并使用上面的 PYTHONPATH/命令。脚本有运行环境要求：依赖应与第 2.2 节区分；本轮实际探针在已安装版本运行，锁定版仅做源码对照。不要把以下审计工具加入生产运行入口。

#### offline_guard.py

```python
"""Block socket connections before test collection; keep tests offline."""
import socket
import os
import dotenv
import threading

os.environ.update(PYTHON_DOTENV_DISABLED="1", LANGSMITH_TRACING="false", LANGCHAIN_TRACING_V2="false")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)
dotenv.load_dotenv = lambda *a, **k: False

_local = threading.local()
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_socketpair = socket.socketpair

def _deny_connection(sock, address, *a, **k):
    if getattr(_local, "socketpair", False) and address[0] in {"127.0.0.1", "::1"}:
        return _original_connect(sock, address, *a, **k)
    raise AssertionError("Offline audit: external socket connection blocked")

def _deny_connect_ex(sock, address, *a, **k):
    if getattr(_local, "socketpair", False) and address[0] in {"127.0.0.1", "::1"}:
        return _original_connect_ex(sock, address, *a, **k)
    raise AssertionError("Offline audit: external socket connection blocked")

def _internal_socketpair(*a, **k):
    _local.socketpair = True
    try:
        return _original_socketpair(*a, **k)
    finally:
        _local.socketpair = False

socket.socket.connect = _deny_connection
socket.socket.connect_ex = _deny_connect_ex
socket.socketpair = _internal_socketpair
```

#### offline_probe.py

```python
"""Audit-only probes: no real models, sockets, database connections, or dotenv."""
import asyncio
import importlib
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import patch

os.environ.update(PYTHON_DOTENV_DISABLED="1", LANGSMITH_TRACING="false",
                  LANGCHAIN_TRACING_V2="false", DATABASE_URI="postgresql://audit:audit@127.0.0.1/audit",
                  REDIS_URI="redis://127.0.0.1:6379/0")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)
import dotenv
dotenv.load_dotenv = lambda *a, **k: False


def deny_network(*a, **k):
    raise AssertionError("Audit probe blocked a socket connection")


import offline_guard  # Allows only Windows' internal asyncio socketpair.

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from open_deep_research.configuration import Configuration
from open_deep_research.state import DeepResearchState, Section, Sections
from open_deep_research.workflow import intake
from open_deep_research.budget import capture_budget_usage


async def planner_probe():
    calls = []
    async def fake_invoke(*a, **k):
        calls.append(len(calls) + 1)
        capture_budget_usage({"model_calls": 1, "input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        return {"parsed": Sections(sections=[Section(name=f"Plan-{len(calls)}",
                description="offline", research=True, agent_role="public_signal")])}, {}
    config_obj = Configuration(allow_plan_feedback=True, budget_enabled=False,
                               research_graph_enabled=False)
    with patch.object(intake.Configuration, "from_runnable_config", return_value=config_obj), \
         patch.object(intake.shared, "configurable_model", SimpleNamespace(with_config=lambda x: object())), \
         patch.object(intake, "structured_output_chain", return_value=object()), \
         patch.object(intake, "get_api_key_for_model", return_value="offline"), \
         patch.object(intake, "ainvoke_model_with_budget", side_effect=fake_invoke):
        builder = StateGraph(DeepResearchState)
        builder.add_node("plan_report_sections", intake.plan_report_sections)
        builder.add_node("research_phase", lambda state: {})
        builder.add_edge(START, "plan_report_sections")
        builder.add_edge("research_phase", END)
        cfg = {"configurable": {"thread_id": "audit-only-planner"}}
        graph = builder.compile(checkpointer=InMemorySaver())
        first = await graph.ainvoke({"messages": [], "workflow": {"brief": "audit"}}, cfg)
        shown = first["__interrupt__"][0].value["sections"][0]["name"]
        before = (await graph.aget_state(cfg)).values
        final = await graph.ainvoke(Command(resume=True), cfg)
        result = {"calls": len(calls), "shown": shown,
                  "approved_state_section": final["report"]["sections"][0].name,
                  "checkpoint_before_report": before.get("report"),
                  "accounted_calls": final["runtime"]["budget"]["model_calls"],
                  "accounted_tokens": final["runtime"]["budget"]["total_tokens"]}
        assert result["calls"] == 2
        assert result["shown"] != result["approved_state_section"]
        assert result["accounted_calls"] == 1
        plain = builder.compile()
        try:
            initial = await plain.ainvoke({"messages": [], "workflow": {"brief": "audit"}}, cfg)
            result["without_checkpointer_interrupt"] = bool(initial.get("__interrupt__"))
            await plain.ainvoke(Command(resume=True), cfg)
            result["without_checkpointer_resume"] = "unexpectedly succeeded"
        except Exception as e:
            result["without_checkpointer_resume"] = f"{type(e).__name__}: {e}"
        return result


async def auth_probe():
    auth_module = importlib.import_module("security.auth")
    ctx = SimpleNamespace(user=SimpleNamespace(identity="bob"))
    value = {"thread_id": "alice-thread", "metadata": {}}
    filters = await auth_module.on_thread_create(ctx, value)
    read_filters = await auth_module.on_thread_read(ctx, {})
    from open_deep_research.memory.context import get_user_id
    result = {"create_run_filter": filters, "create_run_metadata": value["metadata"],
              "read_filter": read_filters,
              "server_injected_user_resolves_to": get_user_id({"configurable": {
                  "langgraph_auth_user_id": "bob", "langgraph_auth_user": {"identity": "bob"}}})}
    assert filters is None and read_filters == {"owner": "bob"}
    from langgraph_runtime_inmem.ops import Runs
    from uuid import uuid4
    from datetime import datetime, timezone
    assistant_id, thread_id, run_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    assistant = {"assistant_id": assistant_id, "graph_id": "audit", "config": {},
                 "metadata": {"created_by": "system"}, "name": "audit"}
    thread = {"thread_id": thread_id, "metadata": {"owner": "alice"}, "status": "idle",
              "config": {}, "created_at": now, "updated_at": now, "values": b""}
    conn = SimpleNamespace(store={"assistants": [assistant], "threads": [thread], "runs": []})
    async def actual_handler(_ctx, _action, val):
        return await auth_module.on_thread_create(ctx, val)
    with patch.object(Runs, "handle_event", side_effect=actual_handler):
        it = await Runs.put(conn, assistant_id, {"config": {}}, thread_id=thread_id,
                user_id="bob", run_id=run_id, metadata={}, prevent_insert_if_inflight=False)
        rows = [row async for row in it]
    result["inmem_created_run_on_other_owner_thread"] = len(rows) == 1
    result["thread_owner_after"] = thread["metadata"]["owner"]
    assert result["inmem_created_run_on_other_owner_thread"]
    return result


async def factory_probe():
    from open_deep_research.workflow.graph import deep_researcher, deep_researcher_graph
    import langgraph_api.graph as api
    from langgraph.pregel import Pregel
    spec = api.GraphSpec(id="audit", path="./src/open_deep_research/deep_researcher.py",
                         variable="deep_researcher_graph")
    loaded = api._graph_from_spec(spec)
    result = {"factory_is_fresh": deep_researcher({}) is not deep_researcher({}),
              "factory_checkpointer": str(deep_researcher({}).checkpointer),
              "compiled_export_loader_accepts": isinstance(loaded, Pregel),
              "compiled_export_checkpointer": str(deep_researcher_graph.checkpointer)}
    assert result["compiled_export_loader_accepts"]
    return result


def body_limit_probe():
    from open_deep_research.web import server
    from fastapi.testclient import TestClient
    from unittest.mock import MagicMock
    class FakeGraph:
        async def astream(self, *a, **k):
            yield {"compile_final_report": {"report": {"final": "offline report"}}}
    store = MagicMock()
    data = json.dumps({"topic": "offline", "ignored_extra": "x" * 70000}).encode()
    with patch.object(server, "WEB_API_TOKEN", ""), \
         patch.object(server, "_deep_researcher_factory", return_value=FakeGraph()), \
         patch.object(server, "get_history_store", return_value=store), \
         patch.object(server, "close_history_store"):
        with TestClient(server.app) as client:
            chunked = client.post("/api/research", content=iter([data]),
                                  headers={"Content-Type": "application/json"})
            fixed = client.post("/api/research", content=data,
                                headers={"Content-Type": "application/json"})
    result = {"bytes": len(data), "limit": server.MAX_REQUEST_BYTES,
              "chunked_status": chunked.status_code,
              "fixed_length_status": fixed.status_code,
              "chunked_has_content_length": "content-length" in chunked.request.headers}
    assert chunked.status_code == 200 and fixed.status_code == 413
    return result


async def nested_checkpoint_probe():
    from typing import TypedDict
    class State(TypedDict, total=False):
        count: int
    attempts = {"a": 0, "b": 0}
    async def a(state):
        attempts["a"] += 1
        return {"count": 1}
    async def b(state):
        attempts["b"] += 1
        if attempts["b"] == 1:
            raise RuntimeError("offline retryable failure")
        return {"count": 2}
    child = StateGraph(State).add_node("a", a).add_node("b", b)
    child.add_edge(START, "a"); child.add_edge("a", "b"); child.add_edge("b", END)
    subgraph = child.compile()
    async def parent_node(state, config):
        return await subgraph.ainvoke(state, config)
    parent = StateGraph(State).add_node("parent", parent_node)
    parent.add_edge(START, "parent"); parent.add_edge("parent", END)
    graph = parent.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "audit-nested"}}
    try:
        await graph.ainvoke({}, cfg)
    except RuntimeError:
        pass
    final = await graph.ainvoke(None, cfg)
    assert attempts == {"a": 1, "b": 2}
    return {"attempts": attempts, "final_count": final["count"],
            "child_inherited_checkpoint": True}


async def main():
    result = {}
    for name, fn in [("planner", planner_probe), ("auth", auth_probe), ("factory", factory_probe),
                     ("nested_checkpoint", nested_checkpoint_probe)]:
        try:
            result[name] = await fn()
        except Exception as e:
            result[name] = {"probe_error": f"{type(e).__name__}: {e}"}
            import traceback
            traceback.print_exc()
    result["body_limit"] = body_limit_probe()
    print(json.dumps(result, indent=2, ensure_ascii=False))


asyncio.run(main())
```
