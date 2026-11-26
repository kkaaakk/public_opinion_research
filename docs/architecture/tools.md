# Tools 模块说明

## 本页速览

| 项目     | 内容                                                                                                                       |
| -------- | -------------------------------------------------------------------------------------------------------------------------- |
| 阅读目标 | 理解 Public Opinion 业务 Agent 可调用工具如何注册、筛选、执行和失败兜底。                                    |
| 关键代码 | `src/open_deep_research/tools/__init__.py`、`src/open_deep_research/tools/manager.py`、`src/open_deep_research/tools/registry.py`、`src/open_deep_research/tools/rag_tool.py`、`src/open_deep_research/runtime/tool_executor.py` |
| 上游文档 | [Agent Loop 模块说明](agent-loop.md)                                                                                       |
| 下游文档 | [RAG 模块说明](rag.md)、[RAG 检索测试记录](../evaluation/rag-retrieval-test-records.md)                                    |

本文关注工具装配层和运行时行为；具体 RAG 检索实现请跳到 RAG 文档。

## 1. 模块定位

`tools/registry.py` 装配 SDK FunctionTool 声明和已配置的 MCP 工具。`tools/manager.py:ToolManager` 是四个业务角色唯一的装配入口：`discover()` 收集本地工具、全局 MCP 与插件工具，证明它们是 `BusinessTool` 声明、名称唯一且领域获准，再按角色领域筛选；`bind()` 把筛选结果交给该 Run 的 `ToolExecutor`。`runtime/business_agent.py:_business_agent_tools` 只是 `ToolManager.discover()` 的入口，`por.role_tools` 仍在 Executor 创建前持久化并校验目录。`SDKAgentRuntime` 不再做第二次类型/重名/领域校验，只把已准入列表转成执行器需要的按名索引。

普通 Python 工具用 `tools/__init__.py:business_tool` 声明：它调用官方 `@function_tool` 生成名称、描述、JSON Schema、参数校验和默认值，并自动把业务函数交给 `ToolExecutor.execute`，业务函数不再显式编排执行器。`register_tool` 仍可为已有声明只附加 domain、effect 与取消排空策略。MCP 工具由官方 `MCPServerStdio` / `MCPServerStreamableHttp` 与 `MCPUtil.to_function_tool()` 生成同等声明，并进入同一执行层。

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

这两个 provider-native 声明保留给兼容调用。业务 SDK Runtime 只接收本地可观测工具：这类声明没有本地领域、Effect 与持久调用，`ToolManager.discover()` 会在装配阶段以 `TypeError` 拒绝，而不是在 Runtime 里才失败。

### 2.4 Agent Loop 结束

业务 Agent 没有专用的反思工具或完成信号工具。模型自主决定是否继续调用检索、RAG、Skill 或 MCP 工具；当证据足够时，模型输出普通最终回复，SDK Runner 按默认 `run_llm_again` 行为结束循环。随后现有 `_finish()` 仍会整理角色研究报告并返回 `AgentRuntimeResult`。

`load_skill` 只返回研究方法，不计为外部检索能力；`has_external_research_tool()` 仅把它视为指导工具。

## 3. 工具装配流程

核心入口：

```python
async def get_all_tools(config: RunnableConfig)
```

位置：

- `src/open_deep_research/tools/registry.py`

装配顺序：

1. 调用 `get_retrieval_tools(config)` 加入检索工具。
2. 收集已有工具名，避免 MCP 工具重名。
3. 调用 `load_mcp_tools(config, existing_tool_names)` 加入 MCP 工具。
4. 返回最终工具列表。

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

1. `por.role_tools` 保存模型可见目录及 effect。恢复时比较名称、描述、Schema、领域和 effect，目录必须与当前声明一致。同一 Application Version 才能恢复 journal。 [版本切换](dbos-deployment.md#应用版本切换)
2. `ToolManager.bind()` 把 `BusinessTool.sdk_tool` 交给 `ToolExecutor.bind`，只替换 `on_invoke_tool`；名称、描述、Schema 和严格模式保持 SDK 声明原样。MCP 工具与本地工具一样以 `BusinessTool` 装配，其声明来自 `MCPUtil.to_function_tool()`。DBOSRunner 在 Workflow 内调用 SDK，`ToolExecutor` 直接调度 `por.agent_tool` 并保持其结果格式。
3. `ToolExecutor` 在等待前检查权限和预占预算。普通 Python 工具由 SDK 验参和应用默认值。MCP 工具按服务方 Schema 验参。预算预占之后才进入业务函数及外部效果边界。
4. 工具 effect 为 `read`、`idempotent` 或 `protected`。未知 effect 使用 `protected`。受保护工具以 Workflow ID、工具名和原生 Tool Call ID 生成操作键，先登记 `operations`，后提交回执；业务函数在登记之后才读取带 `operation_id` 的调用配置。`idempotent` 工具不写回执，调用配置携带同一个派生值作为 `idempotency_key`。
5. `ToolExecutor` 以 Tool Call ID 共享在途结果，并在取消时等待已启动的同步工具及其回执。已提交的 Step 恢复原输出、状态、工具身份、操作 ID 和局部用量；Harness 只接收恢复后的记录。成功结果由 Harness 记录并保留在 SDK 会话历史中。 [执行架构](agent-loop.md) · [DBOS Step](https://docs.dbos.dev/python/tutorials/step-tutorial)

### 观察记录的终态

Harness 只有两类记录会停留在 `running`：执行器已进入、工具体仍在执行中的调用；以及 SDK 钩子已观察但从未交给 `ToolExecutor` 的调用。第六轮补齐了第二类的终态规则：

- `ToolExecutor._perform` 进入即标记「该调用可能执行工具体」，此后它的记录只能由 `_perform` 自己收敛，结算不再代写。
- `por.agent_tool` Step 在进入工具体之前被取消或失败时，执行器立即把该记录落为 `cancelled` 并携带真实错误类型：工具体没跑，因此没有结果、没有额度、没有外部效果。
- `settle()` 排空后，把仍未进入执行器的 `running` 记录统一收敛为 `cancelled`（`error_type="NotInvoked"`）。这类记录没有工具体、额度或外部效果，重放时重新派生。退出结算是进程内的普通异步处理，不是 DBOS Step。
- 结算开始后 SDK 迟到的 `on_tool_start` 不再制造新的 `running` 记录：结算边界之后被观察的调用只能天生终态。

因此 `BatchSettlement.succeeded` 的含义是「每一个已观察到的工具调用都已到达终态」，而不是「全部成功」；被取消的兄弟工具计入终态、不计入成功。已进入执行器但未在排空超时内结束的调用仍保持 `running`，结算判为未解决，Run 按 `ExecutionControlError` 停止而不是冒充成功。

### 未提交 Step 的恢复

`por.agent_tool` 已提交时直接重放结果，不执行业务函数。Step 未提交时按 effect 处理：

- `read` / `idempotent`：重新执行。`tool_calls` 只有一行，额度不重复扣；`ToolExecutor` 追加一条 `budget_warnings`，说明供应商可能收到两次请求。
- `protected`：已有操作记录时，只有证实尚未执行才允许启动原调用。Apify `call-actor` 保留服务端的 Actor Run ID，通过 `get-actor-run` 查询原任务，再用 `get-dataset-items` 读取原 Dataset。无法验证原操作时返回具体原因并保留回执。 [Apify MCP](https://github.com/apify/apify-mcp-server)

外部请求已发出、`por.agent_tool` 尚未提交时崩溃，是唯一无法用现有表消除的重复调用窗口；本轮不新增结果缓存表或分布式事务。完整分析见 [工具执行层与恢复审计](../audits/tool-execution-recovery.md)。

普通工具失败形成与调用配对的错误输出，供模型继续处理。取消和执行控制故障沿原生执行链传播。

带 `effect` 的目录从 `dbos-events-v1` 起写入；旧 Executor 的在途 Run 由其原始发行版排空，当前目录围栏不再为更早的目录格式保留分支。

## 7. Budget Guard 对工具的影响

`ToolExecutor` 和 `budget.py` 共用 `max_tool_calls` 和 `max_search_calls`。正式 Run 中每一次准入的工具调用都会写入 `por_domain.tool_calls`，预算开关只决定是否检查上限，不决定是否记账。已接纳的并发工具在首次等待前占用额度，超限调用会得到配对错误。重复调用和已提交的 Step 各自复用原结果，已记账增量随恢复回执在当前调用中重建一次。 `web_search` 与 `rag_search` 计入检索配额。受保护工具另外写入 `por_domain.operations`，并通过 `tool_call_id` 外键归属到发起它的工具调用。推理模型和报告预留沿用各自的预算规则。

## 8. 工具提示词

当前入口是 [`build_dynamic_tool_prompt(tools, configurable)`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/tools/registry.py#L258)，根据本次实际绑定的工具列表按领域生成名称与简短说明。

业务角色调用链：

1. [`_business_agent_tools`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/runtime/business_agent.py#L194) 委托 `ToolManager.discover()` 根据配置装配工具，再按角色的 `allowed_domains` 过滤，未知或未分类领域默认拒绝。
2. [`_role_tool_prompt`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/runtime/business_agent.py#L173) 把过滤后的同一工具列表传给 `build_dynamic_tool_prompt`，补充角色领域及实际工具名称白名单。
3. [`PublicOpinionAgentSpec.format_system_prompt`](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/src/open_deep_research/public_opinion_agents/base.py#L28) 将生成的 `retrieval_tool_prompt` 注入角色系统提示词。

动态说明要求模型根据已检索证据判断是否继续检索，并在证据足够时用普通最终回复结束；只有实际列表包含 `rag_search` 时，才加入基于 SOURCE 引用作出本地知识或记忆声明的约束。允许 RAG 的角色若没有取得 `rag_search`，`_role_tool_prompt` 会说明内部资料或审批依据缺失时应明确标记，避免编造。

第五轮删除了已弃用且无调用方的静态 helper `get_research_tool_prompt(configurable)`：四角色运行链只使用上述动态入口，静态模板会与实际绑定工具集合脱节。`retrieval_mode` 控制前置工具装配，提示词最终以角色过滤后的实际工具集合为准。

现有契约验证可参见 [角色工具隔离测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_public_opinion_tool_permissions.py#L53)、[角色 prompt 契约测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_public_opinion_agents.py#L59) 和 [无 RAG 图消费测试](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/tests/test_graph_consumer_without_rag.py#L27)；后者验证缺失 RAG 提示及实际工具白名单进入模型上下文。

## 9. 扩展新工具

1. 在对应业务模块实现工具函数。第一个参数声明为 SDK 的 `ToolContext`，其余参数用 Python 类型和默认值生成模型可见 Schema。
2. 用 `@business_tool(domain=..., effect=...)` 声明工具。它复用官方 `@function_tool` 生成的 Schema、校验和默认值，并自动在 SDK 校验之后调用 `ToolExecutor.execute`；业务函数只实现业务逻辑。同步业务函数由 Executor 放入线程，并自动启用取消等待。需要运行配置的业务函数在函数体内通过 `context.context.tool_executor.tool_config(context)` 读取，此时受保护操作已经登记。
3. 将声明加入本地工具集合；`ToolManager` 按角色领域白名单筛选并绑定，无需新增第二个注册表或执行器。
