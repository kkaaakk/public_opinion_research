# 通用研究模式移除 — 完整变更记录

## 概述

本轮修改目标：**彻底移除项目中的"通用研究模式"，让"企业舆情研究模式"成为唯一可运行主流程。**

修改结果统计：

| 指标 | 数值 |
|------|------|
| 修改文件 | 12 个 |
| 新增文件 | 1 个 |
| 删除代码行 | ~1,338 行 |
| 新增代码行 | ~105 行（修改文件中） |
| 新增测试文件 | 214 行（32 个测试用例） |
| 净减少代码行 | ~1,233 行 |

---

## 一、修改文件清单

### 1. `src/open_deep_research/deep_researcher.py`（-842 行，最大改动）

**删除的函数/子图（仅供通用模式使用）：**

| 删除对象 | 类型 | 说明 |
|----------|------|------|
| `supervisor()` | async 函数 | 通用 Research Supervisor 节点，负责委派研究任务给 Researcher |
| `supervisor_tools()` | async 函数 | Supervisor 工具执行节点 |
| `supervisor_subgraph` | StateGraph | 通用 Supervisor 子图编译结果 |
| `researcher()` | async 函数 | 通用 Researcher 并发研究节点，执行自由研究循环 |
| `researcher_tools()` | async 函数 | Researcher 工具执行节点 |
| `researcher_builder` | StateGraph | 通用 Researcher 子图构建器 |
| `researcher_subgraph` | CompiledGraph | 通用 Researcher 子图编译结果 |
| `_public_opinion_mode()` | 辅助函数 | 判断当前是否为舆情模式的布尔函数 |
| `_get_filtered_researcher_tools()` | async 函数 | 根据领域过滤 Researcher 工具集 |

**简化的函数：**

| 函数 | 修改内容 |
|------|----------|
| `write_research_brief()` | 移除 domain_classifier_section、relevant_domains 字段、_public_opinion_mode 检查 |
| `plan_report_sections()` | 移除非舆情模式的 passthrough 分支 |

**重写的函数：**

- **`research_phase()`**：移除 `_public_opinion_mode` 检查和 `relevant_domains` 传递，始终调用 `public_opinion_subgraph`
- **`_fallback_report_generation()`**（原 `final_report_generation`）：移除舆情分支判断，始终使用 `public_opinion_final_report_generation_prompt`

**恢复的函数（误删修复）：**

- **`execute_tool_safely()`**：被舆情 Agent 循环直接调用的工具安全执行函数，在前一轮大段删除时被误删，本轮恢复

**清理的 imports：**

- 移除 `build_domain_classifier_prompt`、`get_filtered_tools`、`tag_builtin_tools`
- 移除 `final_report_generation_prompt`、`lead_researcher_prompt`、`research_system_prompt`
- 移除 `public_opinion_researcher_prompt`（未被主流程直接使用）
- 移除 `ConductResearch`、`SupervisorState`、`ResearcherState`、`ResearcherOutputState`
- 移除 `ResearchComplete`（仅作为字符串引用）
- 移除 `build_dynamic_tool_prompt`、`get_notes_from_tool_calls`、`think_tool`

---

### 2. `src/open_deep_research/state.py`（-63 行）

**删除的类：**

| 类名 | 说明 |
|------|------|
| `ConductResearch` | Pydantic BaseModel，通用 Supervisor 委派研究任务的工具 schema |
| `SupervisorState` | TypedDict，通用 Supervisor 子图状态 |
| `ResearcherState` | TypedDict，通用 Researcher 子图状态 |
| `ResearcherOutputState` | TypedDict，通用 Researcher 输出状态 |

**删除的字段：**

| 所在类 | 字段名 | 说明 |
|--------|--------|------|
| `BusinessAgentRole` | `"general_research"` | Literal 类型中的枚举值 |
| `ResearchQuestion` | `relevant_domains` | 领域标签字段（Pydantic Field） |
| `AgentState` | `relevant_domains` | 主状态中的领域标签 |
| `PublicOpinionState` | `relevant_domains` | 舆情状态中的领域标签 |

---

### 3. `src/open_deep_research/prompts.py`（-190 行）

**删除的提示词常量：**

| 常量名 | 行数 | 用途 |
|--------|------|------|
| `lead_researcher_prompt` | ~58 行 | 通用 Supervisor 系统提示词 |
| `research_system_prompt` | ~46 行 | 通用 Researcher 系统提示词 |
| `final_report_generation_prompt` | ~82 行 | 通用最终报告生成提示词 |

**修改的提示词：**

- `transform_messages_into_research_topic_prompt`：移除 `{domain_classifier_section}` 占位符和领域分类相关指令

**保留的提示词：**

- `public_opinion_final_report_generation_prompt` — 舆情最终报告
- `compress_research_system_prompt` / `compress_research_simple_human_message` — 研究压缩
- `report_planner_instructions` / `section_writer_*` / `final_section_writer_instructions` — 报告撰写
- `clarify_with_user_instructions` — 用户澄清

---

### 4. `src/open_deep_research/configuration.py`（-25 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| `business_scenario` 默认值 | `"general_research"` | `"public_opinion_risk"` |
| `business_scenario` UI 选项 | `[general_research, public_opinion_risk]` | 仅 `public_opinion_risk` |
| `validate_rag_chunk_settings` | 校验 `general_research` 和 `public_opinion_risk` | 仅校验 `public_opinion_risk` |
| `allowed_business_agents` 验证 | 引用 `general_research` | 移除相关引用 |

---

### 5. `src/open_deep_research/mcp/domain_filter.py`（-98 行）

**删除的函数：**

| 函数名 | 说明 |
|--------|------|
| `build_domain_classifier_prompt()` | 生成领域分类提示词（供通用 Researcher 判断领域） |
| `tag_builtin_tools()` | 为内置工具打领域标签 |
| `detect_active_domains()` | 根据研究主题检测活跃领域 |
| `filter_tools_by_domain()` | 按领域过滤工具列表 |
| `get_filtered_tools()` | 获取过滤后的工具列表 |

**保留的函数/对象：**

- `DomainDef` — 领域定义数据类
- `DOMAIN_REGISTRY` — 领域注册表（MCP 工具管理的基础）
- `get_domain()` / `get_domain_description()` / `get_domain_label()` — 领域查询
- `iter_domain_labels()` — 领域标签迭代
- `classify_tools()` — 工具分类（被 `build_dynamic_tool_prompt` 调用）
- `tool_domain_summary()` — 工具领域摘要
- `_tool_domain()` — 内部工具领域检测

---

### 6. `src/open_deep_research/mcp/__init__.py`（-10 行）

从 `__all__` 和 import 中移除已删除的 5 个函数：
`build_domain_classifier_prompt`、`detect_active_domains`、`filter_tools_by_domain`、`get_filtered_tools`、`tag_builtin_tools`

---

### 7. `src/open_deep_research/web/server.py`（-29 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| `ResearchRequest.scenario` | 存在，接收前端场景选择 | 移除 |
| `business_scenario` 配置 | `"business_scenario": request.scenario` | 硬编码 `"public_opinion_risk"` |
| `_NODE_LABEL` 映射 | 包含通用模式节点标签 | 移除通用模式节点标签 |
| import 修复 | I001 排序问题 + E402 | 添加 noqa 注释修复 |

---

### 8. `src/open_deep_research/web/static/index.html`（-31 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| Scenario 选择器 | `<select id="scenario">` 下拉框 | 删除 |
| PO 额外面板 | 条件显示（依赖 scenario 选择） | 始终显示 |
| 页面标题/文案 | 通用研究/舆情研究双语 | 统一为"企业舆情研究" |
| 提示文字 | 通用提示 | "请输入需要分析的品牌、企业、产品或舆情事件" |

---

### 9. `src/open_deep_research/web/static/app.js`（-10 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| `scenarioSelect` 变量 | 存在 | 移除 |
| `toggleScenario()` 函数 | 控制 PO 面板显示 | 移除 |
| 请求体 `scenario` 字段 | 发送 scenario 值 | 不再发送 |
| `NODE_BADGE` 映射 | 包含 `supervisor`/`researcher` | 移除通用模式节点 |

---

### 10. `locustfile.py`（-104 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| `Scenario` 枚举 | `GENERAL = "general_research"` + `PUBLIC_OPINION` | 仅 `PUBLIC_OPINION` |
| `GENERAL_TOPICS` 列表 | 8 条通用研究题目 | 删除 |
| `_build_profile()` | 包含 general_research 分支 | 移除 |
| 文件描述 | "both research scenarios" | "public_opinion_risk scenario" |

---

### 11. `tests/agent_stress.py`（-13 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| 文档示例命令 | `--scenario general_research` | `--scenario public_opinion_risk` |
| `GENERAL_TOPICS` | 通用研究测试题目 | 删除 |
| 场景选择逻辑 | `general_research` 分支 | 移除 |

---

### 12. `README.md`（-28 行修改）

| 修改项 | 修改前 | 修改后 |
|--------|--------|--------|
| 项目定位 | "深度研究 Agent，支持通用研究和舆情两种模式" | "基于 LangGraph、RAG、MCP 和多 Agent 协作的企业舆情风险研究与处置辅助系统" |
| 模式描述 | 详细的双模式说明和切换方式 | 移除，统一为舆情模式 |
| 测试命令 | `python tests/test_xxx.py` | `pytest` |
| 示例请求 | 包含通用研究示例 | 仅保留舆情示例 |

---

## 二、新增文件

### `tests/test_general_mode_removal.py`（214 行，32 个测试）

| 测试类 | 测试数 | 验证内容 |
|--------|--------|----------|
| `TestUniqueMode` | 6 | 默认 business_scenario 为 public_opinion_risk；拒绝 general/generic/deep_research；BusinessAgentRole 不含 general_research |
| `TestGraphStructure` | 4 | 主图无 supervisor/researcher 节点；无 mode router；舆情节点完整存在 |
| `TestConfigurationNoMode` | 3 | 创建配置无需 mode 字段；from_runnable_config 无需 mode；tool_domain_filtering_enabled 已移除 |
| `TestStateCleanup` | 7 | ConductResearch/SupervisorState/ResearcherState/ResearcherOutputState 不存在；各 State 无 relevant_domains |
| `TestPromptsCleanup` | 5 | lead_researcher_prompt/research_system_prompt/final_report_generation_prompt 不存在；PO 提示词保留；transform_prompt 无 domain_classifier |
| `TestDomainFilterCleanup` | 7 | 5 个通用过滤函数不存在；DOMAIN_REGISTRY/classify_tools 保留 |

---

## 三、当前唯一主流程

```
用户输入
  │
  ▼
enrich_query_images        ← 图像上下文增强（可选，依赖 RAG 多模态配置）
  │
  ▼
clarify_with_user          ← 用户澄清（可选，依赖 allow_clarification）
  │
  ▼
write_research_brief       ← 生成研究简报
  │
  ▼
plan_report_sections       ← 章节规划（Plan-and-Execute 模式）
  │
  ▼
research_phase             ← 执行 public_opinion_subgraph：
  │                          ┌─ public_signal      （舆情信号采集 Agent）
  │                          ├─ internal_knowledge  （内部知识检索 Agent）
  │                          ├─ risk_assessment     （风险评估 Agent）
  │                          └─ response_strategy   （处置建议 Agent）
  ▼
write_final_sections       ← 非研究型章节撰写
  │
  ▼
compile_final_report       ← 最终报告编译
  │
  ▼
输出
```

---

## 四、保留的通用组件（舆情流程仍在使用）

| 组件 | 保留原因 |
|------|----------|
| `compress_research` 函数 | PO agents 通过 `_run_public_opinion_agent` 直接调用 |
| `budget` 系统（全套 16 个函数） | PO 流程的 token/调用次数预算守卫 |
| `ResearchComplete` 工具 | PO agents 的研究完成信号 |
| `think_tool` | PO agents 工具白名单中的反思工具 |
| `section_writer` / `write_final_sections` | 公共章节撰写节点 |
| `compile_final_report` | 公共最终报告编译节点 |
| `build_dynamic_tool_prompt` | 被 `_role_tool_prompt` 间接调用 |
| `DOMAIN_REGISTRY` / `classify_tools` | MCP 工具领域管理基础设施 |
| RAG / GraphRAG 全套 | PO 流程的本地知识检索 |
| MCP 全套 | PO 流程的外部工具集成 |
| 社交媒体工具 | PO 流程的舆情信号采集 |
| `write_research_brief` / `plan_report_sections` | 公共规划节点 |
| `enrich_query_images` | 公共图像增强节点 |
| `clarify_with_user` | 公共用户澄清节点 |

---

## 五、测试执行结果

```
tests/test_general_mode_removal.py   32 passed  ✅
tests/test_public_opinion_agents.py   4 passed  ✅
tests/test_budget.py                  8 passed  ✅
──────────────────────────────────────────────
Total:                               44 passed, 0 failed
```

---

## 六、ruff check 结果

| 错误级别 | 修改前 | 修改后 | 说明 |
|----------|--------|--------|------|
| F401（未使用 import） | 7 个 | 0 个 | 全部清理 |
| F821（未定义名称） | 1 个 | 0 个 | 恢复 `execute_tool_safely` |
| I001（import 排序） | 3 个 | 0 个 | 自动修复 + noqa |
| E501（行过长） | ~300 个 | ~300 个 | pre-existing，未修改 |
| Pydantic 弃用警告 | ~50 个 | ~50 个 | pre-existing，未修改 |

---

## 七、全仓搜索残留关键词

| 关键词 | 出现位置 | 保留原因 |
|--------|----------|----------|
| `research_model` | configuration.py, deep_researcher.py, server.py, tests | 配置字段名（指定 LLM 模型如 "openai:gpt-4.1"），与"研究模式"无关 |
| `research_model_max_tokens` | 同上 | 研究模型最大 token 数配置 |
| `general_research` | 仅 test_general_mode_removal.py | 测试断言：验证该值被系统拒绝 |
| `generic_research` | 仅 test_general_mode_removal.py | 测试断言：验证该值被系统拒绝 |
| `relevant_domains` | 仅 test_general_mode_removal.py | 测试断言：验证字段已被移除 |
| `Open Deep Research` | README.md | 上游项目归属声明（langchain-ai/open_deep_research） |
| `mode`（web UI） | index.html, app.js, server.py | 研究深度选择器（Fast/Normal/Deep），非业务模式切换 |

---

## 八、下一轮建议重构项

以下结构在本轮中被识别但不属于"模式收敛"范围，建议在后续轮次处理：

1. **`business_scenario` 配置字段** — 现在只接受 `"public_opinion_risk"` 一个值，可考虑移除
2. **`research_phase` 节点及状态转换** — 已将实际运行 PO 子图的节点统一命名为 `research_phase`，并清理无消费者的协调者状态与配置
3. **State 结构** — AgentState 保留大量通用字段，可按舆情业务语义重新设计（MonitoringTask、EventCluster 等）
4. **web UI `mode` 选择器** — Fast/Normal/Deep 是研究深度，可重命名为"研究深度"避免歧义
5. **`langchain_mcp_adapters` 依赖** — 当前环境存在版本不兼容（`ElicitationFnT` 导入失败）
