# 研究角色结果与统一组装审计

日期：2026-11-01（Asia/Shanghai）。仓库 `kkaaakk/public_opinion_research`。
基线：`31e8ad8e865817c9aa2a610591d90b5d24748562`（PR #38 已合并）。
已 fetch 并核对 main、目标分支及全部 open PR；目标分支没有独有提交，复用
`codex/research-role-results-assembly`。原工作区位于另一个分支且有大量未提交修改，
实现使用独立 worktree，未 stash、覆盖或提交原工作区内容。

## 1. 审计边界与原合同

读取 AGENTS、准备审计、迁移计划、Phase 0/1/2/4 DBOS/5 审计和部署说明后，
检查指定的十个模块、全部符号消费者、原生测试、SDK/Harness 合同及历史参考实现。
最新 main 实际应用版本已经是 `dbos-brief-spec-v1`，SDK runtime 为 `sdk-phase5-v1`；
本轮不倒退到历史文档中的 Studio/Phase 5 版本。

- `run_business_agent()` 是四个角色的共同装配入口；SDK 仍只返回已有
  `AgentRuntimeResult(report, memory, rolling_summary, budget, replace_report)`。
  原 `_build_state_patch()` 把它与 Workspace 结果拼成四域 Patch。
- 生产 `merge_research_patches()` 只有 `por.research` 一个调用方。另有历史 oracle
  和专门的旧合并合同测试；没有公共包导出、CLI 或仓库内脚本消费者。
- 原 Patch 包含 `agents[role]`、`workflow.completed_tasks`（补研另含 round）、
  `runtime.budget`；Graph 模式另有 `research.run_id/working_contexts/working_context_scopes`
  和 `runtime.metrics`。正常研究结果从不写 messages 或 report 域。
- 原通用合并器还处理五域 override 权限、其他角色写入、任务归属、Run ID、Context
  所属及并行报告 sections/final 冲突。后两种报告替换从未由当前研究生产者生成。
- `apply_patch()` 和五个 Reducer 仍有 intake、review、consumer、report、状态规范化
  及历史合同消费者，保留。`role_snapshot()` 继续深拷贝并隔离角色记忆和摘要。
- Review 在 Graph 模式读取真实持久证据和各角色 Working Context；普通模式读两个角色
  的完整报告。它还使用 brief、round、previous review 和 completed_tasks。
  Followup 原有角色过滤、任务身份去重、按角色分组与轮次上限均未重写。

`business_step()` 先取得可撤销许可；存在已结清 Artifact 时直接返回，绝不执行 SDK。
新调用经原 `_step()` 捕获费用、处理超时和清算后持久化 Artifact、usage 和事件，
再返回 DBOS Step；角色子 Workflow 也保存返回值。Artifact 用于提交成功但 journal ACK
丢失的窗口，并不是第二个执行状态机。DBOS journal/status 始终为执行权威。

原 `past` 是同一业务单元以前失败的费用；不是输入 State 的累计预算。
成功结果持久化前只合并一次 `past + 本次增量`。RunClient/History 使用单位 usage，
主 State 重建使用同一结果 delta。不能再把单位 usage 或输入基线加到结果里。

## 2. 改造后的单一业务路径

```text
SDK Runner / Harness → AgentRuntimeResult
  → 两个研究角色各自封装 ResearchRoleResult
  → business_step 保存 JSON research_result + 单位 usage
  → 原 por.role 并行子 Workflow
  → 原 DBOS.wait_first_async 收齐结果
  → read_research_result（仅持久化格式读取）
  → assemble_research_results（固定角色顺序，先构造后提交）
  → 原 Review / 定向补研 / Risk / Strategy / Report / Memory
```

没有新增 LLM 调用、Agent Loop、Workflow、数据库表、Queue、运行状态机或适配服务。
Risk/Strategy 继续使用原 Patch。研究结果不再具备修改任意 State 域的能力。

主要文件职责：

| 文件 | 变化 |
| --- | --- |
| `state.py` | 单次研究结果 schema；复用 WorkingContext、ResearchGraphScope 和 JSON 值类型 |
| `runtime/business_agent.py` | 研究角色封装现有 Runtime/Harness/Workspace 输出；消费者保持原契约 |
| `workflow/python_state.py` | 原子、确定性组装及只读旧 Artifact 解码；原 apply_patch/snapshot 保留 |
| `workflow/dbos_workflow.py` | 持久化新 JSON 结果、合并一次 past、原并行收集后统一组装 |
| `workflow/run_facts.py`、`web/durable.py` | 从新结果读取角色报告，保持原公开事件/History 协议 |
| `workflow/research.py` | 研究函数返回类型说明；原 Review、任务过滤和轮次规则保留 |
| `tests/reference_research.py` | 冻结原 31e8ad8 合并器与测试专用旧边界，不被生产导入 |
| `scripts/research_results_inventory.py` | 复用既有 SLOC 口径，输出完整生产 diff 与各边界统计 |

## 3. 新结果和 State 契约

`ResearchRoleResult` 由 Python 装配，不传给模型作为 Structured Output：

| 字段 | 契约 |
| --- | --- |
| role / round | 两个研究角色 Literal；严格正整数执行轮次，必须匹配已派发包 |
| report / replace_report | 完整报告字符串；默认按原规则追加，true 按原规则替换 |
| memory | 本次新增记忆记录列表，默认 []；实际 Runtime 每次提供一个 dict，封装为单项列表 |
| rolling_summary | 当前角色的滚动摘要，默认空字符串 |
| working_context / working_context_scope | 成对可选的已有 Graph 模型；role/round/run 必须匹配；不能丢弃未知保存字段 |
| budget_delta | 当前业务单元尚未计入主 State 的费用；Step 已包含该单元历史失败费用 |
| metrics_delta | 本次 Workspace 指标，使用原可加指标与 quality 规则 |

Pydantic 拒绝额外顶层字段，记忆/费用/指标仅接受 JSON 值；Graph 结构复用原模型并拒绝
未知字段。DBOS 与 RunFacts 均保存 `model_dump(mode="json")` 的普通字典，
不把新增 Python 类实例写入 journal，重启不依赖新类的 pickle 路径。

主 Workflow 从实际派发包确定轮次和完成任务。组装要求派发集合与结果集合一致，
禁用角色以明确的 skip receipt 对应 None；未派发角色不需要结果。
按 public_signal、internal_knowledge 顺序组装，完成顺序不影响结果。

角色报告和记忆使用原 agents_reducer；进度使用原 workflow_reducer 去重。
预算直接调用 merge_budget_usage，指标直接调用 merge_research_graph_metrics，
不让通用 override token 变成累计费用的替换指令。无 Graph 时保持原空指标行为。
Working Context/Scope 按角色更新；证据仍在原 Graph Store，组装不复制、删改证据。

原合并器 deepcopy 整个 State，再复制每份 Patch。新组装只构造改变的聚合值，
用 model_dump 分离新增可变记录，原有列表/映射不原地修改；全部成功才一次 state.update。
messages、章节和最终报告不进入拷贝。旧记忆记录只被读取，新列表继续保存它们。
只做了下述合成 join 微基准，没有运行端到端吞吐或模型/工具延迟 benchmark，不宣称生产性能明显提升。

## 4. 旧 DBOS journal / Artifact 兼容与回滚

兼容读取针对上述真实旧生产契约，不为任意通用 State Patch 提供第二条研究执行路径。
新旧结果都进入同一个组装函数；读取、校验、组装均无模型/工具/Graph I/O。

- 已提交 Step 或 por.role 返回旧 `{"patch": ...}`：按原角色与派发轮次读取结果。
- RunFacts 已提交而 Step ACK 丢失：原 acquire 直接返回旧 Artifact；保持原许可和
  Artifact 不可改写规则，不重新持久化为新格式，也不增加原生 journal 命令。
- 兼容 string 报告、override 报告、角色记忆/摘要、Graph Context/Scope、预算和指标。
  原 completed_tasks 必须与实际派发任务一致；运行信息不能由旧 Patch 越权修改。
- 旧禁用结果实际可能是空 Patch，也可能是被 business_step 规范化后的完整零预算；
  两者在明确的 role_disabled observation 下都合法。带非零费用或其他业务数据的
  skip 不静默丢弃，拒绝并要求核对。
- 不支持的额外域、跨角色内容、未知保存字段、错误 Scope、任务或轮次明确失败；
  保留原数据，在原 release 完成或由操作方核对，禁止重新 create 研究伪装迁移。
- 未确认的 opaque Agent/工具效果继续由原许可/receipt 守卫隔离，不能因结果可解码
  而获得重放授权。安全重试仍只有 Finalizer 能应用，旧 key 不授权下一失败代次。

原生命令次序、Workflow 名、子 ID、Queue 和版本不变，因此不新增切换版本的第二条
调度路径。本次只向前读取最新基线 `dbos-brief-spec-v1` 的结果；更老 Phase 4/Studio
journals 仍遵守原 matching-release 排空策略。升级先停旧准入、排空旧 Executor，
再启动新 release；不要把不理解 research_result 的旧 Executor 与新版本混跑。
回滚先停止新准入，用本 release 完成其已接纳任务，然后回滚新接纳；不要用旧代码
读取新结果，也不要改 journal 或复制外部操作来恢复。没有改动生产服务或执行生产排空。

真实进程测试不是仅靠 Mock：真实 SDK Runner/Harness 和工具效果见证运行后，
测试专用旧边界写出冻结 Patch，由真实 business_step、RunFacts、DBOS journal 持久化。
随后杀掉完整 Executor 进程树，以当前结果契约重启，覆盖已提交 journal 及
Artifact 已提交/Step ACK 未提交窗口。调用见证和费用在恢复后精确核对。
这证明实际持久化与恢复合同。另做 **1 项真实旧源码升级 smoke，通过（69.54s）**：
从 Git 归档固定 `31e8ad8e865817c9aa2a610591d90b5d24748562` 的 src/tests，
用该版本的 `tests/dbos_process_fixture.py` 和 src 启动旧 Executor；原 checkpoint
场景在真实 Review 边界暂停，核对两个已结清 Producer Artifact 确实只有旧 patch，
再终止进程树。随后用当前 fixture/src、相同 Executor ID 和 UUID 数据库重启，
复用原 `test_process_kill_journal_recovery_never_replays_unknown_tool[checkpoint]`
全部断言：报告成功、工具实际调用/效果仍为 2、model_calls 仍为 26。使用浏览器项目
15435 PostgreSQL，独立临时测试驱动和源码归档位于忽略的 `.tmp`，不是新增生产入口。
该项是额外手工 smoke，不冒充 CI 固定合同数量；未执行生产库升级或所有历史发行版组合。

## 5. 验证证据

所有 Python 测试禁 dotenv、远程 tracing 和非 loopback socket；付费模型/API 调用为 0。
独立 Compose project `por-research-results` 使用 PostgreSQL 17.6 / Neo4j 5.26.31；浏览器另用 `por-research-results-browser` 的 15435 PostgreSQL，避免原生停库测试影响它；
测试只创建/删除自己的 `por_test_<uuid>` PostgreSQL 库，故障只操作标记的本次容器。

| 检查 | 结果 |
| --- | --- |
| 最新 main 离线基线 | 1046 passed / 2 skipped / 121 deselected，312.04s |
| 首轮迁移后完整离线 | 1075 passed / 2 skipped / 125 deselected，242.12s；随后新增结构负例另验 |
| 最终完整离线 | 1078 passed / 2 skipped / 125 deselected，236.90s |
| 新结果契约 | 32 passed，涵盖双顺序、少角色、原子性、Scope、补研追加/替换、去重、预算指标及旧格式 |
| SDK/Harness/Graph/历史参考定向 | 214 passed，18.82s |
| 前端类型/lint/构建 | 通过；继承大 chunk 提示仍存在 |
| 前端 Vitest / 原 Node 请求合同 | 48 passed / 7 passed |
| 生产入口 smoke | 32 imports passed，旧引擎和外网均被阻断 |
| Ruff E/F/I / compileall | 通过；修改模块格式检查通过 |
| 修改模块 Mypy（非 CI 门禁） | state/python_state/business_agent 三模块通过，follow-imports=skip / ignore-missing-imports；未声称全仓类型零错误 |
| 本地完整原生故障矩阵 | 116 passed / 2 failed / 25 deselected，1894.70s；两个新增用例误用持久化 JSON 键序判断业务顺序，原因及修正见下文 |
| 两种完成顺序修正后复验 | 2 passed / 35.53s；真实组装点顺序、持久化报告/记忆/Context/Scope、费用全部精确断言 |
| 真实基线源码 → 当前 Executor smoke | 1 passed / 69.54s；实际旧代码、旧 journal、kill/restart，工具及费用不重复 |
| Studio 浏览器真实链路 | 7 passed / 2.4m；本机 Chrome，真实 HTTP/DBOS/SDK/Neo4j，包含 Brief 场景和六项 Studio 场景 |
| 依赖一致性 | 未通过：继承 markitdown-mcp 要求 mcp~=1.8.0，实际 1.23.0；未改依赖锁 |
| GitHub CI | [PR #39 当前 HEAD checks](https://github.com/kkaaakk/public_opinion_research/pull/39/checks)；quality/tests/原生故障矩阵及 Studio 前端/浏览器均为实际作业，最终固定 Run 和计数列在 PR 描述 |

Windows 两项跳过均为既有 `test_telemetry_delivery.py:261`：POSIX signals 需 Linux
CI 进程，不把 TerminateProcess 当等价信号；没有新增 skip/xfail。其余 integration
按项目原选择边界独立运行，不混入离线通过数量。

如实保留迭代失败：首轮 7/90 定向失败为旧返回断言及新增报告追加期望，迁移断言并
保持原追加规则后通过；首轮 11 项原生定向 8 passed / 3 failed，其中禁用零预算是
真实兼容问题，另两项是测试读取了错误的公开字段名。修复后重跑，不将旧失败轮次
写成通过。完整本地矩阵另外发现 2 个新增顺序用例错误：DBOS JSON 持久化会重排对象键，
不能从最终 JSON 的键序推断组装顺序。修正为在真实 assembler 调用后观察候选 State，
再逐字段核对持久化报告、记忆、Context 和 Scope；真实 finish_unit 的双完成顺序屏障、
费用和四角色断言全部保留，复验 2 passed。没有改生产代码来适配测试，也没有放宽
业务值、并发或恢复断言。所有失败轮次保留为失败，不拼成一次虚构的完整本地绿测。
最终完整 Linux 原生矩阵通过后才交付，计数与当前 HEAD 固定 CI 链接见 PR 描述。
新模型/工具调用没有因这些测试失败被生产代码重试。

复现：

```powershell
uv sync --extra dev --locked --python 3.11
.venv/Scripts/python.exe -m pytest -q -rs --disable-warnings --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
$env:TEST_DBOS_POSTGRES_CONTAINER='por-research-results-postgres-1'
$env:TEST_DBOS_NEO4J_CONTAINER='por-research-results-neo4j-test-1'
.venv/Scripts/python.exe -m pytest -m integration tests/test_brief_dbos.py tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py tests/test_telemetry_dbos.py tests/test_web_history.py tests/test_p1_fixes.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m compileall -q src/open_deep_research
.venv/Scripts/python.exe scripts/phase5_import_smoke.py
.venv/Scripts/python.exe scripts/research_results_inventory.py
node --test tests/durable_frontend.test.cjs
# frontend/: npm ci; npm run typecheck; npm run lint; npm test; npm run build; npm run test:e2e
```

## 6. 简化收益与额外成本

统计使用既有 `dbos_inventory.count`，排除空行、独立注释和 docstring；不把测试中保留
旧实现算作生产删除。命令扫描全部 src 差异，不仅统计被删函数。

| 口径 | 改前 | 改后 |
| --- | ---: | ---: |
| 研究合并函数 | 49 物理行 / 42 SLOC | 62 / 57（统一组装） |
| 独立研究结果 schema | 0 | 39 / 34 |
| 历史结果 reader | 0 | 69 / 64 |
| 原角色 Patch builder | 36 / 36 | 36 / 36（消费者和兼容测试仍使用） |
| run_business_agent | 104 / 93 | 119 / 108 |
| 全部生产 Python 净变化 | — | +176 物理行 / +163 SLOC |
| 合并到业务规则的调用层数 | merge → apply_patch → Reducer，3 | assemble → 角色/进度 Reducer 或费用函数，2 |
| 研究业务可返回 State 域 | 通用五域及 messages | 一个独立结果，无任意 State 域 |
| 生产研究合并路径 | 通用 Patch 校验与合并 | 单一结果组装；旧格式只在读取处处理 |
| 显式合并入口与直接领域规则函数 | 7（merge/apply + 五 Reducer） | 5（assemble + role/workflow/budget/metrics） |
| join 边界的数据契约 | 2（Patch、State） | 2（RoleResult、State）；没有虚称契约数量下降 |
| 持久化 wire 格式 | 1 | 2（当前 JSON 结果 + 有界旧 Patch 读取） |
| 新增结果结构/读取函数 | 0 | 1 个模型（含 2 个校验函数）+ 1 个 reader |
| 新执行/存储设施 | 0 | 0 |

本轮**没有实现代码净减少**。新增的持久化 reader 和强类型结构是实际维护成本，
不能将旧合并器迁入 tests 后的删除行数当成整体收益。常态研究路径减少了需要理解的
任意域 override、跨域写权限与共享 report replacement 冲突；按角色的结果类型把
这些写权限从契约中排除。原合并器确实退出生产，没有同时长期维护两套研究执行流。

继续迁移的判断依据是业务所有权更窄、任务进度集中、只保留一个执行路径，而不是
形式上换成 Pydantic 或声称 LOC/速度改善。如果验收仅要求净减行数，本实现不应被
描述为达到该指标。兼容 reader 在相关 Artifact 保留期结束后才可评估删除，不能现在
删掉以制造代码量收益。函数计数仅统计合并入口与直接选择的领域规则，共用内部辅助函数不重复统计；新 schema 校验和 reader 另列，不隐藏它们的成本。新增需要理解的契约是 ResearchRoleResult；旧 Patch 合同
仅在升级边界和独立 oracle 保留，State/SDK Runtime/Workspace 的业务含义保持原样。

`deepcopy` 开销另做合成测量：Windows 同一解释器，5 组 × 每组 20 次，取每组平均耗时的中位数；每次输入重建在计时外。旧函数使用冻结 oracle，两个角色结果转换成原 Patch；新函数使用同一结果。无 Graph I/O、LLM 或工具。

| 合成 State | 旧 merge，ms/join | 新 assemble，ms/join |
| --- | ---: | ---: |
| 2 条消息、每角色 2 条记忆、2 个章节 | 0.187 | 0.084 |
| 250 条消息、每角色 1000 条记忆、100 个章节 | 16.881 | 0.282 |

消息内容各 1000 字符，记忆记录含 content 和单项 metadata，章节为原 Section 模型；预算初值 model_calls=7，每角色结果增量=2。该测量用于检查全 State 深拷贝的规模影响，受并行测试负载影响，不是生产容量或端到端速度证明。选择局部构造的依据还包括失败原子性和测试中验证的数据隔离；没有为微基准引入运行期缓存或状态同步。

历史 oracle 审计后仅调整了输入边界：把旧合并器原样冻结到测试模块，将新结果转回
旧生产格式，再执行原循环/错误/报告/预算比较。它不调用新 assembler 或新 reader；
没有改研究、补研或失败期望来迎合新行为。新引擎另外由真实 native/reference 对照
验证，旧 oracle 的离线通过数不冒充恢复能力证据。

GitHub 首个实现提交 d38577dc 的 quality、Linux **1080 passed / 125 deselected**、
前端和七项浏览器验收已通过；该轮新增键序测试的修正提交再次触发完整 CI。
Draft 的 Claude Review 步骤按原规则跳过，不能把其工作流 success 说成外部模型审查已运行。

## 7. 审查、剩余风险与面试取舍

实现后以独立于编写的审查轮次核对生产 diff、全部消费者、失败路径、原子性和回放。
修复通用 runtime override 对累计费用的影响、禁用旧 Artifact 的零用量规范化，
并确保未知 Graph 保存字段不会被默认 extra-ignore 静默丢弃。新增相应回归。
这是本会话中的单独审查轮次，不宣称另有人工或外部模型审查者。

- 已有边界保留：不能从未知工具效果或任意 SDK turn 恢复；没有跨 PostgreSQL、
  Neo4j、MCP/provider、Memory 的全局 exactly-once；只读安全重试仍需原正式授权。
- 不支持的历史 Patch 明确拒绝并保留原数据；需要匹配 release 完成或人工核对，
  不自动“修复” journal。已支持基线格式有真实进程恢复证据。
- 实际供应商、付费搜索、远程 MCP、生产 RAG/MySQL/多机 HA 未验证。
  受控真实 Runner/本地 Neo4j 不等于供应商质量或生产容量验收。
- 兼容读取增加代码成本；原消费者 Patch、五域 State/Reducer 和旧依赖冲突继续是
  保留债务，没有趁机全面清理或改变所有 Agent 的返回契约。

可讲解的决策：把“业务结果”与“写全局 State 的指令”分开；让确定性 Python 处理
join/进度而不新增 Supervisor LLM；沿用原 reducer 的报告和记忆规则；以已接受单元
费用作为 delta、与初始累计预算分开；兼容读取但不重放外部成功调用；失败原子性用
局部构造和一次提交实现，而不是增加数据库事务表或第二套运行调度。
