# 源码与 Git 证据索引

## 基线

以下记录学习资料制作时的源码快照，保留原有文件与 symbol 映射用于复盘；不是当前分支状态。当前架构以 [Agent Loop](../architecture/agent-loop.md)、[RAG](../architecture/rag.md) 和 [SDK 迁移计划](../architecture/openai-agents-sdk-migration-plan.md) 为准。

- 制作时默认主分支：`origin/main`
- 制作时默认主分支提交：`12e2f0e`，Merge pull request #11
- 制作时工作树提交：`41f180b`
- 制作时 `git diff HEAD origin/main`：无文件差异
- LangGraph 入口：`langgraph.json` → `src/open_deep_research/deep_researcher.py:deep_researcher`
- Web 入口：`src/open_deep_research/web/server.py:research`

## Chapter 1 项目全貌

- `README.md`
- `pyproject.toml`
- `langgraph.json`
- `src/open_deep_research/web/server.py`
- `src/open_deep_research/deep_researcher.py`

## Chapter 2 主图、State 与 RunnableConfig

- `src/open_deep_research/deep_researcher.py:_create_deep_researcher_builder`
- `src/open_deep_research/deep_researcher.py:research_phase`
- `src/open_deep_research/state.py:DeepResearchState`
- `src/open_deep_research/state.py:workflow_reducer`
- `src/open_deep_research/state.py:agents_reducer`
- `src/open_deep_research/state.py:research_reducer`
- `src/open_deep_research/state.py:report_reducer`
- `src/open_deep_research/state.py:runtime_reducer`
- `src/open_deep_research/configuration.py:Configuration`
- `src/open_deep_research/workflow/state_utils.py:resolve_research_run_id`
- `src/open_deep_research/memory/context.py:get_conversation_id`
- `src/open_deep_research/memory/context.py:get_user_id`

## Chapter 3 Multi-Agent

- `src/open_deep_research/public_opinion_agents/base.py:PublicOpinionAgentSpec`
- `src/open_deep_research/public_opinion_agents/registry.py`
- `src/open_deep_research/public_opinion_agents/public_signal.py`
- `src/open_deep_research/public_opinion_agents/internal_knowledge.py`
- `src/open_deep_research/public_opinion_agents/risk_assessment.py`
- `src/open_deep_research/public_opinion_agents/response_strategy.py`
- `src/open_deep_research/runtime/business_agent.py:run_business_agent`
- `src/open_deep_research/runtime/agent_runtime.py:AgentRuntime.run`
- `src/open_deep_research/deep_researcher.py:research_review`
- `src/open_deep_research/deep_researcher.py:route_after_research_review`

## Chapter 4 Tool、MCP 与 Skill

- `src/open_deep_research/utils.py:get_all_tools`
- `src/open_deep_research/utils.py:get_retrieval_tools`
- `src/open_deep_research/runtime/business_agent.py:_business_agent_tools`
- `src/open_deep_research/runtime/business_agent.py:_execute_tool_safely`
- `src/open_deep_research/mcp/tools.py:load_mcp_tools`
- `src/open_deep_research/mcp/tool_wrapper.py:wrap_mcp_tools`
- `src/open_deep_research/mcp/domain_filter.py:DOMAIN_REGISTRY`
- `src/open_deep_research/social_media/tools.py:get_social_media_tools`
- `docs/guide/social-media-skill.md`
- 制作时 `README.md` 中的 `skills/` 目录树为残留文档；该源码快照不存在此目录。

## Chapter 5 Context 与 Memory

- `src/open_deep_research/runtime/agent_runtime.py:_compact_history`
- `src/open_deep_research/runtime/agent_runtime.py:format_private_memory`
- `src/open_deep_research/research_graph/workspace.py`
- `src/open_deep_research/research_graph/compaction.py:rolling_compact`
- `src/open_deep_research/memory/types.py:MemoryType`
- `src/open_deep_research/memory/writer.py:persist_conversation_memory`
- `src/open_deep_research/memory/store.py:MySQLChatMemoryStore`
- `src/open_deep_research/deep_researcher.py:maybe_persist_chat_memory`
- `tests/run_evaluate.py` 与 `tests/debug_run.py` 中的 `MemorySaver`

## Chapter 6 RAG

- `src/open_deep_research/tools/rag_tool.py:rag_search`
- `src/open_deep_research/rag/service.py:RAGPipeline.query`
- `src/open_deep_research/rag/indexer.py:RAGIndexer`
- `src/open_deep_research/rag/retriever.py:HybridChunkRetriever`
- `src/open_deep_research/rag/retriever.py:reciprocal_rank_fusion`
- `src/open_deep_research/rag/reranker.py`
- `src/open_deep_research/rag/vectorstore.py`
- `src/open_deep_research/rag/graph.py`
- `docs/architecture/rag-design-rationale.md`
- [当前 RAG / GraphRAG 说明](../architecture/rag.md)
- [制作时的 GraphRAG 改进说明](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/docs/graphrag_effectiveness.md)

## Chapter 7 舆情业务流程

- `src/open_deep_research/deep_researcher.py:write_research_brief`
- `src/open_deep_research/deep_researcher.py:plan_report_sections`
- `src/open_deep_research/deep_researcher.py:section_writer`
- `src/open_deep_research/deep_researcher.py:compile_final_report`
- 四个 `public_opinion_agents/*.py` 角色合同
- `data/knowledge/public_opinion/`
- `data/social_media/`

## Chapter 8 Observability

- `src/open_deep_research/observability/langsmith.py`
- `src/open_deep_research/budget.py`
- `docs/architecture/observability.md`
- `src/open_deep_research/web/server.py` 的 usage SSE 事件

## Chapter 9 架构演进

| 提交 | 日期 | 架构意义 |
|---|---|---|
| `9ede889` | 2025-06-11 | 消息型主流程、澄清与 interrupt 基线 |
| `d859138` | 2025-07-13 | V2 Deep Research Agent 基线 |
| `1b0e639` | 2026-04-11 | 独立 RAG 流程 |
| `5c0329b` | 2026-07-23 | 同步舆情 Agent、RAG、Memory、MCP 与 Web |
| `aff29ea` | 2026-07-24 | 移除通用模式，主流程收敛为舆情研究 |
| `019068a` | 2026-08-31 | 按 domain 隔离角色工具 |
| `5989480` | 2026-09-01 | 动态 review 与定向 follow-up 循环 |
| `2bdb9ad` | 2026-09-04 | Research Graph 上下文 harness |
| `49954f9` | 2026-09-12 | 按所有权分组 State |
| `3f0a548` | 2026-09-13 | 统一为单一 DeepResearchState schema |
| `5cc29c0` | 2026-09-14 | 共享 AgentRuntime，集中业务 Agent 执行 |
| `d499191` | 2026-09-17 | Token 统计收敛到 LangChain usage metadata |
| `f70d23c` | 2026-09-20 | LangSmith 负责执行追踪 |
| `d18c87a` | 2026-09-21 | 移除 Agent Observer，保留 LangSmith + budget.py 两条责任线 |
| `ba106da` | 2026-09-21 | 清理重复运行时 helper |
| `deedb2a` | 2026-09-22 | 删除未使用 legacy package |

## 辅助验证

- `tests/test_state_architecture.py`
- `tests/test_public_opinion_agents.py`
- `tests/test_public_opinion_research_loop.py`
- `tests/test_research_graph_context_harness.py`
- `tests/test_token_accounting.py`
- `tests/test_langsmith_integration.py`
- `tests/test_rag.py`
- `tests/test_graph_rag_ablation.py`
