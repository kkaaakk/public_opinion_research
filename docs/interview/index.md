# Public Opinion Research 面试讲解

## 项目介绍（约两分钟）

我做的是一个企业舆情研究和处置辅助系统。比如某款产品出现质量争议，用户只要给出议题，系统就可以汇总公开信号和企业内已有的同类案例，判断风险，再给出有来源的应对建议。研究开始前，系统会先生成一份 Brief。遇到信息不够的议题，它会请用户补充，用户也可以修改并审批研究范围。

服务用 FastAPI 接受请求，将研究交给 PostgreSQL 上的 DBOS 队列。公开信息和内部知识由两个 Agent 并行收集。系统把证据与 Brief 对照，缺什么就派对应角色补查，再依次做风险判断、响应策略和报告。四个角色使用 OpenAI Agents SDK，工具和研究证据的处理放在 Context Harness。浏览器随时能看进度，刷新后可以继续看同一个 Run。

工程上最难的是长任务中途出故障时，怎样保住成果，又避免重复调用收费模型或重复触发外部操作。我们用 DBOS 保存已完成步骤的结果，用业务回执记录外部操作和实际模型用量。执行器重启后能复用已提交的结果。遇到调用已发出但效果说不清的情况，会保留证据，交给人核对。报告交付之后，历史归档还可以独立恢复。这让我们能把故障排查落到具体的 Run、步骤和收据上。 [架构与业务链路](../architecture/agent-loop.md)

## 业务链路口述

从用户提交开始讲。API 先验证操作员令牌和请求键。RunClient 检查 Session 归属，在一个 PostgreSQL 事务里保存请求，并把根 Workflow、事件 relay 和 Finalizer 入队。用户拿到 Run ID 之后，就能边等边读 SSE。

Executor 运行 `por.run`。第一轮 Plan 可以处理图片，随后给出澄清问题或者 Markdown Brief。澄清答复回到原 Run，下一轮 Plan 带着已有上下文继续。如果需要审批，用户可以编辑或请求 AI 修改，最后确认当前版本。根锁定 Brief 后，`por.research` 启动公开舆情和内部知识两个 Producer，各自处理同一版本的 Brief。每轮等子 Workflow 都返回，再按角色顺序合并。

Review 对照目标和证据，把缺口发回对应角色。补查结束后，Risk 汇总风险，Strategy 基于 Risk 建议行动。Report 默认一次综合，在启用高级模式时按章节写。阶段产物和报告通过 `por.events` 进入有序 Stream。Finalizer 看根及业务子 Workflow 的结束与账单结算，再提交终态和 History。归档暂时失败时，可以先读已生成报告。 [人机交互](../architecture/research-brief.md) · [Studio 协议](../architecture/agent-execution-studio.md)

## 可靠性口述

我通常分三个时间点讲。提交时要应对客户端重发。我们把请求指纹和客户端键记在同一个事务里，并用 DBOSClient 在这笔事务入队。同键同参能查回同一个 Run，因此客户端丢响应时可以安全重发。

执行中要应对进程重启。DBOS 保存 Workflow 输入和 Step 结果。已经写入的模型响应和工具输出，重启时直接读取。如果只有一个 Producer 做完，父 Workflow 还能读回它的结果，并等待另一侧恢复。用户点重试则有另一套明确的授权条件。当前失败已交付而且允许安全重试时，Finalizer 核对这一次失败的版本，再要求 DBOS rewind。Brief 审批绑定用户看到的版本，编辑后需要确认新版本。

最难的是外部系统已经收到请求、数据库还没看到结果的窗口。模型先登记物理尝试，再保存 provider 报告的用量。回执可以补写，已发出的调用不能靠猜测重发。工具按执行效果分级，已知只读的操作在缺检查点时可以重新执行，未知效果要人工核对。有稳定业务键的写入按同一个键提交。两个 Producer 共享 Run 预算，给最终报告预留调用额度，实际 Token 用回执核算。 [可靠性设计](../architecture/agent-loop.md#可靠性设计)

## 高频追问

### 1. 为什么从 LangGraph 迁移到 DBOS？

这个项目有长时间审批、执行器重启和外部副作用，之前维护了较多阶段占位、许可与结果恢复逻辑。DBOS 用 Workflow、Step、Queue、原生消息和 Stream 表达这些持久边界。State 的业务归并和 AgentSpec 可以沿用，四个角色的推理和工具选择交给 Agents SDK。 [迁移历史](../architecture/openai-agents-sdk-migration-plan.md)

### 2. Workflow 和 Step 有什么区别？

Workflow 决定调用顺序和分支，恢复时重新进入函数。Step 保存一次有副作用或非确定性工作的结果。相同的原生 Step 已有结果时直接读取。Workflow 和子 Workflow 的输入、状态和结果由 DBOS 管理。

### 3. 为什么使用官方 DBOSRunner？

它让 SDK Runner 仍然掌管 Agent 循环，同时把模型响应放进 DBOS Model Step。项目在这层内登记真实调用，在 Harness 用自己的 Tool Step 和 Context Step 保存证据上下文，省去维护自定义 Agent 恢复协议的成本。

### 4. 模型已经返回但 Step 还没提交，重启会怎样？

如果调用前的尝试记录已落库，恢复时遇到同一物理调用 ID 会停在核对边界。已经有原生 Step 结果时，DBOS 直接返回旧响应。检查点和供应商账单分别观察，定位时按 Run ID、Step 与尝试 ID 对照。

### 5. RunClient 为什么仍有必要？

DBOSClient 提供原生入队和控制接口，RunClient 负责项目自己的 owner、Session、HTTP 请求键、指纹、准入以及可重试失败的授权条件。把请求行与入队放在一笔事务，处理 HTTP 响应丢失与重复提交。

### 6. 为什么删除 RunFacts 和阶段占位？

DBOS 的原生状态和 journal 已能判定某个步骤进行到哪里，原生队列有全局及 owner 并发控制。删掉重复的阶段状态和许可之后，数据库仍保留请求、策略、重试授权、模型尝试、受保护操作五类记录。需要追溯历史的旧单元和回执可继续读。

### 7. 两个 Producer 怎么并行和恢复？

`por.research` 以 Run、角色和轮次派生 ID，原生启动两个子 Workflow，先完成的先读，齐备后按固定角色顺序归并。恢复时 DBOS 返回已完成的子结果。中断侧按同一 ID 恢复到自己的检查点。

### 8. State 怎么流动？

初始消息和接纳配置随请求留存。Plan 返回更新的 State 和决策。Brief 冻结后，Research 向各子 Workflow 传入序列化 State 或角色快照。Producer 返回 `ResearchRoleResult`，Review、Risk 和 Strategy 返回对应 patch。父 Workflow 用显式 reducer 合并。

### 9. Brief 如何防止过期或重复审批？

每次编辑产生新版本，根 Workflow 等待带 owner 和版本的原生消息，在消费时再次校验截止时间。AI 修改由请求键派生一个子 Workflow ID，沿用原审批的截止时间。完成修改后，用户看到并决定是否批准新版本。

### 10. 模型调用和工具为什么用不同的收据？

模型调用要记实际次数、用量和跨角色预算。工具取决于实际效果，重复读取可接受，创建任务或调用行为不明的远端工具需要先写操作意图。可重复写入的工具和存储依赖稳定业务键。 [外部调用可靠性](../architecture/agent-loop.md#外部调用可靠性)

### 11. 什么时候不能自动重试？

受保护外部操作、模型尝试或上下文写入存在未确认的效果时，需要查看原生 journal 和权威外部记录。服务仅受理已交付的安全失败后缀，并检查取消意图、失败代次及账本。人工核对收据只解除相关 Session 的隔离。

### 12. 如何控制 Token 成本并避免重复统计？

模型调用前在事务内检查共享预算，记录 `(run_id, id)` 唯一的物理尝试。实际已知用量回填同一行，在前置阶段保留最终报告额度。重放已完成模型 Step 时不会再走 provider 或写新尝试。缺用量时保留已有计数和 `usage_complete=false`，实际金额以供应商账单为准。

### 13. 为什么保留 por.events？

并行角色、根和归档有多个消息来源，浏览器需要一个有序且可续传的序列。`por.events` 是单个 Stream 的写者，把原生消息追加后发布已交付前缀。SSE 和分页从同一个 offset 读取。

### 14. Finalizer 为什么独立运行？

根和子 Workflow 的终态、已发外部调用的结算，以及 PostgreSQL History 归档可在不同时间完成。Finalizer 可以先发布已清算的研究结局，再补偿 History。它也接收用户授权的重试命令并调用原生 rewind。

### 15. 取消与客户端断线有什么区别？

关闭网页结束了读取连接。取消请求写入持久意图，并在同一事务入队 `por.cancel`。DBOS 级联取消，Finalizer 等业务子 Workflow 退出和已发出的请求结算。其间已调用的模型仍可能产生费用。

### 16. DBOS、History 和 RAG 分别存什么？

`por_dbos` 保存运行与恢复记录，`por_domain` 保存业务准入、模型用量、外部操作回执和报告 History。报告 History 由 `web/history.py` 按 owner 与 generation 幂等写入。RAG 只负责企业知识索引与检索，不再有独立的长期 Memory 或 Research Graph。 [数据库设计](../architecture/dbos-deployment.md#数据库设计)

### 17. 如何升级和回滚？

停止新准入，用原版 API 和 Executor 完成或取消、结算剩余工作。备份 journal、业务表与外部存储，在新版本做 `--migration-check`。旧 `units` 安装按明确迁移步骤归档原表。回滚时先排空新版已接纳工作，再使用与旧 journal 匹配的发行版和数据。 [部署与恢复](../architecture/dbos-deployment.md#事件与归档版本切换)

### 18. 你怎么证明恢复有效？

可以用受控模型和工具在真实 Executor 进程内设断点，结束进程后以同 ID 再启动。逐项比对 DBOS 的 Step 结果、provider 请求计数、尝试 ID、外部操作和 SSE 序列。仓库已有这些场景的合同和历史验收记录，当前仍需按清单验证完整的跨阶段故障组合和部署环境。 [故障验收清单](../audits/dbos-architecture-closeout.md#故障验收清单)
