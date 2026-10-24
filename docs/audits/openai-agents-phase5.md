# Phase 5：旧运行时退役与最终架构收敛

日期：2026-10-24（Asia/Shanghai）。基线为最新 `origin/main` / Phase 4 PR #27 Merge Commit **`a8574f61d0adba4b07ec860d5c6d7de0a0041e76`**，独立分支 `codex/openai-agents-phase5`。远端 SHA 通过 GitHub 连接器及 `git ls-remote` 双重核对。未修改 `.env`、凭据、四角色 AgentSpec/提示词、无关 worktree、stash、备份分支或学习资产。

本次将新研究统一到 **DBOS 3.3.0 + OpenAI Agents SDK 0.24.0 + Research Graph Context Harness**，移除旧 Graph/手写循环。DBOS 的 Workflow、Step、Queue、消息、持久化与恢复继续使用 Phase 4 实现；本次仅补充一个读取终态/游标的 P1 竞态修复，不重做 DBOS。

本地原生合同共 85 个不同用例通过，最新 Commit/CI 以 PR checks 为准；下文区分实际执行结果和未验证的生产事项。全仓格式及一个既有依赖冲突未通过，不能解读为全仓零遗留验收。

## 1. 审计基线与依赖清单

先阅读 `AGENTS.md`、准备审计、迁移规划、Phase 0–4 审计、Phase 4 DBOS 决策与部署说明，再检查实际源码。没有将早期文档中“默认 legacy / 不采用 DBOS”等描述当作现状。

删除前冻结结果：**786 passed / 80 deselected，100.10s**，禁 dotenv/tracing、拦截非 loopback socket；现有 CI 口径 Ruff E/F/I 通过；Node **5 passed**。全仓启用规则 Ruff 为 **439 errors**；`ruff format --check src tests scripts` 为 **88 个文件存在差异**，这些是检查结果，不算通过。

文件级证据见 [可复现 inventory](openai-agents-phase5-inventory.json)，生成器为 [phase5_inventory.py](../../scripts/phase5_inventory.py)。它记录固定基线中 170 个引用文件、103 个 Python 文件的 AST import 及反向消费者、处置分类、36 个退出锁文件的包，以及全量 `src/**/*.py` 的前后 SLOC。静态边不代替动态入口验证；第三方可选探测、外部脚本和历史数据单独说明。

| 分类 | 文件 / 消费链 | 处置 |
| --- | --- | --- |
| 1 可删除 | `runtime/agent_runtime.py` | 删除手写模型/工具循环、旧压缩请求准备；报告/私有记忆助手移入唯一消费者 `SDKAgentRuntime`，没有新增运行时基类 |
| 1 可删除 | `workflow/graph.py`、`langgraph.json` | 删除 Graph Factory、全局编译图与 Graph Server 启动入口 |
| 1 可删除 | `src/security/auth.py` | 唯一部署消费者为已退役 Graph Server 配置；保留的 FastAPI Token、owner、审批和控制权限由 `web/runs.py` 负责 |
| 2 迁移 | `workflow/research.py` | 删除 `StateGraph`、`Send`、子图动态导出和 `research_phase` 包装，保留角色函数、Review、纯 followup 分配 |
| 2 迁移 | `workflow/intake.py` | 删除 `Command`/`interrupt` 适配，DBOS 直接消费既有 `IntakeDecision`；不可变计划审批仍由 native workflow 管理 |
| 2 迁移 | `state.py` | 普通 TypedDict 取代 MessagesState；消息规范化和确定性汇合继续使用 `python_state.py`，未新增状态机 |
| 2 迁移 | `runtime/business_agent.py`、`runtime/__init__.py` | 仅装配 SDK Runtime，无运行时选择或失败 fallback |
| 2 迁移 | `web/server.py`、`web/runs.py`、`web/static/app.js` | 所有新研究创建 durable Run；删除请求内图执行、流驱动归档、信号量、旧解析/abort 分支和执行引擎 UI 开关 |
| 2 迁移 | `configuration.py`、`config_values.py` | 移除两个选择字段，输入别名只验证并丢弃；客户端旧引擎选择 400，旧操作员环境配置 503，均在执行前拒绝 |
| 2 迁移 | `models/factory.py` | 单次业务模型工厂改从已存在的 `langchain-classic.chat_models` 取 `init_chat_model`，保留供应商适配，消除顶层 `langchain` 对 LangGraph 的传递依赖 |
| 2 迁移 | `mcp/tools.py` | 移除依赖 Graph Server store 的 OAuth token 缓存；保留受信配置 token exchange，逐次获取、不跨 owner 共享、失败不匿名连接 |
| 2 迁移 | `web/cli.py`、`tests/debug_run.py`、`tests/run_evaluate.py`、`tests/agent_stress.py`、`locustfile.py` | CLI/调试/评测/压测统一请求 Run API，带 Token 与幂等键，读取 native 阶段/终态；未运行付费评测 |
| 3 保留业务 | `rag/`、`research_graph/`、`memory/`、MCP/social adapters、工具权限、风险/策略、报告 | 保留原数据所有权、模型服务与工具语义；RAG 知识图的当前实现不需要 LangGraph，未为删除依赖破坏检索 |
| 3 保留执行 | `dbos_workflow.py`、`run_facts.py`、`business_steps.py`、`execution.py`、`web/executor.py`、`sdk_context.py` | 原调度、许可、清算、Finalizer 唯一恢复权、执行代次及未知效果保护保留；应用版本明确升级 |
| 4 历史兼容 | `web/history.py`、LangSmith 历史导入/元数据、旧时间和 source 名称 | 读取/导入旧报告，不启动旧执行器；旧 owner 不自动公开 |
| 4 有限兼容 | `deep_researcher.py`、`workflow.get_workflow` | 保留仍有消费者的纯业务函数导出；旧 Graph 导出消失；工厂 stub 仅给出迁移错误，不能运行研究 |
| 历史材料 | 早期审计、评测 JSONL、固定版本教学/面试图表、`CLAUDE.md` | 保留原证据；当前文档入口及两套学习 README 标注版本边界，未重生成资产 |

### 依赖退役

根锁 **317 → 281 个 package entries**，退出 **36 个包**。LangGraph 相关运行时包归零：`langgraph`、`langgraph-api`、`langgraph-checkpoint`、`langgraph-cli`、`langgraph-prebuilt`、`langgraph-runtime-inmem`、`langgraph-sdk`。无独立 RAG 消费者要求保留它们。

另移除无消费者的 `langchain-tavily`、其顶层 `langchain` 依赖、旧 Auth 专用 `supabase`，以及随 Server/Auth 退出的传递依赖；完整 36 项列表见 inventory。Tavily 原客户端和本项目工具没有移除。显式声明现有 `langchain-classic` 工厂依赖及测试的 `pytest-socket`。

LangChain Core、Classic、Community、各 provider adapter、MCP adapters、text splitters、LangSmith 保留，用于业务单次模型调用、文档/检索、协议工具、类型与可选观测。没有将包名包含 LangChain 等同于仍有业务 Agent Loop。`langchain-mcp-adapters` 会探测可选 LangGraph；在包缺失时使用普通 MCP/ToolMessage，入口 smoke 验证此路径不需要旧引擎。

`uv pip check` **未通过**：仅继承的 `markitdown-mcp` 要求 `mcp~=1.8.0`，根环境按现有 override 使用 `1.23.0`；未增加冲突。MarkItDown MCP 是仍有配置消费者的可选文档工具，未为了制造依赖检查全绿而删除功能或更换其环境；独立隔离/升级需后续实施。

## 2. 前后执行链与职责

迁移前默认链路：

```text
Web /api/research 或 Graph CLI/调试
 → deep_researcher factory → LangGraph 主图 / intake Command
 → research_phase → Public Opinion StateGraph / Send / review loop
 → 四角色函数 → run_business_agent → legacy AgentRuntime 手写循环
 → LangChain model/tool → Workspace / 业务工具
 → 报告 / Memory → Web 流消费时归档
```

迁移前的显式 DBOS 路径已经是 `Run API → RunClient → DBOS → SDK Runtime → Harness`，但 SDK Runtime 仍继承旧循环类的共用助手；另可在旧 Graph 中选择 SDK，维护多个执行组合。

迁移后完整链路：

```text
浏览器 / deep-research CLI / 调试及评测客户端
 → FastAPI /api/runs（或 /api/research 创建后订阅）
 → RunClient：Token / owner / Session / 幂等 / 配置 / 资源准入
 → DBOS por.run → por.plan → native recv 不可变计划审批
 → por.research
    → 两个 por.role Producer 并行 → 固定角色顺序 merge_research_patches
    → Review → 按角色聚合 Gap → 定向补查 → Review
    → Risk → Strategy → 章节 / Final Report → Memory

por.role → business_step → 原角色函数 → run_business_agent
 → SDKAgentRuntime → SDKContextHarness → 原生 Runner.run
 → 默认拒绝的工具准入 / 业务工具 / RAG / Research Graph
 → staged persistence / receipt / context settlement
 → Persist-then-Compact / rolling summary / 角色报告 / usage

独立 por.events → DBOS Durable Stream → seq / generation / SSE 重连
独立 por.finalize → History archive / 补偿 / 已授权的安全 rewind
```

DBOS 负责业务编排、执行权、队列、持久步骤、消息与恢复；SDK 负责四角色唯一模型/工具循环；Harness 负责证据、私有上下文、压缩、预算与清算；RAG/Graph/Memory/MCP/报告保留业务职责。FastAPI 和 CLI 负责准入、控制、订阅及展示。没有新增工作流引擎、DBOSRunner、SQL Worker、lease 或自研恢复解释器。

Planning、Intake、Review、Report 中的 LangChain 调用分别为澄清、brief、章节规划、证据充分性结构化判断、章节写作及最终报告；抽取、上下文更新、压缩、Vision、检索重写和 Memory 同样是有界单次业务调用。结构化解析重试按实际尝试计费，不根据工具输出驱动 Agent Loop，因此保留。Review 结果由 DBOS 确定性编排使用，不成为新的自由调度 Agent。

### 兼容与排空

- 新研究不需要选择引擎。`workflow_backend=python/dbos`、`business_agent_runtime=openai_agents` 仅作输入别名并丢弃；`langgraph`、`legacy`、未知值明确拒绝，不能落回旧执行器。
- API 继续返回兼容的执行信息，`execution_engine=dbos`；新配置 schema 中没有选择开关。
- 应用版本 `dbos-phase5-v1`，SDK runtime `sdk-phase5-v1`。Phase 4 SQL Runs 和 Phase 4 DBOS journals 各自按原发行版受控排空；新代码不领取旧 SQL 队列，也不把旧审批或未知工具单元复制成 DBOS 恢复。
- 部署前停止旧准入、盘点 queued/running/paused/unresolved/archive-pending、原版完成/明确取消并清算、保留数据库和回执，再切新版本。回滚同样先停准入并保留匹配的 Executor，不跨引擎/版本重投。
- 操作步骤、初始化、健康检查、CLI 示例及旧 key/fingerprint 处理见 [部署说明](../architecture/dbos-deployment.md)。本任务没有连接生产私有数据库或实际执行生产排空。

## 3. P1：终态回执与游标读取竞态

首轮 82 项原生矩阵通过后，最终默认 `/api/research` 复验实际复现：SSE 收到 seq 72 的 completed `run_status` 就结束，seq 73 的 `done` 没有送达。未通过删除断言将该失败改成成功。

原因是 `RunClient.get()` 先读取 cursor，再读取 done/history。relay 可在两次读取间提交终态，投影得到“新的终态回执 + 旧游标”。SSE 误判 `after >= seq`，提前结束。

修复仅在已读取这些 native 回执后再次读取原生 cursor，并取单调上界。relay 本来就先提交 stream/cursor 后提交对应回执，因此公开投影不会用较旧游标确认一个尚未交付的终态事件。DBOS journal、调度、消息和 Finalizer 逻辑不变。

`test_sse_terminal_receipt_cannot_close_over_an_older_cursor` 在真实 DBOS/PG 中强制前两次 cursor 读取落后，done/history 读取观察新回执，断言最后一个 done 仍交付且模型次数不增加。修复后与 Web/API/CLI 三入口真实 SDK E2E **4 passed / 41.43s**。随后重新执行完整 84 项原生矩阵并另验真实 Neo4j 停启，最终结果见验证表。

最终输入类型回归另实际复现 8 个 500：旧选择字段传数组/对象时，set membership 先触发 TypeError。已先验证字符串类型，再判断允许别名，两个 API 均在访问存储前返回 400。保留新增断言；25 项入口定向合同和最终 804 项完整离线回归通过。该校验不改变有效请求的执行/恢复语义；85 项原生合同及全部 CI 仍以最终提交重跑为准。

Phase 4 的 retry grant 仍绑定失败代次、观察版本及 native target/journal 版本；重复旧 key 只确认旧请求。仅 Finalizer 可执行 rewind，丢 ACK 不成为再次授权。此修复没有增加 HTTP 恢复路径或放宽授权 CAS。

## 4. 业务合同与验证口径

| 合同 | 核对资产 / 实际路径 |
| --- | --- |
| 双 Producer 并行、确定性 join、Review/Gap、Risk → Strategy | `test_dbos_contracts.py`、`test_python_workflow_sdk.py`、纯 followup/reducer 合同；真实 Runner 工具和模型屏障 |
| Source/Evidence/Claim/Finding、四角色隔离、图刷新 | SDK/Harness/Graph 原有合同；native 四角色与固定业务 reference 对照 |
| Persist-then-Compact、完整批次、失败/取消清算、费用 | `test_sdk_phase2_integration.py`、`test_sdk_runtime.py`、`test_token_accounting.py`、native failure/unknown/ACK loss |
| RAG、MCP、Memory、工具默认拒绝 | 保留原 RAG/权限/本地 MCP transport 合同；新增 OAuth 无引擎 store/不共享 token/失败拒连 |
| 默认 API / CLI / 新页面 | 无选择字段、幂等键、旧环境拒绝；CLI 受控 HTTP 传输进入实际 FastAPI + DBOS + SDK，页面 Node 验证默认 durable |
| Web/SSE/History/审批/取消/安全重试 | 原 native 公开合同及新增游标竞态；旧 Web 归档实现测试迁为真实 DBOS/Neo4j 测试 |
| Executor 重启、PG 故障、旧活执行者 fencing、重试代次 | `test_dbos_process.py`、`test_dbos_retry.py`；真实进程树终止/重启及 PostgreSQL 停启 |

没有删除 pytest 用例来提高通过率。基线 7 个旧 Web/History 用例移入 integration：6 个 History/失败/取消用例和 1 个并发限制用例；原请求信号量合同由真实 native queue/领域资源上限验证。6 个旧循环上下文测试改为 SDK 原生历史/输入过滤合同，旧 Graph 结构断言改为 DBOS 拓扑和禁止旧执行路径的断言。完整正式业务报告、私有记忆、预算、来源 ID 和错误传播断言继续保留。

`tests/reference_workflow.py` 仍仅为固定业务比较基准，未被生产导入，不调度生产任务。原 LangGraph/Python 差分测试改为重复受控 SDK 执行的确定性比较；真实新引擎等价由 native/reference 对照证明，不将 reference 的通过数冒充 DBOS 验收。

| 检查 | 实际结果 |
| --- | --- |
| 删除前全量离线 | 786 passed / 80 deselected，100.10s |
| 最终全量离线 | **804 passed / 91 deselected，53.60s**，socket guard；没有 paid model/API 调用 |
| P1 前完整真实本地矩阵 | 82 passed / 25 deselected，962.99s；后续复验仍发现并修复上述竞态，不能以此旧结果作为最终验收 |
| P1 后 targeted 原生回归 | 4 passed，41.43s：确定性游标竞态 + `/api/runs`、`/api/research`、CLI 的实际 SDK/DBOS E2E |
| P1 后最终完整原生矩阵 | **84 passed / 25 deselected，947.39s**；另真实 Neo4j 停启合同 **1 passed / 60.26s**，合计 **85 个不同原生合同 / 0 skipped / 0 xfailed**。CI 选择相同文件并一次运行这 85 项；另 6 项外部环境/历史集成测试不计入 |
| Node（首轮实现验收） | **6 passed**，含新页面默认 durable；原 5 项刷新、重连、审批、取消合同保留。最终 Review 为 7 项，见第 7 节 |
| CI 口径 Ruff | **通过**：全活动源码、`tests/test_*.py`、conftest，另检查新 scripts/CLI/压测客户端；新 scripts/CLI 全启用规则通过 |
| 格式 | **48 个新增/修改 Python 文件通过**；`configuration.py` 保持继承的整文件格式。全仓同口径 **68 个文件仍不符合格式**（基线 88），不是全仓格式通过 |
| 全启用规则 Ruff | **422 errors**（基线 439）；CI 之外的既有 D/UP/T 等债务未整体重排或放宽规则 |
| Compile / 入口 import | compileall 通过；**32 个生产入口导入通过**，移除凭据、禁 dotenv/tracing/联网，旧引擎缺失仍可加载 |
| 根锁 / 依赖 | locked/offline 同步通过，SDK/DBOS 固定版本；pip check 的 MarkItDown/MCP 继承冲突未通过，新增冲突 0 |
| CI | quality/tests/native PostgreSQL+Neo4j jobs 包含新增合同；最终结果以 PR 最新 Commit 的完整 checks 为准，不以旧绿勾替代 |

迭代失败如实保留在工作日志：首次原生迁移测试把四个 History 数值计数与完整 budget 字典混比、把未知 opaque 单元当作 safe failure、假定 native 全局 FIFO；修正为既有 schema/安全语义并增加精确断言。History 的 `interrupted → incomplete` 是旧 schema 的明确投影，未知效果仍 unresolved/不可重试；没有改成伪成功。首次依赖移除暴露顶层模型工厂导入，已改用 Classic 并通过工厂路由合同。负向退役 guard 对新历史 inventory 的原始固定基线符号要求历史标注，采用既有文档约定，guard 本身未修改。压测客户端的缺失初始 success 值由新增 transport 合同发现并修复。

Windows 同步曾遇到测试进程占用旧扩展 DLL；在该测试退出后用缓存修复包元数据并完成同步，没有终止其他进程或清理无关资产。退出时的 pyreadline/SWIG 既有解释器析构提示不算测试失败被跳过；pytest 的断言结果和 exit code 如表记录。

复现命令：

```powershell
uv sync --extra dev --locked --python 3.11
.venv/Scripts/python.exe -m pytest -q --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
$env:TEST_DBOS_POSTGRES_CONTAINER = 'por-dbos-phase5-postgres-1'
$env:TEST_DBOS_NEO4J_CONTAINER = 'por-dbos-phase5-neo4j-test-1'
.venv/Scripts/python.exe -m pytest -m integration tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py tests/test_web_history.py tests/test_p1_fixes.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe scripts/phase5_import_smoke.py
.venv/Scripts/python.exe scripts/phase5_inventory.py --output .tmp/agents-phase5/inventory.json
node --test tests/durable_frontend.test.cjs
```

测试 fixture 禁用 dotenv/tracing。原生测试使用独立 Compose project `por-dbos-phase5` 的 PostgreSQL 17.6 / Neo4j 5.26.31，PG fixture 只创建/删除自己的 `por_test_<uuid>` 库；实际停启故障只针对本次容器。Neo4j 停启用例验证真实 History 查询 503、报告及数值费用保留、恢复后独立归档成功且 SDK 不重跑；其余存储故障注入与真实服务故障分开记述。其他业务 Neo4j/MySQL 没有参与。已有 6 项其他 integration 测试未混入离线或 native 通过数。

## 5. 代码净变化与维护复杂度

与固定 main 对比，完整生产 Python `src/**/*.py` **净减少 802 SLOC / 991 物理行**，包含新 CLI、SDK 助手迁移和 P1 修复。SLOC 排除空白、独立注释及 docstring；不是仅统计删除文件。20 个生产 Python 文件的逐文件前后值见 inventory。静态 UI 另为 JavaScript **净 -39 物理行**，HTML 行数不变；测试、脚本、文档和锁文件另计。

核心变化：旧 Runtime -365 SLOC、旧 Graph -80、旧 Auth -100、Web server -246、研究装配 -120、Intake 适配 -33；SDK Runtime +107、新 CLI +82、迁移校验 +13，其他修改和 P1 读取修复均包含在总净值中。

| 真实维护面 | 基线 → Phase 5 |
| --- | --- |
| 生产业务工作流实现 | LangGraph + DBOS → DBOS |
| 四角色 Agent Loop | 手写循环 + SDK Runner → SDK Runner |
| 可执行 runtime/backend 配置开关 | 2 → 0；只保留校验后丢弃的输入别名 |
| SDK 对旧循环类的继承 | 1 → 0 |
| 请求内研究/流归档/并发信号量 | 退出；使用已存在的 native Run/Finalizer/资源策略 |
| Graph Server、旧 Auth、CLI 启动环境 | 退出，锁文件无 LangGraph runtime |

没有声称 DBOS 调用栈比旧请求内图更浅。native root/child/Step、许可、证据和归档边界继续承担恢复/失败责任；减少的是竞争执行路径、旧循环与适配维护，不是用包装层隐藏复杂度。

另运行既有 `scripts/audit_repository.py --base-ref a8574f61d0adba4b07ec860d5c6d7de0a0041e76 --output .tmp/agents-phase5/final-repository-audit.json`，覆盖全部 Git 文件。该 AST 算法的静态 import cycles 从 4 降到 3，剩余为 Memory/MySQL loader、RAG indexer/service、Web runs/server 的关系；包含局部/类型导入的过估计，不等同于初始化死锁或调用栈层数。

无静态导入者候选另有 `rag/memory_writer.py`、`rag/tooling.py`、`utils.py`。逐文件确认分别为 Memory 写入、RAG 工具和通用业务函数的公开兼容导出，没有旧引擎执行实现或 LangGraph import。仓库外直接导入消费者尚无法穷尽，按既有兼容约束保留，不把静态“无人导入”直接等同于可删除；旧 Graph/Agent Loop 的退役不依赖这些小型业务导出退出。Golden/历史输出/学习资产的分类数量均保持 52 项。

## 6. 观测、残留与限制

Run ID = native 根 Workflow ID = Graph run scope = History 稳定身份输入。role child/unit ID、SDK Harness invocation ID、native tool call/操作回执和证据 ID 可关联。公开 Stream 使用 Run/generation/seq；参数只保存操作指纹，公开事件不复制原始上下文、私有记忆或工具载荷。

SDK RunConfig 仍显式关闭 tracing，并设置 `trace_include_sensitive_data=False`，因此**没有声称已部署外部 SDK Trace ID 关联**。复用 DBOS journal、SDK hooks/usage、业务回执和既有可选 LangSmith；未引入新观测基础设施。LangSmith 单次业务服务的外部上传仍需操作员决定保留/敏感数据策略，本次未开启或做 SaaS 验收。

History 保留四个公开数值 usage 计数；Run 保留完整预算和 cache/tool/警告信息。费用一致性按相同四字段比较，不要求不同 schema 完全相等。报告完成和 History saved 是不同事实：SSE 可在研究 done 后结束，archive pending 时由 Finalizer 独立补偿；重连/状态读取确认当前代次归档，旧回执不能冒充新成功。

剩余 `legacy`/LangGraph 文本仅用于拒绝旧配置、历史记录/metadata/路径兼容、明确迁移错误、负向测试或固定历史材料。ReAct 等词仍见于未改变的业务提示词/压缩说明，并不意味着还有手写执行循环。生产 AST 无 LangGraph import；32 入口缺包 smoke 和禁止旧路径合同验证不需要启动 LangGraph Server。

明确未完成或不宣称的事项：

- 生产部署、旧 SQL/旧 DBOS 任务实际排空、生产数据库 least-privilege/备份/HA、跨主机自动失活接管和高积压性能，需要独立实施；本地两个 Executor 的故障合同不能替代这些验收。
- 无跨 PostgreSQL/Neo4j/MCP/供应商/Memory 的全局 exactly-once；未知外部效果继续隔离、保留占位并要求权威核对，不能恢复任意 SDK turn。
- 新默认 SDK 研究路径保留 Phase 4 的 OpenAI/DeepSeek + Tavily 支持边界，其他供应商/原生搜索明确拒绝；单次业务服务的其他 provider adapter 保留。未主动调用付费模型、远程 MCP、真实外部社媒或生产 API。
- 可选 MarkItDown/MCP 依赖冲突、全仓格式/额外 Ruff 规则债务如实保留。没有将它们写成通过；Mypy 非 CI gate，本次未重新声明其历史错误数为当前结果。
- 在线质量/费用 benchmark 未运行。Golden Sets、历史基线、rubric 和学习资产保留；迁移后的评测客户端以已清算角色报告作 grounding 输入，元数据明确不把它等同于原始工具证据或旧 benchmark。

Phase 5 的执行链、旧实现退役、必要兼容和本地业务验收以最终完整 native 结果及最新 PR CI 为门禁。继承质量债务与上述生产/外部服务边界不属于“全部已通过”的声明。

接口核对使用固定安装包源码与离线合同，并参考 [OpenAI Agents SDK 官方说明](https://developers.openai.com/api/docs/guides/agents/sdk)。运行保证以固定版本实测为准，不将 Session/Trace 等同于 DBOS journal。

## 7. 最终独立 Review 与合并门禁（2026-10-24）

复核起点为 PR #28 的 `8004e14966a2d2c4d1ac5b6656e3da431c3e943b`，与固定 Phase 4 Merge `a8574f61d0adba4b07ec860d5c6d7de0a0041e76` 比较 Diff，并重新读取生产源码、测试、固定安装包实现。本节的结论来自本轮检查和重跑，不以第 1–6 节或旧 CI 的通过声明替代验证。

### 发现与处置

本轮新增 **P0 0 / P1 0 / P2 2**；一项 P2 修复，一项非阻塞待办保留。此前 Phase 5 的 SSE P1 是本次复核对象，不重复计为新发现。

| 发现 | 位置、触发、影响与证据 | 处置 |
| --- | --- | --- |
| P2 R5-01：页面在清算前结束跟随 | `web/static/app.js:342`。Run 返回 `interrupted` 等终态且 `settlement=pending` 时，旧页面立即设置 finished，未订阅清算后的费用；后端仍执行清算/归档，未丢持久账目。新增 `tests/durable_frontend.test.cjs:163`：第一次 GET 为 interrupted/pending/3 tokens，清算后为 failed/settled/12 tokens；修复前只读一次，实际 **6 passed / 1 failed**（断言 1 != 2）。 | 已加 `settlement !== 'pending'` 结束条件；修复后 **7 passed**，核对最终费用、失败状态及没有新 POST。仅修页面观察条件，不改调度、Finalizer 或 SDK。 |
| P2 R5-02：未使用的工具回调注入 | `runtime/business_agent.py:251,514` 与 `runtime/sdk_runtime.py:369,383`。角色装配传入 `_execute_tool_safely`，SDK 只将其存到 `self.execute_tool`，没有读取；实际调用为 `sdk_runtime.py:463` 的 `_register_tool → Harness.function_tool → tool.ainvoke`。全生产/测试/脚本 `rg` 的其他唯一消费者为 Runtime 测试夹具。 | 后续移除该私有 helper、构造参数及夹具参数；本次不改构造接口。它不提供循环、fallback 或第二条实际工具执行路径，不影响迁移验收。 |

### 逐项执行语义核对

- `server.research → runs.create_run/subscription`、`/api/runs` 和 `web/cli.py` 均进入同一 Run API；前端直接创建 `/api/runs`。`run_business_agent` 无条件使用 `sdk_model(max_retries=0)` 和 `SDKAgentRuntime`，无旧引擎降级。全活动源码无 LangGraph import，根锁无 LangGraph 包；32 入口缺旧引擎且禁联网的 smoke 重新通过。
- 原生 `research_workflow` 用两个独立 role Workflow、`wait_first_async` 和固定 `RESEARCH_ROLES` 合并；失败取消兄弟并清算。Review 的分组补查决定在业务 Step 内冻结；Risk 等待结束后才启动 Strategy。`role_snapshot` 深拷贝并去除其他角色私有字段；Harness `_check` 绑定 run/role/task，批次持久化和压缩先后顺序保留。
- `dbos_workflow.py`、`run_facts.py`、`execution.py`、`python_state.py`、四角色规范/提示词相对 Phase 4 **无 diff**。独立读取了准入事务、资源占位、许可旋转、工具指纹/回执、取消 drain、History 版本及补偿逻辑；没有以“无 diff”替代真实故障合同。
- 幂等 create 与 native enqueue 在同一事务；Session 绑定 owner，API 身份来自 Token，拒绝客户端 owner/URL/凭据。审批在请求锁下消费精确 plan version；取消意图持久化后才发 native cancel。未知工具单元不能因 Executor 恢复而重新执行。
- Retry endpoint 仅写失败代次授权和 native 消息；Finalizer 独占 `_rewind_safe` 调用。旧 key 只确认旧请求；原失败目标/journal 时间版本与 generation 均需匹配。丢 ACK、再次失败、旧消息、取消及 unknown 效果由原生合同复验。
- SSE relay 顺序为 `write_stream → cursor → done/history receipt`，公开读取在回执之后再次读取 cursor；SSE 结束还要求清算完成、当前代次 done 和游标已送达。使用固定 Phase 4 的 `RunClient.get` 做测试进程内负对照（不改生产文件、不运行旧 Agent）：同一真实 SSE 回归 **1 failed / 19.95s**，仅两次 cursor 读取导致尾 done 未交付。当前投影的正对照结果另列验证表。
- LangChain Classic 固定仍为基线的 **1.0.1**，不是本次新增升级。断网、移除真实凭据的构造探针实际创建 OpenAI、DeepSeek Chat、DeepSeek Flash 模型，并验证工具绑定与结构化输出 Runnable；原 factory/路由合同保留。未将构造探针记为供应商 API 或在线质量验收。

### 本轮重新执行结果

| 检查 | 结果 |
| --- | --- |
| 根锁离线同步 | `uv sync --extra dev --locked --offline --python 3.11` 通过，281 个锁包 / 216 个已安装包 |
| 完整离线 pytest | **804 passed / 91 deselected / 146 warnings，65.80s**；禁 dotenv/tracing/外部 socket |
| 完整真实原生矩阵 | **85 passed / 25 deselected / 0 skipped / 0 xfailed / 146 warnings，1126.06s**；实际 PostgreSQL/Neo4j、Executor 进程与物理故障，推理为离线 SDK Runner 夹具 |
| SSE 正对照 | 当前实现 **1 passed / 18.48s**，尾 done 交付且模型次数不增加；负对照的预期失败不计入通过数 |
| Node | 新回归先失败；修复后 **7 passed / 0 failed / 0 skipped** |
| CI 质量口径 / 编译 / 入口 | Ruff E/F/I、compileall、32 个生产入口 import 通过，未改 CI 检查范围或 pytest 选择规则 |
| 全启用 Ruff 对比 | 基线快照 195 个 Python/TOML 文件逐一与 Git 内容核对；**439 → 422 errors**。按文件/规则/消息比较、忽略行号漂移后，新增诊断 0；两次 exit code 均为 1，未算通过 |
| 全仓格式对比 | 同口径 src/tests/scripts：**88 → 68** 个待格式化文件，新增待格式化文件 0；未宣称整仓格式通过 |
| 依赖一致性 | `uv pip check` 仍只报 MarkItDown/MCP 继承冲突；两个锁版本与 Phase 4 完全相同（0.0.1a4 / 1.23.0），未修改 override 或删除消费者掩盖失败 |

本轮代码修复仅一行前端观察条件，新增一个 Node 回归；生产 Python 仍净 **-802 SLOC / -991 物理行**，JS 仍净 **-39 物理行**。没有新增工作流引擎或修改四角色业务策略。

合并要求为最终 Review 提交的 PR / Push CI quality、tests、dbos-postgresql-contracts 全部成功，无未解决 P0/P1；仅使用 **Merge Commit**，保留本地/远程开发分支。合并后核对两个 parent、四个 Phase 5 提交的祖先关系，以及 GitHub main、origin/main、本地 main 一致，再做必要回归并检查 main CI。实际合并 SHA 与最终 CI 链接以 PR #28 和最终交付为准，不能用合并前的绿勾替代。

前述生产未验证事项继续适用：本地验证不代表生产旧任务已排空、HA/跨主机自动接管、远程 MCP 或跨服务 exactly-once。预算继续使用既有调用 guard 和累计账目，不宣称提供跨并行角色的事务性费用硬配额。P2 R5-02、全仓质量债务、MarkItDown 独立环境与生产部署均是明确后续事项；迁移开发完成不等于生产上线完成。
