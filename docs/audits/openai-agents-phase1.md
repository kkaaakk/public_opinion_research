# OpenAI Agents SDK Phase 1：四业务 Agent Runtime 迁移

日期：2026-10-17（Asia/Shanghai）。范围：四个角色的实际模型/工具循环，以及原业务装配层的可选择 Runtime 接入。

**结论：PublicSignalAgent、InternalKnowledgeAgent、RiskAssessmentAgent、ResponseStrategyAgent 均已有真正可执行、通过离线 Runner 测试的 OpenAI Agents SDK Runtime。LangGraph Workflow 保留；Phase 2 完整 Research Graph Context Harness 尚未实施。**

## 1. Git、环境和阶段边界

- 在已有 `codex/openai-agents-phase1` 上实施，起点 `7a72aadc`，即合并后的 Phase 0。开始时工作区干净；未创建、重置或改写分支。
- 保留现有提交、其他 worktree、stash、安全 ref、原始 `.env` 与本地资产；没有使用 `git clean`，没有修改 `CLAUDE.md`，没有合并 DSH/main 或删除旧分支。
- 使用根锁、根 `.venv`、Python 3.11.9 和固定的 `openai-agents==0.24.0`。`jsonschema` 从既有传递依赖显式列为直接依赖，仅增加 root dependency/specifier 两条锁记录，未变更任何已锁包的版本。
- 首次锁同步遇到 Windows 临时目录 PE resource ACL 错误；将 `TMP`、`TEMP` 和 uv 缓存放在工作区后成功执行原锁同步，没有更改依赖版本绕过问题。
- 保持六阶段：Phase 0 可行性；Phase 1 四角色 Runtime；Phase 2 完整 Graph Harness；Phase 3 Python Workflow；Phase 4 Web/SSE/Session；Phase 5 旧 Runtime/依赖退役。

读取了 `AGENTS.md`、准备审计、Phase 0 审计、迁移计划，以及当前 runtime、角色、模型、预算、工具、MCP、Research Graph 和 workflow 源码。接口以本地安装的 SDK 0.24.0 为准，并核对[官方运行文档](https://developers.openai.com/api/docs/guides/agents/running-agents)与[Agent 定义文档](https://developers.openai.com/api/docs/guides/agents/define-agents)。

## 2. 调用链和切换

```text
原有 LangGraph 节点 / StateGraph / Send / reducers
  -> runtime/business_agent.py：Spec、首轮/补查任务、Prompt、工具域过滤、角色状态
  -> business_agent_runtime
       legacy（默认）：AgentRuntime.run -> 原自有 ReAct loop
       openai_agents：SDKAgentRuntime.run
         -> SDK Agent（官方 Model、原 Prompt、FunctionTools、文本输出）
         -> SDK Runner.run（模型 -> 工具 -> 模型，退出）
         -> Phase 0 SDKContextHarness 的采集、输入过滤、退出清算
  -> 原 Graph 报告服务 / _compress_research
  -> AgentRuntimeResult -> 原 State patch / reducers
```

显式启用配置：

```python
config = {
    "configurable": {
        "business_agent_runtime": "openai_agents",
        "research_model": "provider:model",  # 使用已验证适配器支持的实际配置
    }
}
```

也可使用现有配置解析方式设置 `BUSINESS_AGENT_RUNTIME=openai_agents`；不写入或改动操作方 `.env`。`legacy` 是默认值，非法选择直接报错。SDK 失败不会自动回退并重跑业务工具。Web/API 请求模型、SSE、Session、历史、审批和生产入口没有改造。

`AgentRuntime` 仅把既有报告收尾提取为 `_finish`，旧循环保持原实现；SDK 子类复用构造参数、私有记忆和报告合同，从不调用旧 `run`。SDK 循环只通过 Phase 0 wrapper 调用一次 `Runner.run`，没有在外部另写推理循环。同步工具取消处理中的等待仅用于观察已启动线程，不调用模型或重放工具。

## 3. 四角色结果

| Agent / role | SDK Agent/Runner 实际执行 | 保留合同 |
| --- | --- | --- |
| PublicSignalAgent / `public_signal` | 完成；原公众信号 Prompt，允许 core/web_search/social_media；实际检索与继续推理 | 首次研究、针对证据缺口的补查、角色私有记忆、正式报告 |
| InternalKnowledgeAgent / `internal_knowledge` | 完成；原内部知识 Prompt，允许原 Spec 工具域；实际 RAG/工具调用与继续推理 | 内部资料/预案/历史记忆职责，首轮与补查，独立私有记忆 |
| RiskAssessmentAgent / `risk_assessment` | 完成；保留核验与风险合同，消费原有上游报告或 Graph 上下文 | 在 Review/补查完成后执行，角色报告与私有记忆；不增加取证角色路由 |
| ResponseStrategyAgent / `response_strategy` | 完成；保留策略 Prompt 和 RAG/core 工具权限；没有授予社媒写工具 | 风险之后串联、回应/FAQ/行动/监测合同，独立私有记忆 |

四角色均保留 `report`、`memory`、`rolling_summary`、`budget`、`replace_report`。报告包含 role/name/round/mode/expected-output，Graph 模式继续覆盖正式报告，标准模式维持原 reducer 合并方式；记忆仍最多注入本角色最近 5 条，新记忆内容限制为原 1,800 字符规则。已存 rolling summary 被保留并注入，不宣称 SDK 已完成新的 rolling compaction。

首轮/补查 assignment、任务 ID、Review 内容、上游报告选择和 `_build_state_patch` 复用原实现。只有 public/internal 接收补查任务，这个原业务约束没有扩展到 risk/strategy。Spec 文件和所有业务 Prompt 未修改。

## 4. 工具与模型边界

- `Agent.tools` 中是官方 `FunctionTool`；业务实现仍为原 BaseTool / StructuredTool，调用原 `ainvoke`，同步工具沿原 executor 路径运行，异步工具直接 await。原 `RunnableConfig` 继续传给业务工具。
- 注册使用原 `tool_call_schema`，排除注入参数；`strict_json_schema=False` 保留 optional/default 的原参数合同。新增 JSON Schema 验证覆盖 MCP 字典 Schema；随后仍由原工具做其 Pydantic 验证，不把原 Schema 改成另一个必填集合。工具计数按准入尝试记录，包含验证失败，未准入调用不计数。
- 业务装配层保留领域默认拒绝，Runtime 注册及实际执行时再次核对角色领域/名称。重复工具名拒绝；未知模型工具名由 SDK fail closed，业务工具不执行。
- 相同 native call ID 的重复请求共享执行 Task，包括并发重复，不额外执行或计费；同 ID 的参数/身份冲突继续由 Phase 0 collector 拒绝。
- 用原 MCP adapter 执行其实际 `call_tool` 转换路径，保留命名空间、参数、`isError`、ToolException、ToolMessage error 状态，以及文本和 `structuredContent`。结构化 artifact 与文本同时传回 SDK，未重建 MCP 客户端体系。
- 工具 Python 异常、已处理的 ToolMessage 错误、显式业务错误 envelope、RAG 已知失败前缀均记录 failed；明确的 MCP 取消记录 cancelled。使用原业务错误格式 `Error executing tool: ...`，而非从 end hook 猜测成功。
- 模型使用 Phase 0 `models/sdk.py`，保持 `provider:model`、配置/环境凭据和端点解析。支持范围继续是 OpenAI Responses/Chat Completions、DeepSeek、Ark、Groq 的已验证适配；未接入供应商显式报错。SDK 输入是原生字典/响应 items，没有把 LangChain Model/Message 当原生 SDK 接口。
- 保留现有 GraphExtractor、ContextManager、报告生成和工具内部 LangChain 服务；它们不是第二个业务 Agent loop。

一项明确的错误语义差异：未知工具名在 SDK 路径终止调用并返回异常，旧路径则返回配对错误消息后允许继续推理。SDK 的拒绝行为已测试，不会尝试执行未知工具或静默改走旧 Runtime。直接向 `SDKAgentRuntime` 注入自定义官方 Model 的调用方也须保证其 provider client 无隐藏重试；业务装配层创建的客户端已设置并验证该条件。

## 5. 预算与失败/取消兼容

| 项目 | 实现与验证 |
| --- | --- |
| 模型请求 | SDK `on_llm_start` 记录每次实际尝试，包含失败和取消；`on_llm_end` 只读取本次响应真实 Usage |
| Token | input/output/total、cache read/write、reasoning 映射回原 budget；不把 `RunResult` 累计用量再次相加；无 usage 不估造 |
| 重试 | 业务 SDK 的 owned OpenAI client 设置 `max_retries=0`，`ModelSettings.retry.max_retries=0`；避免 hook 看不到的 HTTP 重试。失败向上传播，不另写重试循环；Phase 0 其他调用方默认不变 |
| 模型准入 | 在公共 input filter 处检查既有 model/token/call 预算并保留最终工作流报告预留；达到额度结束 SDK 推理，再沿原报告路径收尾 |
| 工具/搜索 | 并行调用在第一次 await 前预占原 tool/search 增量；预算拒绝返回配对工具结果，记录 failed 和降级原因，不执行业务工具 |
| 嵌套成本 | 每个工具独立 capture 并合并一次；Graph 模型和报告服务捕获真实调用增量；不重复合并 Harness 返回的同一预算。压缩失败/取消也携带已发生的 attempts |
| State | Runtime 返回本角色 delta，绝不再次提交 initial/cumulative usage；原 LangGraph reducer 汇合。编译子图测试以非零初始预算验证无重复累计 |
| 错误/取消 | 原异常向上传播，附 `budget_usage` 和 `context_settlement`；兼容 ambient budget capture。失败时不返回伪成功报告 |
| 同步取消 | 已启动线程不能由 awaiter 杀死；保持 collector 活跃、复用退出 drain，有限迟到工具的真实成功及内部用量完成后仍传播父任务取消 |
| 研究终止 | `ResearchComplete` 使用 SDK stop-at-tool，max_turns 和预算停止复用原报告收尾；不重新启动 Runner |

预算仍保持既有粒度：Token 是供应商响应后实测限额，不能提前精确预测；工具内部每 URL 扇出、Graph 服务内部重试和并行 LangGraph 节点继续沿原行为结算，尚非全局原子硬配额。SDK 路径没有借迁移伪称消除了这些既有边界。若已无 Graph 角色报告模型额度，明确输出保留研究笔记及降级原因，不额外消费最终工作流报告预留。

Phase 0 的有限清算窗口、不可取消的同步存储、极晚完成任务和进程崩溃限制仍适用；有 pending/in-flight/settlement error 时不能宣称预算或证据已最终结清。跨进程补偿、耐久取消/恢复仍未实施。

## 6. Phase 0 复用与未实现能力

直接复用 `sdk_model`、`SDKContextHarness.function_tool`、官方 RunHooks、`call_model_input_filter`、collector、入图/消费接缝和 shielded exit settlement。窄扩展：允许 standard workspace 只采集/清算、不入图；允许保留原非严格工具 Schema 和业务错误 formatter。Phase 0 独立 Harness 的默认行为与测试保留。

业务 Producer 的 evidence-tools 集合来自已经过权限过滤的本角色工具，按原 Producer 规则排除 `think_tool` / `ResearchComplete`；没有只收默认两种工具而遗漏社媒/MCP 别名的成功结果。Consumer 沿 Phase 0 接缝读取现有 Graph；Research Graph 核心实现没有修改。

以下均未宣称完成：

1. **Phase 2 完整生产 Harness**：跨四角色的完整 Producer/Consumer 证据交接、完整 Working Context 更新策略、所有批次/失败/压缩/终止的全链路集成及真实服务验收。当前只是单调用接缝与原图策略复用。
2. **SDK rolling compaction**：超过原配置的 context compaction threshold 时显式抛 `SDKContextLimitExceeded`，请求不发送，也不丢弃历史；保留已有 summary 和 Phase 0 receipt-backed micro-compaction。完整 rolling compaction 留给 Phase 2。
3. **未经验证的工具/内容路径**：托管/原生搜索字典在 SDK Runtime 注册时拒绝；本地文本与结构化工具已验证，multimodal MCP 输出显式返回失败而不伪装成已理解图像。相关限制均有定向测试，旧 Runtime 路径保留。
4. **工作流替换**：未实施 Phase 3；`workflow/` 无源码改动，StateGraph/Send/join/Review/Gap/risk/strategy 顺序不变。
5. **Web/SSE/Session、身份审批、RunState 恢复、跨进程取消、耐久执行、生产轨迹**：未实施 Phase 4，也没有合并 DSH。
6. **退役和全仓简化**：未删除旧 Runtime、LangGraph/LangChain 或清理其依赖；未进行 Phase 5 或迁移后独立待办。

本次没有调用付费模型或搜索 API。离线 mock HTTP 不代表真实供应商、Neo4j/MySQL/Milvus/ES、远程 MCP transport 或生产部署已验收；Phase 0 真实 DeepSeek 结果保持原审计范围。

## 7. 自动化验证

SDK 角色定向测试使用官方 `agents.testing.ScriptedModel` 或官方 Model + mock HTTP 执行真实 Runner；只替换模型/provider 或外部存储边界，没有伪造 Runner。新增 90 项测试，另完整复跑 Phase 0 的 SDK 测试。

- 四角色实际模型→工具→模型、同步/异步、参数错误（包括 MCP dict Schema 和畸形 JSON）、角色注册和执行权限、重复 call ID、异常、取消、预算和原业务输出。
- 真实 MCP SDK result 经既有 langchain-mcp adapter 的协议/错误/artifact 路径；官方 Responses/Chat Completions 模型的 mock HTTP 序列化及单次 503 失败，无隐藏重试。
- 四角色各自沿现有 Graph 接缝执行，包含社媒/MCP 工具别名、报告覆盖和状态更新；这不是 Phase 2 的跨角色全链路验收。
- 现有编译 LangGraph 子图实际执行五次 SDK 角色调用：首轮双研究用 barrier 证明并发，join 后 Review，公众信号补查后再次 Review，风险→策略串联；检查非零起始预算、完整正式报告、completed tasks 和私有记忆合并。
- 四角色 legacy 业务装配层实际执行原模型/工具循环与报告压缩，确保兼容路径不是仅构造对象。

| 检查 | 修改前真实基线 | 最终结果 |
| --- | --- | --- |
| 根锁 sync | 成功；313 个解析包 | 成功；无包版本变更 |
| 全量离线 pytest | **574 passed、6 deselected、154 warnings**；79.94s | **664 passed、6 deselected、154 warnings**；53.87s，退出码 0 |
| SDK 定向（含 Phase 0） | Phase 0 全部保持 | **136 passed**；21.24s |
| 新 Phase 1 定向 | 无 | **90 passed**（包含于全量及 SDK 定向） |
| Ruff CI E,F,I | 原基线通过 | 通过；未降低规则 |
| SDK/新测试完整 Ruff、format | Phase 0 基线通过 | 通过 |
| Mypy 原审计命令 | **35 errors / 16 files / 103 source files** | **35 errors / 16 files / 104 source files**；错误消息集合新增=0、移除=0 |
| compileall | 原基线通过 | 生产包、安全包、scripts、evaluation 通过 |
| Import smoke | 原审计 17 入口 | **24 入口**，dotenv/凭据/tracing/出网禁用 |
| CLI smoke | Phase 0 已有 | `sdk_model_smoke.py --help` 通过，未启用真实 API |
| `uv pip check` | 既有 MCP 冲突 | **仍失败 1 项**：markitdown-mcp 要求 `mcp~=1.8.0`，安装 `1.23.0`；无新增冲突 |

最终数字使用本分支实际基线，未把历史 557 当成当前测试数量。全量仍只沿用项目原配置排除 6 个 integration；没有扩大 skip、删除旧测试或弱化旧断言。初轮新增测试失败暴露的非对象参数报告投影、MCP artifact 字典读取和报告错误用量已修复后重测；Phase 0 的原客户端默认构造参数也保持兼容。

部分 Windows 测试进程退出时仍打印 pyreadline3 `Console.__del__` / WinError `0xc0000008`，pytest 退出码为 0；这是已有第三方控制台析构问题，不视为功能验证，也未在本阶段修改。

## 8. 复现命令和日志

在仓库根目录、根 `.venv` 执行：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:TMP = Join-Path (Get-Location) '.tmp/agents-phase1'
$env:TEMP = $env:TMP
$env:UV_LINK_MODE = 'copy'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_sdk_runtime.py tests/test_sdk_business_integration.py tests/test_sdk_context.py tests/test_sdk_models.py --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/audit_repository.py scripts/sdk_model_smoke.py
.venv/Scripts/python.exe -m ruff check src/open_deep_research/runtime/sdk_runtime.py src/open_deep_research/runtime/sdk_context.py src/open_deep_research/models/sdk.py tests/test_sdk_runtime.py tests/test_sdk_business_integration.py
.venv/Scripts/python.exe -m ruff format --check src/open_deep_research/runtime/sdk_runtime.py src/open_deep_research/runtime/sdk_context.py src/open_deep_research/models/sdk.py tests/test_sdk_runtime.py tests/test_sdk_business_integration.py
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security scripts evaluation
.venv/Scripts/python.exe .tmp/agents-phase1/import_smoke.py
.venv/Scripts/python.exe scripts/sdk_model_smoke.py --help
uv pip check --python .venv/Scripts/python.exe
```

日志和基线对比保留在忽略的 `.tmp/agents-phase1/`：`baseline-pytest.txt`、`full-final.txt`、`sdk-final.txt`、`baseline-mypy.txt`、`final-mypy.txt`、`dependencies.txt`、`import-smoke.txt`。Import probe 复用准备审计的 17 入口无凭据/无出网脚本，再加载 7 个 SDK/角色模块；它是本地验证资产，不是新生产入口。Mypy 和依赖检查的非零退出码如实保留。

## 9. 交付后下一步

Phase 1 交付为现有 `codex/openai-agents-phase1` 上的提交并推送，不合并 main。四业务 Runtime 迁移完成，生产默认仍保持兼容。

Phase 2 需要继续完成完整 Research Graph Context Harness：对全部业务工具批次及失败状态建立生产准入，贯通四角色证据传递和 Working Context，实施 SDK rolling compaction，统一压缩/终止/异常清算与预算，并以真实服务和全链路用例验收。LangGraph 替换仍等 Phase 3，不能因 Phase 1 的离线子图验证提前宣称后续阶段完成。

## 10. GitHub CI 最终验收（2026-10-18 补充）

本次仅补齐远程 CI 验收。在已有 `codex/openai-agents-phase1` 上从 Runtime 实现提交 `121f660853c520870b26c5e4b2edbb5f21315732` 继续，未修改业务源码、架构、测试、依赖或 Phase 2 范围。

检查发现 `.github/workflows/ci.yml` 的 push 白名单仅包含 `main` 和 `codex/openai-agents-migration`，因此此前 Phase 1 push 没有触发 CI。将准备分支的单个名称替换为 `'codex/openai-agents-*'`，复用于 Phase 0、Phase 1 和后续 Phase 分支，同时继续覆盖准备分支。`main`、`pull_request`、只读 permissions、`quality` / `tests` 的全部步骤和命令保持不变。没有使用跳过 CI 的提交标记，也没有降低门禁。

最终 CI 配置提交 SHA：**`8b71ccdffa4ec0a898b30db0fc9a58fe7b5af775`**。该提交已推送，并由真实 `push` 事件触发 [CI 39118677318](https://github.com/kkaaakk/public_opinion_research/actions/runs/39118677318)，运行结论为 **success**，所有 Jobs 均已完成：

| Job | 结果与实际验证 | 用时 / 链接 |
| --- | --- | --- |
| `quality` | success；根锁安装、原 Ruff E/F/I、compileall、公共 runtime boundaries import 均通过 | 41s；[Job 117649613035](https://github.com/kkaaakk/public_opinion_research/actions/runs/39118677318/job/117649613035) |
| `tests` | success；Linux / Python 3.11.17，收集 670 项，**664 passed、6 deselected、154 warnings**，pytest 退出码 0 | Job 1m17s，pytest 39.75s；[Job 117649613229](https://github.com/kkaaakk/public_opinion_research/actions/runs/39118677318/job/117649613229) |

本地额外检查了 YAML 解析、main/准备分支/Phase 0–5 的 glob 匹配，比较修改前后 `jobs`、permissions 和 PR trigger 完全相同；`git diff --check` 通过。远程首次运行即通过，没有失败 Jobs，也没有为了通过 CI 修改任何业务行为或测试断言。

远程运行存在非阻断提示：旧版 checkout/setup-python/setup-uv Actions 的 Node.js 20 弃用及托管方切换到 Node.js 24、`ubuntu-latest` 即将迁移 Ubuntu 26，以及两个并发 Jobs 争用同一 uv cache key 导致其中一次缓存保存未成功。实际安装和质量/测试命令均成功；这些提示没有被隐藏，本次不捆绑升级 Actions 或 runner 镜像。第7节的既有 Mypy 35 项错误和 MCP 依赖冲突仍保留：现有 CI 不包含这两项门禁，本次没有将它们改称通过。

以上固定链接及完整 SHA 对应最后一次 CI 配置变更。本节随后以纯审计文档提交收尾，该提交也由同一 push 规则运行 CI，交付前等待两个 Jobs 全部结束。包含本节的最终审计提交 SHA 可由 `git log -1 --format=%H -- docs/audits/openai-agents-phase1.md` 获取；最终 HEAD、对应 CI 运行链接及远程一致性检查在交付回复中列出。[本分支 CI 运行记录](https://github.com/kkaaakk/public_opinion_research/actions/workflows/ci.yml?query=branch%3Acodex%2Fopenai-agents-phase1) 可追溯配置提交和文档收尾提交。

本次变更文件仅为 `.github/workflows/ci.yml` 和本审计文档；不合并 main，不执行 Phase 2。
