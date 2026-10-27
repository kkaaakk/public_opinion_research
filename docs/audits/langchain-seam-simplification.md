# LangChain 接缝精简与模型调用体系完善（方案 A）

日期：2026-10-27（Asia/Shanghai）。保留 LangChain，不迁移 AsyncOpenAI。
第 1–8 节保留初始 HEAD `73fc662c` 的实施/验收事实；后续独立最终 Review、
修复和新的验收门禁见第 9 节。最终 HEAD、实际全量结果和 CI 链接以 PR #33 的
冻结 Review COMMENT 为准，不把初始结果外推为最终提交已经通过。
基线为最新 `main` / PR #32 Merge SHA
`dbd743b4fc4625d05292a1372cc2ee5a5d85d632`，远端 `ls-remote` 与 GitHub PR 元数据核对一致。
分支：`codex/langchain-seam-simplification`。使用独立 managed Worktree；原工作目录的
未提交修改、配置、其他 Worktree、Stash 和分支保留。

先读取 AGENTS、[专项依赖审计](https://github.com/kkaaakk/public_opinion_research/blob/5428eea1/docs/audits/langchain-dependency-audit.md)、
[PR #32 最终审计](langfuse-otel-final-review.md)、准备/迁移及 Phase 0/1/4/5 边界，
再核对源码、测试、脚本、动态赋值和安装的固定依赖。30–100 eLOC 的净减少是历史估算，
本次没有把它设为验收条件。

## 1. 最小生产修改与正确性

| 文件（`src/open_deep_research/` 下） | 最终职责与减少的重复 |
| --- | --- |
| `rag/loaders/knowledge.py` | Vision 直接调用改为现有 `invoke_model_with_budget`；分类、图片/PDF 描述继续共用同一入口。没有增加第二套预算 Wrapper |
| `workflow/intake.py` | 用户图片共享 Vision 的成功用量通过原 `runtime.budget` patch 返回；既有 capture 同时保留失败用量。复用 `model_budget_admission` 预留最终报告调用，不重做工作流 |
| `models/providers.py` | 移入原 ModelRef、解析、key 和 Ark endpoint 函数；新增共同的 compatible endpoint 解析。该模块不导入 LangChain 后端 |
| `models/factory.py` | 供应商别名修正、真实 endpoint 转发、缺包明确拒绝；Ark/Flash 共用 metadata/构造参数装配。兼容导出原 Resolver，减少 Proxy 的重复参数拆装 |
| `models/sdk.py` | 直接使用后端无关 Resolver；删除一份嵌套 config、provider endpoint、环境和 URL 校验装配 |
| `models/__init__.py` | 保留公共导入；构造函数延迟导出，不再因 SDK 只需要 Resolver 而 eager-import Classic/OpenAI/DeepSeek 后端 |
| `runtime/business_agent.py` | 删除无生产读取/调用的 `_execute_tool_safely` 及生产注入；压缩/Graph 模型交给工厂解析完整 provider 配置 |
| `runtime/sdk_runtime.py` | 旧 `execute_tool` keyword 改为可选、接受后立即丢弃；删除无用实例状态，真实工具执行完全保留 |
| `workflow/research.py`、`workflow/reporting.py`、`search/tavily.py` | 同 Intake/压缩一起取消调用方预先取 key，再遗漏 endpoint 的装配；完整可信 config 交给原工厂 |

Vision 的图片内容块、MIME、prompt、模型参数及文本归一化方式保留。请求成功后先捕获
实际 usage，再解析文本，因此解析异常不会丢掉已观察费用。模型请求失败计 attempt；
没有 usage 时 token 为零，不估算。文件读取、编码、OCR 和模型构造失败不计模型请求。
只消费 helper 的已有 capture，不把返回 delta 再提交一次。

检查共享调用方时另复现了用户图片成功路径的归集遗漏：`_step` 在成功时消费业务 patch，
不会自动把旁路 capture 加入 patch。Intake 现在将同一 delta 写入原 patch，同时向原失败
capture 传递；空输出、provider/解析失败仍保留费用。异常和取消继续走原异常边界，
没有新增取消控制器、线程重试或 DBOS 命令。无图/无真实尝试仍返回原有空 patch。

预算账本、Usage reducer、结构化输出解析重试、并行清算、Harness 保护/压缩、
DBOS Workflow/Finalizer/授权、Operation Receipt、SDK Runner Loop 和追踪出口均未修改。
应用及 SDK runtime 版本保持原值。

## 2. 供应商与配置合同

正式、可选、未验证范围及精确优先级见 [供应商合同](../architecture/model-providers.md)。
“正式”表示根依赖及离线真实构造受回归约束，不表示本轮验收过真实云端推理。

- 正式 LangChain 构造：OpenAI、Ark、DeepSeek（含 Flash）、Groq、Anthropic、Google
  GenAI、Vertex、Bedrock、Ollama。测试实际进入锁定的各 provider 类；Vertex 用显式
  project/AnonymousCredentials，Bedrock 用显式测试凭据，均不访问凭据发现或服务端。
- `google` 根据既有 `GOOGLE_API_KEY` 语义归一为 `google_genai`；此前该前缀没有可达的
  Classic 构造。Vertex 使用显式 `google_vertexai`，保留原生 ADC/区域/项目合同。
- `mistral` 归一为 Classic 实际识别的 `mistralai`；Mistral/Cohere 保留可选分派，
  根锁没有相应 integration，缺包抛出明确配置错误；没有假称构造或服务端已验证。
- Ollama 继续由 Classic 优先选择 `langchain-ollama`，缺包时真实回退 Community；
  显式 `base_url` 现在正确传给模型。未删除 Community。
- SDK Resolver 仍仅支持 OpenAI/Ark/DeepSeek/Groq；其他 provider 在请求前拒绝。
  原生 Model 注入、客户端所有权、API 选择和 `max_retries=0` 的 Agent 行为保留。
- 两条共享路径的 key 与 endpoint 优先级由同一 Resolver 决定。修复 Flash 和通用
  LangChain 分派吞掉显式 `base_url`，以及运行配置 endpoint 在调用方只取 key 后丢失。
  OpenAI 还保留旧 `OPENAI_API_BASE` 别名。
- `GET_API_KEYS_FROM_CONFIG=true` 且配置缺 key 时，共享 SDK provider 的 LangChain
  路径也明确拒绝，避免下游构造器悄悄使用环境密钥。显式 key 仍优先。

原有 `get_api_key_for_model` / `resolve_api_key` 等兼容导入保留，但 11 处生产调用方
不再各自预装 key。SDK ModelSettings 与 LangChain 的协议参数仍由各自成熟实现处理；
没有再造统一 ModelSettings 转换器、ModelService 或客户端管理器。

## 3. 实际删除与保留的遗留接口

源码、测试、scripts/evaluation、导出和动态赋值的复核结果：

| 接缝 | 处置与证据 |
| --- | --- |
| `_execute_tool_safely` | 删除整个 19 eLOC 私有实现；当前生产只有传入/保存，没有读取/调用 |
| `runtime_args.execute_tool` / `self.execute_tool` | 删除生产传递及实例状态；没有触及 `_register_tool` 内的真实 invoke/execute |
| `SDKAgentRuntime(execute_tool=...)` / ToolExecutor 类型 | 保留最小 keyword 兼容；回归传入一旦调用就失败的旧 executor，真实 Runner 仍只执行一次实际工具 |
| `_ConfigurableChatModel.with_config` | 保留当前公共入口，直接把模型参数和 provider config 交给 `create_chat_model`；39 → 31 eLOC（整个 Proxy） |
| Proxy 的 `bind_tools/with_retry` | 生产无消费者，但既有导出和显式兼容测试仍要求该行为；保留最简单的延迟状态及转发。本轮没有宣称已完全删除 Fluent 状态，也没有把 LCEL 必要重试当成死代码 |
| `models` / `models.factory` 的 Resolver 导出 | 保留并测试对象同一性；只改变权威实现归属与导入时机 |

`evaluation/cost_profile/run_cost_profile.py`、`evaluation/plan_execute_ab/run_benchmark.py`
只对旧 helper 名称做赋值；`tools/code_graph_analysis.py` 保存旧 AgentRuntime 路径。
这些固定历史基线不被生产导入，也没有消费当前 SDK 的旧 executor 状态。保留原文件与
学习/评测数据，未为历史 Agent Loop 重新引入一条执行接缝。仓库之外未登记的脚本无法
穷尽验证，因此公共构造与 Fluent 接口选择保留兼容。

## 4. 调用链、代码量与开销

以下只计明确列出的项目主干函数；排除第三方内部及解析/配置分支。构造分支另列。

| 路径 | 修改前 → 修改后 | 函数 / 模块变化 |
| --- | --- | --- |
| Vision 请求 | `_invoke_vision_model → LC.invoke` → `_invoke_vision_model → invoke_model_with_budget → LC.invoke` | 1/1 → 2/2；新增的是必要记账边界 |
| 有界结构化调用 | 业务步骤 → `ainvoke_model_with_budget` → LC structured chain → `require_valid_structured_output` | 3/2 → 3/2；原解析重试保留 |
| SDK 四角色 | 角色函数 → `run_business_agent` → `SDKAgentRuntime.run` → `SDKContextHarness.run` → 官方 Runner | 4/4 → 4/4 |
| SDK 真实工具 | Harness.observed → Runtime.invoke → Runtime.execute → BaseTool | 3/2 → 3/2；删除的旧 helper 原本不在热路径 |
| 显式 key 的 OpenAI Proxy 构造（含配置 helpers） | `with_config → create → parse` → `with_config → create → parse / resolve_base_url / _configurable` | 3/1 → 5/2；补齐统一 endpoint 决策/校验，不冒称所有栈都变短 |

普通服务原先先 `get_api_key_for_model → resolve_api_key → parse_model_ref`，再进入
`with_config → create_chat_model → parse_model_ref`；现在完整 config 直接进入原工厂，
模型名称解析一次，key/endpoint 使用同一个 ModelRef。SDK 不再绕过工厂模块去取
Resolver 而被迫加载 LC provider。模型包从 3 个模块变成 4 个，新增的是搬入原解析器的
后端无关模块，没有第二套分派服务。

全量 `src/**/*.py` 的实测有效物理行（去空行、纯注释、AST docstring）：

| 指标 | 实测 |
| --- | --- |
| 基线 → 当前 | 25,475 → 25,531 eLOC |
| 新增 / 删除 / 净变化 | +284 / −228 / **+56 eLOC** |
| 旧工具 helper | 19 → 0 eLOC |
| Lazy Proxy | 39 → 31 eLOC |
| `sdk_model` 函数 | 58 → 38 eLOC |

统计覆盖全部生产 Python；跨文件搬移按新增/删除分别记，净值不重复计。包含对变更文件
应用 Ruff 的折行；不把 factory 中搬走的 Resolver 算成净删除。新增预算交接、endpoint
契约和惰性导出有实际职责；净增不被包装成“减少 56 行”。基线/当前源码均用 AST 确定
docstring 行，再对非空非注释行做 SequenceMatcher（`autojunk=False`）统计 add/delete。
本地逐文件结果在忽略的 `.tmp/langchain-seams/metrics.json`，可用此口径从 Git 基线重算。

未新增网络往返、模型请求或热路径 client 管理器。Vision 增加现有两种 callback 和
ContextVar 记账，构造增加常数级解析/URL 校验；原业务请求重试和 SDK 工具栈不变。
没有测量生产延迟或吞吐，不把函数/eLOC 数量当作性能数据。

## 5. 依赖与安装

`pyproject.toml`、`uv.lock` 与基线均无差分：根锁仍 296 package entries；本机锁定环境
231 packages。LF 规范化后的锁 SHA-256：
`f7388965a71a8c878f5af17e0973bd1985fc6e9b18ba2f644680b921bd759c7b`。

Vertex 可选化本轮不实施。真实 ChatVertexAI 构造已补齐，但未证明移入 extra 后默认/extra
安装、跨平台锁及导入契约全部一致；没有用旧锁闭包估算声称实际减少依赖。

`uv pip check` 仍 **exit 1**：既有 `markitdown-mcp` 要求 `mcp~=1.8.0`，根 override 使用
`mcp==1.23.0`。未新增冲突，未删除工具或降级 MCP 来消除提示。

## 6. 回归与验证证据

Python 3.11.9，根 `.venv` 与锁定依赖；禁 dotenv/default tracing，pytest-socket 只允许
loopback/unix。模型使用真实 LangChain 生命周期、官方 SDK + Mock HTTP 或 ScriptedModel，
付费推理请求 **0**。没有删除断言、降低标准或用旧结果冒充新用例。

| 验证 | 本轮实际结果 |
| --- | --- |
| 修改前全量离线 | 844 passed / 104 deselected，157.28s |
| 新审计回归初始 red | Vision/provider/import 初始 39 项：27 failed / 12 passed；原始失败保留于 `red.log` |
| 共享 Intake baseline 函数复现 | 在独立进程只替换不可变基线函数：5 failed / 1 passed / 8 deselected；未改生产文件 |
| 最终全量离线 | **897 passed / 105 deselected**，122.21s；较基线新增 53 项离线回归 |
| 既有 PG/DBOS/Neo4j 指定原生矩阵 | **98 passed / 25 deselected**，1488.64s；真实 Executor kill/restart、停库、重试、取消、未知效果与归档 |
| 新增 Vision 原生合同 | **1 passed**，21.75s；完整 Run 从 28 calls / 311 tokens 到 29 / 326，unit artifact/usage 均仅 1 call / 15 tokens，重复读取不增加用量 |
| SDK、Tool、RAG、结构化、失败清算、OTel 开关/出口 | 纳入上述离线/原生矩阵；新增真实构造覆盖所有正式 provider，而非只 mock Classic 工厂 |
| LangSmith 原生 callback 合同 | **1 passed / 19 deselected**，无 SaaS 请求 |
| Execution Studio | typecheck、lint、42 单元测试、build 全通过；原有真实 Chrome E2E **6 passed** |
| Classic 浏览器协议 | **7 passed** |
| Ruff / 格式 / 编译 | 活跃源码 E/F/I、18 个变更 Python 文件格式、compileall 通过 |
| 生产入口 / 根锁 | 32 production imports（阻断退役引擎与网络）；`uv sync --extra dev --locked --python 3.11`、`uv lock --check` 通过 |

98 项原生矩阵在 Intake 补充回归前启动；新增行为又用独立真实 Run 合同验证，分列结果，
不将 99 项写成一次尚未运行的单进程矩阵。PR 的标准 CI 会按最终源码收集 99 项。
新 Vision 单元/调用方矩阵与全部离线测试均在最终生产修改后完成。

首次 E2E 为 **6 failed**，原因是本机未安装配置指定的 Chromium revision，浏览器没有
启动。保留原日志；在忽略目录使用临时配置选择已安装 Chrome，未改测试断言或受控
后端后 **6 passed（1.9m）**。新增兼容用例初次因测试 helper 拼写错误失败，改用已导入的
官方 `function_call` 后通过，不归类为生产回归。一次格式检查指出新增 SDK 测试需要折行，
修正后 18 文件格式通过。

服务项目 `por-langchain-seams`（PG :15434 / Neo4j :17689）与 browser 专用
`por-langchain-studio`（:25435 / :27690）分开，避免原生停库影响浏览器。只操作本轮带
`por-integration=dbos-phase4` 标签的测试容器及 UUID 数据库；未接触业务容器。
日志、临时配置、模型夹具输出保留在忽略的 `.tmp/langchain-seams/`。

主要复现入口：

```powershell
.venv/Scripts/python.exe -m pytest --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -m integration tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py tests/test_telemetry_dbos.py tests/test_web_history.py tests/test_p1_fixes.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe scripts/phase5_import_smoke.py
uv sync --extra dev --locked --python 3.11
```

## 7. 非 Agent Langfuse Generation：独立评估，未实施

本节仅为后续独立 PR 的结论；本轮没有安装 LC instrumentor、增加 Generation callback、
放宽 exporter scope 或更改预算。现有 Langfuse 4.18.0 / OTel 1.45.1、SDK OI 2.6.0 与
PR #32 的安全出口保留。本轮没有重复建立官方 Langfuse 服务端，不把 PR #32 历史 UI
验收称为本次新增功能的验收。

| 候选 | 核查结论 |
| --- | --- |
| 现有 LangChain callbacks | ModelAttemptCounter/官方 UsageMetadataCallbackHandler 是预算事实来源；LangSmith opt-in 不等同 Langfuse Generation。预算 helper 的外层 span 不能替代逐次模型生命周期，否则结构化重试粒度不正确 |
| Langfuse v4 CallbackHandler | 锁定 4.18.0 实际导入抛 ModuleNotFoundError，依赖顶层 `langchain`；源码还会采集输入/输出。当前不是可直接接入的最小方案，不为它恢复已退役高层框架 |
| OpenInference LangChain instrumentation | 官方源码 hook `langchain-core` CallbackManager，支持传入既有 tracer_provider/TraceConfig；依赖声明无需顶层 Agent 框架。是优先 PoC 候选，但本轮未安装/锁定/宣称已验收 |
| 当前 OTel 管道 | CorrelationProcessor 已提供 Run/unit/role/round 等父子身份；SafeExporter 精确 scope allowlist 当前不允许 LC scope。后续应复用这一 Provider/出口，不再创建新 Provider 或 uploader |

建议后续最小实施：

1. 单独锁定一个 OI LangChain 版本，先在现有 Core/Classic/partner 组合做 in-memory
   exporter PoC；不要同时启用 Langfuse CallbackHandler、OI LC 和全局 OpenAI 客户端插桩。
2. LC 生命周期只由 OI LC 拥有 Generation；SDK 原生模型继续只由 OI Agents 拥有。
   Agent 内部的有界抽取/压缩是独立真实 LC 请求，应成为当前 DBOS/Agent 的子请求。
3. 在现有 opt-in 初始化/关闭中复用同一 Provider，添加精确 LC scope；所有内容隐藏、
   exception/status/event/link 清理和最终白名单继续保留。不上传 prompt、工具参数、
   response、retrieval 文档或私有 memory。不能只靠前端不显示内容。
4. OI 的全局 CallbackManager hook 也会产生 chain/tool/retriever span，PoC 必须检查
   嵌套父子与冗余边界；不能粗暴过滤父 span 来掩盖重复 Generation。使用公共选项，
   不将私有 `_tracer` 实现变成项目新的长期适配层。
5. 验证成功/失败各 1 Generation；raw 输出成功但解析失败再请求为 2 个请求 span；
   无 usage 不猜 token；并行、同步线程、取消、DBOS 恢复和 SDK+LC 混合用例没有泄漏、
   多记账或额外调用。用内容哨兵检查完整 OTLP 出口。
6. 不改 `budget.py` 的 admission/capture/merge；观测 usage 只读取同一响应，绝不提交费用。
   若 OI 隐藏内容时丢 usage，先证明问题，再在原 span 补数值，不能增加第二个 Generation。

回调的一次模型生命周期可能包住 vendor SDK 的透明 HTTP 重试；LC structured retry 则会
重新触发模型生命周期。后续 PR 必须分别报告这两类重试的可观测范围，不能宣称 callbacks
已看到每个底层网络尝试，也不能为追踪改变现有 client 重试或预算计量。

官方依据（2026-10-27 读取）：
[OI LC Core/Classic 兼容说明](https://arize-ai.github.io/openinference/python/instrumentation/openinference-instrumentation-langchain/)、
[instrumentor 源码](https://github.com/Arize-ai/openinference/blob/main/python/instrumentation/openinference-instrumentation-langchain/src/openinference/instrumentation/langchain/__init__.py)、
[依赖声明](https://github.com/Arize-ai/openinference/blob/main/python/instrumentation/openinference-instrumentation-langchain/pyproject.toml)、
[OI 隐私配置](https://arize-ai.github.io/openinference/spec/configuration.html)。
在线 [Langfuse 集成页](https://langfuse.com/integrations/frameworks/langchain) 当前已描述 v5，
不将滚动文档外推为已安装 v4.18.0 的合同；v4 结论以本地锁定源码和导入探针为准。

## 8. 剩余边界

没有验证真实付费 provider、云端配额、生产吞吐或所有模型的 multimodal/tool/structured
能力；Mistral/Cohere 的可选安装和 Vertex extra 仍待独立验收。5 项下载语义模型的 RAG
integration 没有运行，不计通过数。Proxy Fluent 和旧导出因兼容保留，维护面积并未归零。
未知/未上报 token 不能从本地估算补成实际费用；取消/故障后不能为了补观测重放模型或工具。

本次交付保持 Draft，未合并、未部署；本地 Worktree 和全部测试证据保留。

## 9. 独立最终 Review 与修复

日期：2026-10-27。审查起点 `73fc662cd335da6cfaaed707e43bb5624b3edd36`；
main `dbd743b4fc4625d05292a1372cc2ee5a5d85d632`。重新 fetch、读取完整生产/
测试/文档 diff、固定 LC/DBOS 实现、调用与动态赋值，核对初始 CI、评论、Reviews 和
未解决 threads。初始 CI/Studio/Claude 工作流均 success，评论/Review/thread 均为 0；
没有以此代替源码和边界复现。

### 9.1 发现、影响与处置

初始定位行号均属于 `73fc662c` 的 `src/open_deep_research/`。

| 编号 | 等级 / 位置 | 触发条件、影响和复现 | 修复 |
| --- | --- | --- | --- |
| R1 | P1，`workflow/intake.py:103–114` | Vision 在线程中已发出；取消/超时结束 `to_thread` 等待后立即关闭 capture。线程晚到的实际费用丢失，还可能处理下一张图。4 个取消/超时回归先失败 | 复用现有 `owned_io` 等待已发请求清算；原 admission 的 usage 回调检查 owner 的取消状态，拒绝后续图像请求；不重试、不新增预算 Wrapper |
| R2 | P1，`workflow/business_steps.py:90–92` | 配置 Step deadline 且重复取消；第二次取消能打断 `wait_for` 自身的 cleanup wait，外层早于原子任务读取费用。补充 2 个 deadline/repeated-cancel 回归先失败 | 显式持有原 execute Task，在原异常分支 shield/drain 原 Task 后读取 capture；仍抛出原取消/超时/失败，不创建第二次业务执行 |
| R3 | P1，`budget.py:345` | 官方 Usage handler 要求非空 model_name 且只处理 on_llm_end。固定 Google GenAI 真实模型返回合法 usage 但省略可选 model_version 时记为 0；真实 Core 流错误的部分 usage 也被忽略。3 个实际生命周期回归先失败 | 在现有官方 handler 上补输入适配：只给本 collector 的不可变响应副本提供未识别名称；on_llm_error 的真实 LLMResult 交给同一官方聚合。锁、add_usage、capture/ledger 和模型/错误结果保持原职责 |
| R4 | P2，`models/factory.py:94–95` | 既有 `openai_api_key/openai_api_base` 或 Groq 原生字段经 kwargs 传入；key 被拒绝，或显式 host 被默认 DeepSeek 地址覆盖。4 个固定实现的真实构造回归先失败 | 只在原工厂边界归一这些既有参数，再进入同一 Resolver；标准参数优先。没有扩充 provider 或建立新配置层 |
| R5 | P2，`models/__init__.py:17,48` | `typing.get_type_hints(get_api_key_for_model)` 因 TYPE_CHECKING-only 的 RunnableConfig 抛 NameError；影响公共 helper 的标准运行时检查。1 个回归先失败 | 用运行时可解析的 Mapping 注解，保持参数及配置行为；不恢复后端 eager import |

发现 P0 **0**、P1 **3**、P2 **2**；五项已实施修复，剩余可执行 P0/P1/P2 **0**，
最终通过结论仍受下述实际 HEAD 门禁约束。R1–R5 的初始日志在忽略目录
`.tmp/pr33-review/red.log`（9 failed）、`deadline-red.log`（2 failed）、
`observed-usage-red.log`（3 failed）；`repeat-red.log` 的无 deadline 场景本来通过，
没有冒称所有重复取消都失败。

R3 不是 Token 估算或第二账本。模型缺少名称时只投影 collector 输入；其他 callback
和原始响应仍看到原数据。错误 callback 仅消费 Core 已提供的 LLMResult，缺 usage
仍为 0；不发新的 on_llm_end 事件，不改变错误 Span，不上传内容。新增 structured retry
回归验证两次真实响应为 2 calls / 30 tokens，raw metadata 不被修改。

R2 位于既有纯业务失败清算 helper；没有改 native Workflow/Step 注册、命令序列、授权、
重试授予、Finalizer、Effect Receipt 或 RunFacts。R1 等待 provider 返回/既有 provider
超时再清算，不承诺取消等待者能撤销远程请求。SDK Runner、Harness、Graph 和工具执行
去重/权限路径未改；没有接入 LC Generation instrumentation 或 Vertex extra。

### 9.2 本轮证据与最终门禁

补修后的独立定向矩阵已实际得到 **296 passed / 1 deselected**，包含 Vision、预算、
Token、真实 provider 构造、SDK Model/Tool、LangSmith 与 OTel。另有真实 PG/DBOS/Neo4j
Vision **3 passed / 63 deselected**：成功 Run 仍恰好增加 1 call / 15 tokens；取消时
unit 在请求返回前保持 pending，返回后费用只结算一次、第二张图不调用、SDK 不启动。
保留初始 fixture 的 7 calls / 77 tokens，最终为 8 / 92 或 provider 失败时 8 / 77，
另断言单位增量恰为 1 / 15 或 1 / 0，重复读取不增加费用。

原生新增测试初次失败另包含两个测试客户端假定：cancel RPC 落库早于 DBOS 的本地
取消送达；fixture 本来有 7 / 77 的既有预算。测试现在只读记录真实 owner Task 并等待
其收到取消，再释放模型 gate；原单次费用/不重复调用断言保留，且增加显式基线累计和
unit delta 断言。没有修改生产取消 API、放宽清算或删除断言来掩盖失败。

最终候选提交冻结后必须重新执行：全量离线、一次完整 101 项指定原生矩阵、LangSmith
原生合同、Studio TS/lint/unit/build/browser、Classic、Ruff/变更文件格式/编译、32 入口、
根锁与 pip check。模型均为 Fake/ScriptedModel/Mock client，dotenv/default tracing 禁用，
socket 只允许 loopback/unix，付费推理为 0。精确 HEAD、实际数量/退出码及 CI run 链接
写入 PR 的冻结 Review COMMENT 和描述；未执行的集合不预先写成通过。

与 main 的最新生产 eLOC 实测为 **+336 / −233 / 净 +103**，25,475 → 25,578；
相对初始 PR 为净 +47，包含必要的清算持有、兼容参数归一和 Usage 输入适配。
仍按同一 AST/docstring/物理行口径计算，包含移动与变更文件格式，不冒称净减少。
测量文件 `.tmp/pr33-review/metrics.json` 可从同一 Git 基线复算。

预算入口数量和 SDK/真实工具主干不变；Intake 的同步等待改为既有 owned_io，
业务步骤明确持有原子任务，官方 Usage 聚合前增加缺名称/部分失败投影。
新增常数级 Task/类型检查；只有缺名称时复制浅层响应视图，没有新增模型请求或网络往返。
未测量生产吞吐/延迟。依赖和锁无差分，既有 markitdown-mcp/mcp 冲突仍单列。

最终只在当前候选的全部必要 CI 通过、无阻塞 Review/thread、权限/规则复核后 Ready，
以 expected head SHA 约束 Merge Commit；保留分支，不使用 squash/rebase 或 admin bypass。
合并后的双 Parent、HEAD 祖先/代码树、main 同步与 push CI 以同一 PR 留档，不为填写
自引用 SHA 再改已经测试的代码树。Langfuse 非 Agent Generation 继续作为独立后续 PR。
