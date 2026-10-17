# Interview Question Bank

基线：`origin/main @ 12e2f0e`。回答中的升级方案均为 `PROPOSED`，除非明确写为当前实现。

这里的“当前实现”指上述学习基线；保留历史源码映射用于面试复盘。新运行时的实施边界见 [OpenAI Agents SDK 迁移计划](../architecture/openai-agents-sdk-migration-plan.md)。

## Q1 为什么不用微服务？

### 30 秒回答

当前主要瓶颈是模型调用、网页摘要重复和上下文增长，而不是 Python 服务吞吐。我选择模块化单体保持调用链和 State merge 可解释。只有当 RAG、采集或编排出现独立扩缩、SLA、故障边界或团队所有权时再拆。

### 深入回答

微服务能提供独立扩缩和故障隔离，但会引入网络失败、序列化、版本协调、事件一致性、跨服务追踪和运维成本。拆服务也不会自动减少 Token 或重复摘要。因此先解决真实成本中心，再按触发指标拆热点。

### 面试官可能继续问

- 单体是不是单点故障？
- 多实例后并发怎么管？
- 什么时候 RAG 值得独立部署？

### Source

- `architecture-decisions.md` ADR-001
- `src/open_deep_research/deep_researcher.py`

## Q2 为什么使用 LangGraph？

### 30 秒回答

业务天然包含 fan-out、review、conditional follow-up、State merge 和串行风险阶段。LangGraph 能把这些关系显式编码，比一个大 Agent loop 更容易调试、限预算和追踪。

### 深入回答

项目通过主图和 `public_opinion_subgraph` 分离规划、研究和报告。Graph edge 决定大部分角色转换；只有 review 的结构化 `next_tasks` 使用 `Send` 动态补查。

### 面试官可能继续问

- 为什么不用 Supervisor？
- Reducer 如何合并并行状态？

### Source

- `deep_researcher.py:_create_deep_researcher_builder()`
- `deep_researcher.py:public_opinion_builder`

## Q3 为什么不用一个 Agent？

### 30 秒回答

公开信号、内部事实、风险验证和响应策略的数据权限与输出合同不同。拆成角色后可以限制 Tool domain、保留角色私有上下文，并让风险阶段消费上游证据。

### 深入回答

四个角色共享 `AgentRuntime`，不是四套完全不同的程序。差异由 `PublicOpinionAgentSpec` 提供，避免复制运行时逻辑。

### Source

- `public_opinion_agents/base.py:PublicOpinionAgentSpec`
- `runtime/business_agent.py:run_business_agent()`

## Q4 Multi-Agent 怎么协作？

### 30 秒回答

首次运行时 `public_signal` 与 `internal_knowledge` 并行。`research_review` 等待两者后判断是否存在 gap。若有 gap，按 `target_role` 用 `Send` 定向补查；随后 `risk_assessment` 和 `response_strategy` 串行执行。

### Source

- `deep_researcher.py:public_opinion_builder`
- `deep_researcher.py:route_after_research_review()`

## Q5 项目里哪里用了并发？

### 30 秒回答

请求层有进程内 Semaphore；Graph 有初始角色并行和动态 Send；AgentRuntime 用 `asyncio.gather` 执行 Tool；Tavily 同时并发 query 和网页摘要；Section Writer 并行写章节；RAG 和 OCR 用 `to_thread`；Memory 索引用 daemon Thread。

### Source

- `web/server.py`
- `deep_researcher.py`
- `runtime/agent_runtime.py`
- `utils.py`
- `memory/writer.py`

## Q6 asyncio.gather 有什么问题？

### 30 秒回答

它缩短独立 I/O 的总等待时间，但最慢任务决定整批完成时间。当前 Tool 执行先用 safe wrapper 把异常转成 ToolMessage，`gather` 没有使用 `return_exceptions=True`，Runtime 也没有统一的 per-tool timeout 和 domain bulkhead。

### 面试官可能继续问

- Timeout、Bulkhead、Circuit Breaker 分别解决什么？
- 一个 MCP 调用 hang 住会怎样？

### Source

- `runtime/agent_runtime.py:AgentRuntime.run()`
- `runtime/business_agent.py:_execute_tool_safely()`

## Q7 怎么防止 Token 爆炸？

### 30 秒回答

先减少原始数据和重复摘要，再压缩对话历史，最后用 Research Graph 提供角色相关的语义 Working Context。预算守卫限制 model/tool/search calls，`_compact_history()` 在 pressure 达阈值时触发 rolling compaction。

### Source

- `budget.py`
- `runtime/agent_runtime.py:_compact_history()`
- `research_graph/compaction.py:rolling_compact()`

## Q8 Follow-up 成本高为什么还保留？

### 30 秒回答

它解决研究完整性问题。一次性 Research 不能保证覆盖关键 evidence gap。正确方向不是删除质量机制，而是把补查从完整 AgentRuntime 降级为只返回 evidence delta 的 Gap Worker。

### Source

- `deep_researcher.py:research_review()`
- `deep_researcher.py:route_after_research_review()`
- [固定版本的中文成本回顾](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/docs/ARCHITECTURE_OPTIMIZATION_LOG_CN.md)
- [保留的 Before/After 成本比较](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/evaluation/cost_profile/BEFORE_AFTER_COST_COMPARISON.md)

## Q9 为什么需要 Context Compression？

### 30 秒回答

Tool Result 和 AIMessage 会随 ReAct 迭代增长，后续每次模型调用都会重新付输入 Token。压缩将旧步骤变成 rolling summary，并保留当前任务、Working Context、相关子图和最近原始步骤。

### Source

- `runtime/agent_runtime.py:_compact_history()`
- `research_graph/compaction.py:rolling_compact()`

## Q10 压缩也花 Token，值得吗？

### 30 秒回答

不一定。压缩是有成本的投资。应该记录 `compression_tokens` 和后续 `downstream_tokens_saved`，用近似净节省判断 ROI。若一次压缩消耗 20k 但只节省 5k，就是负收益。

### Source

- `budget.py`
- `research_graph/metrics.py`

## Q11 并行 Agent 怎么保证预算？

### 30 秒回答

当前每个分支有预算守卫，完成后由 `runtime_reducer` 合并 usage delta。但这不是原子 reservation，两个分支可能同时看到相同余额。建议增加 BudgetBroker，在执行前 `reserve`，完成后 `commit` 或失败时 `release`。

### Source

- `budget.py:can_spend_model_call()`
- `state.py:runtime_reducer()`

## Q12 为什么 Reducer 解决不了预算竞争？

### 30 秒回答

Reducer 发生在并行分支返回 State Patch 之后，解决的是合并，不是执行前互斥。预算竞争属于 check-then-act race，需要原子 reserve，而不是事后相加。

## Q13 Semaphore 为什么不适合多实例？

### 30 秒回答

`asyncio.Semaphore` 是进程内对象。若运行 4 个独立 worker，每个配额为 2，理论全局上限约为 8。多实例需要 Redis lease、distributed semaphore 或 durable queue 的 worker concurrency。

### Source

- `web/server.py:_RESEARCH_SEMAPHORE`

## Q14 项目挂了怎么恢复？

### 30 秒回答

当前生产图 compile 时没有 Checkpointer，长任务进程崩溃后不能原地 resume。当前规模下可以接受重跑；当任务更长、单次成本更高或用户不能接受重跑时，应接 PostgreSQL/Redis Checkpointer，再配合 durable job queue。

### Source

- `deep_researcher.py:deep_researcher()`
- `tests/run_evaluate.py` 中的 `MemorySaver`

## Q15 RAG 为什么不直接拆服务？

### 30 秒回答

当前 RAG 通过 `to_thread` 避免阻塞事件循环，模块边界已经独立。只有当 embedding/index query 的 CPU、内存或扩缩曲线与 Agent orchestration 明显不同，或出现独立 SLA 与团队所有权时，拆服务才有收益。

## Q16 MCP Server 挂了怎么办？

### 30 秒回答

`load_mcp_tools()` 按 server 单独构造 client，并在独立 try/except 中发现工具。一个 server 失败会记录 warning 并继续加载其他 server。下一步可增加 discovery cache、health TTL、timeout 与 circuit breaker。

### Source

- `mcp/tools.py:load_mcp_tools()`

## Q17 流量扩大 10 倍怎么办？

### 30 秒回答

先压测确认瓶颈。短期做 cache、bounded fan-out、BudgetBroker、timeout 和 production checkpointer；多实例后接 durable queue 与全局 admission control；只有 RAG、采集或 MCP 出现独立资源和 SLA 边界时才拆热点服务。

## Q18 最大技术债是什么？

### 30 秒回答

成本放大与 durability：网页逐条摘要、Follow-up 粒度过粗、Context 重复，以及生产无 Checkpointer。它们比“是否微服务”更直接影响当前质量、成本和恢复能力。

## Q19 如果再做一个月，优先改什么？

### 30 秒回答

第一周做 summary cache 和跨 round 去重；第二周做 bounded concurrency 与 per-tool timeout；第三周做 BudgetBroker；第四周接生产 Checkpointer，并用固定 Case 回归质量、Token、延迟和失败率。

## Q20 设计得最好的一点是什么？

### 30 秒回答

业务角色与共享 Runtime 分离。AgentSpec 负责角色合同和 Tool 权限，AgentRuntime 统一 ReAct、预算、Tool 回流和压缩。这样既能表达 Multi-Agent，又不会复制四套执行逻辑。

### Source

- `public_opinion_agents/base.py`
- `runtime/business_agent.py`
- `runtime/agent_runtime.py`

## 面试时不要这么说

- 不要说：“用了 asyncio，所以性能很好。”
  - 改成：“主要 I/O 路径并发执行，但 fan-out 会放大外部资源和预算竞争，因此需要 bounded concurrency。”
- 不要说：“微服务性能更好。”
  - 改成：“微服务提供独立扩缩与故障隔离，但增加网络、一致性和运维成本。”
- 不要说：“LangGraph 是多 Agent 框架。”
  - 改成：“这里主要用 LangGraph 做显式状态编排，多 Agent 通过不同角色合同和 Graph 节点体现。”
- 不要说：“RAG 能防止幻觉。”
  - 改成：“RAG 提供可引用上下文，事实性仍依赖来源质量和验证流程。”
