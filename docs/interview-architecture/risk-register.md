# High-risk Code Register

基线：`origin/main @ 12e2f0e`。风险等级描述潜在影响与触发条件，不代表线上事故已经发生。

下表保留该学习基线的代码映射与当时建议。后续 SDK 迁移的职责、风险和验收以 [迁移计划](../architecture/openai-agents-sdk-migration-plan.md) 为准，不能将历史 LangGraph 升级方案直接作为当前实施指令。

| Priority | Status | Risk | Code evidence | Trigger | Current protection | Recommended next step |
|---|---|---|---|---|---|---|
| P0 | OPEN | 网页逐条摘要放大模型调用 | `utils.py:tavily_search()` 内对所有 URL 创建 `summarization_tasks` 并 `asyncio.gather` | 多 query、多 URL、follow-up 重复检索 | URL 去重；单次摘要 60s timeout | URL+content hash 摘要缓存；批量摘要；Semaphore；低价值页面不进 LLM |
| P0 | PARTIALLY MITIGATED | Follow-up 重新跑完整 ReAct | `deep_researcher.py:route_after_research_review()` → `Send` → `run_business_agent()` | review 产生多个 gap，第二轮仍执行完整角色运行时 | `max_research_rounds`；task 去重；预算守卫 | Gap Worker：一个任务只补一条证据，返回 delta，不生成完整 role report |
| P0 | PARTIALLY MITIGATED | 并行 Agent 的全局预算可能超调 | 子图 START 同时进入 `public_signal_agent` 与 `internal_knowledge_agent`；各分支读取同一初始 budget 快照 | 两个分支同时看到剩余额度 | reducer 最终合并 usage；分支内有 guard | 共享原子 BudgetBroker / reservation；或每分支预分配额度 |
| P1 | PARTIALLY MITIGATED | Tool 并发缺少统一外层 timeout / bulkhead | `AgentRuntime.run()` 对 known tools 使用 `asyncio.gather` | MCP、HTTP、DB 工具慢或不返回 | 工具异常转 ToolMessage；部分工具自带 timeout | `gather` + per-tool `wait_for`；按 domain 设置并发上限；熔断与取消传播 |
| P1 | ACCEPTED CURRENT SCALE | Web 并发限制只在单进程有效 | `web/server.py:_RESEARCH_SEMAPHORE` | 多 worker / 多实例 | 每进程 Semaphore | 任务队列 + 全局租约；或 Redis semaphore；请求级 admission control |
| P1 | PARTIALLY MITIGATED | 后台 Memory 索引线程不具备持久交付 | `memory/writer.py:trigger_memory_index_refresh()` 使用 daemon `threading.Thread` | 进程退出、部署滚动、索引异常 | query-time `ensure_ready()` 兜底；异常记录日志 | Outbox + durable worker；幂等 memory_id；重试与 dead-letter |
| P1 | ACCEPTED CURRENT DEPLOYMENT | 生产 Graph 无 Checkpointer | `deep_researcher.py:deep_researcher()` compile 未传 checkpointer | 长任务进程崩溃、需要 resume | Web 返回 trace_id；长期 memory 独立写入 | 先接 PostgreSQL/Redis checkpoint；按 thread_id 恢复；设置 retention |
| P1 | PARTIALLY MITIGATED | MCP 工具发现重复且串行 | `get_all_tools()` 每个角色调用 `load_mcp_tools()`；每个 server 顺序 `client.get_tools()` | 多角色、多 server、stdio 启动成本高 | 单 server 故障隔离 | 按 config fingerprint 缓存 tool schema；并行 discovery；健康状态与 TTL |
| P1 | PARTIALLY MITIGATED | Tool 异常文本直接回流模型 | `business_agent.py:_execute_tool_safely()` 返回 `Error executing tool: {exc}` | 异常含 DSN、URL、内部字段 | API 层不向用户返回堆栈；异常由 wrapper 捕获 | 结构化安全错误；敏感字段清洗；完整异常只进受控日志/trace |
| P2 | OPEN | DBHub DSN 出现在子进程参数 | `mcp/tools.py:_build_dbhub_config()` 把 `--dsn` 加入 args | 本机进程列表可被读取 | 日志中 DSN 已掩码；DB read-only flags | 使用环境变量/secret file/stdin 传递；最小权限账号 |
| P2 | ACCEPTED CURRENT SCALE | CPU/同步 RAG 工作挤占默认线程池 | `tools/rag_tool.py:rag_search()` 两次 `asyncio.to_thread` | 多请求并发、embedding/index query 耗时 | Web Semaphore | 专用 executor；按 RAG provider 限流；重任务独立 worker |

## Evidence from the recorded cost case

来源：[固定版本的中文成本回顾](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/docs/ARCHITECTURE_OPTIMIZATION_LOG_CN.md)。原始统计可从 [Before/After 成本比较](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/evaluation/cost_profile/BEFORE_AFTER_COST_COMPARISON.md) 和 [After 单案例明细](https://github.com/kkaaakk/public_opinion_research/blob/534c74a6ccab829fcca0bc8947df3a949665f8c4/evaluation/cost_profile/AFTER_SINGLE_CASE_COST_PROFILE.md) 复核。

- Dynamic loop 版本：233 次模型调用、60 次 Tool 调用、642.46 秒。
- 基线版本：135 次模型调用、46 次 Tool 调用、493.97 秒。
- 已知 Token：1,332,387 vs 827,944，增加约 60.9%。
- 单 Case 产生 187 次网页摘要调用。
- `public_signal` 第二轮 input 从约 8,170 tokens 增长到约 96,307 tokens。
- 6 次研究压缩消耗约 298,064 个已知 tokens。
- Follow-up 相关已知 Token 至少 629,027。

这些数字来自一次固定 Case 对比，适合定位成本中心，不足以证明通用容量上限。
