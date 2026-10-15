# Slide outline

| # | Title | Goal | Primary source |
|---:|---|---|---|
| 1 | 企业舆情风险智能体平台 | 建立源码学习手册定位 | README, repository |
| 2 | 阅读基线与事实边界 | 说明分支、提交和状态标签 | git, langgraph.json |
| 3 | 系统解决的业务问题 | 解释舆情任务的输入和产出 | agent specs |
| 4 | 运行入口与技术栈 | 展示 Studio、Web、CLI 和依赖 | pyproject, langgraph.json |
| 5 | 当前目录地图 | 给出按责任阅读的目录结构 | src tree |
| 6 | 当前总体架构 | 展示真实组件和可选边界 | deep_researcher, runtime |
| 7 | 一次请求的鸟瞰流程 | 建立端到端心智模型 | server, graph |
| 8 | 项目全貌学习检查点 | 自测架构入口与边界 | slides 2–7 |
| 9 | 两条外部调用入口 | 对比 LangGraph API 与 FastAPI SSE | langgraph.json, server.py |
| 10 | 主 Graph 的创建与编译 | 定位 builder、factory 和 graph name | deep_researcher.py |
| 11 | State 的五个所有权域 | 解释消息与五个 reducer 域 | state.py |
| 12 | 当前 LangGraph 节点与边 | 还原主图和子图 | deep_researcher.py |
| 13 | 一次请求的完整调用链 | 标注函数、输入和输出 | server.py, graph, runtime |
| 14 | RunnableConfig 的来源 | 解释调用方如何构造 config | server.py, configuration.py |
| 15 | thread_id、user_id 与 run_id | 区分三种身份范围 | memory/context.py, state_utils.py |
| 16 | 断点式源码追踪路线 | 提供可执行调试顺序 | core source files |
| 17 | 主流程学习检查点 | 自测 State、Config、Edge | slides 9–16 |
| 18 | 当前 Multi-Agent 模式 | 定性为图编排的角色工作流 | subgraph, AgentSpec |
| 19 | 四个 Agent 的职责合同 | 对比 Prompt、Tool、Context | public_opinion_agents |
| 20 | Agent 协作图 | 展示并行、review、串行后处理 | public_opinion subgraph |
| 21 | 共享 AgentRuntime 的 ReAct 循环 | 拆解 LLM–Tool–Workspace 循环 | agent_runtime.py |
| 22 | Agent Context 与 Memory 隔离 | 说明共享与私有部分 | state reducers, runtime |
| 23 | 动态研究循环 | 解释 gap-driven Send 补查 | research_review, router |
| 24 | Multi-Agent 学习检查点 | 自测角色和交接 | slides 18–23 |
| 25 | Tool 生命周期 | 从定义到 ToolMessage 回流 | utils, business_agent |
| 26 | Tool Domain 权限矩阵 | 解释角色级最小权限 | domain_filter, specs |
| 27 | MCP Server 到 Agent | 追踪发现、包装、筛选、执行 | mcp/tools.py |
| 28 | 多 MCP 管理与冲突处理 | 解释故障隔离和命名空间 | mcp/tools.py, wrapper.py |
| 29 | 当前 Skill 的真实边界 | 澄清不存在通用 Skill runtime | source tree, docs |
| 30 | Graph、Agent、Skill、MCP、Tool | 建立五层责任边界 | current architecture |
| 31 | Tool、MCP、Skill 学习检查点 | 自测工具链 | slides 25–30 |
| 32 | LLM Context Assembly | 展示最终模型输入的组成 | AgentRuntime.run |
| 33 | Memory 实现状态 | 区分短期、私有、长期、checkpoint | state, memory, tests |
| 34 | 长期记忆写入与检索闭环 | 展示 MySQL 到 RAG 的路径 | memory writer/store, RAG |
| 35 | Context Compression | 展示阈值、保护区和结果去向 | agent_runtime, compaction |
| 36 | Context 与 Memory 学习检查点 | 自测范围与持久化 | slides 32–35 |
| 37 | 当前 RAG 架构 | 展示索引与查询两条链 | rag/service, indexer |
| 38 | Hybrid Retrieval、Fusion 与 Rerank | 解释真实排序顺序 | retriever, reranker |
| 39 | GraphRAG 与 Research Graph | 避免混淆两个“图” | rag/graph, research_graph |
| 40 | 舆情研究任务生命周期 | 把业务流程映射到节点 | graph, agent specs |
| 41 | 从规划到报告 | 解释 planning、verification、writing | deep_researcher.py |
| 42 | Token Usage 与 Trace | 展示 budget.py 与 LangSmith 分工 | observability docs, budget.py |
| 43 | 架构演进时间线 | 按问题和设计阶段组织 Git 历史 | git log |
| 44 | 四次关键重构 | 对比旧问题和当前结构 | selected commits |
| 45 | 源码学习地图 | 给出 LangGraph、Agent、MCP、RAG 路线 | source tree |
| 46 | 最终学习检查点 | 汇总可验证的问题和限制 | whole deck |
