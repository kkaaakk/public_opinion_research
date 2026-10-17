# OpenAI Agents SDK Phase 0 实施与验证

日期：2026-10-16 至 2026-10-17（Asia/Shanghai）。范围：SDK 基础接入和 Research Graph Context Harness 可行性验证。

**2026-10-17 补充：已使用项目现有 DeepSeek 配置完成真实 API 验证，非思考/思考模式均通过，结果见第10节。第1–9节保留首次提交 `2a29be4d` 的离线交付基线；“未真实调用”等表述仅指当时的执行范围。**

**结论：真实 SDK Runner 的模型→并行工具→模型循环能够通过公开输入过滤器复用现有 Producer / Consumer。生产入口保持原实现，未进入 Phase 1。**

## 1. Git 基线与审查边界

开始时工作区干净，当前分支 `codex/openai-agents-migration`，HEAD 为 `2c876398639a11c9b2a66232419da8374e187e83`。实际 `ls-remote` 确认远程同名分支也是该提交；从它创建独立分支 `codex/openai-agents-phase0`。Windows 默认 Schannel 查询失败后，单次命令使用 Git OpenSSL 后端成功，未关闭 TLS 校验或修改持久 Git 配置。

检查了现有 worktree 列表，未移动其他 checkout。未应用/删除 stash、修改 safety ref、清理本地数据、改写 `.env` 或编辑 `CLAUDE.md`。基线锁环境先执行 `uv sync --extra dev --locked --python 3.11`，再记录测试和静态检查。

本次审查围绕实际接缝：

| 现有模块 | 核对内容 / Phase 0 决策 |
| --- | --- |
| `runtime/agent_runtime.py`、`business_agent.py` | 原循环、并行 ToolExecutor、success 标志、workspace 生命周期、角色私有记忆和预算；原实现不修改 |
| `research_graph/` | ToolBatchItem、TaskDescriptor、run scope、Producer 写入回执、Consumer、GraphExtractor、ContextManager、检索和 micro-compaction；核心算法不修改 |
| `workflow/`、`public_opinion_agents/` | LangGraph 当前入口、四角色 Spec、取证汇合/复核/补查与报告；本阶段不接管这些业务 |
| `tools/`、`mcp/` | raw search / RAG 工具、域准入、命名空间、连接生命周期；通过窄包装或 SDK 的公开 MCP 回调采集结果 |
| `models/factory.py`、`budget.py` | 复用 provider:model、配置/环境凭据、Ark 地址解析；旧 LangChain accounting 不冒充 SDK accounting |

## 2. 文件与职责

| 新增 / 修改 | 职责 |
| --- | --- |
| 新 `src/open_deep_research/runtime/sdk_context.py` | SDKContextHarness、工具事实记录、公开 RunHooks / input filter、Producer 批处理、Consumer 检索、退出清算 |
| 新 `src/open_deep_research/models/sdk.py` | 官方 Responses / Chat Completions Model 的配置与客户端生命周期适配，无自有推理循环 |
| 新 `tests/test_sdk_context.py` | 真 SDK loop + ScriptedModel + 真实 Graph 算法 / 内存 Store；仅图抽取和上下文更新的模型推理使用确定性替身 |
| 新 `tests/test_sdk_models.py` | 官方 Model 和 OpenAI 客户端的模拟 HTTP 协议、错误、usage、配置和关闭测试 |
| 新 `scripts/sdk_model_smoke.py` | 必须显式 `--enable-real-api` 的付费供应商 smoke 入口；本次未执行真实请求 |
| `pyproject.toml`、`uv.lock` | 精确固定 SDK 与必要传递依赖解析 |
| `tests/conftest.py` | 默认禁止 Agents SDK tracing；原 dotenv / LangSmith 隔离保留 |
| `AGENTS.md`、迁移计划、本记录 | 更新已完成 Phase 0 和仍使用旧生产链路的事实，记录接口、验收与限制 |

首次提交没有新增工作流引擎、通用 Harness 空包或第二套 Agent Loop，也没有修改原 runtime、workflow、Research Graph、RAG、社媒、MCP 服务或 Web/API 生产源码。后续真实补验涉及的窄修正单列在第10节。

## 3. SDK 版本、接口和依赖

实际查询官方 release 和 PyPI 元数据后选择 **`openai-agents==0.24.0`**，对应官方源码 commit `17d994bd3045dae71acda676a4ef4762ed8ecd94`。在根 `.venv` 的 **Python 3.11.9** 上安装和验证。参考：[官方发布](https://github.com/openai/openai-agents-python/releases/tag/v0.24.0)、[SDK 总览](https://developers.openai.com/api/docs/guides/agents/sdk)、[固定依赖声明](https://github.com/openai/openai-agents-python/blob/17d994bd3045dae71acda676a4ef4762ed8ecd94/pyproject.toml)。

实际读取固定版本的 `docs/running_agents.md`、模型/测试文档，以及 `run_config.py`、`lifecycle.py`、`tool.py`、`tool_context.py`、`run_internal/{turn_preparation,run_loop,tool_execution}.py`、`mcp/{server,util}.py` 和官方模型实现。源码保存在忽略的 `.tmp/agents-phase0/upstream/`，没有修改 SDK 源码。

| 公开接口 | 实际合同 |
| --- | --- |
| `Agent` / `Runner.run` | SDK 唯一负责模型和工具循环；本适配器每次只调用一次 Runner |
| `RunConfig.call_model_input_filter` | 接收 `CallModelData(model_data, agent, context)`；context 为调用方原始本地对象；返回 `ModelInputData(input=list, instructions=str或None)`，可 async |
| `RunHooks.on_tool_start(context, agent, tool)` | 本地 FunctionTool 通常提供 ToolContext，含 tool_name、tool_call_id、tool_arguments；其他工具族不能假设相同 |
| `RunHooks.on_tool_end(context, agent, tool, result)` | 单个工具回调，result 没有通用 success 标志；不是工具批次汇合点 |
| `FunctionTool(on_invoke_tool=...)` | 原始 invoker 接收 ToolContext 和参数 JSON；包装层先记录执行状态，再返回错误文本或抛出异常 |
| MCP `custom_data_extractor` / `failure_error_function` | 前者能取到 isError、结构化结果、显示工具名和参数；后者采集协议/调用异常；结构化结果是只读映射，边界转普通 dict |
| `agents.testing.ScriptedModel` | 官方离线 Model；实际运行 Runner，不模拟 Runner 或自己实现 loop |

必要锁更新如下，未执行全仓无条件依赖升级：

| 依赖 | 准备基线 → 本环境 |
| --- | --- |
| openai-agents | 未安装 → 0.24.0 |
| openai | 2.20.0 → 3.31.0 |
| pydantic / pydantic-core | 2.11.5 / 2.33.2 → 2.14.1 / 2.50.1 |
| langchain-openai / langchain-core | 1.1.9 / 1.2.22 → 1.7.2 / 1.6.11；旧 openai adapter 的 `<3` 上界需要调整 |
| langsmith | 本环境 0.7.1 → 0.14.11 |
| starlette | 0.49.1 → 1.8.0；FastAPI 保持 0.138.0 |
| mcp | 保持 1.23.0 |

另有 anyio、idna、jiter、pyjwt、python-multipart、typing-extensions、typing-inspection、urllib3 更新；新增 griffelib、httpcore2、httpx2、langchain-protocol 等。根锁 package 记录 306→313，共 21 个包名的版本集合变化；不同 Python marker 的多个记录不等于本机重复安装。模拟 OpenAI 3 HTTP 使用其实际依赖 `httpx2.MockTransport`。完整锁版本差异留在 `.tmp/agents-phase0/lock-diff.json`。

## 4. 模型供应商兼容验证

模型名始终来自 `provider:model` 参数。凭据复用现有 `GET_API_KEYS_FROM_CONFIG` / `apiKeys` 或环境解析；没有固定实际模型或真实 Key。显式 `api_key` / `base_url` 可覆盖配置；Ark 保留原地址校验。模型参数用官方 ModelSettings，包括 `extra_body`。

| 路径 | Phase 0 实测 |
| --- | --- |
| OpenAI Responses | 官方 OpenAIResponsesModel + 两轮模拟 HTTP：tool call/output 配对、最终结果、usage 成功 |
| OpenAI Chat Completions | 官方 OpenAIChatCompletionsModel：assistant tool_calls / tool_call_id 配对成功 |
| DeepSeek | 官方 Chat Completions adapter：deepseek-flash 名称透传、thinking 配置透传、reasoning_content 下一轮回放成功 |
| 火山方舟 | 官方 Chat Completions adapter：ep-* 名称、Ark endpoint、原凭据配置成功 |
| Groq | 官方 Chat Completions adapter：名称、兼容 endpoint 和凭据透传成功 |
| Anthropic / Bedrock / Cohere / Google / Mistral / Ollama 等 | 本阶段没有接入其专用 SDK provider；显式报错，原生产模型配置仍保留。可直接向 Agent 传官方 Model，后续需独立验证 |

401、429、503 通过官方客户端/SDK 分别传播 AuthenticationError、RateLimitError、InternalServerError，测试客户端禁用重试以固定尝试数。两次成功模型请求的 usage 为 requests=2、input_tokens=24、output_tokens=6；调用方提供的客户端不被关闭，工厂拥有的客户端退出时关闭。

**本节记录首次离线协议兼容验证，不代表各供应商线上可用性、模型质量或完整功能验收。** 首次交付未调用真实模型；后续 DeepSeek 真实补验见第10节，其他供应商仍未做线上验证，也未运行付费 benchmark。

## 5. 实际生命周期与批次边界

并行测试使用 barrier：只有两个工具都已进入执行，工具才允许返回，因此证明 SDK 实际并行执行；第二次模型的 responder 直接断言图已写入、两份证据统一处理、Working Context 已更新。

实测顺序为：

```text
on_agent_start
call_model_input_filter(1): 首次 scoped retrieval + 有界任务/证据上下文
on_llm_start(1) → SDK Model → on_llm_end(1)
  on_tool_start(A) → raw invoker(A) → 明确结果状态 → on_tool_end(A)
  on_tool_start(B) → raw invoker(B) → 明确结果状态 → on_tool_end(B)
  （A/B 可并行、完成先后不限）
call_model_input_filter(2): 消费全部已完成且未处理的结果
  Producer: GraphExtractor → Store.write_delta → 回执
            → 检索增量 → ContextManager 更新 → micro-compaction
  替换有回执的 function_call_output 正文，构造一个最新上下文块
on_llm_start(2) → SDK Model → on_llm_end(2) → on_agent_end
Runner 返回 → 显式 terminal settlement → 返回 SDK RunResult
```

单次 `on_tool_end` 只记录调用事实，从不直接触发入图。一个 collector batch 内可因现有 extraction token limit 分成多个图抽取子批；部分子批写入失败时，不生成可用于压缩整个未提交批次的替换。

| 工具族 | 回调覆盖与采集结论 |
| --- | --- |
| 本地 FunctionTool，使用 harness.function_tool 原始 invoker | start/end + 包装层明确 succeeded/failed/cancelled；可作为证据 |
| 普通 @function_tool，没有明确状态包装 | start/end 可见，但错误可能已转换为普通字符串；标记 unobserved，不入图 |
| SDK 本地 MCP server | 转为 FunctionTool，start/end 可见；公开 MCP 回调保留 isError；成功、isError、调用异常和 SDK MCPToolCancellationError 均已测 |
| 托管 FileSearch 等 | 由 provider 执行，归一化响应 item 没有触发本地 start/end；本阶段不入图。Hosted MCP 同样不能当成本地 MCP |
| 本地 Shell / Computer / ApplyPatch / Custom 等其他族 | 源码存在各自 hook 分支，部分只给 RunContextWrapper；本适配器不据此推断通用 call ID / success，未宣称完成这些族的证据采集 |

未处理的本地 Python 工具异常由 SDK 包装成 UserError，原异常保留在 `__cause__`；已有 AgentsException 传播方式不同。工具自己失败、父任务取消、并行兄弟失败后的收尾不能假设都触发 end hook，结果包装层是必要的。

## 6. Producer / Consumer 和协议保护

Producer 默认证据白名单精确为 `web_search`、`rag_search`。其他社媒或 MCP 工具必须按实际公开工具名显式配置；白名单仅控制证据准入，不替代业务工具权限。只传入明确成功的白名单结果，失败、取消、unobserved 和非证据工具均清算为已观察但不入图。

复用 `ResearchWorkspace.ingest` → `ResearchGraphProducerStrategy` 的真实 GraphExtractor、Store、ContextManager 和 micro-compaction。SDK 边界只把已观察调用按模型轮次投影成 LangChain AIMessage / ToolMessage，供旧策略使用；该投影从不发送给模型。SDK 的原始 reasoning、function call 和其它响应 item 保持原形，仅有成功回执的文本 output 可被替换；非字符串 output 保持原形。

写入失败时保留原始 observation、pending 状态和模型可见原文；失败/取消的 ingest 会恢复本次未完成的 receipt 更新，避免后续误用回执。重试仅处理证据批次，不重跑业务工具。正常运行最终仍有未入图结果时抛 ResearchGraphSettlementError，不能用模型已生成最终文本掩盖清算失败。

提交前额外发现并先复现了遗留 URL 去重回执问题：不同 tool call / SDK invocation 返回同一 URL 与内容时，旧策略可能跳过写入，却在 duplicate receipt 中列出新计算、未入库的 Source ID。两个新增失败测试证明该问题；SDK 边界现要求所有回执引用都在其已写入 node_ids 中，否则记录 compaction_deferred 并保留原文。没有改写旧策略；此处宁可延后重复材料的压缩，也不向模型提供不存在的图引用。

Consumer 第一次模型请求前用 `workspace.context_for()` 检索同一研究 Run 已有证据，`workspace.protected_context()` 复用任务、Working Context、子图与摘要的渲染。默认后续请求使用首次证据快照；`consumer_refresh_every=N` 可显式定期刷新。Consumer 不调用 Producer ingest 或 micro-compaction，自己的工具结果保持原文。

上下文默认最多 16,000 字符，且检索继续受现有 node/edge 数量和字段渲染上限控制；这是字符边界，不冒称精确 token quota。每个模型请求最多一个本适配器生成的上下文块，不累积旧副本；SDK system instructions 保持原值，动态证据放在 user input。测试确认 `RunResult.to_input_list()` 没有缓存这些注入块，且原调用方输入未被修改。

标识为 `research run_id + role + task_id + sdk_run_id + native tool_call_id`。要求显式提供研究 run_id，每个 SDK 调用创建独立 harness；Graph call ID 由上述标识稳定派生，并在 transcript 中同时记录 native ID 与 Graph ID。同一 native ID 的重复处理只入图一次，身份/参数或结果冲突报错。Graph 本身继续按研究 Run 共享证据，Consumer 因而可以读取上游角色；collector 的 Role/Task 隔离不等价于租户授权。

## 7. 退出清算与实测覆盖

所有 SDK 退出都通过 run wrapper 显式清算，包括 final tool 直接终止、max_turns、模型异常、工具异常、父任务取消和入图期间取消。退出清算用独立受 shield 保护的 task；重复取消不打断已开始的证据提交，随后仍向调用方传播取消。

SDK 退出后，对包装层已登记的迟到本地 FunctionTool 最多等待一个 `settlement_timeout`；之后仍清算已经完成的证据，并用 in_flight_call_ids / pending_call_ids / error_type 明示剩余工作。Graph 清算另有同等异步 timeout，两个等待阶段总计可到两倍该值；这不约束 Runner 内部或同步存储阻塞的时长。迟到结果继续进入原 collector，可显式 `await harness.settle("late_tool_retry")`，不重放工具。Graph 自己取消清算时保留 pending 并传播，不陷入重复 shield 等待。

新增 42 项测试：25 项 Context Harness、17 项 SDK Model / provider。覆盖统一并行批次、失败/非证据过滤、第二轮上下文、Consumer 首轮/定期读取、全失败与部分写入失败、recent_raw_steps、final-tool/异常/取消/重复取消、迟到结果补清算、重复调用、重复 URL 回执溯源、Run/Task 隔离、MCP 的真实 SDK 转换、托管工具无本地回调、两种官方 API 协议、DeepSeek reasoning、usage、凭据、资源关闭和显式付费开关。

## 8. 验证结果与复现

| 检查 | 修改前基线 | 最终结果 |
| --- | --- | --- |
| 根锁 sync / Python | 成功 / 3.11.9 | SDK 锁 sync 成功 / 同一根 .venv |
| 原有离线测试 | 510 passed、6 deselected、153 warnings | 升级 SDK 依赖后单独复跑：510 passed、6 deselected、154 warnings |
| 新 SDK 定向测试 | 无 | 42 passed；未新增 skip / xfail；最终全量也覆盖全部 42 项 |
| 最终全量离线测试 | 510 passed | 552 passed、6 deselected、154 warnings；75.68 秒，退出码 0 |
| Ruff，原 CI 的 E,F,I | 通过 | 通过，未降低规则 |
| 新 Python 文件完整配置 Ruff / format | 无 | 通过 / 5 个文件格式检查通过 |
| Mypy 原审计命令 | 35 errors / 16 files / 101 source files | 35 errors / 16 files / 103 source files；逐条 error 集合新增=0、消失=0 |
| uv pip check | markitdown-mcp 要求 mcp~=1.8.0，实际 1.23.0 | 相同 1 项遗留冲突；没有新增版本 override 掩盖它 |
| Compile / CLI 入口 | 原审计已有 | src/open_deep_research、src/security、scripts 编译通过；SDK smoke --help 通过 |

全部离线命令禁 dotenv、tracing，使用与准备审计相同的 socket guard；本地 loopback 白名单不意味着测试调用了真实供应商。日志在忽略的 `.tmp/agents-phase0/`，不提交运行输出或源码下载件。

中间全量曾有 1 项新增迟到工具测试失败：测试用的 50ms 清算窗口也意外限制了离线图模型 fixture 的调度，补清算返回 TimeoutError。将该测试窗口改为 1s，并新增对初次 ToolDrainTimeout 的明确断言，仍由未释放的 Event 确定性地制造迟到结果；生产默认 30s 和原有断言保持不变。部分 Windows 运行在退出时另打印 pyreadline3 Console 析构 WinError 0xc0000008，测试退出码仍为 0；没有将该第三方控制台问题算作 SDK 功能通过或在本阶段修改其实现。

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_sdk_context.py tests/test_sdk_models.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/audit_repository.py scripts/sdk_model_smoke.py
.venv/Scripts/python.exe -m ruff check src/open_deep_research/runtime/sdk_context.py src/open_deep_research/models/sdk.py scripts/sdk_model_smoke.py tests/test_sdk_context.py tests/test_sdk_models.py
.venv/Scripts/python.exe -m ruff format --check src/open_deep_research/runtime/sdk_context.py src/open_deep_research/models/sdk.py scripts/sdk_model_smoke.py tests/test_sdk_context.py tests/test_sdk_models.py
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security scripts
uv pip check --python .venv/Scripts/python.exe
.venv/Scripts/python.exe scripts/sdk_model_smoke.py --help
```

Mypy 和 pip check 当前返回非零，分别对应上述原有 35 项 / 1 项问题。最终 pytest 默认沿用项目排除 6 个 integration 测试的策略，未修改旧测试断言或门禁范围。

真实 API smoke 只有显式 opt-in 才读取 dotenv 和运行模型；示例命令必须用操作方已配置的 `provider:model` 替换占位值：

```powershell
.venv/Scripts/python.exe scripts/sdk_model_smoke.py --enable-real-api --model 'provider:model'
```

首次交付没有执行此命令。后续真实 DeepSeek 调用见第10节；真实 Neo4j / MCP transport / 生产服务的 SDK E2E 仍未验证。

## 9. 已知限制与 Phase 1 可复用接口

可直接复用 `SDKContextHarness(workspace, evidence_tools=...)`、`function_tool(..., invoke=raw_invoker, is_success=...)`、`filter_model_input`、本地 MCP 的 `mcp_result` / `tool_error`、`settle(reason)`、`ObservedToolResult` / `BatchSettlement` 和 `sdk_model(...)`。现有 LangChain 工具可以在 raw invoker 中调用其 `ainvoke` 并继续使用原参数验证；不要包装一个已经吞掉错误的 callable 后再猜测 success。

本阶段的明确限制：

- 只验证新的单角色、非 streaming SDK 调用；Sessions、RunState 恢复、handoff、嵌套角色和审批恢复没有接入。handoff、覆盖本过滤器和 output guardrails 的配置显式拒绝；output guardrail 的最终证据准入需要单独设计。
- 本地 MCP 需要显式安装两个公开采集回调，并由调用方管理连接和清理。仅名字在白名单中而没有可靠状态的工具不会入图；Hosted MCP / hosted search 的证据采集仍未接入。
- GraphExtractor / ContextManager 的内部 LangChain 推理与预算调用按原实现保留；边界适配不把这些服务改造成第二个 Agent Loop。harness.budget_usage 只合并已有策略返回的增量，SDK usage 在 RunResult 中；尚未接入全业务预算准入、失败 HTTP 重试计费或跨角色结算。
- 模型输入过滤只改变本次发送内容，SDK 原始历史仍保留工具原文。跨调用恢复必须重新装配收集器、回执和上下文；不能仅保存 RunResult 就宣称已恢复业务执行。
- pending/live collector 是进程内状态，transcript 是调试事实，不是耐久任务日志；hard crash、租约、跨进程取消和副作用幂等没有在本阶段完成。现有同步图 Store 也不提供硬性异步取消保证。
- 仅验证 micro-compaction；现有 rolling_compact 未接入新 SDK loop。图部分写入和 ContextManager 后续失败不能假设有跨服务事务，失败原文和 pending 仍须保留。
- 旧 Producer 的跨调用重复 URL 回执仍需在后续窄修复中归一到真实已存在的 Source ID；本阶段仅在 SDK 边界阻止不可靠回执压缩，原 LangGraph 路径保持原行为。
- 模拟 HTTP 不证明真实供应商对所有模型、structured output、托管工具、reasoning 和错误重试的完整兼容性；未接入的供应商显式列为未验证。

下一阶段可在这些公开接缝上迁移一个业务角色及其预算/输出合同，再按用户要求推进 Python 确定性工作流；本次没有提前执行。提交说明为 `feat: validate OpenAI Agents SDK Phase 0 context harness`，实际 SHA 和最终分支状态以交付时 `git log -1` / `git status` 为准。本次只创建本地提交，未推送、未合并 main。

## 10. 2026-10-17 真实 DeepSeek 补验

用户指出实际验证请求已包含真实 API 测试，随后按该授权启用真实调用。复用根 `.env` 的 `DEEPSEEK_API_KEY` / `DEEPSEEK_API_BASE` 和现有 `deepseek:deepseek-flash`，实际服务返回 HTTP 200；没有要求重新提供 Key，没有打印或提交凭据。先前 `2a29be4d` 已按用户要求推送到同名 GitHub 分支，本次补验在该分支追加。

### 10.1 实际运行范围与结果

SDK 主模型、GraphExtractor 的抽取模型和 ContextManager 的更新模型均调用真实 DeepSeek。工具只返回两份标为 synthetic 的本地固定材料，Graph 使用 InMemoryResearchGraphStore；这验证了 SDK 与 Graph 的实际模型/协议/策略链路，不代表真实网页检索、社媒采集或 Neo4j 服务验收。

| 最终通过的场景 | 真实模型调用 | 输入 / 输出 tokens | 结果 |
| --- | ---: | ---: | --- |
| 基础 SDK/provider smoke | 1 | 14 / 7 | 严格得到 SDK_SMOKE_OK |
| 非思考 Producer → Consumer | SDK Producer 2 + Graph 2 + SDK Consumer 1 = 5 | 20,107 / 1,918 | 同轮两工具并行、统一批次、第二轮更新上下文、Consumer 首轮读取、引用校验、退出清算通过 |
| 思考 Producer → Consumer | SDK Producer 2 + Graph 2 + SDK Consumer 1 = 5 | 17,880 / 2,112 | 同上；真实 SDK 多轮思考工具调用成功，SDK usage 记录 258 reasoning tokens |

最终通过的三轮合计 **11 次模型调用，38,001 input tokens / 4,037 output tokens / 42,038 total tokens**。这是最终通过轮次的统计，**不包含前面调试失败及重跑的额外用量**；reasoning tokens 已包含在 output tokens 中，没有重复累加或推算货币金额。

非思考轮次实际产生 2 Source、2 Evidence、3 Claim、1 Finding；思考轮次产生 2 Source、2 Evidence、2 Claim、1 Finding。两轮均有 EXTRACTED_FROM、SUPPORTS、DERIVED_FROM、SUPPORTED_BY，工具原文只在写入和 ContextManager 成功后替换为回执。第二次 SDK 模型请求中均恰好有两个压缩输出、一个上下文块、已有 Evidence 和更新后的 Working Context；Consumer 第一次请求已经包含 Evidence，且不写入或压缩 Graph。

思考模式使用 SDK 官方 Chat Completions Model，工具前后两次模型调用均成功；没有自建 reasoning 回放或 Agent Loop。DeepSeek 要求携带 tools 的后续请求保留 reasoning_content，且思考模式不支持 required/named tool_choice；本验证使用 auto 工具选择及 SDK 原生回放。[DeepSeek 思考与工具调用文档](https://api-docs.deepseek.com/guides/thinking_mode/)、[Chat Completions 接口](https://api-docs.deepseek.com/api/create-chat-completion/)

### 10.2 真实补验发现并处理的问题

1. **基础冒烟只打印结果，不能证明输出符合要求。** 原 128-token smoke 在默认思考模式下收到带额外标记的结果，却未报错。现对 DeepSeek 基础文本 smoke 显式关闭思考、设置 256 上限，并严格检查最终输出等于 SDK_SMOKE_OK；补充正确/错误响应的离线回归。
2. **测试预算收缩造成结构化更新失败。** 最初临时使用抽取 2048 / 上下文 1024，上下文模型返回无 parsed result；恢复项目现有 4096 / 2048 后跑通。成功轮次的 ContextManager 输出曾为 1074 tokens，说明不能把 1024 当作可靠上限；早期失败未捕获 finish_reason，故没有把其具体终止原因伪报为已证实。新 runner 记录真实 finish_reason 和 usage。
3. **上下文失败后的去重确认可能假成功。** 真实错误暴露出“图已写入、ContextManager 未完成；后续 URL 去重分支不重建上下文却确认批次”的情况。先用确定性离线测试复现 DID NOT RAISE，再在 SDK adapter 记录需要完成上下文更新的调用；未观察到成功更新时保留 pending，并在退出时报告 ResearchGraphSettlementError。没有通过删断言或把原文压掉制造通过。
4. **抽取字段合同不明确。** 真实模型曾只返回 entities/events，或用 local_ref/supports 等字段，导致 Evidence 或引用关系缺失。只补充 GraphExtractor 提示词的准确字段合同及最小示例，并说明合格事实材料需要 source-bound evidence；正常化、稳定 ID、Store 和核心图算法未重写。新增测试将提示词示例交给既有 normalizer/materialize，验证四类溯源关系。线上断言进一步要求实际关系链完整及模型引用均已存在。

调整涉及：新增 `scripts/sdk_context_smoke.py`；修改基础 smoke、SDK 失败清算保护、GraphExtractor 的提示词；三个测试文件补回归。共享抽取提示词也会被旧链路复用，**Web/API 默认入口和四角色装配仍未切换到 SDK**。

### 10.3 回归、复现与限制

补验后全量离线回归 **557 passed、6 deselected、154 warnings，80.80 秒，退出码 0**。定向 SDK/Graph 回归 59 passed。Ruff 的原 CI E,F,I、新 SDK/脚本完整配置 Ruff、5 个文件格式检查和编译通过。Mypy 仍为 35 errors / 16 files / 103 source files，错误消息集合无新增；依赖未变，原 markitdown-mcp/MCP 冲突仍保留。

离线检查仍禁用 dotenv/tracing 并开启 socket guard；以下真实命令在单独终端显式启用，不混入 pytest：

```powershell
$env:PYTHON_DOTENV_DISABLED = '0'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe scripts/sdk_model_smoke.py --enable-real-api --model deepseek:deepseek-flash
.venv/Scripts/python.exe scripts/sdk_context_smoke.py --enable-real-api --model deepseek:deepseek-flash --output .tmp/agents-phase0/real-sdk-context-disabled-final.json
.venv/Scripts/python.exe scripts/sdk_context_smoke.py --enable-real-api --model deepseek:deepseek-flash --thinking enabled --output .tmp/agents-phase0/real-sdk-context-thinking-final.json
```

真实通过结果、失败轮次、diagnostic JSON、合成工具 transcript 与用量留在忽略的 `.tmp/agents-phase0/`，没有提交运行输出。每次使用新的研究 Run ID，不连接生产数据库；两个 live runner 都要求显式 opt-in。诊断只记录结构化 Graph 输出和计数，不保存模型 reasoning 文本。

仍未完成：其他供应商真实调用、真实数据库/MCP transport、生产 Web/API、streaming/resume、跨角色统一预算和耐久执行。ContextManager 已失败且只剩 URL 缓存命中的批次现在会明确保持未结算，调用方仍需重建/补偿上下文；没有借 Phase 0 实现一套通用恢复引擎。真实模型输出质量仍具有随机性，本补验不等于长期质量基准或已满足所有业务合同。
