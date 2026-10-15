# Source Learning Index

本索引把知识点映射到当前 `origin/main @ 12e2f0e` 的真实文件、symbol、推荐断点和观察变量。函数名优先，行号仅作为临时辅助。

## Request 与入口

### FastAPI SSE 请求

- 文件：`src/open_deep_research/web/server.py`
- Symbol：`research()`
- 推荐断点：`research()` 内构造 `config`、调用 `_deep_researcher_factory(config).astream(...)` 的位置
- 观察变量：`request`、`thread_id`、`config`、`initial_state`、`chunk`、`final_report`、`budget`
- 下一步：`deep_researcher()`、`write_research_brief()`

### LangGraph Studio / API 入口

- 文件：`langgraph.json`
- Symbol：`graphs["Deep Researcher"]`
- 目标：理解 LangGraph CLI 如何找到 `deep_researcher.py:deep_researcher`

## Graph

### 主图创建

- 文件：`src/open_deep_research/deep_researcher.py`
- Symbols：`_create_deep_researcher_builder()`、`deep_researcher()`、`deep_researcher_builder`
- 推荐断点：`_create_deep_researcher_builder()`、`deep_researcher()`
- 观察变量：`builder`、node name、edge、`config_schema`
- 下一步：`research_phase()`

### 舆情子图

- 文件：`src/open_deep_research/deep_researcher.py`
- Symbols：`public_opinion_builder`、`public_opinion_subgraph`、`research_phase()`
- 推荐断点：`research_phase()`、`route_after_research_review()`
- 观察变量：`research_run_id`、`subgraph_config`、`result`、`budget_update`

## State

### State schema 与 reducer

- 文件：`src/open_deep_research/state.py`
- Symbols：`DeepResearchState`、`workflow_reducer()`、`agents_reducer()`、`research_reducer()`、`report_reducer()`、`runtime_reducer()`
- 推荐断点：`agents_reducer()`、`runtime_reducer()`
- 观察变量：`current_value`、`new_value`、override token、role key、budget delta
- 目标：理解 State Patch 在每个节点返回后如何合并；reducer 不是业务节点。

## RunnableConfig 与业务配置

### RunnableConfig 入口

- 文件：`src/open_deep_research/web/server.py`
- Symbol：`research()`
- 观察变量：`config["configurable"]`、`config["metadata"]`

### Configuration 映射

- 文件：`src/open_deep_research/configuration.py`
- Symbol：`Configuration.from_runnable_config()`
- 推荐断点：`from_runnable_config()`
- 观察变量：`configurable`、`field_names`、`values`
- 目标：理解环境变量如何覆盖 `configurable`，以及 Pydantic `Configuration` 如何生成。

## Multi-Agent

### Agent 角色合同

- 目录：`src/open_deep_research/public_opinion_agents/`
- Symbols：`PublicOpinionAgentSpec`、`get_public_opinion_agent_spec()`、四个模块中的 `AGENT`
- 观察：`allowed_domains`、`expected_output`、`context_strategy`、`tool_policy`

### 动态补查路由

- 文件：`src/open_deep_research/deep_researcher.py`
- Symbols：`research_review()`、`route_after_research_review()`、`_effective_followup_tasks()`、`_build_followup_send_payload()`
- 推荐断点：`route_after_research_review()`
- 观察变量：`review.next_tasks`、`effective_tasks`、`grouped_tasks`、`Send` payload

## AgentRuntime

### Agent 装配

- 文件：`src/open_deep_research/runtime/business_agent.py`
- Symbols：`run_business_agent()`、`_business_agent_tools()`、`_build_business_agent_assignment()`
- 推荐断点：`run_business_agent()`
- 观察变量：`spec`、`assignment`、`tools`、`model`、`workspace`、`system_prompt`

### ReAct 主循环

- 文件：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime.run()`
- 推荐断点：`AgentRuntime.run()` 中构造 `model_messages`、取得 `response`、读取 `tool_calls`、执行 `asyncio.gather`、调用 `workspace.ingest()` 的位置
- 观察变量：`messages`、`model_messages`、`response`、`tool_calls`、`allowed`、`skipped`、`results`、`batch`、`budget_update`、`graph_final`

## Tool

### 工具装配

- 文件：`src/open_deep_research/utils.py`
- Symbols：`get_retrieval_tools()`、`get_all_tools()`
- 推荐断点：`get_all_tools()`
- 观察变量：`tools`、`retrieval_tools`、`existing_tool_names`、`mcp_tools`

### 角色域过滤与执行

- 文件：`src/open_deep_research/runtime/business_agent.py`
- Symbols：`_business_agent_tools()`、`_execute_tool_safely()`
- 观察变量：`allowed_domains`、`filtered_tools`、`rejected_tools`、`observation`、`captured_budget`

## MCP

### 多 Server 加载

- 文件：`src/open_deep_research/mcp/tools.py`
- Symbol：`load_mcp_tools()`
- 推荐断点：`load_mcp_tools()` 内 `MultiServerMCPClient(...)` 与 `client.get_tools()`
- 观察变量：`servers`、`server_name`、`server_conn`、`tools`、`wrapped`

### 工具冲突包装

- 文件：`src/open_deep_research/mcp/tool_wrapper.py`
- Symbols：`wrap_mcp_tools()`、`prefixed_tool_name()`
- 观察变量：`original_name`、`existing_names`、`prefixed`

### Tool domain

- 文件：`src/open_deep_research/mcp/domain_filter.py`
- Symbols：`DOMAIN_REGISTRY`、`get_tool_domain()`、`tag_tools_with_domain()`

## Skill

- 当前源码树没有通用 Skill runtime。
- `social_media_skill` 是文档/业务组合命名，运行实现位于 `src/open_deep_research/social_media/tools.py`。
- 相关底层原语：`src/open_deep_research/social_media_mcp.py`

## Context

### 模型输入装配

- 文件：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime.run()`
- 文件：`src/open_deep_research/public_opinion_agents/base.py`
- Symbol：`PublicOpinionAgentSpec.format_system_prompt()`
- 文件：`src/open_deep_research/research_graph/workspace.py`
- Symbol：`ResearchWorkspace.before_model()`
- 文件：`src/open_deep_research/research_graph/strategy.py`
- Symbols：`StandardContextStrategy.before_model()`、`_ResearchGraphStrategyBase.before_model()`
- 目标：核对 `SystemMessage`、assignment、Research Graph context、private memory 与 ReAct history 的真实顺序。

## Memory

### ID 与 scope

- 文件：`src/open_deep_research/memory/context.py`
- Symbols：`get_conversation_id()`、`get_user_id()`
- 文件：`src/open_deep_research/workflow/state_utils.py`
- Symbol：`resolve_research_run_id()`
- 观察变量：`conversation_id`、`thread_id`、`user_id`、`research_run_id`

### 长期记忆写入

- 文件：`src/open_deep_research/deep_researcher.py`
- Symbol：`maybe_persist_chat_memory()`
- 文件：`src/open_deep_research/memory/writer.py`
- Symbols：`persist_conversation_memory()`、`trigger_memory_index_refresh()`
- 文件：`src/open_deep_research/memory/store.py`
- Symbol：`MySQLChatMemoryStore`
- 观察变量：`conversation_id`、`user_id`、`records`、`index_status`

## Context Compression

- 文件：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime._compact_history()`
- 文件：`src/open_deep_research/research_graph/compaction.py`
- Symbols：`rolling_compact()`、`render_protected_context()`
- 推荐断点：`_compact_history()`、`rolling_compact()`
- 观察变量：`estimated_tokens`、`pressure`、`protected`、`rolling_summary`、`result.messages`、`tokens_removed`

## RAG

### Agent-facing tool

- 文件：`src/open_deep_research/tools/rag_tool.py`
- Symbol：`rag_search()`

### 查询门面

- 文件：`src/open_deep_research/rag/service.py`
- Symbol：`RAGPipeline.query()`
- 推荐断点：`RAGPipeline.query()`
- 观察变量：`query_vector`、`candidate_count`、`retrieval_results`、`selected`

### Hybrid retrieval

- 文件：`src/open_deep_research/rag/retriever.py`
- Symbols：`HybridChunkRetriever.retrieve()`、`reciprocal_rank_fusion()`、`apply_structured_metadata_boost()`
- 观察变量：`vector_results`、`keyword_results`、`merged_results`

### Rerank

- 文件：`src/open_deep_research/rag/reranker.py`
- Symbols：`CrossEncoderReranker.rerank()`、`create_reranker()`

## GraphRAG

- 文件：`src/open_deep_research/rag/graph.py`
- Symbols：`GraphRAGIndex`、`Neo4jGraphRAGIndex`、`create_graph_index()`
- 生命周期：知识索引生命周期；用于扩展检索候选。

## Research Graph

- 目录：`src/open_deep_research/research_graph/`
- 入口：`ResearchWorkspace`
- 关键 Symbols：`ResearchWorkspace.before_model()`、`ResearchWorkspace.ingest()`、`ResearchWorkspace.context_for()`、`create_context_strategy()`
- 生命周期：`research_run_id` 范围；用于保存一次研究运行内的证据与 Working Context。

## Observability

### LangSmith tracing

- 文件：`src/open_deep_research/observability/langsmith.py`
- Symbols：`ensure_langsmith_configuration()`、`correlation_metadata()`、`node_metadata()`、`agent_metadata()`、`trace_span`
- 目标：定位执行树、节点、模型、工具、RAG 阶段和错误。

### Token 与预算

- 文件：`src/open_deep_research/budget.py`
- Symbols：`ainvoke_model_with_budget()`、`model_accounting_config()`、`filter_tool_calls_for_budget()`、`estimate_context_tokens()`、`context_pressure_ratio()`
- 观察变量：`model_calls`、`tool_calls`、`search_calls`、`input_tokens`、`output_tokens`、`cache_read`、`cache_creation`

## Report

- 文件：`src/open_deep_research/deep_researcher.py`
- Symbols：`section_writer()`、`write_final_sections()`、`compile_final_report()`、`_fallback_report_generation()`
- 推荐断点：`compile_final_report()`
- 观察变量：`sections`、`completed_sections`、`missing_names`、`report`、`budget_usage`
