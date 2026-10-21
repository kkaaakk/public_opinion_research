# OpenAI Agents SDK Phase 3：Python 确定性 Workflow

日期：2026-10-21（Asia/Shanghai）。仓库：`kkaaakk/public_opinion_research`。

Phase 3 完成 Python 进程内业务编排、显式 State 合并、并行失败取消和费用清算。默认入口仍为原 LangGraph + legacy Runtime；新 Workflow 必须显式选择 SDK Runtime。Phase 4 的 Web/SSE/Session、持久 Run、审批恢复和队列，以及 Phase 5 退役与依赖清理，均未实施。本次没有调用真实付费模型或付费检索 API。

## 1. 基线与范围

- 工作区起始干净。fetch 最新 `origin/main` 后，从 PR #22 的合并提交 `dabb2fbc506c845dc77415637bc9283201db6dde` 创建 `codex/openai-agents-phase3`。
- 实施前读取实际 `AGENTS.md`、准备审计、迁移计划、Phase 0/1/2 审计，以及当前 workflow/runtime/state/budget/Graph 源码。实际旧入口位于 `workflow/graph.py`；`deep_researcher.py` 是兼容导出，并非另一个 Agent 实现。
- 根 Python 3.11 环境执行 locked sync：313 个解析包、250 个已安装包；`openai-agents==0.24.0` 保持不变。没有修改依赖或锁文件。
- 修改前全量基线为 **713 passed / 6 deselected / 154 warnings，55.17s**。Mypy 为 **33 errors / 15 files / 104 source files**。
- 保留其他分支、worktree、stash、备份及未跟踪数据；不修改 `.env`、`CLAUDE.md`、前端、Web/API、Session、生产队列和用户审批接口。不自动合并 PR。

## 2. 实际架构与调用链

```text
旧路径（保留默认）：
deep_researcher / workflow.graph
  → 编译主 StateGraph（8 个节点）
  → research_phase → 编译研究 StateGraph（5 个节点）
  → 双研究 / join / research_review / Send 补查 / Risk → Strategy
  → section_writer → write_final_sections → compile_final_report
  → LangGraph channels 调用原 State reducers

新路径（显式 Python API）：
get_workflow(config) → PythonWorkflow.ainvoke
  → enrich_query_images / clarification_step / research_brief_step / section_plan_step
  → gather_owned(public_signal_agent, internal_knowledge_agent)
  → merge_research_patches → research_review
  → followup_assignments → 仅对应原角色补查 → 汇合 → 再 Review
  → risk_assessment_agent → response_strategy_agent
  → 原章节写作 / 最终章节 / compile_final_report
  → apply_patch 显式调用同一组纯业务合并函数

两路径共享角色调用链：
业务角色函数 → run_business_agent
  → 原 Spec / Prompt / 工具权限 / TaskDescriptor / ResearchWorkspace
  → SDKAgentRuntime.run → SDKContextHarness.run → 唯一 SDK Runner.run
  → 原 Producer/Consumer、Working Context、BatchSettlement、退出清算
  → 原报告服务 → AgentRuntimeResult → 原角色 State patch
```

新 Workflow 不调用 `research_phase`、旧图的 `ainvoke/compile`、`StateGraph`、`Send` 或 Pregel。`research.py` 的兼容图对象改为首次访问时构造；独立导入 Python Workflow 不导入 `workflow.graph`，也不构造研究图。旧 `public_opinion_builder/public_opinion_subgraph` 导出、旧路由和旧工厂继续可用，原测试保持。

本阶段仍有 LangGraph 的**类型/兼容依赖**：唯一 State schema 继续继承原 MessagesState，旧 intake wrappers 继续使用 Command/interrupt。新调度不调用这些 wrappers，所用 intake 决策只是 `IntakeDecision(goto, update)` 数据。没有宣称 Phase 5 的零 LangGraph 依赖已实现。

```python
from open_deep_research.workflow import get_workflow

config = {"configurable": {
    "workflow_backend": "python",
    "business_agent_runtime": "openai_agents",
    "allow_plan_feedback": False,
    # 可选；到期发出取消后，仍等待 SDK 原有有界清算。
    "workflow_step_timeout_seconds": 120,
}}
result = await get_workflow(config).ainvoke({"messages": messages}, config)
```

`WORKFLOW_BACKEND` 与其他 Configuration 字段一样支持环境读取；该开关由新 Python API factory 使用，不接入现有 Web 工厂。`PythonWorkflow().ainvoke(..., start_at="research")` 可接收已经准备好的 brief/section plan，仍从首轮研究开始，不是暂停恢复接口。不允许 legacy Runtime 或计划审批进入新路径；这些配置在任何 I/O 前明确拒绝。Clarification 问题正常返回并结束；自动 planning 的原提示词、结构化重试和预算降级规则保持。人工反馈仍由原图处理，不在 Phase 3 再实现一套审批。

## 3. 状态、所有权和确定性

仍只有 `DeepResearchState` 一套 schema。没有事件日志、第二套 State model、BudgetManager 或新的 Agent Loop。

| 状态域 | 显式合并与所有权 |
| --- | --- |
| messages | 复制/规范化原消息；按 ID 更新，新增消息追加，删除指定 ID；不依赖图消息 channel |
| workflow | Workflow 拥有 brief/review/pending_tasks；轮次单调取 max；completed_tasks 用原 identity 去重；Producer 只可完成本角色任务 |
| agents | 按 role 合并。正式报告默认追加，显式 override 替换；memory 追加，rolling_summary 更新。每个角色只获得自己的 memory/summary，其他角色只暴露正式报告 |
| research | 共享 run_id/已提交 Graph；Working Context/scope 按 role 合并。Producer 不能更换 run_id 或写其他角色的上下文 |
| report | 保留完整计划和已完成章节；章节追加顺序固定，最终按计划顺序、章节名选择最后有效内容。并行 sections/final 替换必须一致，否则拒绝整个 join |
| runtime | budget/Graph metrics 沿用原纯合并语义，仅累计本次 delta；不把每个角色的 initial/cumulative budget 重加一次 |

每个步骤读取独立深拷贝快照。两研究角色在同一轮各自产生 patch，全部成功并验证后，以固定 `public_signal → internal_knowledge` 顺序在候选副本上汇合，最后整体提交；不按实际完成顺序写共享字典。重叠角色私有写入、跨角色任务完成、run 更换或共享 report 冲突被拒绝。章节写作显式返回完成后的计划状态，避免依赖对旧图输入对象的原地修改。

新 research 边界与旧 `research_phase` 一样：保留私有 memory、报告计划、输入预算及 run_id；清除上一研究的角色报告/rolling_summary、review/tasks 和 Working Context。补查延续同一 run，保留角色结果、memory、summary、Graph 引用，使用新 round/task scope。

`followup_assignments` 是新旧路由共享的纯业务判定：research_complete、最大轮次、enabled roles、任务描述有效性及 completed identity 去重决定路由；预算仍不替代证据充分性判断。同角色多个缺口一次执行，Risk/Strategy 不承接取证缺口。Runtime 保留 ResearchComplete、max turns、模型/工具预算与最终报告预留的原语义。

## 4. 失败、超时、取消和幂等

- `gather_owned` 持有所有子任务。任一失败或外部取消时只发出一次兄弟取消，然后 shield 等待全部子任务退出；重复父取消不打断已开始的清算。正常汇合前不会 Review。
- 全部错误按角色顺序保留；选取原始业务错误作为主因，被取消的前序兄弟不能掩盖后序模型/工具失败。`WorkflowExecutionError` 提供 failures/kinds、诊断 State、本次 budget_usage 和各角色 context_settlement/context_harness。取消继续抛原 `CancelledError`，附 workflow_state/workflow_failures/费用，不能返回伪成功。
- 已完成兄弟的有效 patch 在异常诊断 State 中只提交一次；失败角色仅提交已实际发生的费用。Graph 已提交证据不回滚；失败 Runtime 未生成的正式角色报告不会被伪造。该诊断 State 不代表数据库事务或可恢复 Run。
- 可选 step deadline 使用 `asyncio.wait_for`，将取消传播给 SDK。超时不是强杀：先等待 Runtime 原有清算，再抛 TimeoutError 类失败。截止与清算时间分开；默认不额外设置工作流截止。
- 不增加工作流重试。SDK 模型维持 `max_retries=0` 的实际尝试记账；工具层、结构化输出、图阶段清算与报告的既有有限策略复用。本层没有新增外部 I/O 操作，未发现需要在本层重试的新安全读边界；不对已完成角色、成功工具或 memory 写入自动重放。
- 报告章节也采用同一 owned-task 取消/等待规则；章节失败后保留所有已发生调用费用。Planner/最终报告内部 capture 失败和报告已生成但 memory I/O 失败，都显式携带费用。原报告上下文重试耗尽时，旧路径保留原错误报告；Python 使用 `fail_on_error=True` 明确失败。
- 原 SDK call ID 复用、工具回执、来源版本去重、staged persistence、Persist-then-Compact、Consumer 刷新、滚动摘要及 30s settlement 窗口均复用 Phase 0–2，未重新实现。

SDK 可恢复的单工具错误仍按原工具 observation/状态策略处理；不可清算的证据或角色异常向上传播。预算停止可以正常返回明确降级的报告，这与模型、超时、工具/清算失败和取消不同。没有为这些语义引入第五个 Agent。

## 5. 同 fixture 对照与测试范围

新增 **51 项**测试，均为默认全量的一部分，无新增 skip/xfail，无旧断言放宽。原 Phase 2 全链路测试只提取为可复用 fixture；原全部断言继续执行。

- barrier 证明两角色真实并行、全部汇合后 Review；反转完成顺序得到相同合并 State。
- 无缺口、定向补查、同角色分组、双角色补查、重复任务、多轮、上限和提前终止。
- Risk→Strategy 严格顺序、风险报告传入、完整正式报告、私有 memory 和 summary、非零输入预算、角色 Working Context/scope。
- 单失败/双失败、成功兄弟加失败兄弟、超时、外部/重复取消、章节错误主因、清算完成与无遗留调度任务；成功副作用后模型失败不重放工具。
- 真实 SDK Runner + ScriptedModel + 真正 Python Workflow，含完整 clarification/brief/section plan/取证/Review/消费者/报告。GraphExtractor、ContextManager、报告、Usage callbacks、回执和 Store 均执行真实本地代码，只有模型响应和外部工具数据为夹具。
- 长原生历史在两个 Producer 中触发 SDK 原有 rolling compaction，新 State 保留其 summary/私有 memory；低预算保留明确降级与最终报告预留。
- 真实 FastMCP stdio 的 initialize/list/call，经现有 adapter、SDK、Graph、报告走完新 Workflow，并关闭 Session/子进程。

`test_fixed_fixture_langgraph_and_python_business_contracts_match` 用相同材料跑实际旧图及实际新 Workflow：整个 workflow/report、各角色报告与私有 memory、证据节点、任务及每个 budget 字段相等。固定测试 Harness invocation IDs，避免跨执行 UUID 造成不同的 Source/Evidence IDs；正常生产仍使用随机 invocation IDs，不改变 Graph 准入/ID 算法。两次固定 fixture 均为 **7 初始 + 24 增量 = 31 model_calls、6 次工具**，精确 token/details 合同一致。

允许的非确定性差异：原生调用/消息 UUID、时间/延迟、并行完成顺序，以及 Producer 首轮 Working Context 中“本轮过滤时另一角色是否已提交”的历史引用集合。后者来自原逐轮 Graph 刷新语义；合并顺序固定并不把并发数据库可见性改造成事务快照。对照要求事实/冲突/缺口摘要和目标一致、全部引用指向实际最终证据、两 Consumer 的最终 active evidence 一致。没有放宽预算、任务、报告或持久证据对照。

## 6. 本地验证结果

| 检查 | 实际结果 |
| --- | --- |
| locked sync | 成功，依赖/锁不变 |
| 全量 pytest | **764 passed、6 deselected、156 warnings，49.78s，退出码 0** |
| Phase 3 新测试 | **51 passed**；真实 SDK Runner/图/本地 MCP/长摘要包含其中 |
| Ruff CI E/F/I | 全生产包、conftest、全部 test_*.py 及新 smoke 通过 |
| 新模块/新测试/新 smoke 完整 Ruff | 通过 |
| format | 12 个新增/修改 workflow、测试、smoke 文件通过。`configuration.py` 的整体格式检查仍失败；固定 main 副本同样失败，保留其既有整体格式以避免无关全文件重排，新配置声明符合 formatter 风格 |
| compileall | 生产包、安全包、scripts、evaluation 通过 |
| import smoke | 禁凭据/禁 dotenv/tracing/阻断 socket，26 个入口导入通过；独立 Python Workflow 导入不构造或导入 workflow graphs |
| Mypy 原审计命令 | **33 errors / 15 files / 107 source files**，非零；忽略行号后逐消息多重集新增=0、移除=0 |
| 依赖一致性 | 非零，仍仅原 markitdown-mcp 要求 mcp~=1.8.0 / 实装1.23.0 冲突 |
| git diff --check | 通过 |

不把 Mypy、依赖冲突或 Configuration 既有整体格式称为通过。新增 warnings 来自额外调用原兼容图工厂的已有弃用提示；未提高错误容忍或改变测试排除规则。Windows 最初受限环境的 Git 元数据/MCP 命名管道/Docker 管道访问问题通过正常权限执行相同操作解决，没有跳过测试。

## 7. 外部服务与生产边界

本地 MCP **VERIFIED（合成只读服务）**；现有 `neo4j:5.26.31` healthy 容器的 loopback Bolt **VERIFIED（合成 smoke）**。新 `scripts/sdk_phase3_smoke.py` 运行真正的完整 Python Workflow、四角色 SDK Runner 与真实 Neo4j Store；推理为离线夹具。两 Producer 写出 2 Source / 2 Evidence，两 Consumer 读取已提交证据，Strategy 收到风险分析，最终报告及全部退出清算成功。

smoke 仅读取现有本地数据库密码，不加载模型密钥；随机 `phase3_smoke_<uuid>` 先验证不存在，finally 只删除本次 Run，查询确认剩余0，关闭 Store/缓存 driver。没有重启容器或触碰其他 Run。最终输出 `provider_api_calls=0`、`created_run_cleaned=true`；完整 fixture 预算为7初始+21增量=28 model_calls，2工具，235 input / 76 output / 311 total tokens。这是会计夹具数据，不是供应商账单。

未验证真实付费模型/检索、远程 MCP、生产 RAG/MySQL/Milvus/ES、多 worker、供应商质量/性能、跨进程崩溃恢复。同步线程/同步 DB 无法强杀，极晚完成工具继承 SDK 有界 drain 后仍需显式补清算的限制；异常保留 context_harness 所有者，可在工具结束后调用原 settle 接口，不重放角色或工具；新 Workflow 不声称消除了底层限制。没有数据库持久 Run、分布式锁、全局原子硬预算、事务回滚、跨进程 exactly-once。Phase 4 可据此开始集成设计，不能直接将现有 Web 请求改成新路径后称生产验收。

## 8. 修改文件与抽象数量

| 文件 | 职责 |
| --- | --- |
| runtime/sdk_runtime.py | 异常保留原 Harness 所有者，供有界 drain 后的极晚结果显式补清算；无新任务/循环 |
| workflow/python_workflow.py（新增） | 唯一 Python 业务顺序、step deadline/费用、失败诊断 |
| workflow/python_state.py（新增） | 同一 State schema 的快照、所有权、确定性原子汇合 |
| workflow/execution.py（新增） | 一个 owned parallel-task 函数；无模型/工具循环 |
| workflow/intake.py | 纯业务决策与原 Command/interrupt wrapper 分离；保留旧调用合同 |
| workflow/research.py | 共享 follow-up 判定；旧图延迟装配；四角色执行仍委托原 Runtime |
| workflow/reporting.py | 章节任务收尾、显式计划更新、失败费用及新路径严格终态 |
| workflow/__init__.py、configuration.py | 显式 backend selector / 可选 step deadline |
| tests/test_python_workflow.py、test_python_workflow_sdk.py | 51项新增离线验收 |
| tests/test_sdk_phase2_integration.py | 保留原断言并提取双后端可复用 fixture |
| scripts/sdk_phase3_smoke.py | 本地 Neo4j opt-in 合成验收与本次 Run 清理 |
| 本审计、迁移计划 | 交付证据、Phase 3 完成与 Phase 4/5 边界 |

AST 对比 workflow 顶层定义：**新增3个类、16个模块级函数，移除0个类/函数**。三个类仅是 PythonWorkflow、WorkflowExecutionError、IntakeDecision；没有新增 State schema、业务 Agent、Manager/Factory/Adapter/Service 类或 Agent Loop。新增3个生产模块，未移动 runtime/Graph/RAG 所有权。兼容期旧两张图和原循环/依赖保留，Phase 5 再统一退役。

新调用层级中，`get_workflow` 仅选择后端，`_step` 仅提供快照、超时和失败费用，`gather_owned` 仅拥有任务生命周期，`apply_patch` 直接调用原纯合并函数；没有多重 Manager 转发或新的通用工作流解释器。唯一重复循环是原业务要求的 Review/Gap 轮次，不是模型/工具循环。

## 9. 复现

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:UV_LINK_MODE = 'copy'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_python_workflow.py tests/test_python_workflow_sdk.py --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/sdk_phase3_smoke.py
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security scripts evaluation
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
uv pip check --python .venv/Scripts/python.exe
# 单独显式启用，仅本地 Neo4j 与离线推理：
.venv/Scripts/python.exe scripts/sdk_phase3_smoke.py --enable-local-neo4j --output .tmp/agents-phase3/neo4j-smoke.json
```

验证日志、Mypy逐消息比较、配置格式基线、导入探针、transcripts 和合成输出保留在忽略目录 `.tmp/agents-phase3/`，不提交用户配置或运行数据库。MCP 子进程在 Windows 需正常命名管道权限。

## 10. GitHub 与验收结论

实现提交 **[`d276ea6c126191239374b7fa77e06ac50d948f84`](https://github.com/kkaaakk/public_opinion_research/commit/d276ea6c126191239374b7fa77e06ac50d948f84)** 已推送 `codex/openai-agents-phase3`。已创建并关联面向 main 的 **[PR #23](https://github.com/kkaaakk/public_opinion_research/pull/23)**；实际核对 OPEN、merged=false、mergeable=true，未自动合并。PR base 仍为本次起点 `dabb2fbc`。

该实现提交的真实 PR CI **[39574383486](https://github.com/kkaaakk/public_opinion_research/actions/runs/39574383486)** 已全部完成，两个 Jobs 均为 success：

| Job | 实际远程结果 | 固定证据 |
| --- | --- | --- |
| quality | locked 安装、原 Ruff E/F/I、compileall、公共入口导入全部成功 | [119136152921](https://github.com/kkaaakk/public_opinion_research/actions/runs/39574383486/job/119136152921) |
| tests | 读取实际 Job 日志核实 **764 passed、6 deselected、156 warnings，49.45s**，退出码0 | [119136152394](https://github.com/kkaaakk/public_opinion_research/actions/runs/39574383486/job/119136152394) |

首次代码 CI 即通过，无失败门禁补丁、测试放宽或 CI 配置变更。本节是随后补记固定 CI 证据的纯文档提交，也沿现有 push/PR 规则运行 CI；交付前继续核对最终文档 HEAD 的 quality/tests，最新固定链接在交付回复中给出，可从[本分支 CI 记录](https://github.com/kkaaakk/public_opinion_research/actions/workflows/ci.yml?query=branch%3Acodex%2Fopenai-agents-phase3)追溯。

本地 Phase 3 功能与离线合同已满足；继承问题和真实服务限制如上保留。具备进入 Phase 4 的实现基础；Phase 4/5 尚未完成，新路径没有生产 Web/Session/耐久执行验收。
