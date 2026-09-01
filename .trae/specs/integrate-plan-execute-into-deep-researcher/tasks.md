# Tasks

## 阶段 1：基础设施

- [x] Task 1: 扩展 State 数据结构
  - [x] SubTask 1.1: 在 `src/open_deep_research/state.py` 新增 `Section` BaseModel（name, description, research, content, agent_role, status）
  - [x] SubTask 1.2: 在 `state.py` 新增 `Sections` BaseModel（sections: list[Section]）
  - [x] SubTask 1.3: 扩展 `AgentState`，增加 `sections`、`completed_sections`（用 operator.add reducer）、`feedback_on_report_plan`（用 operator.add reducer）字段
  - [x] SubTask 1.4: 验证现有状态迁移不破坏（所有新字段有默认值）

- [x] Task 2: 新增配置项
  - [x] SubTask 2.1: 在 `src/open_deep_research/configuration.py` 的 `Configuration` 类新增 `allow_plan_feedback: bool = False`
  - [x] SubTask 2.2: 新增 `report_structure: str = DEFAULT_REPORT_STRUCTURE`（从 legacy configuration.py 迁移默认模板）
  - [x] SubTask 2.3: 新增 `planner_model: str = ""`（空则回退到 research_model）
  - [x] SubTask 2.4: 新增 `planner_model_max_tokens: Optional[int] = None`
  - [x] SubTask 2.5: 新增 `section_writer_model: str = ""`（空则回退到 final_report_model）
  - [x] SubTask 2.6: 新增 `section_writer_model_max_tokens: Optional[int] = None`
  - [x] SubTask 2.7: 验证配置项在 LangGraph Studio UI 中可见（x_oap_ui_config metadata）

- [x] Task 3: 新增 Prompt 模板
  - [x] SubTask 3.1: 在 `src/open_deep_research/prompts.py` 新增 `report_planner_instructions`（从 legacy 迁移，增加舆情 agent_role 声明要求）
  - [x] SubTask 3.2: 新增 `section_writer_from_role_reports_prompt`（舆情模式用，按 agent_role 提证据写作）
  - [x] SubTask 3.3: 新增 `final_section_writer_instructions`（从 legacy 迁移，用于非研究 section 写作）
  - [x] SubTask 3.4: 验证所有 prompt 模板的 format 占位符与代码调用一致

## 阶段 2：核心节点实现

- [x] Task 4: 实现 `plan_report_sections` 节点
  - [x] SubTask 4.1: 在 `deep_researcher.py` 实现 `plan_report_sections(state, config)` 函数
  - [x] SubTask 4.2: 主图统一使用 Public Opinion 流程，不再按业务模式 passthrough
  - [x] SubTask 4.3: 实现 Budget Guard 集成（预算不足时退化为单 section，agent_role 包含全部 4 角色）
  - [x] SubTask 4.4: 实现 section 数量限制逻辑（基于 `available_research_unit_slots`，None 时上限 5）
  - [x] SubTask 4.5: 实现 `planner_model` 配置回退逻辑（空则用 research_model）
  - [x] SubTask 4.6: 实现舆情模式 prompt 增强（要求每个 section 声明 agent_role，覆盖舆情风控典型维度）
  - [x] SubTask 4.7: 实现预算预留时序保护：先获取 `available_research_unit_slots`，再减去 section_writer 预留的 slots（等于 research=True 的 section 数量），扣除后 <=0 则退化为单 section
  - [x] SubTask 4.8: 实现可选的 `interrupt()` 人工反馈循环（内联于 plan_report_sections 节点中，不再需要独立的 human_feedback_on_plan 节点）
  - [ ] SubTask 4.9: 编写单元测试覆盖：正常规划、预算退化、section 截断、预算预留时序保护、人工批准和人工打回

- [x] Task 5: 实现 `section_writer` 节点
  - [x] SubTask 5.1: 在 `deep_researcher.py` 实现 `section_writer(state, config)` 函数
  - [x] SubTask 5.2: 实现按 `section.agent_role`（逗号分隔）过滤 `role_reports` 的证据提取逻辑：对每个角色名 `strip()` 去空格
  - [x] SubTask 5.3: 实现角色名有效性校验：必须是 4 个预设角色之一，无效则 `LOGGER.warning` 并忽略
  - [x] SubTask 5.4: 实现 `agent_role` 为空、所有角色无效、或所有有效角色都无报告时的兜底（使用全部 role_reports）
  - [x] SubTask 5.5: 实现并行写作：`asyncio.gather(*tasks, return_exceptions=True)`
  - [x] SubTask 5.6: 实现异常处理：单个 section 写作失败时 content 留空、status 保持 pending、不回填 completed_sections、`LOGGER.warning` 记录异常
  - [x] SubTask 5.7: 实现 `section_writer_model` 配置回退逻辑（空则用 final_report_model）
  - [x] SubTask 5.8: 实现 Budget Guard 集成（预算不足时复用 role_reports 原文降级，标记 section done）
  - [x] SubTask 5.9: 实现 section content 回填到 `completed_sections`，status 标记 done
  - [ ] SubTask 5.10: 编写单元测试覆盖：正常流程、预算不足降级、agent_role 部分匹配、agent_role 为空、agent_role 含无效角色名、单个 section 写作异常不阻塞其他 section

- [x] Task 6: 实现 `write_final_sections` 节点
  - [x] SubTask 6.1: 在 `deep_researcher.py` 实现 `write_final_sections(state, config)` 函数
  - [x] SubTask 6.2: 实现过滤 `research=False` 的 section 并行写作
  - [x] SubTask 6.3: 实现用已完成 section（research=True 且 status=done）的 content 作为上下文
  - [x] SubTask 6.4: 实现使用 `final_report_model` 写作
  - [x] SubTask 6.5: 实现并行写作：`asyncio.gather(*tasks, return_exceptions=True)`
  - [x] SubTask 6.6: 实现异常处理：单个 section 写作失败时 content 留空、`LOGGER.warning` 记录异常、交给 compile_final_report 走缺失补齐
  - [x] SubTask 6.7: 实现 Budget Guard 集成
  - [x] SubTask 6.8: 实现无非研究 section 时的空返回
  - [ ] SubTask 6.9: 编写单元测试覆盖：有非研究 section、无非研究 section、预算不足、单个 section 写作异常

- [x] Task 7: 实现 `compile_final_report` 节点
  - [x] SubTask 7.1: 在 `deep_researcher.py` 实现 `compile_final_report(state, config)` 函数
  - [x] SubTask 7.2: 实现以 `state.sections`（原始规划顺序）为基准遍历，通过 `section.name` 映射 `completed_sections` 中的 content
  - [x] SubTask 7.3: 实现去重逻辑：如果 `completed_sections` 中存在多个同名 section，取最后一个的 content（最新结果）
  - [x] SubTask 7.4: 实现缺失 section 的兜底补齐（预算充足时用 final_report_model）
  - [x] SubTask 7.5: 实现缺失 section 的降级注释（预算不足时追加 Note）
  - [x] SubTask 7.6: 实现全量退化逻辑（所有 section 缺失时退化为从 notes 合成，使用 `public_opinion_final_report_generation_prompt`）
  - [x] SubTask 7.7: 保留 `reserve_final_report_call` 预算预留机制
  - [x] SubTask 7.8: 保留 `maybe_persist_chat_memory` 调用
  - [x] SubTask 7.9: 实现舆情模式专用兜底 prompt（`public_opinion_final_report_generation_prompt`）
  - [ ] SubTask 7.10: 编写单元测试覆盖：全部完成、部分缺失+预算充足、部分缺失+预算不足、全量退化、同名 section 去重取最新

## 阶段 3：主图组装与路由

- [x] Task 8: 实现 `human_feedback_on_plan` 节点 — **已合并到 `plan_report_sections` 节点中作为内联 interrupt/resume 处理**
  - ~~SubTask 8.1-8.6~~: interrupt() 和 resume 值处理已在 `plan_report_sections` 节点内部实现（第 706-725 行）

- [x] Task 9: 改造 `research_phase` 舆情模式路由
  - [x] SubTask 9.1: 修改 `research_phase` 在舆情模式下，调用 `public_opinion_subgraph` 后再调用 `section_writer`
  - [x] SubTask 9.2: 实现 `section_writer` 结果（completed_sections）合并到返回的 state update
  - [x] SubTask 9.3: `research_phase` 直接调用 `public_opinion_subgraph` 并回写状态
  - [ ] SubTask 9.4: 编写测试覆盖：`research_phase` 调用子图并正确回写主图状态

- [x] Task 10: 组装主图
  - [x] SubTask 10.1: 在 `deep_researcher_builder` 增加 `plan_report_sections` 节点
  - [x] SubTask 10.2: 无需独立 `human_feedback_on_plan` 节点（内联于 plan_report_sections）
  - [x] SubTask 10.3: 增加 `write_final_sections` 节点
  - [x] SubTask 10.4: 增加 `compile_final_report` 节点
  - [x] SubTask 10.5: 修改边：`write_research_brief → plan_report_sections`（通过 Command goto）
  - [x] SubTask 10.6: `plan_report_sections` 中通过 interrupt() 内联处理人工反馈路由
  - [x] SubTask 10.7: `plan_report_sections → research_phase`（通过 Command goto）
  - [x] SubTask 10.8: 人工反馈打回时通过 `goto="plan_report_sections"` 重新规划
  - [x] SubTask 10.9: 修改边：`research_phase → section_writer → write_final_sections`
  - [x] SubTask 10.10: 修改边：`write_final_sections → compile_final_report`
  - [x] SubTask 10.11: 修改边：`compile_final_report → END`
  - [x] SubTask 10.12: 验证 `langgraph.json` 入口仍为 `deep_researcher`，无改动
  - [x] SubTask 10.13: 验证固定 Public Opinion 四 Agent 拓扑和主图状态转换不受影响

## 阶段 4：集成验证

- [ ] Task 11: 端到端集成测试
  - [ ] SubTask 11.1: 编写舆情模式端到端测试：plan → 4 角色 → section_writer → write_final_sections → compile
  - [ ] SubTask 11.2: 编写预算不足退化测试：舆情模式全量退化等价于当前行为
  - [ ] SubTask 11.3: 编写人工反馈测试：allow_plan_feedback=True 的批准和打回流程（含 interrupt/resume）
  - [ ] SubTask 11.4: 编写 Public Opinion 主流程回归测试：确保四 Agent 行为不变
  - [ ] SubTask 11.5: 运行 `ruff check` 确保无 lint 错误
  - [ ] SubTask 11.6: 运行 `mypy` 确保类型正确
  - [ ] SubTask 11.7: 运行现有测试套件确保无回归

# Task Dependencies

- Task 1（State）→ Task 2（Config）→ Task 3（Prompts）：顺序依赖，必须先完成 ✅
- Task 4（plan_report_sections）、Task 6（write_final_sections）、Task 7（compile_final_report）：依赖 Task 1-3，三者之间无依赖 ✅
- Task 5（section_writer）、Task 8（human_feedback_on_plan → 已合并到 Task 4）：依赖 Task 1-3 ✅
- Task 9（research_phase 改造）：依赖 Task 5（section_writer 函数实现）✅
- Task 10（主图组装）：依赖 Task 4、6、7、9 ✅
- Task 11（集成测试）：依赖 Task 10 完成 ⏳
