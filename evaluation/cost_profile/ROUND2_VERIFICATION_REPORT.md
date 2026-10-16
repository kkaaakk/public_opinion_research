# Round 2 验证报告（main@35adc85 + deepseek-v4-flash）

- 验证日期：2026-09-17
- 代码版本：`35adc854a09e43079a354973bf209a3a22241a36`（docs: preserve architecture and evaluation records）
- Case：`evaluation/cost_profile/single_case.json`（Tesla 中国车辆安全舆情，固定 Case）
- 对比基准：`5989480` After Benchmark（`evaluation/cost_profile/AFTER_SINGLE_CASE_COST_PROFILE.md`）
- 产物：`round2_full_cost_profile.json`（完整成功运行）、`after_v4flash_cost_profile.json`（同模型对照组）、`round2_cost_profile.json`（早期超时快照）

## 0. 执行摘要

1. **未发现代码级测试回归**：251 个测试中，所有失败均可归因于（a）本地 `.env` 的 `RESEARCH_GRAPH_ENABLED=true` 与当前 main 的冲突、（b）在 `5989480` 上同样失败的既有问题、（c）需要联网下载模型的环境限制。**产品代码零改动。**
2. **P0 环境阻断（已定位，未修复）**：当前 `.env`（`RESEARCH_GRAPH_ENABLED=true` + 无 `RESEARCH_GRAPH_URI`）会使每个业务 Agent 在启动时抛出 `research_graph_uri is required`，端到端流程完全无法完成；同时污染测试（16 个失败）。`.env.example` 默认是 `false`。
3. **端到端运行**：共尝试 3 次，第 3 次**完整成功**（42.96 分钟，含最终报告）。前两次分别为 40 分钟超时、25.5 分钟瞬时网络错误中断。
4. **同模型架构对比（核心结论，v4-flash 下 main vs 5989480）**：main 单次运行 379 calls / 2.36M known tokens / 42.96 分钟；对照组 436 calls / 3.07M / 46.9 分钟。即 **calls −13%、tokens −23%、时长 −8.5%**；ReAct 峰值上下文 **114,598 vs 181,760（−37%）**（`rolling_compact` 机制生效）；网页摘要 317 vs 382（−17%）。单轮运行存在模型随机性，上述差值不构成严格归因，但方向一致且上下文上限改善是结构性的。
5. **仍远高于 deepseek-chat 时代**：完整 benchmark（5989480 + deepseek-chat）为 233 calls / 1.33M tokens / 10.7 分钟。模型切换（chat→v4-flash，端点已不支持 chat）是成本膨胀的主因。
6. **研究质量未见下降**：main 最终报告 6,945 字符，覆盖用例全部 6 个必需维度（时间线、事实核查、官方/独立信源、替代解释、召回/合规/品牌风险、不确定性与后续核验），与对照组 7,312 字符报告同级；gap 发现（3–4 个）、冲突识别（2–3 个）、follow-up 闭环（3–4 个任务全部执行）均正常。已知差异：v4-flash 不输出 URL 引用（两版均 0 URL，deepseek-chat 为 55 URL → 模型行为）；单节回退问题两版相同。

## 1. 基准身份与最近架构改动

`5989480` 之后的 main 提交（按时间顺序）：

| commit | 内容 | 对成本/上下文的行为影响 |
|---|---|---|
| `2bdb9ad` | feat: add research graph context harness | 新增 opt-in Research Graph/Workspace；`RESEARCH_GRAPH_ENABLED` 等配置；默认关闭（`.env.example=false`） |
| `49954f9` | refactor: group research state by ownership | state 重组（`role_reports`→`agents`、`workflow`、`report`、`runtime`）；新增 `runtime/agent_runtime.py`（含 `rolling_compact` 上下文压缩）；`research_graph/workspace.py` |
| `3f0a548` | refactor: unify graph state schema | state 类型统一（15 行级改动） |
| `5cc29c0` | refactor: centralize business agent execution | 业务 Agent 执行集中到 `runtime/business_agent.py`（592 行）；`deep_researcher.py` 减 510 行；`strategy.py` 重构 |
| `35adc85` | docs: preserve architecture and evaluation records | 仅文档/评测产物归档 |

默认路径（graph 关闭）的实质新增：`rolling_compact`（ReAct 历史超 75% 模型上限时滚动压缩）、`tavily_search_raw`（不逐页摘要，仅 graph producer 使用）、更丰富的 Observer component 标签（含轮次）。prompt 差异仅 1 行（review prompt 增加 `{completed_tasks}`）。

## 2. 测试回归结论

执行方式：`uv run python -m pytest tests`（251 收集）。全量运行在 `tests/test_rag.py::test_rag_pipeline_retrieves_relevant_local_context` 处挂起（`sentence_transformers` 需联网下载模型 → 环境限制）。分文件限时执行后：

| 类别 | 结果 | 归因 |
|---|---|---|
| 其余全部测试（87+） | 通过 | — |
| 16 个测试（role_reports 传播、research loop、p1_fixes 等） | 失败 | **仅当 `.env` 的 `RESEARCH_GRAPH_ENABLED=true` 被加载时**。`Configuration.from_runnable_config` 优先级为 `os.environ > runtime config`，多个模块 import 时 `load_dotenv()` 将 true 注入 pytest 进程；随后 Neo4j store 因缺 URI 抛错 |
| `test_agent_observer_integration.py` 的 retry/interrupt 两个用例 | 失败 | **`5989480`（POR-after worktree）上同样失败** → 既有问题非回归 |
| `test_rag.py` 的 neo4j graph_rag 两个用例 | 失败 | Neo4j 不可用；`5989480` 上同样失败 |
| `test_rag.py` sentence_transformers 用例 | 挂起 | 需下载模型，环境限制 |

设置 `RESEARCH_GRAPH_ENABLED=false` 后，除上述既有失败与联网用例外，测试全部通过（核心子集 87/88，唯一失败为该优先级 artifact；RAG 子集 45+20 通过、2 个 Neo4j 用例失败、1 个联网用例挂起）。

## 3. 端到端运行

### 3.1 尝试记录

| 尝试 | 结果 | 时长 | Model Calls | Tool Calls | 说明 |
|---|---|---:|---:|---:|---|
| #1 | timeout（2400s 上限） | 2400.4s | 388 | 92 | 完成 R1+R2、2 次 Review、4 个 follow-up；risk 阶段被截断 |
| #2 | APIConnectionError | 1528.4s | 302 | 58 | public_signal R2 中瞬时网络错误 |
| #3 | **success** | **2577.5s** | **379** | **115** | **完整流程 + 最终报告 6,945 chars** |

运行前置修正（评测 harness，非产品代码）：`RESEARCH_GRAPH_ENABLED=false`（复现 5989480 等效路径）、v2 tuple stream chunk 归一化、ownership state schema 解析、60s 检查点落盘、针对结构化输出 None 与网络错误的重试。

### 3.2 三方指标对比（全部成功运行）

| 指标 | After `5989480`（deepseek-chat） | Control：`5989480`（v4-flash） | **Round 2 `35adc85`（v4-flash）** | R2 vs Control |
|---|---:|---:|---:|---:|
| 状态 | success | success | **success** | — |
| Duration | 642.5s | 2,816.2s | **2,577.5s** | −8.5% |
| Model Calls | 233 | 436 | **379** | **−13%** |
| Tool Calls | 60 | 101 | 115 | +14% |
| Input Tokens（known） | 1,274,421 | 2,933,712 | **2,250,771** | **−23%** |
| Output Tokens（known） | 57,966 | 132,687 | 113,630 | −14% |
| Total Tokens（known） | 1,332,387 | 3,066,399 | **2,364,401** | **−23%** |
| 网页摘要调用 | 187 | 382 | 317 | −17% |
| react_reasoning | 34 | 42 | 50（含 1 次 rolling_compact） | +19% |
| research_compression | 6 | 6 | 6 | 0 |
| research_review | 2 | 2 | 2 | 0 |
| rolling_compact | —（无机制） | —（无机制） | 1 | 新机制 |
| ReAct 最大输入 | 96,307 | **181,760** | **114,598** | **−37%** |
| 模型调用累计时长 | 1,645s | 10,609s | 8,471s | −20% |
| role_reports 重复注入（估算） | 171,151 | 226,750 | 186,317 | −18% |
| 最终报告 | 3,946 chars | 7,312 chars | **6,945 chars** | −5% |
| Research Review | 2 | 2 | 2 | 0 |
| 完成的 follow-up 任务 | 5 | 4 | 3 | −1 |

各 Agent 调用分解（三版）：

| 角色/阶段 | After chat | Control v4-flash | Round 2 v4-flash |
|---|---:|---:|---:|
| internal_knowledge | 15（13 react + 2 comp） | 18（16 react + 2 comp） | 22（20 react + 2 comp） |
| public_signal | 190（15 react + 173 摘要 + 2 comp） | 341（15 react + 324 摘要 + 2 comp） | 300（21 react + 277 摘要 + 2 comp） |
| risk_assessment | 19（4 react + 14 摘要 + 1 comp） | 67（~8 react + ~58 摘要 + 1 comp） | 45（4 react + 40 摘要 + 1 comp） |
| response_strategy | 3（2 react + 1 comp） | 4（3 react + 1 comp） | 6（5 react + 1 comp） |
| section_writer | 1 | 1 | 1 |
| research_review | 2 | 2 | 2 |

工具调用分解：

| 工具 | After chat | Control v4-flash | Round 2 v4-flash |
|---|---:|---:|---:|
| web_search | 9 | 16 | 17 |
| rag_search | 22 | 46 | 60 |
| think_tool | 13 | 16 | 16 |
| 社媒工具合计 | 10 | 17 | 18 |
| ResearchComplete | 6 | 6 | 4 |
| 合计 | 60 | 101 | 115 |

### 3.3 网页摘要扇出（最大成本项）

- 机制：`web_search`（tavily）对每个去重后的 URL 单独调用一次摘要模型（`utils.py:summarize_webpage`，`asyncio.gather` 并行）。每次 `web_search` 携带 2–8 个 query、工具返回 43K–121K 字符。
- Round 2：17 次 `web_search` → 317 次摘要（≈19/次）；Control：16 次 → 382（≈24/次）；After-chat：9 次 → 187（≈21/次）。**扇出机制三版一致；差异主要来自每次搜索实际返回/去重的 URL 数（模型查询批次决定的随机因素）**。
- 摘要调用平均耗时：5.6s（chat）→ 16.7s（control）→ 20.2s（Round 2）；累计 6,403s（Round 2，并行执行，是墙钟时长的主要来源）。
- 新代码中已有 `tavily_search_raw`（跳过逐页摘要），但仅在 Research Graph producer 策略下启用；默认路径仍走逐页摘要。

### 3.4 ReAct 上下文膨胀与 rolling_compact

以精确 input_tokens 序列计（每角色每轮峰值）：

| 版本 | public_signal R1 | public_signal R2 | internal R1 | internal R2 | risk | strategy |
|---|---:|---:|---:|---:|---:|---:|
| After（5989480, chat） | 47,873 | 96,307 | 16,295 | 18,475 | 42,942 | 38,490 |
| Control（5989480, v4-flash） | **181,760** | 150,395 | 31,459 | 18,947 | 96,483 | 42,718 |
| **Round 2（main, v4-flash）** | **114,598** | 108,084 | 36,787 | 30,077 | 67,661 | 48,602 |

结论：
- v4-flash 的搜索结果更大，若无约束上下文可冲到 182K（对照组）。
- main 的 `rolling_compact` 会触发（完整运行 1 次、早期超时运行 3 次），把 public_signal 峰值压到 114K（**相对同模型 −37%**）。
- 但压缩下限仍偏高：触发线为 0.75×131,072≈98K，压缩保留“最近 3 步原始消息”，其中含巨型工具结果，压缩后仍约 92–105K（早期快照中可见 111K→68K→94K、105K→92K 的序列），属“防爆”而非“降本”。压缩调用本身消耗大（单次 input≈整个历史）。
- 仍高于 After+chat 的 96K 峰值，但在同模型对比中为明确改善。

### 3.5 role_reports 重复注入

默认（graph 关闭）路径与基准相同：assignment/review/section 阶段把**完整上游角色报告**格式化进 prompt（`runtime/business_agent.py:_role_context`、`deep_researcher.py:research_review`）。Graph 模式的 scoped context 替代方案依赖 Neo4j，本环境不可用。

完整运行估算（字符数÷4，全量口径）：

| 消费方 | Round 2 调用数 | Round 2 估算 tokens | Control 估算 tokens |
|---|---:|---:|---:|
| research_review | 2 | 20,161 | 23,986 |
| risk_assessment | 4 | 56,632 | 122,952 |
| response_strategy | 5 | 88,335 | 57,135 |
| section_writer | 1 | 21,189 | 22,677 |
| **合计** | 12 | **186,317** | **226,750** |

（After-chat 基准为 171,151。）Round 2 相比对照下降 18%，主要来自报告更精炼与 risk/strategy 调用数不同；**注入机制本身未变**。

### 3.6 时间去向（Round 2 完整运行）

| 调用类型 | 调用数 | 累计时长 | 说明 |
|---|---:|---:|---|
| webpage_summarization | 317 | 6,403s | 并行执行，墙钟主要构成 |
| react_reasoning | 50 | 927s | 大上下文推理 |
| research_compression | 6 | 790s | 轮末压缩 |
| research_review | 2 | 150s | |
| section_writer | 1 | 73s | |
| report_planner/brief/clarify | 3 | 127s | |
| 工具 web_search | 17 | 929s | 含内部并行摘要等待 |
| **墙钟总计** | | **2,577s** | |

## 4. 研究质量评估（完整运行）

**覆盖用例全部 6 个必需维度：**

| 维度 | 报告中的体现 |
|---|---|
| 事故与监管时间线 | 表格化时间线：6 起事件 + 召回 + 历史背景节点（含窗口边缘事件标注） |
| 车型/软件/道路/伤亡事实 | 逐事件“已证实事实 / 单方说法 / 待补查”三级核查；召回编号、数量、生产时段、生效日期完整 |
| 官方与独立信源互证 | 交警认定、SAMR 公告、特斯拉客服口径、媒体多源交叉；标注网通社 AI 生成报道为低权威 |
| 技术故障 vs 驾驶行为 | 事件 2/4 显式列出两类假设与证据支持度，明确“不预设责任” |
| 召回/整改/法律/品牌影响 | 风险章含召回执行覆盖率、OTA 沟通缺口（引用内部复盘）、客服投诉趋势、置信度分级 |
| 不确定性与后续核验 | 每事件“待补查”清单 + 报告末尾后续核验任务 |

**Gap 与冲突识别（来自 Review#1）**：研究缺口 3 个（社媒结构化数据缺失、G50 事故官方口径缺位、召回后续执行追踪）、冲突 2–3 个（内部合规快照 Q2 2026 滞后 vs 8 月召回、内部知识库无事故记录 vs 公开信号）、next_tasks 3 个且全部被对应 Agent 执行（completed tasks = 3）。

**与对照组质量同级**：对照组最终报告 7,312 字符、18+ 编号来源、同样覆盖全部维度；Round 2 为 6,945 字符。两版均因预算逻辑回退为单节报告（`5989480` 同样发生），但 section_writer 仍产出结构化长文。

**质量差异点（均为模型行为，非架构）**：
- 引用形态：Round 2 与对照的 role reports/final report 均 0 URL（deepseek-chat 为 55 URL）；prompt 要求 `[n] Source Title: URL`，v4-flash 仅保留来源名称。
- clarif结构化输出偶发 None（2/4 次尝试）、summarization 偶发 None（有降级保护）。

**环境缺口**：社媒 API（127.0.0.1:9000）全程不可用（WinError 10061），社会面一手证据缺失；两版报告中均被正确标记为数据缺口并降低相关置信度。

## 5. 与 5989480 After Benchmark 的对比判断

**模型因素（已用对照组剥离）：**
- 同代码 `5989480` 换模型（chat→v4-flash）：calls 233→436（+87%）、known tokens 1.33M→3.07M（+130%）、时长 642s→2816s（×4.4）、摘要 187→382（+104%）、ReAct 峰值 96K→182K（+89%）、重复注入 171K→227K（+32%）。
- 当前端点已不支持 deepseek-chat（404 UnsupportedModel），该差异不可回避；**成本膨胀的主因是模型**。

**架构因素（同 v4-flash，单次运行，存在随机性）：**
- **改善**：calls −13%、tokens −23%、时长 −8.5%、ReAct 峰值 −37%、摘要 −17%、重复注入估算 −18%。
- **代价/未改善**：react 调用 +19%（次数更多、单次更小）、tool calls +14%（rag_search 60 vs 46）、`rolling_compact` 压缩调用本身有开销；role_reports 全量注入机制未变（估值的下降来自报告尺寸与下游调用数，而非机制）。
- **结论**：在同模型前提下，最新架构在整体成本、上下文上限与时长上有可测量的改善；但在 deepseek-chat 时代基准下，运行成本仍是 178% tokens / 240% 时长（模型所致）。

**质量因素**：两版在同模型下同级；未见流程性下降（gap/冲突/follow-up/最终报告完整性均达标）。

## 6. 优先级发现与建议修改点（本轮未改动任何产品代码）

| 优先级 | 问题 | 证据 | 建议 |
|---|---|---|---|
| **P0** | `.env` 的 `RESEARCH_GRAPH_ENABLED=true` 无 `RESEARCH_GRAPH_URI`，业务 Agent 启动即抛错，E2E 完全不可用；同时污染测试（16 个失败） | 运行 #2 报错栈；测试差异仅由该 env 引起 | 修复本地 `.env`（false 或 `RESEARCH_GRAPH_BACKEND=memory`）；复审 `Configuration.from_runnable_config` 的 `env > runtime config` 优先级（与常见约定相反） |
| **P0** | 网页摘要扇出：每次搜索逐 URL 调用摘要模型；单 case 317–382 次调用、累计 6,400–7,100s | §3.3 | 默认路径接入 `tavily_search_raw`（新代码已有）或批量/上限摘要；同 URL 跨轮去重缓存 |
| **P0** | 上下文高位震荡：`rolling_compact` 压缩后仍 92–114K（触发线 98K），且压缩调用自身昂贵 | §3.4 | 降低保留窗口（`recent_raw_steps`）、对巨型工具结果做 micro-compaction（graph 模式 receipt 机制下放到 standard 路径）、限制工具结果注入长度 |
| **P1** | role_reports 全量重复注入（review/risk/strategy/section），估算 186K tokens/run | §3.5 | 改为传递 claim/来源/冲突/Gap 摘要；或推进 graph 模式（需 Neo4j 可用） |
| **P1** | 报告单节回退：规划 ≥5 个 research section 即回退单节（无预算限制时也触发） | `deep_researcher.py:654`；`5989480` 与 main 均发生 | 修正 `available_research_unit_slots` 保留位的 off-by-one 设计 |
| **P1** | 模型层稳定性：clarify structured output 偶发 None、summarization None（有降级） | 运行日志 | 对 `None` 增加显式校验/重试（当前 `with_retry` 只覆盖异常） |
| **P2** | 评测 harness 适配：旧解析器不认识 v2 tuple chunk、ownership state、`rolling_compact` call_type | 本轮已补丁适配 | 建议并入 `run_cost_profile.py`/`render_cost_profile.py` 长期维护 |

## 7. 局限性

- 单 Case、单轮成功运行；另一轮超时、一轮网络中断，共 3 次尝试。对照组与 Round 2 各 1 次成功运行，架构差值包含模型随机性（尤其查询批次/摘要数量），不构成严格归因；上下文上限改善（rolling_compact）为机制性证据。
- Round 2 成功运行在对照组退出后独占端点；对照组运行期间与 Round 2 尝试 #2/#3 有重叠 → 时长对比对 Round 2 相对有利，方向性参考。
- 模型已从 `deepseek:deepseek-chat` 切换为 v4-flash，且旧模型在当前端点不可用（404 UnsupportedModel）。
- 社媒 API、Neo4j、HuggingFace 下载均不可用，相关能力未验证。
- 评测 harness 为适配新版 stream/schema 做了只读补丁（`run_round2_profile.py`、`run_after_v4flash.py`、`postprocess_reports.py`）；产品代码零改动。

## 附：证据文件索引

| 文件 | 内容 |
|---|---|
| `round2_full_cost_profile.json` | Round 2 完整成功运行原始数据（379 calls） |
| `round2_full_cost_profile_reports.json` | Round 2 报告级后处理（role reports/review/final/重复注入） |
| `after_v4flash_cost_profile.json` | 对照组原始数据（5989480 + v4-flash） |
| `round2_cost_profile.json` | 早期 40 分钟超时快照（用于 rolling_compact 触发序列） |
| `archive/reports/r2_final.txt` / `archive/reports/af_final.txt` | 两版最终报告文本（迁移准备时原样归档） |
| `archive/reports/ps_report.txt` / `archive/reports/ik_report.txt` | Round 2 角色报告文本（迁移准备时原样归档） |
| `compare_three.py` / `analyze_round2.py` / `analyze_time.py` / `analyze_growth.py` / `analyze_quality.py` / `analyze_tools.py` | 分析脚本 |
