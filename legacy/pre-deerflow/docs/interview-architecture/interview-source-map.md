# Interview Source Map

## 核心请求链

- Question：一次请求怎么跑到最终报告？
- Concept：Graph orchestration / State patch / report writing
- File：`src/open_deep_research/web/server.py`
- Symbol：`research()`
- Next：`deep_researcher.py:research_phase()` → `runtime/business_agent.py:run_business_agent()` → `runtime/agent_runtime.py:AgentRuntime.run()` → `deep_researcher.py:compile_final_report()`
- Inspect：`initial_state`、`config`、`chunk`、`report.final`

## Graph 与角色协作

- Question：Multi-Agent 怎么协作？
- Concept：fan-out / review / conditional Send / serial risk stage
- File：`src/open_deep_research/deep_researcher.py`
- Symbol：`public_opinion_builder`、`route_after_research_review()`
- Inspect：`review.next_tasks`、`effective_tasks`、`grouped_tasks`、`Send` payload

## Tool 并发

- Question：Tool 为什么可能拖住整个 Agent round？
- Concept：`asyncio.gather` / slowest-task tail
- File：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime.run()`
- Inspect：`known`、`results`、`batch`、`tool_calls`
- Related：`runtime/business_agent.py:_execute_tool_safely()`

## 请求级并发

- Question：Semaphore 为什么不能约束多实例？
- Concept：process-local admission control
- File：`src/open_deep_research/web/server.py`
- Symbol：`_RESEARCH_SEMAPHORE`、`event_stream()`
- Inspect：worker / instance 数量与每进程配额

## 预算竞争

- Question：Reducer 为什么解决不了预算超调？
- Concept：non-atomic check-then-act race
- File：`src/open_deep_research/budget.py`
- Symbols：`can_spend_model_call()`、`filter_tool_calls_for_budget()`
- File：`src/open_deep_research/state.py`
- Symbol：`runtime_reducer()`
- Inspect：分支读取的 budget snapshot、合并发生时机

## 网页摘要成本

- Question：为什么一次 Case 会出现大量模型调用？
- Concept：query fan-out + per-URL summary fan-out
- File：`src/open_deep_research/utils.py`
- Symbols：`tavily_search()`、`tavily_search_async()`、`summarize_webpage()`
- Inspect：`unique_results`、`summarization_tasks`、`asyncio.gather`

## Follow-up 成本

- Question：为什么 gap 会放大为完整 ReAct？
- Concept：dynamic Send + shared business-agent runtime
- File：`src/open_deep_research/deep_researcher.py`
- Symbols：`research_review()`、`route_after_research_review()`、`_build_followup_send_payload()`
- Next：`runtime/business_agent.py:run_business_agent()`

## Context 增长

- Question：模型输入为什么越来越大？
- Concept：AIMessage + ToolMessage history / repeated role reports
- File：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime.run()`
- Inspect：`messages`、`model_messages`、`ToolMessage`

## Context Compression

- Question：什么时候压缩，保留什么？
- File：`src/open_deep_research/runtime/agent_runtime.py`
- Symbol：`AgentRuntime._compact_history()`
- File：`src/open_deep_research/research_graph/compaction.py`
- Symbol：`rolling_compact()`、`render_protected_context()`
- Inspect：`estimated_tokens`、`pressure`、`protected`、`rolling_summary`、`result.messages`

## RAG 边界

- Question：为什么当前不拆 RAG 服务？
- File：`src/open_deep_research/tools/rag_tool.py`
- Symbol：`rag_search()`
- File：`src/open_deep_research/rag/service.py`
- Symbol：`RAGPipeline.query()`
- Inspect：`asyncio.to_thread`、CPU/memory profile、p95、独立 scaling demand

## MCP 故障隔离

- Question：一个 MCP Server 挂了怎么办？
- File：`src/open_deep_research/mcp/tools.py`
- Symbol：`load_mcp_tools()`
- Inspect：per-server try/except、`client.get_tools()`、`wrapped`

## Memory 索引可靠性

- Question：后台索引线程丢了怎么办？
- File：`src/open_deep_research/memory/writer.py`
- Symbol：`trigger_memory_index_refresh()`、`_refresh_memory_index()`
- Inspect：daemon Thread、query-time ensure_ready fallback、幂等 memory_id

## Checkpointer

- Question：任务崩溃后如何恢复？
- File：`src/open_deep_research/deep_researcher.py`
- Symbol：`deep_researcher()`、`deep_researcher_builder.compile()`
- Inspect：生产 compile 是否传入 checkpointer
- Reference：`tests/run_evaluate.py` 中的 `MemorySaver`

## Observability

- Question：怎么定位慢节点、错误和 Token 成本？
- File：`src/open_deep_research/observability/langsmith.py`
- Symbols：`trace_span`、`node_metadata()`、`agent_metadata()`
- File：`src/open_deep_research/budget.py`
- Symbols：`ainvoke_model_with_budget()`、`model_accounting_config()`
- Inspect：trace tree 与 usage policy 分工
