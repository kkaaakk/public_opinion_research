# 业务执行与 Agent Loop

当前架构为 DBOS 3.3.0 + OpenAI Agents SDK 0.24.0 + Research Graph Context Harness。
部署见 [DBOS 部署说明](dbos-deployment.md)，实测范围见 [Phase 5 审计](../audits/openai-agents-phase5.md)。

```text
Web / CLI → FastAPI Run API → RunClient（身份、幂等和资源准入）
  → DBOS por.run → por.plan → 不可变计划审批 → por.research
  → 两个 por.role Producer 并行 → 固定角色顺序汇合
  → Review → 按角色批处理 Gap → 定向补查 → Review
  → Risk → Strategy → 章节写作 → 最终报告 / Memory

por.role → business_step → 原角色函数 → run_business_agent
  → SDKAgentRuntime → SDKContextHarness → Runner.run → 原业务工具
  → Research Graph 持久化回执 → Working Context / 压缩 → 角色报告与费用清算

独立 por.events → DBOS Durable Stream → SSE
独立 por.finalize → History archive / 补偿 / 经授权的安全重试
```

DBOS 管理 Workflow、Step、Queue、持久化、消息和恢复；执行 journal 是权威。
领域事实保留 owner、资源预留、可撤销许可、操作回执、费用和已清算 artifact，
不自行维护任务领取、lease、Workflow Cursor 或另一套调度状态机。

SDK Runner 是四角色唯一的模型/工具循环。`SDKAgentRuntime` 直接拥有角色装配、
报告和私有记忆助手；没有旧 Runtime 父类或失败后 fallback。四个 AgentSpec 的职责、
提示词、工具 allowlist 和研究策略保持原样。

Harness 管理 Evidence/Claim/Finding/Source 溯源、角色私有上下文、工具批次、
Persist-then-Compact、滚动摘要、预算和退出清算。仅已提交并核对回执的证据可压缩。
未知工具效果不自动重放；SDK Session 是私有轨迹记录，不是 Workflow checkpoint。

`workflow/intake.py` 的澄清、brief、章节计划和 `research.py` 的 Review 是有界单次
结构化业务调用；有限结构化解析重试不是 Agent Loop。报告章节、最终报告、Graph
抽取/上下文更新/压缩、检索重写、Vision 和长期记忆同样是单一业务服务调用。
它们继续使用 LangChain messages/model/runnable/callback，费用通过共享预算捕获。
这些调用没有根据工具输出反复选择工具、也不决定跨角色执行顺序。

`state.py` 是普通 TypedDict，包含 `messages`、`workflow`、`agents`、`research`、
`report`、`runtime` 六个域。`workflow/python_state.py` 负责消息规范化、角色快照和
固定顺序的显式 reducer 合并；不需要 LangGraph channels。RAG 知识图与研究证据图
拥有独立存储职责，均不依赖 LangGraph Workflow。

`deep_researcher.py` 仅保留业务函数兼容导出。旧 Graph、Pregel、Send、Command 和
Graph Server Auth 导出已退役；`get_workflow` 只返回明确迁移错误，不能运行研究。
`workflow_backend`、`business_agent_runtime` 输入别名的允许/拒绝策略见部署文档。

核心实现：`web/runs.py`、`web/durable.py`、`workflow/dbos_workflow.py`、
`runtime/sdk_runtime.py`、`runtime/sdk_context.py`、`research_graph/workspace.py`。
没有新增执行引擎、Agent Loop、SQL Worker 或 DBOSRunner 包装层。
