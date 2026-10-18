# OpenAI Agents SDK Phase 2：Research Graph Context Harness 完整集成

日期：2026-10-18（Asia/Shanghai）。仓库：`kkaaakk/public_opinion_research`。

Phase 2 完成四角色 SDK Runtime 的 Research Graph 集成、Working Context 更新、长上下文滚动摘要和批次退出清算。LangGraph 继续编排双研究并行、Review/Gap、补查、Risk→Strategy 和报告；默认 Runtime 仍为 `legacy`。没有推进 Phase 3–5。

## 1. 起点、环境和变更范围

- 开始时工作区干净，本地 `main` / 最新 `origin/main` 均为 `3d774042ed29522fe83eb8e8e74f576c521d9f48`，即 Phase 1 PR #21 合并提交。确认包含最终解耦提交 `e31e6ce4` 和后续审计。
- fetch 后从 `origin/main` 创建 `codex/openai-agents-phase2`，没有基于旧 Phase 1 分支继续。交付前再次核对 origin/main 未改变。
- 保留其他 worktree、两项 stash、安全引用、操作方配置和数据；没有 git clean、修改 `.env` / `CLAUDE.md`、删除其他分支或自动合并。
- 根 `.venv`、Python 3.11.9、`uv sync --extra dev --locked --python 3.11` 成功。SDK 固定 `openai-agents==0.24.0`，依赖声明和锁文件没有变化。
- 实现前重建 main 基线：**669 passed、6 deselected、154 warnings，45.45s**；Mypy **35 errors / 16 files / 104 source files**。没有沿用历史测试数字。

| 文件 | 实际职责与复用 |
| --- | --- |
| `runtime/sdk_context.py` | 继续使用原 collector、官方 RunHooks、input filter 和唯一 Runner；按原模型轮次批次补清算、刷新作用域图、记录原生响应边界与可压缩完整单元 |
| `runtime/sdk_runtime.py` | 原角色装配/工具准入/报告合同；加入滚动窗口、预算协同、未知工具业务状态拒绝；Harness 只接收配置、workspace、工具名和 `_RunBudget`，没有 Runtime 回引 |
| `runtime/business_agent.py` | Graph 模式的 Strategy 接收有界风险分析；分组补查任务保留全部目标和任务关联；不修改四角色 Prompt/Spec |
| `research_graph/strategy.py` | 复用 GraphExtractor、来源版本去重、Store 与回执；在原 Producer 内保留抽取、已写子批次、ContextManager 阶段，失败后从未完成阶段继续 |
| `research_graph/workspace.py` | 复用 ContextManager 的统一有界更新入口；私有 Working Context 深拷贝、当前任务目标和 run/role/round 作用域；输出可序列化 patch |
| `research_graph/store.py` | 原内存/Neo4j 存储和 provenance 查询保持；同一共享 Store / event loop 的 Producer 批次互斥，避免并发来源检查与抽取竞争 |
| `research_graph/compaction.py` | Micro-Compaction 保持 Persist-then-Compact；抽出原滚动摘要模型服务供 legacy/SDK 共用，SDK 不使用 LangChain 消息作为 provider 输入；回执参数改为只读 Mapping，消除两处类型错误 |
| `budget.py` | 原 counters、Usage/capture、policy 和 reducer 保持；可选作用域准入覆盖图模型、摘要、报告和嵌套调用及重试，不另建 BudgetManager |
| `state.py` | 合并角色 Working Context scope；run 改变时清除旧 run 的上下文，继续保留原角色/任务/预算 reducer 合同 |
| `tests/test_sdk_phase2_integration.py` | 新增 44 项真实 SDK Runner、官方 HTTP 序列化、编译 LangGraph 和本地 MCP stdio 验证 |
| `tests/test_sdk_context.py` | 原失败清算断言更新为成功抽取仅一次，并增加重试后仍仅一次断言；原文、pending 和退出失败断言保留 |
| `scripts/sdk_phase2_smoke.py` | 显式 opt-in 的本地 Neo4j 验收入口，复用离线测试推理夹具；不把外部数据库加入默认 pytest 或增加 skip |

没有新增通用 Service/Adapter/Coordinator 层，没有替换图业务算法、SDK Model adapter 或 Agent Loop。生产模型仍来自 `models/sdk.py`，Graph/RAG/工具内部服务继续沿原 LangChain 边界。

## 2. 实际调用链和批次可靠性

```text
现有 LangGraph business node
  → run_business_agent：角色 Spec / 原 Prompt / 权限 / 私有记忆 / TaskDescriptor
  → SDKAgentRuntime → SDKContextHarness.run → 唯一 Runner.run
      input filter：处理所有已完成未消费的原 SDK 模型批次
        → ResearchWorkspace.ingest（共享 Store 的进程内批次锁）
        → Producer Strategy → GraphExtractor → Store.write_delta
        → 写回执结构/作用域确认 → 持久来源/provenance 再查询确认
        → ContextManager → 再次确认来源 → 发布 Working Context / 工具回执
        → Micro-Compaction
      → 有界当前研究上下文 → 必要时 Rolling Compaction → 官方模型请求
      → SDK 并行执行原 FunctionTools → collector 记录真实终态
      → 下一轮 input filter 或原退出 settlement
  → 原图报告/标准报告服务 → AgentRuntimeResult → 原业务 State patch
```

`on_tool_end` 仅观察，从不逐工具入图或串行化 SDK 工具执行。业务包装器在吞错/格式转换前确认成功；失败、参数错误、预算拒绝、MCP 取消、未知/pending/running 状态和未包装的未知状态均不能成为成功证据。命名空间、schema、原域权限和执行时复核继续复用，Consumer 的分析/工具结果不会写成 Producer 原始证据。

同 native call ID 继续共享 Phase 1 执行 Task，身份或参数冲突拒绝，不重新执行或重复计费。不同 call ID 的实际工具调用各自计数；重复来源不代表未执行过工具。图来源去重使用既有 URL+内容版本查询和真实持久 Source ID，压缩引用不使用未写入的新 Source ID。

批次保留原模型轮次身份。抽取成功但写入失败时，保留抽取结果与已写子批次游标；退出清算只重试剩余写入。ContextManager 失败时不再被 URL 缓存捷径假确认，而是重试上下文更新。回执不完整、作用域不符或来源查询失败时，保留原文、pending 和明确 error_type。来源在异步上下文更新期间失效时，更新后的再次核验失败会恢复之前的 Working Context。

这些阶段状态只存在当前进程/调用中。它们不实现数据库跨服务事务、跨进程业务工具 exactly-once 或崩溃恢复。

## 3. 四角色交接和 Working Context

- 两个 Producer 共享同一 `run_id` 的原 Store/cache，但各自有 role、task、research round、SDK invocation、私有记忆和 Working Context。SDK 模型/工具仍并行；来源检查→抽取→写入→上下文确认在同 Store/event loop 内串行，确保去重结果不依赖偶然执行顺序。
- Review 继续使用现有 `research_review` 节点，从同 Run 的已提交图和合并 Working Context 读取证据。汇合由原 LangGraph join 负责。
- Gap 继续只路由给原 public/internal 角色。同一 Run 延续，round/task scope 更新；分组任务的全部目标和 evidence-needed 进入 TaskDescriptor，主 task_id 保持兼容，全部任务 ID 保存在新来源 metadata 的 `research_task_ids`。
- Risk 与 Strategy 在第一次模型请求前检索已提交证据；有相关节点时，使用原 ContextManager 更新本角色有界工作区。每次请求重检有界子图，内容改变时更新 Consumer Working Context，补查后不会停留在旧快照。
- Strategy 的 assignment 额外携带最多 12,000 字符的上游风险报告，并标明它是分析；超长时明确标记截断。风险文本、SDK 生成观点、Consumer 工具结果均不作为原始证据入图，没有增加社媒发布等权限。
- Working Context 在模型成功并通过 provenance 校验后才发布，保留事实/冲突/缺口/active IDs/进度；继续使用原容量、检索节点/边数和上下文字符上限。不同 Run、其他 role 和未来 round 的已存上下文不会被加载；同 Run 补查保留事实而更新任务目标。
- 原 reducer 按角色合并上下文和 scope；新 Run 清除旧图上下文。业务节点只返回角色预算 delta。正式角色报告、memory、rolling_summary、replace_report 合同保持，RAG Graph 和 Research Graph 数据所有权没有合并。

独立 Phase 0 Harness 的默认“首轮检索、显式定期刷新、Consumer 不调用 ContextManager”兼容保留；业务 Harness 显式启用逐轮刷新及 Consumer 上下文更新。

## 4. 两类压缩与供应商支持

实际检查根环境 SDK 0.24.0 的 `memory/openai_responses_compaction_session.py`、Session 协议、Runner input filter 和官方两类 Model 的本地实现，并打开 [OpenAI 官方 Session 上下文管理示例](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory)。版本固定证据优先于滚动文档。

### Micro-Compaction

继续使用既有 `micro_compact_messages`：只有完整持久回执及 Working Context 成功后，才能替换对应原始工具正文；保留原 call/output 和真实 Graph IDs。`recent_raw_steps` 继续保留近期原文。写入、回执或上下文失败不会删除工具原文。

### Agent Rolling Compaction

官方 `OpenAIResponsesCompactionSession` 确实存在，但调用 `responses.compact`、仅适用于 OpenAI Responses，不支持 DeepSeek/Ark 的 Chat Completions；默认按候选项数触发的 Session 策略也不能直接表达本项目 pending evidence/私有记忆/Working Context 保护。Phase 2 保持当前 client-managed SDK input，统一选用现有应用侧摘要服务，没有增加 Session、sandbox 或新的 loop。

1. 估算实际 instructions、过滤后 input 和工具 schemas，沿用容量/阈值配置。已知模型使用原窗口表，显式 `research_graph_context_capacity_tokens` 优先；未知 alias 使用 **32,768 tokens 的应用侧上限**，不是对供应商窗口的声明。较小端点应显式配置真实容量。
2. 使用官方 `on_llm_end` 响应边界划分原生历史，完整保留同一 SDK 响应内 reasoning、并行 function calls 和配对 outputs。未知、缺配对、重复身份或不能确认边界的历史不进行猜测性裁剪。
3. 当前任务、角色私有记忆、受保护的最新 Working Context/图上下文以及最近 `max(1, recent_raw_steps)` 个原生响应单元留在摘要外。Producer 的失败/pending/未确认回执单元固定保留。Consumer 可以摘要旧分析，但不生成证据。
4. 只摘要新可压缩单元，传入一份旧摘要，使用原 `RollingCompactOutput` / 结构化重试服务；新历史摘要输入最多约 30,000 字符，超长时保留头尾并明确标记中段省略，完整原文仍在 collector/transcript。摘要请求不调用工具。
5. 成功后只替换请求视图中的完整旧单元，携带一份新摘要和已确认引用集合；后续 filter 重新应用同一窗口，避免 SDK 原始历史再进入请求、动态上下文和旧摘要重复堆积。摘要中的新 Graph ID 必须来自本次可验证的证据/子图。
6. 验证摘要非空、长度有界、请求确实缩短且低于阈值后，才提交窗口与 summary。摘要错误、无缩减、虚构引用、预算拒绝或受保护内容仍无法容纳时，保留原始历史并抛出带原因的 `SDKContextLimitExceeded`，继续原异常清算。

| 主 SDK 模型路径 | Phase 2 实际处理 / 验证 |
| --- | --- |
| OpenAI Responses | 应用侧摘要；官方 Model + mock HTTP 连续多轮压缩、原生配对通过，不调用 responses.compact |
| DeepSeek Chat Completions | 同一应用侧摘要；官方 Model + mock HTTP 保持保留单元的 `reasoning_content` 与工具回放，连续压缩通过 |
| Ark/火山 Chat Completions | 同一应用侧摘要；官方 Model + mock HTTP 多轮工具序列化及连续压缩通过 |
| Groq | 复用原已验证的 Chat adapter；本次没有新增 Groq 多轮压缩专项或真实服务验收 |
| 未接入的 provider / hosted native tools / multimodal MCP | 保持 Phase 1 的显式限制和拒绝，不静默回退 legacy |

摘要模型由现有 `rolling_compaction_model` / max_tokens 配置选择，可以与主 SDK 模型不同。摘要和最终角色报告用途不同；只有摘要服务共用，不重跑 legacy rolling loop，也不重复注入旧 summary。Token 估算只作压力/指标，摘要模型的真实回调 Usage 才进入预算；无 Usage 的测试明确保持 billed token 增量为零。

压缩不是无限容量保证：单个受保护任务/最新原文超限、过多必须保留引用、pending evidence 或不支持的 native 历史仍可能安全失败。输入窗口被压缩不意味着 SDK 内部原始 transcript 内存被删除。

## 5. 预算、退出、取消和资源

沿用 Phase 1 `_RunBudget`、budget capture、实际 SDK start/Usage 和报告收尾。SDK 失败不切回 legacy。所有 SDK 模型按实际尝试计数，Usage 不重复合并 RunResult totals；图模型、ContextManager、摘要、角色报告和工具内 LangChain 调用通过原 accounting 捕获实际成本。

可选作用域准入接到原 `ModelAttemptCounter` 的请求前回调，覆盖结构化链的真实重试；同一次调用的 pending attempts、工具/报告子 capture 的已完成消耗参与准入。预算不足不强行摘要，不消费配置要求的最终工作流报告预留。工具并发准入继续在第一个 await 前预占原工具/搜索计数。

模型 Token 是响应后实测；并行 LangGraph 角色各自读取预算快照，并行工具内部模型扇出也没有跨调用原子预占。因此仍不是全局原子硬配额；没有为此引入数据库分布式计数。

正常结束、ResearchComplete、max turns、模型/工具/Graph/ContextManager 错误、预算不足、外部及重复取消、并行工具取消、同步工具迟到和摘要失败，继续通过原 `run → settle / _settle_on_exit` 路径。终态工具记录和成功证据批次被处理；未完成结果保留 pending / in-flight / error_type，不生成成功清算报告。已成功工具不因图清算失败重放。

原 30s 有界 drain、shielded 收尾、live collector 和显式 `settle("late_tool_retry")` 接口保留。业务模型工厂退出关闭其拥有的 client；MCP smoke 的 ClientSession/stdio 由 async context 关闭；共享 Graph Store 由原 cache 清理/服务关闭负责，不在一个角色结束时关闭其他角色还在用的驱动。

无法强杀已运行同步线程或同步 DB 操作；极晚结果仍需进程存活后的补清算。没有进程崩溃恢复、跨 Worker 取消、SDK Session 耐久化或 Phase 4 Run 生命周期保证。

## 6. 验证结果

| 检查 | 本次结果 |
| --- | --- |
| 根锁同步 | 成功，313 个解析包 / 250 个已安装包，无依赖修改 |
| 全量 pytest | **713 passed、6 deselected、154 warnings，51.21s，退出码 0** |
| SDK Phase 0/1/2 + Graph/预算/角色/权限/工作流/State 定向 | **283 passed、150 warnings，27.51s，退出码 0** |
| 新 Phase 2 测试 | **44 项**，全部包含在定向和全量中，无新增 skip/xfail |
| Ruff | 原 CI E/F/I 全范围通过；SDK Harness/Runtime、新测试/本地 smoke 脚本完整规则通过 |
| format / compileall | 修改/新增 Python 文件格式通过；生产包、安全包、scripts、evaluation 编译通过 |
| import / CLI smoke | 原 24 个入口在无凭据/禁出网条件导入通过；两个 SDK smoke CLI help 通过 |
| Mypy | **33 errors / 15 files / 104 source files**，仍非零退出；相较 main 无新增消息，修复回执 Mapping 的两处旧类型错误 |
| 依赖一致性 | 仍非零退出：唯一旧冲突 `markitdown-mcp → mcp~=1.8.0`，实际 `1.23.0`；无新增冲突 |

全量沿用原 6 项 integration 排除、socket guard、禁 dotenv/tracing，没有调用付费模型或检索 API。Windows 沙箱最初拒绝 MCP 子进程的 asyncio 命名管道；在正常权限下运行同样测试和 socket guard 后通过，没有跳过或替换测试。部分检查过程的失败和修复日志保留在 `.tmp/agents-phase2/`，没有将失败轮次当作通过。

44 项新增测试覆盖短上下文、四角色私有记忆、连续摘要、长 Graph Producer、非 Responses 原生协议、摘要失败/空值/过大/未知引用、无 Usage、低预算、重试和工具内部预算、状态拒绝、批次写入/上下文/无效回执、部分子批次失败、Consumer 刷新、来源并发去重、Run 隔离、分组任务、未知容量、五类终止以及并行取消。

编译 LangGraph 全链路使用实际 `research_phase → public_opinion_subgraph` 和报告节点：两个 Producer 的模型 barrier 和四个工具的 barrier 证明并行；Review 首轮读到两方持久证据后发出缺口任务；原 Producer 补查、新证据再次可见；Risk 首轮读图；Strategy 首轮读图和风险报告；最终章节/报告完成。保留非零初始预算，逐字段验证 **7 初始 + 24 增量 = 31 model_calls**、6 次实际工具和精确 fixture Usage、角色 memory 与 Working Context 合并，而非只检查次数。

旧回归还覆盖 default legacy 的实际模型/工具/报告流程、重复 call ID、权限错误、MCP 字典 schema/error/artifact、超限、取消与同步迟到补清算。唯一旧断言的变更是图写失败后的抽取次数从两次改为一次，并强化“显式补清算后仍仅一次”；源数据/pending/异常断言未放宽。

## 7. 外部服务验收

| 范围 | 实际结论 |
| --- | --- |
| 本地 MCP stdio | **VERIFIED**：启动官方 FastMCP 合成只读工具进程，真实 initialize/list/call，经现有 LangChain MCP adapter → SDK → Extractor → Store/回执；ClientSession 和子进程关闭。无常驻服务依赖 |
| 本地 Neo4j | **VERIFIED（受控合成 smoke）**：已有 `neo4j:5.26.31` healthy，loopback Bolt；真实 Neo4jResearchGraphStore + 四角色真实 Runner，模型推理为离线夹具；两 Producer 的 2 Source / 2 Evidence 被两 Consumer 首轮读取，Strategy 同时收到风险报告，回执/预算/清算通过 |
| Neo4j 数据保护 | 仅显式读取本地数据库凭据，不加载模型凭据；随机 `phase2_smoke_<uuid>` 先确认不存在，再写合成数据；finally 只删除本次 Run，查询确认剩余 0，并关闭驱动；没有重启容器或清理用户其他数据 |
| 真实供应商模型、付费 web/社媒 API、远程 MCP、生产 RAG/Milvus/ES/MySQL | **NOT VERIFIED**：本次没有授权自动使用付费凭据；离线/模拟 HTTP 不能当作真实供应商和生产数据验收。Phase 0 的历史 DeepSeek live 结果不能冒充本次 Phase 2 live 验证 |

可复现的 Neo4j smoke 复用仓库 `tests/` 中的确定性模型夹具，需要根 dev 环境；它是单独 opt-in 验收入口，未加入默认 pytest 外部依赖。fixture Usage 验证会计链路，不是实际供应商账单。

## 8. 复现命令

在仓库根目录运行；Windows 中包含真实 MCP 子进程的测试需要允许 asyncio 创建本地命名管道的正常权限环境。

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:UV_LINK_MODE = 'copy'
$env:TMP = Join-Path (Get-Location) '.tmp/agents-phase2'
$env:TEMP = $env:TMP
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_sdk_phase2_integration.py tests/test_sdk_runtime.py tests/test_sdk_context.py tests/test_sdk_models.py tests/test_sdk_business_integration.py tests/test_research_graph_context_harness.py tests/test_budget.py tests/test_public_opinion_agents.py tests/test_public_opinion_tool_permissions.py tests/test_public_opinion_research_loop.py tests/test_state_architecture.py --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/audit_repository.py scripts/sdk_model_smoke.py scripts/sdk_phase2_smoke.py
.venv/Scripts/python.exe -m ruff check src/open_deep_research/runtime/sdk_context.py src/open_deep_research/runtime/sdk_runtime.py tests/test_sdk_phase2_integration.py scripts/sdk_phase2_smoke.py
.venv/Scripts/python.exe -m ruff format --check src/open_deep_research/budget.py src/open_deep_research/research_graph/compaction.py src/open_deep_research/research_graph/store.py src/open_deep_research/research_graph/strategy.py src/open_deep_research/research_graph/workspace.py src/open_deep_research/runtime/business_agent.py src/open_deep_research/runtime/sdk_context.py src/open_deep_research/runtime/sdk_runtime.py src/open_deep_research/state.py tests/test_sdk_context.py tests/test_sdk_phase2_integration.py scripts/sdk_phase2_smoke.py
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security scripts evaluation
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
uv pip check --python .venv/Scripts/python.exe
.venv/Scripts/python.exe scripts/sdk_phase2_smoke.py --help
# 单独显式启用：仅本地 Neo4j + 离线模型，不运行付费模型。
.venv/Scripts/python.exe scripts/sdk_phase2_smoke.py --enable-local-neo4j --output .tmp/agents-phase2/neo4j-smoke-result.json
```

Mypy 和 pip check 仍按上述已知问题返回非零。原 import smoke 的 17 个入口和 7 个 SDK/角色入口沿 Phase 1 审计的方法执行，清除 API_KEY/TOKEN、屏蔽 socket connect、禁 dotenv/tracing。日志、Mypy 前后差异和 live smoke 输出只保存在忽略目录，不提交本地输出。

## 9. Phase 3 可复用接口与剩余边界

可直接复用 `run_business_agent(role, state, config)`、`SDKAgentRuntime.run(task) → AgentRuntimeResult`、`ResearchWorkspace` / TaskDescriptor / `state_update()`、原 Graph Producer/Consumer 策略、`SDKContextHarness.function_tool / filter_model_input / settle`、BatchSettlement 和现有 budget delta/reducers。Phase 3 只需替换工作流调度与状态合并的宿主，保留本次验证的角色边界、并发汇合、Review/Gap 与 Risk→Strategy 顺序。

实施 Phase 3 前应维持同样的真实 Runner/图证据/报告/预算合同，明确确定性调度的取消和错误传播；不把 in-process pending 阶段当作持久 RunState。多 worker 原子去重/硬预算、DB 异步 I/O/驱动长期生命周期、真实供应商质量与性能、远程服务、Web/SSE/Session 和故障恢复仍需相应阶段独立验收。默认 legacy、旧 Runtime、LangGraph/LangChain 依赖和兼容入口均继续保留。

## 10. Git 与 GitHub 验收

实现提交、PR 和最终 CI 的实际固定链接将在远程 Actions 完成后补记于此；不以本地通过代替 GitHub 结果。本分支继续使用既有 `.github/workflows/ci.yml` 的 `codex/openai-agents-*` push 与 PR 触发规则，`quality` / `tests` 未降低门禁。
