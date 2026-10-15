# Interview Architecture Deck Outline

| # | Title | Interview goal |
|---:|---|---|
| 1 | Public Opinion Research 架构深挖与演进设计 | 定义面试主题 |
| 2 | 面试讲述主线 | 建立 Problem → Decision → Evidence → Upgrade 叙事 |
| 3 | 当前场景与设计约束 | 解释为什么不能只追求“最大架构” |
| 4 | 静态代码图谱方法与边界 | 说明分析方法、规模与局限 |
| 5 | 核心模块图谱 | 展示模块依赖与风险热点 |
| 6 | 一次请求的核心链路 | 从 HTTP 到 report.final |
| 7 | 并发点总览 | 映射请求、Graph、Tool、Search、RAG、Memory 并发 |
| 8 | 请求级并发控制 | 解释进程内 Semaphore 的收益和边界 |
| 9 | Graph 级并发 | 初始双 Agent、动态 Send 与 budget 超调风险 |
| 10 | Tool 级并发 | asyncio.gather、timeout 与 bulkhead |
| 11 | 搜索与摘要双层 fan-out | 定位 187 次摘要的结构原因 |
| 12 | Section Writer fan-out | 质量、延迟和成本权衡 |
| 13 | 线程池与后台线程 | to_thread、daemon thread 和丢失风险 |
| 14 | 风险热力图 | 按概率与影响排序 |
| 15 | 高风险代码片段清单 | 面试可逐项展开的风险索引 |
| 16 | 摘要成本放大 | 真实代码、数据和优化阶梯 |
| 17 | Follow-up 放大 | 质量收益与成本代价 |
| 18 | ReAct Context 增长 | 压缩何时划算 |
| 19 | 并行预算竞争 | 分支快照问题与 BudgetBroker 方案 |
| 20 | 可靠性与安全边界 | Checkpointer、错误清洗、DSN、全局并发 |
| 21 | ADR：显式 Graph + 模块化单体 | 当前方案的设计理由 |
| 22 | 为什么现在不拆微服务 | 方案比较与升级条件 |
| 23 | 分阶段升级路线 | Harden → Durable Jobs → Service Split → Event Driven |
| 24 | 优化 Backlog | P0/P1/P2 执行顺序与验证指标 |
| 25 | 面试追问与回答框架 | 把 trade-off 讲成工程判断 |
| 26 | 一分钟总结 | 收束设计、证据和演进路径 |
| 27 | 项目最大的 5 个工程问题 | 快速展示自我审视能力 |
| 28 | 我做架构判断的方法 | 解释判断维度而非堆技术 |
| 29 | 面试追问链 | 练习连续追问 |
| 30 | 面试官可能沿哪几条线追 | 建立四条复习路线 |
| 31 | Appendix A：Concurrency Questions | 并发类问题速查 |
| 32 | Appendix B：Architecture / Cost / Evolution | 架构、成本和演进问题索引 |
