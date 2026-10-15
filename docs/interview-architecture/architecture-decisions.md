# Architecture Decisions for Interview Discussion

## ADR-001: 使用显式 LangGraph + 模块化单体

### Status

Accepted for the current stage.

### Context

- 团队和部署规模尚不需要多个独立服务。
- 研究任务需要可见的节点、边、并行分支、条件路由与状态合并。
- LLM、Tool、RAG、MCP 的调试复杂度已经很高，继续引入分布式事务和消息一致性会放大运维成本。

### Decision

保持一个 Python 部署单元，用 LangGraph 显式编排主图与舆情子图；用模块边界隔离 Agent Runtime、RAG、MCP、Memory 与 Observability。

### Alternatives considered

- Supervisor swarm：路由灵活，但决策不可预测，难做预算上限与断点学习。
- 微服务：可独立扩缩，但当前会引入网络故障、版本协调、队列与数据一致性成本。
- 单一 AgentExecutor：实现简单，但难表达并行角色、review loop 与 reducer state。

### Consequences

- 正面：调用路径清晰；本地调试简单；部署成本低；可以先优化真实成本中心。
- 负面：CPU/RAG/Tool 不能独立扩缩；进程故障影响整次任务；全局并发需要外部设施才能跨实例治理。

### Upgrade trigger

- 多实例后需要全局 admission control。
- RAG 或社媒采集的资源曲线与 Graph runtime 明显分离。
- 不同团队需要独立发布或 SLA。

## ADR-002: 保留 Dynamic Review，但限制研究放大

### Status

Accepted with mitigation work.

### Context

一次性研究容易遗漏关键证据。`ResearchReview` 能识别 gap，并用 `Send` 定向补查。但实测 follow-up 可能重新执行完整 ReAct，成本显著上升。

### Decision

当前保留 review loop，以质量优先；通过 `max_research_rounds`、任务去重、预算守卫和角色限定控制最坏路径。

### Alternatives considered

- 完全删除 follow-up：成本稳定，但关键 gap 无法自动补齐。
- 每次完整重跑：覆盖高，但成本和延迟不可接受。
- Gap Worker：理想下一步；只返回 Evidence Delta，需要新的合同与评测。

### Consequences

- 正面：研究质量可以基于 gap 迭代；Graph 路由仍显式可审计。
- 负面：成本尾部变长；并行分支可能同时消耗剩余预算。

### Upgrade trigger

- follow-up Token 占单次总量超过约 30%。
- p95 研究延迟或单任务成本超过业务预算。
- Gap 补查成功率可稳定评测。

## ADR-003: 当前使用进程内并发，规模增长后引入队列和资源隔离

### Status

Accepted for low-to-moderate traffic.

### Context

当前并发来自 LangGraph fan-out、`asyncio.gather`、`asyncio.to_thread` 与后台 Thread。它减少了单请求等待时间，也保持了简单部署。

### Decision

在单进程/少量实例阶段继续使用进程内并发；为各 fan-out 增加 timeout、Semaphore 和预算 reservation。只有当资源竞争和故障隔离成为真实问题时，再拆成队列消费者。

### Alternatives considered

- 立即 Kafka/Celery 化：可恢复与横向扩展更强，但引入 broker、幂等、顺序、追踪和运维负担。
- 全串行：实现稳定，但研究延迟过高。

### Consequences

- 正面：低运维成本；代码链路短；适合快速验证业务价值。
- 负面：默认线程池、事件循环和单机内存成为共享瓶颈；进程退出时后台任务可能丢失。

### Upgrade trigger

- 多 worker 后 Semaphore 失去全局约束。
- Tool/RAG p95 导致事件循环或线程池饱和。
- 需要任务级 retry、暂停、恢复或优先级。
