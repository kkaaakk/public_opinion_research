# Checklist

## State 数据结构
- [ ] `Section` BaseModel 定义在 `state.py`，包含 name, description, research, content, agent_role, status 字段
- [ ] `Sections` BaseModel 定义在 `state.py`，包含 sections: list[Section]
- [ ] `AgentState` 扩展了 sections, completed_sections, feedback_on_report_plan 字段
- [ ] `completed_sections` 使用 operator.add reducer 实现并行结果累加
- [ ] `feedback_on_report_plan` 使用 operator.add reducer 实现反馈累积
- [ ] 所有新字段有默认值，不破坏现有状态迁移

## 配置项
- [ ] `allow_plan_feedback: bool = False` 配置项存在且有 x_oap_ui_config metadata
- [ ] `report_structure: str` 配置项存在，默认值为 DEFAULT_REPORT_STRUCTURE
- [ ] `planner_model: str = ""` 配置项存在，空值回退到 research_model
- [ ] `planner_model_max_tokens: Optional[int] = None` 配置项存在
- [ ] `section_writer_model: str = ""` 配置项存在，空值回退到 final_report_model
- [ ] `section_writer_model_max_tokens: Optional[int] = None` 配置项存在

## Prompt 模板
- [ ] `report_planner_instructions` 定义在 prompts.py，包含舆情 agent_role 声明要求
- [ ] `section_writer_from_role_reports_prompt` 定义在 prompts.py，包含 section_name, section_description, evidence 占位符
- [ ] `final_section_writer_instructions` 定义在 prompts.py，包含 section_name, section_description, context 占位符
- [ ] `public_opinion_final_report_generation_prompt` 定义在 prompts.py（compile 兜底用）
- [ ] 所有 prompt 占位符与代码调用一致

## plan_report_sections 节点
- [ ] 函数签名 `plan_report_sections(state: AgentState, config: RunnableConfig)` 正确
- [ ] 主图统一进入 `research_phase`，不再按业务模式 passthrough
- [ ] 舆情模式使用 planner_model（配置回退到 research_model）+ with_structured_output(Sections)
- [ ] Budget Guard 集成：预算不足时退化为单 section（agent_role 包含全部 4 角色）
- [ ] section 数量限制：不超过 available_research_unit_slots 返回值（None 时上限 5）
- [ ] 预算预留时序保护：扣除 section_writer 预留的 slots（等于 research=True 的 section 数量），扣除后 <=0 则退化为单 section
- [ ] 舆情模式 prompt 增强：要求每个 section 声明 agent_role
- [ ] allow_plan_feedback=True 时调用 `interrupt({"sections": [s.dict() for s in sections]})` 暂停
- [ ] 单元测试覆盖：正常规划、预算退化、section 截断、预算预留时序保护、人工批准和人工打回

## human_feedback_on_plan 节点
- [ ] 函数签名 `human_feedback_on_plan(state, config)` 正确
- [ ] 接收 Command(resume=<value>) 传入的 resume 值
- [ ] resume 值为 True（bool）→ Command(goto="research_phase")
- [ ] resume 值为字符串 → Command(goto="plan_report_sections", update={"feedback_on_report_plan": [feedback]})
- [ ] resume 值为其他类型 → 抛出 TypeError
- [ ] 单元测试覆盖：True 批准、字符串打回、非法类型抛 TypeError

## section_writer 节点
- [ ] 函数签名 `section_writer(state, config)` 正确
- [ ] 按 section.agent_role（逗号分隔）过滤 role_reports 提取证据，对每个角色名 strip() 去空格
- [ ] 角色名有效性校验：必须是 4 个预设角色之一，无效则 LOGGER.warning 并忽略
- [ ] agent_role 为空、所有角色无效、或所有有效角色都无报告时使用全部 role_reports 兜底
- [ ] 并行写作使用 asyncio.gather(*tasks, return_exceptions=True)
- [ ] 异常处理：单个 section 写作失败时 content 留空、status 保持 pending、不回填 completed_sections、LOGGER.warning 记录异常
- [ ] 使用 section_writer_model（配置回退到 final_report_model）
- [ ] Budget Guard 集成：预算不足时复用 role_reports 原文降级
- [ ] section content 回填到 completed_sections，status 标记 done
- [ ] 单元测试覆盖：正常流程、预算不足降级、agent_role 部分匹配、agent_role 为空、agent_role 含无效角色名、单个 section 写作异常不阻塞其他 section

## write_final_sections 节点
- [ ] 函数签名 `write_final_sections(state: AgentState, config: RunnableConfig)` 正确
- [ ] 过滤 research=False 的 section 并行写作
- [ ] 使用已完成 section（research=True 且 status=done）的 content 作为上下文
- [ ] 使用 final_report_model 写作
- [ ] 并行写作使用 asyncio.gather(*tasks, return_exceptions=True)
- [ ] 异常处理：单个 section 写作失败时 content 留空、LOGGER.warning 记录异常、交给 compile_final_report 走缺失补齐
- [ ] Budget Guard 集成
- [ ] 无非研究 section 时返回空 dict
- [ ] 单元测试覆盖：有非研究 section、无非研究 section、预算不足、单个 section 写作异常

## compile_final_report 节点
- [ ] 函数签名 `compile_final_report(state: AgentState, config: RunnableConfig)` 正确
- [ ] 以 state.sections（原始规划顺序）为基准遍历，通过 section.name 映射 completed_sections 中的 content
- [ ] 去重逻辑：如果 completed_sections 中存在多个同名 section，取最后一个的 content（最新结果）
- [ ] 缺失 section + 预算充足 → 用 final_report_model 补齐
- [ ] 缺失 section + 预算不足 → 追加 Note 说明未完成 section
- [ ] 全量退化（所有 section 缺失）→ 从 notes 合成（使用 public_opinion_final_report_generation_prompt）
- [ ] 保留 reserve_final_report_call 预算预留机制
- [ ] 保留 maybe_persist_chat_memory 调用
- [ ] 单元测试覆盖：全部完成、部分缺失+预算充足、部分缺失+预算不足、全量退化、同名 section 去重取最新

## research_phase 路由改造
- [ ] 舆情模式下调用 public_opinion_subgraph 后调用 section_writer
- [ ] section_writer 结果（completed_sections）合并到返回的 state update
- [ ] `research_phase` 调用 `public_opinion_subgraph` 并完成主图/子图状态转换
- [ ] 测试覆盖：`research_phase` 调用子图并正确回写 role reports、memories、notes 和 budget

## 主图组装
- [ ] `plan_report_sections` 节点添加到 deep_researcher_builder
- [ ] `human_feedback_on_plan` 节点添加
- [ ] `write_final_sections` 节点添加
- [ ] `compile_final_report` 节点添加
- [ ] 边：write_research_brief → plan_report_sections
- [ ] 边：plan_report_sections → human_feedback_on_plan（条件：allow_plan_feedback）
- [ ] 边：plan_report_sections → research_phase（无反馈或批准）
- [ ] `plan_report_sections` 打回时回到自身重新规划
- [ ] 边：research_phase → section_writer → write_final_sections
- [ ] 边：write_final_sections → compile_final_report
- [ ] 边：compile_final_report → END
- [ ] `langgraph.json` 入口仍为 deep_researcher，无改动
- [ ] Public Opinion 固定流程不受影响

## 向后兼容
- [ ] plan_report_sections 预算不足 → 单 section（agent_role 包含全部 4 角色）
- [ ] section_writer 预算不足 → 复用 role_reports 原文作为 section content
- [ ] write_final_sections 无非研究 section → 空返回
- [ ] compile_final_report 所有 section 缺失 → 从 notes 合成（使用 public_opinion_final_report_generation_prompt）
- [ ] 全量退化等价于当前舆情模式行为（端到端测试验证）

## Public Opinion 固定流程
- [ ] 不引入独立 Supervisor / Researcher 子图
- [ ] 固定流程：write_research_brief → plan_report_sections → research_phase → section_writer → compile_final_report
- [ ] 四个业务 Agent 的依赖拓扑保持不变

## 集成验证
- [ ] 舆情模式端到端测试通过
- [ ] 预算不足退化测试通过
- [ ] 人工反馈测试通过（含 interrupt/resume）
- [ ] Public Opinion 主流程回归测试通过
- [ ] `ruff check` 无 lint 错误
- [ ] `mypy` 类型检查通过
- [ ] 现有测试套件无回归
