# 集成 Plan-and-Execute 到 Deep Researcher 舆情模式 Spec

## Why

当前主实现（`src/open_deep_research/deep_researcher.py`）的舆情模式存在一个结构性缺陷：`write_research_brief` 只生成了一个扁平的研究简报，4 个角色 agent 跑完后，最终报告的结构完全依赖 `final_report_generation` 从一堆 `notes` 里临时重建，既不可控也无法让人审核。

Legacy 的 Plan-and-Execute 方案（`src/legacy/graph.py`）提供了"先规划大纲、再按 section 研究、最后按序拼装"的结构骨架，但缺乏当前主实现的预算管理、角色协作等执行引擎能力。

两者是正交的：Plan-and-Execute 提供**结构骨架**，当前舆情模式提供**执行引擎**。本次变更将 Plan-and-Execute 的结构化规划能力作为**必选骨架**集成到舆情模式中，让报告结构从"事后组织"变成"事前规划 + 事中跟踪 + 事后拼装"。

**当前主图只运行舆情模式（`business_scenario == "public_opinion_risk"`），不包含独立的 Supervisor + Researcher 编排层。**

## What Changes

- **新增** `plan_report_sections` 节点：在舆情模式下将 `research_brief` 转化为结构化 `Section` 列表，每个 section 声明 `agent_role` 映射到舆情角色
- **新增** `Section` / `Sections` 数据结构到主实现的 `state.py`，扩展 `Section` 字段以支持完成状态跟踪和角色映射
- **新增** `section_writer` 节点：舆情模式 4 角色 agent 跑完后，按 section 的 `agent_role` 从 `role_reports` 提证据写作 section content
- **新增** `write_final_sections` 节点：并行写不需要研究的 section（引言、结论等），复用已完成 section 作为上下文
- **新增** `compile_final_report` 节点：按原始 section 顺序拼装最终报告，替代当前 `final_report_generation` 的从 notes 合成逻辑（仅舆情模式）
- **改造** `AgentState`：增加 `sections`、`completed_sections`、`feedback_on_report_plan` 字段
- **改造** `research_phase`：舆情模式路由增加 `plan_report_sections → public_opinion_subgraph → section_writer → write_final_sections → compile_final_report` 流程
- **新增** 配置项：`allow_plan_feedback`、`report_structure`、`planner_model`、`planner_model_max_tokens`、`section_writer_model`、`section_writer_model_max_tokens`
- **新增** Prompt 模板：`report_planner_instructions`（舆情增强版）、`section_writer_from_role_reports_prompt`、`final_section_writer_instructions`
- **保留** 向后兼容：所有新节点在预算不足时退化为当前行为（单 section / 复用 role_reports 原文 / 从 notes 合成）
- **不新增** Supervisor Agent、Researcher 子图或 `ConductResearch` 委派链路

## Impact

- **Affected specs**: 无（这是首个针对 deep_researcher 舆情模式结构的 spec）
- **Affected code**:
  - `src/open_deep_research/state.py` — 新增 Section/Sections 数据结构，扩展 AgentState
  - `src/open_deep_research/deep_researcher.py` — 新增节点，改造舆情模式主图边与 research_phase 路由
  - `src/open_deep_research/configuration.py` — 新增配置字段
  - `src/open_deep_research/prompts.py` — 新增 prompt 模板
  - `src/open_deep_research/budget.py` — 复用现有预算函数（无改动，仅消费）
  - `src/open_deep_research/public_opinion_agents/` — 无改动（4 角色 DAG 保持不变）
  - `langgraph.json` — 无改动（入口仍是 `deep_researcher`）
  - `tests/` — 新增针对 plan/section_writer/compile 的单元测试
- **Not affected**:
  - `compress_research` 作为四个业务 Agent 的内部压缩步骤保留
  - `ConductResearch`、`ResearchComplete` 工具
  - 已移除的通用模式子图不属于当前主图

## ADDED Requirements

### Requirement: 舆情模式结构化报告规划

当 `business_scenario == "public_opinion_risk"` 时，系统 SHALL 在 `write_research_brief` 之后、`public_opinion_subgraph` 之前，提供 `plan_report_sections` 节点，将研究简报转化为结构化的 `Section` 列表。

每个 `Section` SHALL 包含以下字段：
- `name`: section 名称
- `description`: 主要内容概述
- `research`: 是否需要基于角色证据写作（bool）
- `content`: 实际内容（规划时为空）
- `agent_role`: 依赖的舆情角色（逗号分隔，如 "public_signal,internal_knowledge"）
- `status`: 完成状态（`pending` / `done`）

`plan_report_sections` SHALL 使用 `planner_model`（配置项，默认回退到 `research_model`）+ `with_structured_output(Sections)` 生成 section 列表。

`plan_report_sections` SHALL 受 Budget Guard 保护：当剩余预算不足以保留最终报告模型调用时，退化为生成单个 section（名称为 "Research Report"，description 为 research_brief，agent_role 包含全部 4 角色），跳过结构化规划。

`plan_report_sections` SHALL 支持预算感知的 section 数量限制：section 数量不超过 `available_research_unit_slots` 返回值（若为 None 则上限 5）。

**预算预留时序**：`plan_report_sections` 在计算可用 slots 时，SHALL 预先扣除 `section_writer` 阶段所需的预算（即 `section_count * 1 model call unit`），避免规划阶段超发"空头支票"导致后续 `section_writer` 预算不足。具体逻辑：先获取 `available_research_unit_slots`，再从中减去预留给 section_writer 的 slots（等于 research=True 的 section 数量），用剩余 slots 限制最终 section 数量。如果扣除后剩余 slots <= 0，退化为单 section。

舆情模式下的 planner prompt SHALL 额外要求：
- 每个 section 必须声明 `agent_role`，取值为 `public_signal` / `internal_knowledge` / `risk_assessment` / `response_strategy` 之一或组合
- section 结构应覆盖舆情风控报告的典型维度（事件概述、公众信号、内部证据、风险评估、响应建议等）

#### Scenario: 舆情模式正常规划
- **WHEN** 舆情模式启用，预算充足
- **THEN** 系统在 `write_research_brief` 后调用 `plan_report_sections`，生成 2-5 个结构化 section
- **AND** 每个 section 有明确的 name、description、agent_role
- **AND** 至少有 2 个 section 的 research=True

#### Scenario: 舆情模式预算不足退化
- **WHEN** 舆情模式启用，Budget Guard 判定无法保留最终报告模型调用
- **THEN** 系统跳过规划，生成单个 section（name="Research Report"，agent_role="public_signal,internal_knowledge,risk_assessment,response_strategy"）
- **AND** 流程继续，不阻塞

#### Scenario: section 数量限制
- **WHEN** planner 生成了 8 个 section，但 `available_research_unit_slots` 返回 3
- **THEN** 系统截取前 3 个 section 作为最终规划

#### Scenario: 预算预留时序保护
- **WHEN** `available_research_unit_slots` 返回 4，但 planner 规划了 4 个 research=True 的 section
- **THEN** 系统扣除 section_writer 预留的 4 个 slots，剩余 0
- **AND** 退化为单 section（agent_role 包含全部 4 角色）

#### Scenario: Public Opinion 是唯一主流程
- **WHEN** 主图启动
- **THEN** `plan_report_sections` 和 `research_phase` 按固定 Public Opinion 流程执行

### Requirement: 人工审核舆情报告大纲

系统 SHALL 通过 `allow_plan_feedback` 配置项（默认 `False`）控制是否启用人工审核。

当 `allow_plan_feedback=True` 且舆情模式启用时，`plan_report_sections` SHALL 通过 LangGraph `interrupt()` 暂停执行，将 sections 列表（含 agent_role 映射）作为 interrupt value 展示给用户。

**interrupt/resume 机制**：
- `plan_report_sections` 调用 `interrupt({"sections": [s.dict() for s in sections]})` 暂停
- 恢复时通过外部调用传入 `Command(resume=<value>)`
- `human_feedback_on_plan` 节点接收 resume 的值并路由

`human_feedback_on_plan` 节点 SHALL 按以下规则解析 resume 值并路由：
- resume 值为 `True`（bool）→ 批准计划，`Command(goto="research_phase")`
- resume 值为字符串 → 作为反馈累加到 `feedback_on_report_plan`，`Command(goto="plan_report_sections", update={"feedback_on_report_plan": [feedback]})`
- resume 值为其他类型 → 抛出 `TypeError`（与 legacy `human_feedback` 节点行为一致）

#### Scenario: 启用人工审核并批准
- **WHEN** `allow_plan_feedback=True` 且用户通过 `Command(resume=True)` 恢复
- **THEN** `plan_report_sections` 路由到 `research_phase`

#### Scenario: 启用人工审核并打回
- **WHEN** `allow_plan_feedback=True` 且用户通过 `Command(resume="请增加竞品声量对比 section")` 恢复
- **THEN** `human_feedback_on_plan` 节点将反馈追加到 `feedback_on_report_plan`
- **AND** 路由回 `plan_report_sections`，planner 在 prompt 中看到累积反馈

#### Scenario: 默认禁用人工审核
- **WHEN** `allow_plan_feedback=False`（默认）
- **THEN** `plan_report_sections` 直接生成 section 并继续，不暂停，不经过 `human_feedback_on_plan` 节点

### Requirement: 舆情模式 Section Writer 阶段

当 `business_scenario == "public_opinion_risk"` 时，`research_phase` SHALL 在 4 个角色 agent 跑完（`public_opinion_subgraph` 完成）后，调用 `section_writer` 阶段。

`section_writer` SHALL 对每个 `research=True` 的 section，根据 `section.agent_role` 字段从 `role_reports` 中提取相关角色的证据，用 `section_writer_model`（配置项，默认回退到 `final_report_model`）写出 section content。

`section_writer` SHALL 并行处理多个 section（使用 `asyncio.gather(*tasks, return_exceptions=True)`）。

**异常处理**：`asyncio.gather` SHALL 使用 `return_exceptions=True` 参数，确保单个 section 的 LLM 调用失败不会导致整个节点崩溃。处理逻辑：
- 如果某个 section 的写作抛出异常，该 section 的 `content` 留空，`status` 保持 `pending`
- 异常 section 不回填到 `completed_sections`，交给 `compile_final_report` 节点走"缺失 section 补齐/降级"逻辑
- 异常详情记录到日志（`LOGGER.warning`）

`section_writer` SHALL 受 Budget Guard 保护：
- 预算充足时，按 section 调用模型写作
- 预算不足时，复用 `role_reports` 原文作为 section content（降级），并标记 section 为 done

`section_writer` 的证据提取逻辑：
- 解析 `section.agent_role`（逗号分隔），对每个角色名 `strip()` 去除空格
- 对解析出的角色名做有效性校验：必须是 `public_signal` / `internal_knowledge` / `risk_assessment` / `response_strategy` 之一
- 如果角色名无效，记录 `LOGGER.warning` 并忽略该角色
- 从 `role_reports` 中取有效角色的报告作为证据
- 如果 `agent_role` 为空、所有角色都无效、或所有有效角色都无报告，使用全部 `role_reports` 作为证据兜底

#### Scenario: 舆情模式正常流程
- **WHEN** 舆情模式启用，4 个角色 agent 已完成，预算充足
- **THEN** 对每个 research=True 的 section，按 `agent_role` 提取证据并写作
- **AND** section content 回填到 `completed_sections`
- **AND** section status 标记为 done

#### Scenario: 舆情模式预算不足降级
- **WHEN** 舆情模式启用但预算不足以调用 section_writer 模型
- **THEN** 复用 `role_reports` 原文作为 section content（降级）
- **AND** 标记 section 为 done

#### Scenario: agent_role 部分匹配
- **WHEN** section 的 `agent_role="public_signal,internal_knowledge"`，但 `internal_knowledge` 角色未启用
- **THEN** 使用 `public_signal` 角色的报告作为证据
- **AND** 在 section content 中注明 `internal_knowledge` 角色未提供证据

### Requirement: 非研究 Section 并行写作

系统 SHALL 提供 `write_final_sections` 节点，对所有 `research=False` 的 section 并行写作。

`write_final_sections` SHALL 使用已完成 section（`research=True` 且 status=done）的 content 作为上下文。

`write_final_sections` SHALL 使用 `final_report_model` 写作，受 Budget Guard 保护。

**异常处理**：`write_final_sections` 的并行写作 SHALL 使用 `asyncio.gather(*tasks, return_exceptions=True)`。如果某个 section 写作抛出异常，该 section 的 `content` 留空，异常详情记录到 `LOGGER.warning`，交给 `compile_final_report` 走缺失 section 补齐逻辑。

如果没有 `research=False` 的 section，`write_final_sections` SHALL 直接返回空 dict，不消耗预算。

#### Scenario: 有非研究 section
- **WHEN** 规划包含 1 个引言（research=False）和 1 个结论（research=False）
- **THEN** 系统并行调用 `write_final_sections` 写作这两个 section
- **AND** 写作时使用已完成的研究 section 作为上下文

#### Scenario: 无非研究 section
- **WHEN** 所有 section 的 research=True
- **THEN** `write_final_sections` 返回空 dict，跳过

### Requirement: 按 Section 顺序拼装舆情最终报告

当 `business_scenario == "public_opinion_risk"` 时，系统 SHALL 用 `compile_final_report` 节点替代当前 `final_report_generation` 的从 notes 合成逻辑。

**拼装基准与去重**：`compile_final_report` SHALL 以 `state.sections`（原始规划顺序）为基准遍历，通过 `section.name` 映射到 `completed_sections` 中的 content。如果 `completed_sections` 中存在多个同名 section（并行写入或重试导致），取**最后一个**的 content（最新结果）。这样既保证了顺序，又解决了去重问题。

`compile_final_report` SHALL 用 `"\n\n".join` 拼装所有非空 content。

当某些 section 因写作失败或预算不足而缺失 content 时，`compile_final_report` SHALL：
- 如果预算允许，用 `final_report_model` 基于已有内容补齐缺失 section
- 如果预算不足，在报告末尾追加注释说明未完成的 section 名称

`compile_final_report` SHALL 保留 Budget Guard 的最终报告预算预留机制（`reserve_final_report_call`）。

`compile_final_report` SHALL 保留 `maybe_persist_chat_memory` 调用。

`compile_final_report` SHALL 使用舆情专用的 `public_opinion_final_report_generation_prompt` 作为兜底（当需要补齐或全量退化时）。

#### Scenario: 所有 section 完成
- **WHEN** 所有 section 都有 content
- **THEN** 按原始顺序拼装，生成完整报告

#### Scenario: 部分 section 缺失且预算充足
- **WHEN** section "风险评估" 缺失，但预算允许补齐
- **THEN** 用 `final_report_model` 基于已有 section 内容补齐 "风险评估"
- **AND** 按原始顺序拼装

#### Scenario: 部分 section 缺失且预算不足
- **WHEN** section "风险评估" 缺失，预算不足以补齐
- **THEN** 按已有 section 顺序拼装
- **AND** 在报告末尾追加：`> Note: 以下 section 因预算限制未完成: 风险评估`

### Requirement: 向后兼容退化

舆情模式的所有新节点 SHALL 在以下条件下退化为当前行为，不阻塞流程：

- `plan_report_sections` 预算不足 → 生成单个 section（agent_role 包含全部 4 角色）
- `section_writer` 预算不足 → 复用 `role_reports` 原文作为 section content
- `write_final_sections` 无非研究 section → 返回空
- `compile_final_report` 所有 section 都缺失 → 退化为从 notes 合成（当前行为，使用 `public_opinion_final_report_generation_prompt`）

#### Scenario: 全量退化等价于当前行为
- **WHEN** 预算不足导致所有新机制被跳过
- **THEN** 流程退化为：单 section → 4 角色 agent → role_reports 原文作为 content → 拼装
- **AND** 输出与当前舆情模式行为基本一致

## MODIFIED Requirements

### Requirement: 舆情模式主图流程

当前舆情模式主图流程为：
```
START → enrich_query_images → clarify_with_user → write_research_brief → plan_report_sections → research_phase → section_writer → write_final_sections → compile_final_report → END
```

其中 `research_phase` 调用 `public_opinion_subgraph`，负责主图与子图的状态转换。

修改后舆情模式流程为：
```
START → enrich_query_images → clarify_with_user → write_research_brief → plan_report_sections → research_phase → section_writer → write_final_sections → compile_final_report → END
```

其中：
- `human_feedback_on_plan` 为条件节点，仅在 `allow_plan_feedback=True` 时启用
- `research_phase` 内部调用 `public_opinion_subgraph`，完成后由主图继续调用 `section_writer`
- `final_report_generation` 节点在舆情模式下被替换为 `compile_final_report`

当前项目只有 Public Opinion 流程，不存在独立 Supervisor Agent 或通用模式分支。

### Requirement: AgentState 状态结构

`AgentState` SHALL 新增以下字段：
- `sections: list[Section]` — 规划出的 sections
- `completed_sections: Annotated[list[Section], operator.add]` — 并行子图结果自动累加
- `feedback_on_report_plan: Annotated[list[str], operator.add]` — 反馈累积

保留现有字段：`research_brief`、`role_reports`、`agent_memories`、`raw_notes`、`notes`、`budget_usage`、`final_report`。

`notes` 和 `raw_notes` SHALL 作为中间产物保留，作为 `compile_final_report` 兜底的输入素材。

### Requirement: research_phase 舆情模式路由

`research_phase` SHALL 在舆情模式下，调用 `public_opinion_subgraph` 后，再调用 `section_writer` 阶段，将 `role_reports` 转化为按 section 组织的 `completed_sections`。

`research_phase` SHALL 调用 `public_opinion_subgraph`，并将 `role_reports`、`agent_memories`、`notes`、`raw_notes` 和 `budget_usage` 写回主图。

## REMOVED Requirements

### Requirement: 舆情模式 final_report_generation 从 notes 合成

**Reason**: 被 `compile_final_report` 按 section 顺序拼装替代，提供更可控的报告结构。

**Migration**: `compile_final_report` 在所有 section 都缺失时，退化为从 notes 合成（复用 `public_opinion_final_report_generation_prompt`），保证向后兼容。`final_report_generation` 函数在通用模式保留，但舆情模式主图边不再直接调用。
