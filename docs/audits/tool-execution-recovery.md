# 第五轮：工具执行层与 DBOS 恢复简化

本轮只改工具声明、装配、执行和恢复这四层：删掉装配阶段与 Runtime 之间重复的校验，把 `ToolExecutor` 的主流程收敛成一条可读链，并按工具 `effect` 明确「已提交 / 未提交 Step」的恢复语义。执行仍属 DBOS，准入与次数仍属 PostgreSQL，业务结果仍在 State。

## 0. 本轮不动的东西

ResearchWorkflow、PlanWorkflow、报告节点、两个研究 Agent 的角色契约、Review / Risk / Strategy、模型账本与 Token 预算、State、上下文压缩、前端、RAG 数据中台、评测与部署架构均未改动。三张调用记录表（`model_attempts` / `tool_calls` / `operations`）的结构和口径不变，本轮没有数据库 Schema 变更。

## 1. 工具执行链

改前：

```text
业务函数
  → @business_tool(domain, effect)
  → SDK FunctionTool + BusinessTool
  → ToolManager.discover()      按角色领域筛选
  → ToolManager.index()         再次校验类型 / 重名 / 领域
  → ToolManager.bind()
  → SDK Agent 调用
  → ToolExecutor.invoke() → _tool_step() → perform()
  → _authorize / _admit / execute / _settle_call
  → DBOS por.agent_tool
```

改后：

```text
业务函数
  → @business_tool(domain, effect)     声明只写一次
  → SDK FunctionTool（官方 Schema、校验、默认值）
  → ToolManager.discover()             唯一的装配准入：类型 / 重名 / 领域 / 能力就绪
  → ToolManager.bind()                 只替换调用回调
  → SDK Agent
  → ToolExecutor.invoke() → _step() → _perform()
        _authorize   当前领域复检（执行边界）
        _admit       预算准入 + por_domain.tool_calls
        execute      受保护操作登记 + 业务执行 + 用量/回执结算
        _settle_call 账本终态
  → DBOS por.agent_tool（已提交结果即恢复权威）
```

`ToolManager` 现在只有两个方法：`discover()` 返回已准入的 `list[BusinessTool]`，`bind()` 返回绑定本次 Executor 的 `FunctionTool`。`SDKAgentRuntime` 不再调用 `index()`，只把已准入列表转成执行器需要的按名索引。

## 2. 真正删除的重复

| 位置 | 删除内容 | 为什么它确实是重复的 |
| --- | --- | --- |
| `tools/manager.py` | `ToolManager.index()` | 与 `discover()` 在**同一份列表**上重复做类型、重名、领域三项校验；`SDKAgentRuntime.run` 每次 Run 都再跑一遍。类型校验前移到 `_declared()`，只做一次。 |
| `tools/manager.py` | `discover()` 的 `tool_name()` 兼容分支 | 装配边界只接受 `BusinessTool`，不再需要从 dict 里取 `name`。 |
| `runtime/sdk_runtime.py` | `manager.index(self.tools)` | Runtime 只需要按名索引，不需要第二次准入判断。 |
| `runtime/tool_executor.py` | `_increment()` | 一行包装，两处调用都改成直接 `budget_from_tool_calls`。 |
| `runtime/tool_executor.py` | `_admit` 中两段相同的预算拒绝代码 | 合并为 `_skip()`，两处早退共用一条降级说明。 |
| `runtime/tool_executor.py` | `_tool_step` 的 7 参数穿参 | 改为 `_Invocation` 参数对象：Step、恢复、排空三条路径共享一份调用事实。 |
| `runtime/business_agent.py` | `_tool_catalog` 的非 `BusinessTool` 分支 | 声明边界已拒绝非声明工具，分支不可达。 |
| `tools/registry.py` | `get_research_tool_prompt()`（已标弃用） | 静态说明没有生产调用方；四角色只走 `build_dynamic_tool_prompt()`。 |
| `tools/registry.py` | `get_notes_from_tool_calls()`、`get_raw_search_tool()` | 仅经 `utils.py` 再导出，仓库内无调用方。 |
| `search/tavily.py` | `web_search_raw`、`_tavily_search_raw`、`tavily_search_raw` | 原 Research Graph producer 专用；图已退役，无调用方。 |
| `mcp/domain_filter.py` | `get_domain_description()`、`tool_domain_summary()` | 无调用方，仅 `mcp/__init__` 再导出。 |

**保留、且不是重复**的校验与包装：

- `discover()` 的领域过滤是**装配边界**（决定模型能看到什么），`_authorize()` 是**执行边界**（决定被撤销授权的工具不能继续跑）。两者输入相似、时机不同，都保留。
- `_settle_call()` 只写 `tool_calls.status`，不回写工具结果：`por.agent_tool` 提交结果才是权威，账本写失败只告警。
- `bind()` 保留为独立方法：`discover()` 在 `por.role_tools` 内执行，此时本次 Run 的 `ToolExecutor` 还不存在，装配必须晚一步发生。
- `_restore_role_tools()` 的目录比较保留。**审查结论**：无 `effect` 的目录来自 `por.role_tools` 引入之前的运行时；DBOS 只恢复同 `application_version` 的 journal，因此它不是跨版本兼容路径，而是目录围栏的宽松分支。删除它需要同时删除进程级场景 `runner-legacy-catalog`，本轮无法离线验证杀进程测试，故保留并在此登记，留待下一轮处理。

## 3. Tool Call 身份只派生一次

| 身份 | 派生方式 | 存放位置 | 说明 |
| --- | --- | --- | --- |
| SDK Tool Call ID | SDK 生成 | `ObservedToolResult.tool_call_id` | 一次调用的原生身份。 |
| 账本 ID | `digest((workflow_id, call_id))` | `tool_calls.id`、`operations.tool_call_id` | 同一逻辑调用只扣一次额度。 |
| 操作 ID | `digest((workflow_id, tool_name, call_id))` | `operations.id` | 只给 `protected` 工具。 |
| 幂等键 | 同操作 ID | 调用配置 `metadata.idempotency_key` | `protected` 取 Operation ID；`idempotent` 取派生值，不写回执。 |

两个 `digest` 都在 `ToolExecutor.ledger_id()` / `operation_id()` 里，并注明「改变派生会孤立在途 Run 的回执」。本轮没有改派生公式。

## 4. 恢复规则

DBOS 的语义：**已提交的 Step 直接重放；未提交的 Step 可能重新执行**。`por.agent_tool` 只证明「这次调用有结果」，不证明「第三方 API 恰好被调用一次」。

| 状态 | `read` | `idempotent` | `protected` |
| --- | --- | --- | --- |
| Step 已提交 | 重放 journal 结果，不执行业务函数 | 同 `read` | 同 `read` |
| Step 未提交（进程崩溃） | 重新执行；账本只有一行，额度不重复扣；记一条 `budget_warnings` 说明供应商可能收到两次 | 重新执行；调用配置携带稳定幂等键，供支持幂等的服务去重 | 拒绝重跑：`start_operation` 命中已有回执即 `unconfirmed`，Run 停止并要求核对 |
| 外部请求已发出、结果未知 | 允许重跑（同上），风险记入告警 | 允许重跑，风险记入告警 | `operations.outcome='unknown'`，后续任何 Executor 都无法再登记新操作 |
| 工具失败 | 配对错误结果交回模型，状态 `failed` | 同 `read` | 回执写 `unknown`，异常按控制故障传播 |
| 取消 | 排空已启动的同步工具后记 `cancelled` | 同 `read` | 回执写 `not_executed` 或 `unknown`，不冒充成功 |

第 8.3 节列出的八个窗口逐条落到下面的事实上：

1. **Step 已提交、结果未返回 SDK**：恢复时 `restore_tool()` 写回记录，`_report_messages` 为缺失的 `function_call_output` 补齐配对 ToolMessage。
2. **Tool Call 已登记、外部请求未开始**：`read`/`idempotent` 重跑；`protected` 因回执已存在而停止，`finish_operation('not_executed')` 只用于「登记后立刻被取消」的分支。
3. **外部请求已发出、未拿到结果**：`protected` 写 `unknown` 并抛 `unconfirmed`；`read` 只能重跑，风险入告警。
4. **外部请求完成、Step 未提交**：`protected` 回执已是 `returned`，重放仍 fail closed（回执不含响应体）；`read` 重跑。
5. **Operation 已写、Outcome 未定**：`start_operation` 的第一条闸门按 `outcome='unknown'` 拒绝整个 Run 的新外部效果。
6. **结果已提交、局部 Usage 未恢复**：`invoke_step` 返回 `budget`，重放时 `budget.add(saved["budget"])` 只补一次；失败路径由 `exc.budget_usage` 带回。
7. **并发工具中某个失败或被取消**：`gather_owned` 取消兄弟任务并等待其排空，保留全部结果与用量；`_settle_call` 逐调用结算。
8. **同一 Tool Call ID 再次调用**：`_tool_calls` 在途 future 共享第一次结果；`start_tool` 拒绝同一 ID 配不同参数。

**仍然无法消除的窗口**：外部请求已经发出、但 `por.agent_tool` 尚未提交时崩溃（第 3、4 种情况）。要做到「只读 API 完全不重复调用」，必须把真实响应额外落库，且仍存在「响应已到、落库前崩溃」的窗口。本轮按要求不新增结果缓存表或分布式事务，只把风险显式记录为 `budget_warnings`，`protected` 工具则继续 fail closed。

## 5. MCP 与插件

- 入口未变：`MCPServerSpec` → 官方 `MCPServerStdio` / `MCPServerStreamableHttp` → `MCPUtil.to_function_tool()` → `BusinessTool` → `ToolManager` → `ToolExecutor.execute()`。
- `mcp/tool_wrapper.py` 没有新增包装：仍只替换 `on_invoke_tool`，每次调用新建连接。因为我们替换了 SDK 回调，SDK 自带的参数校验不会运行，所以本地在声明期 `check_schema`、调用期 `validate`，保持「先验参、后登记」的顺序。
- 服务方 `annotations` 仍不能覆盖 `MCPServerSpec` 里的 `domain` / `effect`。
- 插件仍只追加自身声明的 MCP 服务与 `load_skill`；`load_skill` 是普通 `read` 工具。
- 业务 Agent 仍不使用 SDK 原生 `Agent.mcp_servers`，避免绕过预算、`por.agent_tool` 与回执。

## 6. 测试

本轮只跑离线定向用例，未运行 PostgreSQL 合同测试、杀进程恢复测试，也未调用真实模型、Tavily 或 MCP。

| 范围 | 用例 |
| --- | --- |
| 声明与 SDK Schema | `test_business_tools.py` 前 4 例：Schema/默认值/`ToolContext` 首参/参数错误不登记 Operation |
| 装配隔离 | `test_tool_manager_filters_role_and_plugin_tools`、`test_tool_manager_rejects_duplicate_tool_names`、`test_tool_manager_rejects_hosted_search_declarations`、`test_public_opinion_tool_permissions.py`（12 例） |
| 去重与预算 | `test_duplicate_call_id_runs_the_business_function_once`、`test_tool_budget.py`（5 例）、`test_budget_isolation.py`（2 例） |
| 已提交 Step | `test_committed_step_result_restores_without_rerunning`、`test_native_agent_replay.py`（4 参数） |
| 未提交 Step（新增） | `test_read_tool_reruns_after_an_uncommitted_step_and_warns`、`test_protected_tool_never_reruns_after_an_uncommitted_step`、`test_idempotent_tool_receives_a_stable_idempotency_key` |
| 状态与异常 | `test_tool_control_errors.py`（12 例）、`test_call_ledger.py`（14 例） |
| MCP | `test_mcp_sdk_tools.py`（15 例），含取消排空与 read/protected 回执 |

以上 11 个模块合计 145 例通过。

`test_sdk_runtime.py::test_duplicate_business_tool_names_are_rejected_before_execution` 随 Runtime 的重复校验一起移除；同一条断言由 `test_business_tools.py::test_tool_manager_rejects_duplicate_tool_names` 在装配边界继续保证。

## 7. 版本与迁移

`VERSION` 保持 `por-call-ledger-v1`。本轮没有改 `por.agent_tool` 的 Step 名称、边界、持久化载荷、命令顺序或恢复协议，也没有改数据库结构，因此不升级 Application Version、不需要 `--migration-check`、不需要排空在途 Run。

两处行为变化需要知会运维：

1. 工具调用账本的准入返回值从布尔改为 `admitted` / `duplicate` / `rejected`，`duplicate` 会生成一条 `budget_warnings`。表结构与 `status` 取值未变。
2. 声明边界现在拒绝非 `BusinessTool` 的托管检索声明，失败点从 Runtime 前移到装配阶段，错误信息不变。

## 8. 交付状态

分支、基线 Commit、最终 Commit 与 TODO 见 PR 描述；Draft、未合并，工作目录无脚本或临时文件，无 Worktree。
