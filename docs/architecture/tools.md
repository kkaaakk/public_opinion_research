# Tools 模块说明

## 本页速览

| 项目     | 内容                                                                                                                       |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| 阅读目标 | 理解 Public Opinion 业务 Agent 可调用工具如何注册、筛选、执行和失败兜底。                                    |
| 关键代码 | `src/open_deep_research/tools/registry.py`、`src/open_deep_research/tools/rag_tool.py`、`src/open_deep_research/runtime/business_agent.py`、`src/open_deep_research/runtime/tool_executor.py` |
| 上游文档 | [Agent Loop 模块说明](agent-loop.md)                                                                                       |
| 下游文档 | [RAG 模块说明](rag.md)、[RAG 检索测试记录](../evaluation/rag-retrieval-test-records.md)                                    |

本文关注工具装配层和运行时行为；具体 RAG 检索实现请跳到 RAG 文档。

## 1. 模块定位

`tools/registry.py` 装配 SDK FunctionTool 声明和已配置的 MCP 工具。四个业务角色通过 `_business_agent_tools` 按允许的领域筛选。普通 Python 工具用官方 `@function_tool` 声明：名称、描述、JSON Schema、参数校验和默认值都由 SDK 生成，工具体只把通过校验的参数交给 `ToolExecutor`。MCP 工具由官方 `MCPServerStdio` / `MCPServerStreamableHttp` 与 `MCPUtil.to_function_tool()` 生成同等声明。`tools/__init__.py:register_tool` 只附加 domain、effect 与取消排空策略，不重建声明。

`utils.py` 和 RAG 公共入口继续提供已有的 Tavily、RAG LangChain 导出，供直接调用这些工具的客户端使用。

## 2. 核心工具

### 2.1 `rag_search`

位置：

- `src/open_deep_research/tools/rag_tool.py`

SDK 工具只公开 `query: str`。`rag_search_tool` 从 `ToolContext` 读取配置并调用共享检索函数。

职责：

- 读取运行时 `Configuration`。
- 检查 `rag_enabled` 与本地数据源配置。
- 把全局配置转换成 `RAGPipelineConfig`。
- 解析当前 `conversation_id` 与 `user_id`，用于 MySQL memory 范围过滤。
- 可选调用 query rewrite，把用户问题改写成更适合向量检索和 BM25 的查询。
- 调用 `RAGPipeline.query()`。
- 返回带 SOURCE、CHUNK ID、METADATA、EXCERPT 和 Sources 列表的纯文本上下文。

返回的带引用文本由 SDK 配对为工具输出。Harness 收集证据，并为报告服务生成消息投影。

失败时不会抛出到 agent loop，而是返回：

```text
Local RAG search failed: ...
```

执行入口将该返回值记录为普通工具失败，SDK 可以继续推理。

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

这两个 provider-native 声明保留给兼容调用。业务 SDK Runtime 只接收本地可观测工具，遇到这些声明会拒绝运行。

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

- `src/open_deep_research/tools/registry.py`

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
- `_load_server_tools(spec, existing_tool_names)`
- `fetch_tokens(config)`
- `get_mcp_access_token(...)`

传输使用 Agents SDK 0.24.0 官方客户端 `MCPServerStdio` / `MCPServerStreamableHttp`。`MCPServerSpec` 保存可信本地策略（`domain`、`effect`）和连接参数；发现阶段用短生命周期连接执行 `list_tools()`，再由 `MCPUtil.to_function_tool()` 生成原生 `FunctionTool` 声明。`mcp/tool_wrapper.py` 只替换 `on_invoke_tool`，让每次调用重新建立连接并进入现有 `ToolExecutor.execute()`。服务方 `annotations` 不会改变领域或 Effect。

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
- MCP 工具与已有名称冲突时使用服务名前缀。

认证逻辑：

- 通过 Supabase access token 换取 MCP access token。
- 当前配置需要鉴权时在发现阶段交换令牌。

错误处理：

- 对 MCP `interaction required` 错误做用户友好包装。
- 连接失败时返回空工具列表，不中断主流程。

业务路径使用 SDK 原生 MCP 声明取得服务方 Schema、协议状态和结构化结果。`mcp/tool_wrapper.py` 将文本、`structuredContent`、`isError`、超时和断连映射到现有执行契约；非文本多模态结果会被明确拒绝。SDK 的 `Agent.mcp_servers` 会由 Runner 直接发现并调用 MCP 工具，跳过当前的工具预算、`por.agent_tool` 和 operations 回执接缝，因此业务 Agent 不直接使用该入口。 [Agents SDK 0.24.0 MCP 实现](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/mcp/util.py)

## 6. 业务 Agent 中的工具执行

1. `por.role_tools` 保存模型可见目录及 effect。恢复时比较名称、描述、Schema、领域和 effect。已有 `dbos-events-v1` 的目录可缺少 effect；此时当前工具声明（包括插件 Apify MCP 的 `protected`）保持不变。
2. `SDKAgentRuntime._register_tool` 把 `BusinessTool.sdk_tool` 交给 `ToolExecutor.bind`，只替换 `on_invoke_tool`；名称、描述、Schema 和严格模式保持 SDK 声明原样。MCP 工具与本地工具一样以 `BusinessTool` 装配，其声明来自 `MCPUtil.to_function_tool()`。DBOSRunner 在 Workflow 内调用 SDK，`ToolExecutor` 直接调度 `por.agent_tool` 并保持其结果格式。
3. `ToolExecutor` 在等待前检查权限和预占预算。普通 Python 工具由 SDK 验参和应用默认值。MCP 工具按服务方 Schema 验参。预算预占之后才进入业务函数及外部效果边界。
4. 工具 effect 为 `read`、`idempotent` 或 `protected`。未知 effect 使用 `protected`。受保护工具以 Workflow ID、工具名和原生 Tool Call ID 生成操作键，先登记 `operations`，后提交回执；业务函数在登记之后才读取带 `operation_id` 的调用配置。已有操作缺少对应的 Step 结果时，恢复会停止并要求核对。
5. `ToolExecutor` 以 Tool Call ID 共享在途结果，并在取消时等待已启动的同步工具及其回执。已提交的 Step 恢复原输出、状态、工具身份、操作 ID 和局部用量；Harness 只接收恢复后的记录。成功结果由 Harness 记录并保留在 SDK 会话历史中。 [执行架构](agent-loop.md) · [DBOS Step](https://docs.dbos.dev/python/tutorials/step-tutorial)

普通工具失败形成与调用配对的错误输出，供模型继续处理。取消和执行控制故障沿原生执行链传播。

回滚到旧 Executor 前，由支持 effect 的版本处理完已经写入带 effect 目录的活跃 Run。旧 Executor 的目录比较要求原有字段集合。

## 7. Budget Guard 对工具的影响

`ToolExecutor` 和 `budget.py` 共用 `max_tool_calls` 和 `max_search_calls`。已接纳的并发工具在首次等待前占用额度，超限调用会得到配对错误。重复调用和已提交的 Step 各自复用原结果，已记账增量随恢复回执在当前调用中重建一次。 `web_search` 与 `rag_search` 计入检索配额。推理模型和报告预留沿用各自的预算规则。

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

1. 在对应业务模块实现工具函数。第一个参数声明为 SDK 的 `ToolContext`，其余参数用 Python 类型和默认值生成模型可见 Schema。
2. 用官方 `@function_tool(...)` 装饰器声明工具，再用 `register_tool(..., domain=..., effect=...)` 附加业务元数据。工具体调用 `ToolExecutor.execute` 执行实际业务函数；同步业务函数由 Executor 放入线程，并在 `drain_on_cancel=True` 时等待线程与回执完成。
3. 将声明加入 Registry 对应的装配分支。角色领域白名单决定可见范围。
