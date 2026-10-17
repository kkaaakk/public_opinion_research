# Tools 模块说明

## 本页速览

| 项目     | 内容                                                                                                                       |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| 阅读目标 | 理解 Public Opinion 业务 Agent 可调用工具如何注册、筛选、执行和失败兜底。                                    |
| 关键代码 | `src/open_deep_research/tools/registry.py`、`src/open_deep_research/tools/rag_tool.py`、`src/open_deep_research/runtime/business_agent.py`、`src/open_deep_research/runtime/agent_runtime.py` |
| 上游文档 | [Agent Loop 模块说明](agent-loop.md)                                                                                       |
| 下游文档 | [RAG 模块说明](rag.md)、[RAG 检索测试记录](../evaluation/rag-retrieval-test-records.md)                                    |

本文关注工具装配层和运行时行为；具体 RAG 检索实现请跳到 RAG 文档。

## 1. 模块定位

`tools` 模块负责把项目内能力包装成 LangChain/LangGraph 可调用工具，让 Public Opinion 业务 Agent 在工具调用循环中使用。

当前独立的 `src/open_deep_research/tools/` 目录包含本地 RAG 检索工具及共享装配逻辑：

- `src/open_deep_research/tools/rag_tool.py`
- `src/open_deep_research/tools/registry.py`

`src/open_deep_research/utils.py` 仅保留旧导入路径的兼容导出；新调用应使用具体实现模块。

因此实际工具体系可以分成两层：

- 工具实现层：具体工具函数，例如 `rag_search`、`tavily_search`、`think_tool`。
- 工具装配层：根据配置把 web search、RAG、MCP、控制工具组合成业务 Agent 可用工具列表。

## 2. 核心工具

### 2.1 `rag_search`

位置：

- `src/open_deep_research/tools/rag_tool.py`

定义：

```python
@tool(description=RAG_SEARCH_DESCRIPTION)
async def rag_search(query: str, config: RunnableConfig = None) -> str
```

职责：

- 读取运行时 `Configuration`。
- 检查 `rag_enabled` 与本地数据源配置。
- 把全局配置转换成 `RAGPipelineConfig`。
- 解析当前 `conversation_id` 与 `user_id`，用于 MySQL memory 范围过滤。
- 可选调用 query rewrite，把用户问题改写成更适合向量检索和 BM25 的查询。
- 调用 `RAGPipeline.query()`。
- 返回带 SOURCE、CHUNK ID、METADATA、EXCERPT 和 Sources 列表的纯文本上下文。

返回值是字符串，因为它会作为业务 Agent 的 `ToolMessage.content` 进入后续压缩和最终报告生成阶段。

失败时不会抛出到 agent loop，而是返回：

```text
Local RAG search failed: ...
```

这样可以避免单次本地检索异常中断整轮 deep research。

### 2.2 `tavily_search`

位置：

- `src/open_deep_research/search/tavily.py`

职责：

- 用 Tavily 执行多个 web query。
- 按 URL 去重。
- 对 raw content 调用 summarization model 生成摘要。
- 返回包含 SOURCE、URL、SUMMARY 的文本。

在 `search_api=tavily` 且 retrieval mode 允许 web search 时，装配为名为 `web_search` 的工具。

### 2.3 Native Web Search

位置：

- `src/open_deep_research/tools/registry.py`

入口：

- `get_search_tool(search_api)`

当 `search_api=openai` 时，返回 OpenAI native web search tool schema。

当 `search_api=anthropic` 时，返回 Anthropic native web search tool schema。

这类工具不是普通 LangChain `BaseTool`，而是传给模型的 provider-native tool 字典。

### 2.4 `think_tool`

位置：

- `src/open_deep_research/tools/registry.py`

职责：

- 让业务 Agent 在工具调用之间记录反思。
- 不做外部 IO。
- 返回 `Reflection recorded: ...`。

它是一个控制质量的工具，prompt 明确要求：

- 业务 Agent 在检索后使用。
- 不要和 search / RAG 工具并行调用。

### 2.5 `ResearchComplete`

位置：

- `src/open_deep_research/state.py`

用途：

- 作为控制工具绑定给四个业务 Agent。
- 业务 Agent 调用它表示自己的角色研究完成。

## 3. 工具装配流程

核心入口：

```python
async def get_all_tools(config: RunnableConfig)
```

位置：

- `src/open_deep_research/tools/registry.py`

装配顺序：

1. 加入核心控制工具：
   - `ResearchComplete`
   - `think_tool`
2. 调用 `get_retrieval_tools(config)` 加入检索工具。
3. 收集已有工具名，避免 MCP 工具重名。
4. 调用 `load_mcp_tools(config, existing_tool_names)` 加入 MCP 工具。
5. 返回最终工具列表。

## 4. Retrieval Mode 与工具选择

配置枚举：

```python
class RetrievalMode(Enum):
    WEB_ONLY = "web_only"
    RAG_ONLY = "rag_only"
    HYBRID = "hybrid"
```

工具选择规则位于：

- `get_retrieval_tools(config)`
- `rag_requested(configurable)`

规则：

| retrieval_mode | web_search          | rag_search                |
| -------------- | ------------------- | ------------------------- |
| `web_only`     | 取决于 `search_api` | 不启用                    |
| `rag_only`     | 不启用              | `rag_enabled=True` 时启用 |
| `hybrid`       | 取决于 `search_api` | `rag_enabled=True` 时启用 |

如果 `search_api=none`，则不加入 web search。

如果 `rag_enabled=False`，即使 `retrieval_mode=rag_only/hybrid` 也不会加入 `rag_search`。

## 5. MCP 工具接入

位置：

- `src/open_deep_research/mcp/tools.py`

核心函数：

- `load_mcp_tools(config, existing_tool_names)`
- `wrap_mcp_authenticate_tool(tool)`
- `fetch_tokens(config)`
- `get_mcp_access_token(...)`

兼容的单服务器配置对象（`mcp_config` 路径）：

```python
class MCPConfig(BaseModel):
    url: Optional[str]
    tools: Optional[List[str]]
    auth_required: Optional[bool]
```

加载条件：

- `mcp_config.url` 存在。
- `mcp_config.tools` 指定允许暴露的工具名。
- 如果 `auth_required=True`，必须能拿到 MCP token。
- MCP 工具名不能和已有工具名冲突。

认证逻辑：

- 通过 Supabase access token 换取 MCP access token。
- token 存入 LangGraph store。
- 过期后自动清理并重新获取。

错误处理：

- 对 MCP `interaction required` 错误做用户友好包装。
- 连接失败时返回空工具列表，不中断主流程。

## 6. 业务 Agent 中的工具执行

位置：

- `src/open_deep_research/runtime/business_agent.py`
- `src/open_deep_research/runtime/agent_runtime.py`

相关函数：

- `run_business_agent(...)`
- `_business_agent_tools(...)`
- `_execute_tool_safely(...)`
- `AgentRuntime.run(...)`

执行流程：

1. `run_business_agent` 通过 `_business_agent_tools` 装配并按角色领域白名单过滤工具。
2. 模型基于工具列表生成 tool calls。
3. Agent loop 从最近一条 AI 消息取出 tool calls。
4. 通过 Budget Guard 过滤不可执行的 tool calls。
5. 对允许执行的工具并行调用 `_execute_tool_safely`。
6. 把结果包装成 `ToolMessage`。
7. 根据迭代次数、`ResearchComplete`、预算状态决定继续当前角色循环或结束研究；图策略通过 `ResearchWorkspace.finalize` 收尾，标准策略通过 `_compress_research` 压缩研究结果。

工具执行异常会被捕获成文本：

```text
Error executing tool: ...
```

这样单个工具异常不会直接打断 graph。

## 7. Budget Guard 对工具的影响

位置：

- `src/open_deep_research/budget.py`
- `src/open_deep_research/runtime/agent_runtime.py`

工具相关预算：

- `max_tool_calls`
- `max_search_calls`
- `max_model_calls`
- `reserve_final_report_call`

影响：

- 超预算的工具调用不会执行。
- 系统会生成一个合成 `ToolMessage`，说明该工具被跳过。
- native web search 会通过 response metadata 计入 search call。
- `rag_search` 被标记为 search 类型，因此也会计入 search budget。

## 8. 工具提示词

当前入口是 [`build_dynamic_tool_prompt(tools, configurable)`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/tools/registry.py#L258)，根据本次实际绑定的工具列表按领域生成名称与简短说明。

业务角色调用链：

1. [`_business_agent_tools`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/runtime/business_agent.py#L194) 根据配置装配工具，再按角色的 `allowed_domains` 过滤，未知或未分类领域默认拒绝。
2. [`_role_tool_prompt`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/runtime/business_agent.py#L173) 把过滤后的同一工具列表传给 `build_dynamic_tool_prompt`，补充角色领域及实际工具名称白名单。
3. [`PublicOpinionAgentSpec.format_system_prompt`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/public_opinion_agents/base.py#L28) 将生成的 `retrieval_tool_prompt` 注入角色系统提示词。

动态说明始终要求检索后单独调用 `think_tool` 反思；只有实际列表包含 `rag_search` 时，才加入基于 SOURCE 引用作出本地知识或记忆声明的约束。允许 RAG 的角色若没有取得 `rag_search`，`_role_tool_prompt` 会说明内部资料或审批依据缺失时应明确标记，避免编造。

[`get_research_tool_prompt(configurable)`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/tools/registry.py#L213) 是已标记弃用的静态兼容 helper，仍经 `utils.py` 导出；当前四角色运行链使用上述动态入口。`retrieval_mode` 控制前置工具装配，提示词最终以角色过滤后的实际工具集合为准。

现有契约验证可参见 [角色工具隔离测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_public_opinion_tool_permissions.py#L53)、[角色 prompt 契约测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_public_opinion_agents.py#L59) 和 [无 RAG 图消费测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_graph_consumer_without_rag.py#L27)；后者验证缺失 RAG 提示及实际工具白名单进入模型上下文。

## 9. 扩展新工具

新增一个普通 research tool 的推荐路径：

1. 在合适模块中实现 async tool，并用 `@tool` 包装。
2. 设置清晰的 description。
3. 如果它属于检索工具，在 `get_retrieval_tools` 中按配置加入。
4. 如果它属于常驻控制工具，在 `get_all_tools` 初始列表中加入。
5. 如需计入 search budget，设置 metadata：

```python
tool.metadata = {
    **(tool.metadata or {}),
    "type": "search",
    "name": "your_tool_name",
}
```

1. 更新对应业务 Agent prompt 中的工具说明。
2. 增加工具级测试和 agent loop 集成测试。

## 10. 常见问题

### 业务 Agent 报没有外部研究工具

业务 Agent 会调用 `has_external_research_tool(tools)`。

生产者和标准策略角色如果只有 `ResearchComplete` 和 `think_tool`，会抛出包含以下信息的错误：

```text
Public Opinion agent '...' (...) requires tool domains: ... Missing tools ...
```

图消费策略的风险/回应角色可以通过 ResearchWorkspace 检索图证据，不要求额外外部检索工具；对应契约见上文的无 RAG 图消费测试。

解决方式：

- 配置 web search。
- 或启用 RAG 并设置 `retrieval_mode=rag_only/hybrid`。
- 或配置 MCP 工具。

### `rag_search` 不出现

检查：

- `rag_enabled=True`
- `retrieval_mode` 是 `rag_only` 或 `hybrid`
- `rag_knowledge_base_paths`、`rag_memory_paths` 或 `rag_memory_mysql_url` 至少有一个可用来源

### MCP 工具不出现

检查：

- `mcp_config.url`
- `mcp_config.tools`
- 工具名是否和已有工具冲突
- 如果 `auth_required=True`，是否有可交换的 Supabase token
