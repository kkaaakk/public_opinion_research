# POR Manager Agent 的并发、清算与恢复验证

2026-11-28 · 架构决策状态为建议

推荐方案 B。Manager 使用官方 `DBOSRunner` 调用原生 `Agent.as_tool()`，每次子 Agent 调用持有自己的 `SDKContextHarness` 和 `ToolExecutor`。复用现有的有界清算与持久化账本。SDK 已经负责工具调度、错误结果生成和首轮取消，应用侧只需处理到退出边界仍未收尾的工作，以及预算和外部效果的确认。

有两个决定迁移边界的实测结果。Manager 被取消时，两条子 Agent 调用和两条业务工具调用仍可能在途。子 Agent 的整体 Step 未提交时，DBOS 恢复会重新从第一轮模型调用开始，连崩溃前已返回的业务工具也会再次执行。已提交的子 Step 直接返回结果，跳过 SDK `Usage` 累加与子 Hooks。

## 基线与实验环境

| 项目 | 实际值 |
| --- | --- |
| Git 基线 | 2026-11-28 查询 GitHub `refs/heads/main`，与本地 `origin/main` 一致。完整 SHA `f57c736012c1e3a8aed38c17ccfde3a9f11afeb8`。实验 Worktree 从该 SHA 创建，分支 `codex/manager-agent-settlement`。[合并提交](https://github.com/kkaaakk/public_opinion_research/commit/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8) |
| 锁文件与运行版本 | `openai-agents==0.24.0`、`dbos==3.3.0`（项目启用 `otel` extra）、`dbos-openai-agents==0.3.0`。三项通过 `uv.lock` 和 `importlib.metadata.version()` 交叉确认。传递依赖 `openai==3.31.0` 用于生成流式事件模型。[uv.lock](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/uv.lock) · [Agents SDK](https://pypi.org/project/openai-agents/0.24.0/) · [DBOS](https://pypi.org/project/dbos/3.3.0/) · [DBOSRunner](https://pypi.org/project/dbos-openai-agents/0.3.0/) |
| 运行环境 | Windows，CPython 3.11.9，使用主工作区现有的根 `.venv` 从实验 Worktree 执行。DBOS 用独立容器中的 loopback PostgreSQL 17.6 和临时 SQLite 3.45.1 各跑一遍。DBOS 主实验采用 POR 的 `RunJSON`，另有默认序列化器用例。 |
| 输入与观测 | 所有模型均为 `ScriptedModel`。同批任务在各自到达 `tool-start` 后才释放或触发取消。日志记录调用 ID、进程 ID、事件次序、工具体与 Agent Tool 的在途快照、SDK 额外 Task 数量及原生 DBOS Step。测试代码对 trace 与实际 journal 作断言。模型、Apify 和外部 MCP 请求为零。 |

```mermaid
flowchart TD
    M[Manager Workflow] --> D[DBOSRunner 调用原生 Runner]
    D --> P[Public Agent Tool Step]
    D --> K[Knowledge Agent Tool Step]
    P --> PR[原生 Runner]
    K --> KR[原生 Runner]
    PR --> PB[slow_tool / failure_tool / normal_tool]
    KR --> KB[slow_tool]
```

## SDK 0.24.0 源码调用链

### Agent Tool 的生成与上下文

| 问题 | 源码确认 |
| --- | --- |
| FunctionTool 的生成 | `Agent.as_tool()` 创建默认 `{input: str}` 或指定 dataclass/Pydantic 参数 Schema。内部 `_run_agent_impl` 解析 JSON、验证参数、生成子输入。`_build_wrapped_function_tool` 把它封装为原生 `FunctionTool.on_invoke_tool` 并附上 `AGENT_AS_TOOL` 来源，原调用会处理输出提取与子审批关联。[agent.py 606–789](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L606-L789) · [agent.py 1060–1127](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L1060-L1127) · [tool.py 742–800](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/tool.py#L742-L800) |
| 启动子循环 | `_run_agent_impl` 将模型生成的子任务传给 `Runner.run()`。指定 `on_stream` 则使用 `Runner.run_streamed()`、事件生产者和回调消费者。两个入口均经 SDK 的 `get_default_agent_runner()`。`max_turns`、`hooks`、`session`、`previous_response_id` 和 `conversation_id` 由 `as_tool()` 的选项显式传入。子 `RunConfig` 缺省继承外层 `ToolContext.run_config`。外层模型 override 也会继承，除非显式提供子配置。运行级 `RunHooks` 由子调用参数指定，Agent 自身的 `AgentHooks` 仍由子 Agent 配置。[agent.py 755–789](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L755-L789) · [agent.py 921–1059](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L921-L1059) · [run.py 242–352](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run.py#L242-L352) · [turn_preparation.py 134–143](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/turn_preparation.py#L134-L143) |
| 共享关系 | 标准 Runner 路径会为每个 Agent Tool 调用创建新的子 `ToolContext`。它引用外层 `.context` 的同一个业务对象和同一个 `.usage` 对象。`ensure_context_wrapper()` 保留传入的 wrapper。子循环有各自的输入、响应与工具调用记录，同一 `Agent` 实例的模型对象、配置、工具对象和显式传入的 Session 仍可以共享。修改共享的工具列表元素或工具对象不会产生调用级副本。[agent.py 756–789](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L756-L789) · [agent_runner_helpers.py 377–383](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/agent_runner_helpers.py#L377-L383) · [lifecycle.py 37–58](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/lifecycle.py#L37-L58) |
| 调用标识 | 外层 `ToolContext.tool_call_id` 在调用入口可读，子 `RunResult.agent_tool_invocation` 暴露相同 ID、工具名和参数。子业务 `ToolContext.tool_call_id` 对应它自己的工具调用。SDK 新运行用 UUID 初始化私有 `agent_tool_state` 命名空间，服务于嵌套审批及结果关联。公共、可持久化的业务作用域需要应用把外层调用 ID 与所属 Run/Workflow 一起传递。两个独立子循环内的工具 ID 可以相同。[ToolContext](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/tool_context.py#L43-L65) · [RunResult.agent_tool_invocation](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/result.py#L520-L535) · [run.py 698–711](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run.py#L698-L711) · [agent_tool_state.py](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent_tool_state.py#L32-L67) |
| 包装器参数 | SDK 检查 `on_invoke_tool` 的首参注解。明确使用 `ToolContext` 会保留调用元数据。明确使用基础 `RunContextWrapper` 会让 SDK 传递一个只带基础上下文的副本。实验用 `dataclasses.replace` 替换调用回调，将原 `ToolContext` 和原参数字符串传给 SDK 生成的函数。[tool.py 2221–2252](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/tool.py#L2221-L2252) |

### 并发、失败与 Hooks

`Runner.run → AgentRunner._run_impl → run_single_turn → get_single_step_result_from_response → execute_tools_and_side_effects → _execute_tool_plan → execute_function_tool_calls → _FunctionToolBatchExecutor`。Manager 的 Agent Tool 和子 Agent 的 FunctionTool 使用相同的批处理实现。工具类别的并发用 `gather_with_cancel`，同批 FunctionTool 用 `create_task`、`pending_tasks` 和 `asyncio.wait(FIRST_COMPLETED)`，每个工具另有调用及后处理 Task。成功时等批次完成再返回，并保留模型给出的结果顺序。SDK 在内部跟踪 Task，公开入口没有业务工具 Task 的逐笔清算接口。 [run.py](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run.py#L1789-L1910) · [tool_planning.py](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_planning.py#L951-L1081) · [tool_execution.py](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L1627-L1719)

| 行为 | 源码确认 |
| --- | --- |
| 普通错误与控制错误 | `_FailureHandlingFunctionToolInvoker.__call__` 捕获 `Exception`。配置的 `failure_error_function` 产生错误结果，普通默认反馈为固定提示。`None` 使错误逃出回调。`_run_single_tool()` 对 `AgentsException` 原样抛出，把其他 `Exception` 包为 `UserError` 并保留 `__cause__`。`as_tool()` 外层也有独立错误转换。SDK 会按转换配置处理 POR 控制异常，POR 的错误分类需显式保留。[tool.py 697–708](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/tool.py#L697-L708) · [tool.py 1997–2041](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/tool.py#L1997-L2041) · [tool_execution.py 1847–1862](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L1847-L1862) |
| 同批硬失败 | `_raise_failure_after_draining_siblings` 取消还在执行调用体的兄弟 Task。已进入后处理阶段的 Task 有单独等待窗口。取消等待上限为 0.25 秒及 64 次即时推进，后处理上限为 0.1 秒。等待可提前结束。其余 Task 挂回调消费结果或报告异常，随后抛出所选失败。未启动的排队调用不会补入该失败批次。[tool_execution.py 172–175](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L172-L175) · [tool_execution.py 494–551](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L494-L551) · [tool_execution.py 1672–1758](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L1672-L1758) |
| 外层取消与工具自行取消 | `_cancel_pending_tasks_for_parent_cancellation()` 发取消并挂回调。`_await_invoke_task()` 使用 `shield` 转发到调用 Task，在外层取消分支允许未完成的调用继续收尾。未被标为取消收尾的工具局部 `CancelledError` 另经工具错误转换器，配置默认转换时可以变成工具结果。[tool_execution.py 1760–1790](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L1760-L1790) · [tool_execution.py 2089–2139](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L2089-L2139) · [tool_execution.py 2201–2236](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L2201-L2236) |
| 生命周期结束回调 | `on_agent_end` 和 Agent 的 `on_end` 在生成最终输出的路径调用。`on_tool_end` 在成功输出或已转换的错误输出流经后处理后调用。取消、异常逃逸或审批中断可能绕过它。Runner 的资源 `finally` 覆盖该函数持有的模型及相关资源，业务调用和回执需要各自的清算边界。[turn_resolution.py 353–410](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/turn_resolution.py#L353-L410) · [tool_execution.py 2131–2199](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_internal/tool_execution.py#L2131-L2199) · [run.py 2443–2468](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run.py#L2443-L2468) |
| 流式结束 | `Runner.run_streamed` 维护后台 Run Loop。`stream_events()` 读取结果并处理 loop 异常。`cancel(mode="immediate")` 取消后台任务并结束事件流，工具批次仍走原有取消路径。`after_turn` 允许当前轮完成并在下一轮之前停止。`as_tool(on_stream=...)` 额外管理事件派发队列，在失败时取消子流并关闭迭代器。两种流式退出都要按实验观测在途业务任务。[result.py 941–985](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/result.py#L941-L985) · [result.py 1005–1150](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/result.py#L1005-L1150) · [agent.py 947–1042](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/agent.py#L947-L1042) |

## 场景 1–9 的 SDK 结果

29 项离线断言通过。表中“实验验证失败”表示所列保障被反例推翻，该反例由脚本断言确认。在途数量来自标明的退出快照，后续屏障释放由实验驱动程序负责。

| 场景与用例前缀 | 状态 | 观测结果 |
| --- | --- | --- |
| 1 `01-normal-parallel` | 实验验证通过 | Public 的 `slow_tool` 与 `normal_tool` 同时到达开始屏障。先放行 `normal_tool` 后 Runner 仍在等待。两工具结束后才有 Public 最终输出，Manager 退出时业务在途数和额外 Task 数均为 0。 |
| 2 `02-ordinary-default` | 实验验证通过 | 普通异常变成错误工具结果，失败调用有 `on_tool_end`。兄弟 `slow_tool` 维持正常执行，放行后 Public 发起下一轮并正常结束。 |
| 2 `02-ordinary-propagated` | 实验验证通过 | 禁用转换后，Manager 得到 `UserError → OrdinaryFailure`。兄弟收到取消，普通 `finally` 完成后退出，样本在途数 0。未调用失败工具或被取消工具的 `on_tool_end`，Public 的 `on_agent_end` 缺席。 |
| 2 `02-bounded-failure-drain` | 实验验证失败 | “硬失败返回前所有兄弟工作都结束”被推翻。让慢工具在 `finally` 等屏障，Runner 抛出时仍有 1 条业务调用和 2 个额外 Task。 |
| 2 `02-bounded-post-invoke-hook` | 实验验证失败 | “硬失败返回前已开始的结束 Hook 都完成”被推翻。业务工具体已退出，受屏障控制的 `on_tool_end` 仍在运行。此时业务在途数 0，额外 Task 数 3。 |
| 3 `03-control-default` / `03-control-propagated` | 实验验证失败 / 通过 | 默认转换吞下模拟 `BudgetStop`，Public 在同批慢工具结束后成功发起 `after-error`。两层关闭转换后得到 `UserError → BudgetStop`，取消同批慢工具且无下一轮工具。 |
| 4 `04-child-cancel` | 实验验证失败 | 直接取消 Public 的 `on_invoke_tool` 调用 Task。Manager 得到 `CancelledError`，两条业务调用接到取消并进入 `finally`。Runner 退出时两条仍在等待，无子 Agent 的 `on_agent_end`。 |
| 5 `05-manager-failure-default` | 实验验证通过 | Public 的内部原因为普通异常，Public 没有最终输出。外层 `as_tool` 默认将子错误作为结果交给 Manager，Knowledge 继续正常执行。Manager 等 Knowledge 后成功结束。 |
| 5 `05-manager-failure-propagated` / `05-streamed-child-failure` | 实验验证失败 | 外层关闭转换时，Manager 传播 Public 的 `UserError`。SDK 请求取消 Knowledge。Manager 抛出时 Knowledge 的 Agent Tool 已退出，其业务工具仍有 1 条在途。子 Agent 使用 `on_stream` 时复现同样的业务尾部。 |
| 6 `06-manager-cancel` / `06-streamed-child-cancel` | 实验验证失败 | Manager `Task.cancel()` 后，两条子调用和两条业务调用收到取消。Runner 退出快照中两类各有 2 条在途，随后子包装器进入 `finally`，业务 `finally` 保持挂起直到放行。无最终输出 Hooks。子 Agent 流式时同样复现。 |
| 7 `07-duplicate-shared` | 实验验证通过 | 同一 Public 实例的两次并行调用拥有不同子 wrapper 和响应列表，每边各 2 条响应，外层 ID 为 `call-left` 与 `call-right`。两边都能执行名为 `inner` 的内层调用 ID。底层业务对象、SDK `Usage` 和继承的配置共享，Manager 聚合 6 次模型请求。 |
| 7 `07-duplicate-contained-*` / `07-duplicate-propagated-*` | 实验验证通过 | 外层默认转换时，左侧普通异常或工具局部取消产生错误工具结果，右侧持续运行并成功结束，Manager 聚合 5 次请求。外层关闭转换时，两种失败都退出 Manager 并取消右侧。调用级状态隔离和失效传播是独立行为。 |
| 7 `07-duplicate-isolated` | 实验验证通过 | 包装器只在各自的外层 `ToolContext` 上暂换业务对象，并在 `finally` 恢复。左右两份字典同时持有各自的 `inner` 记录，无覆盖。Manager 的 `on_tool_end` 仍看到原业务对象，三方共享原生 Usage。这里使用模拟业务对象。 |
| 8 `08-remote-survives` | 实验验证失败 | “本地工具取消会停止已发出的远端动作”被推翻。模型中的供应商任务独立于工具 Task。工具和 Agent Tool 均已退出时，模拟远端任务仍在途，放行后使用同一模拟回执完成。真实供应商行为留给 Operations 核对。 |
| 9 `09-*-valid` / `09-*-invalid` | 实验验证通过 | 原样委托 `on_invoke_tool`。有无包装器的有效输入均保留结构化 `{task, edition}`、输出提取、子配置过滤器、Hooks 和共享 SDK Usage。非法 `edition` 均产生 `ModelBehaviorError` 且子循环未启动，已进入的包装器仍触发 `finally`。 |
| 9 `09-inherited-config-separate-hooks` | 实验验证通过 | 缺省子配置时，父配置的过滤器在父子两边执行。省略子 Hooks 时，父 Hooks 只记录 Manager 事件及其 Agent Tool 调用，子运行的 Usage 仍进入同一对象。 |
| 9 `09-wrapper-second-cancel` | 实验验证失败 | `try/finally` 覆盖已进入的原 `as_tool` 回调，包括参数、运行与结果提取。正常、异常及取消路径均观测到进入 `finally`。在异步清理等待屏障时再次取消同一 Task，清理被打断。已脱离 Runner 的孙任务可以比包装器活得更久。 |
| 流式 `stream-manager-failure` / `stream-manager-immediate` / `stream-manager-after-turn` | 实验验证通过 | 失败经 `stream_events()` 抛出，退出时 1 条业务任务在途。立即取消的事件迭代正常结束，退出时 2 条业务任务仍在收尾。`after_turn` 允许当前两条工具正常返回，Manager 停在第 1 轮，无在途业务调用。 |

## 场景 10 的 DBOS 结果

Manager Workflow 调 `DBOSRunner.run()`。它克隆 Manager、替换 model provider、用 `_model_call_step` 保存 Manager 模型响应，并通过 `Turnstile` 依模型返回的 call ID 顺序开放 Step 入口。它在调用每个工具前就放行下一个入口，所以 Step body 仍并行。原生 FunctionTool 批处理保持 SDK 的 Task 管理。子 Agent 传具体 `Model` 和显式 `RunConfig`，通过 `DBOS.run_step_async` 包住原 `as_tool` 回调。Step 内调用原生 `Runner`，journal 中的子执行粒度为 `lab.public_agent` 与 `lab.knowledge_agent`。`DBOSRunner 0.3.0` 的 Model Wrapper 不支持流式耐久模型调用。[DBOSRunner 0.3.0 的 runner.py](https://github.com/dbos-inc/dbos-openai-agents/blob/0.3.0/dbos_openai_agents/runner.py) · [DBOS run_step_async](https://github.com/dbos-inc/dbos-transact-py/blob/3.3.0/dbos/_core.py#L2440-L2472)

PostgreSQL 与 SQLite 各有 8 项离线断言通过。步骤序号来自 DBOS 公共 journal 查询。

| 用例 | 状态 | PostgreSQL 和 SQLite 的实际行为 |
| --- | --- | --- |
| `normal` | 实验验证通过 | Public 与 Knowledge Step 同时在运行。Workflow `SUCCESS`，journal 依次是 Manager 模型 `#1`、Public `#2`、Knowledge `#3`、Manager 模型 `#4`。Usage 为 6 次请求，退出时在途数 0。 |
| `exception` | 实验验证通过 | `RunJSON` 让 Public 保存错误 Step `#2`，Workflow `ERROR`。实时调用方得到 `UserError → OrdinaryFailure`。Knowledge 收到取消，仍有 1 条业务工具在清理。取消的 Knowledge Step 无已提交结果。 |
| `control-error` | 实验验证失败 | “直接持久化 SDK 异常会保留 POR 控制语义”被推翻。实时 `UserError.__cause__` 是 POR `ExecutionControlError(unknown_outcome=True)`，`is_execution_control_failure` 将其识别为控制失败。读回 `RunJSON` 的 Public 错误 Step 得到 `RuntimeError("UserError")`，控制类型和 `unknown_outcome` 均丢失。 |
| `python-cancel` | 实验验证失败 | “取消本地 Task 会让 DBOS journal 自动进入 `CANCELLED` 并等待下层清理”被推翻。调用方收到 `CancelledError` 后 Workflow 仍为 `PENDING`，仅 Manager `#1` 已提交。等到两个 Step 的 `finally` 均运行以后，仍有 2 条业务工具卡在 `finally`。测试随后关闭这条实验 journal。 |
| `dbos-preemptible` | 实验验证失败 | `DBOS.cancel_workflow_async()` 使 Workflow `CANCELLED`，`preemptible=True` 使活跃子 Step 收到取消。调用方得到 `DBOSAwaitedWorkflowCancelledError`，两个业务工具仍可在清理。被抢占的子 Step 没有结果行。 |
| `dbos-nonpreemptible` | 实验验证通过 | `preemptible=False` 时 journal 已 `CANCELLED`，仍有 2 条子调用和 2 条工具体在运行。显式放行后两个业务工具均正常返回，调用方最终得到 `DBOSAwaitedWorkflowCancelledError`。样本中子 Step 没有结果行。 |
| `crash-recovery` | 实验验证通过 | 第一进程在屏障后 `os._exit(73)`，未运行 Public 的工具和 Step `finally`。重开同一 journal 和 Workflow ID，DBOS 复用 `#1` 和 `#3`，重跑 `#2`。第二进程完成 `#4`，最终 `SUCCESS`。详见下文。 |
| `default-serializer-error` | 实验验证失败 | DBOS 默认序列化器尝试 pickle 带 `run_data` 的 SDK 异常，其中引用工具闭包。调用方得到 `AttributeError`，Public 缺少已提交的错误 Step。Workflow 为 `ERROR`。POR 主路径使用 `RunJSON` 避开这一序列化故障。 |

恢复前的 journal 为 `#1 ManagerModel` 和 `#3 Knowledge`。Public 的第一个模型响应已生成，一次 `normal_tool` 已结束，另一次 `slow_tool` 在等待。确认 `#3` 提交后，独立子进程执行 `os._exit(73)`，跳过 Python 栈展开、事件循环收尾和 `DBOS.destroy()`。恢复进程使用同一 application version，接手此前唯一 Executor 的 journal。原 `#1` 和 `#3` 记录逐字段相同。Manager 仍看到 Knowledge 输出，并执行 Manager 的 `on_tool_end`。Knowledge 的模型、业务工具、子 Hooks 与包装器都没有重跑。Public 从模型首轮重新执行，已经返回的 `normal_tool` 再执行一次。实验故意让其内层 call ID 在两个进程不同，用于验证旧 ID 无法代表这次重复。 [DBOS 3.3.0 的恢复实现](https://github.com/dbos-inc/dbos-transact-py/blob/3.3.0/dbos/_recovery.py#L12-L68) · [Step 读写与取消](https://github.com/dbos-inc/dbos-transact-py/blob/3.3.0/dbos/_core.py#L2292-L2356)

两进程合计 7 次实际模拟请求，无中断时为 6 次。恢复结果的共享 SDK Usage 为 4 次。它包括重放的 Manager 首响应、恢复后 Manager 的最后一轮和 Public 两轮，也缺失直接复用 Knowledge 字符串时的两次子模型请求。该数值的变化与 Hook 是否重放有关。POR 的物理用量继续以 `model_attempts` 和 `tool_calls` 为准。若下一步确需从 Manager 向 SDK 层展示恢复用量，应显式保存并逐次投影子用量差值。并发子循环共享总 `Usage`，直接取两个总量快照相减会计入其他调用。 [POR 模型账本](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/budget.py#L73-L113) · [POR RunJSON](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/web/run_data.py#L65-L153)

## POR 现有清算机制审查

主分支中，`ToolExecutor` 持有 `_tool_calls` 和 `drain()`。`_closing`、`settle()`、`_settle_owned()`、`BatchSettlement`、`last_settlement` 归 `SDKContextHarness`。`SDKAgentRuntime.run()` 每次构造一套 Harness、Executor 和绑定后的 Agent，最后由 Harness 在 Run 退出时清算。 [SDKAgentRuntime.run](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_runtime.py#L240-L312) · [工具执行恢复审计](tool-execution-recovery.md)

| 当前组件 | 原职责与新架构下的需求 | SDK 保障和处理建议 |
| --- | --- | --- |
| `ToolExecutor._tool_calls` | 每个 Executor 以 call ID 记首调用完成的 Future。重复 ID 共享首调用结果，`drain` 观察已经实际进入执行器的调用。相同内层 ID 出现在两个子循环时，实例混用会串记录。 | SDK 跟踪调度 Task，范围限于它自己的批处理与调用账本。保留现有调用级 Future 及同调用去重，随子调用隔离。场景 2、4、6、7 支持这一决定。[ToolExecutor.invoke](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L147-L241) |
| `SDKContextHarness._closing` | 建立退出栅栏。关闭后新观察但未进入执行的记录生成为终态，执行器在 `_perform` 的准入前检查。已在准入或回执写入中的调用仍需收尾。 | 保留本地栅栏。批次选择失败后停止补入队列无法替代 POR 的准入、终态及所有权约定。场景 4 和 6 说明退出与子调用结束可分离。[start_tool 与 close_unexecuted](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_context.py#L228-L294) · [_perform](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L268-L327) |
| `ToolExecutor.drain(timeout)` | 对未完成的业务 Future 做有界等待。超时报告仍在途，留给原所有者。 | 保留并直接复用。SDK 同批有界等待覆盖调度路径。场景 2、4、5、6 和 10 均出现业务收尾跨越 SDK 或 Step 退出的样本。成功路径中 Future 已全部完成时，现有实现直接返回。[drain](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L213-L219) |
| `SDKContextHarness.settle()` | 关准入、调用 `drain`、结束未执行的记录，报告仍运行的 call ID、timeout 或其他错误类型。它处理在进程内的观察数据。 | 保留。SDK 的输出 Hooks 和 task callbacks 没有 POR 账本状态。正常和异常入口可在迁移时复用同一个退出调用，避免在 Manager 再实现同样的逐工具逻辑。[settle](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_context.py#L674-L724) |
| `_settle_owned()` | 创建独立清算 Task，通过 `shield` 等待并在完成后恢复取消，对重复取消保有清算所有权。 | 保留。场景 9 证明单层 `finally` 内的 `await` 可被再次取消。仍以现有等待上限决定是否能宣告已清算。[_settle_owned](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_context.py#L726-L741) |
| `BatchSettlement` | 就地记录 `reason`、`in_flight_call_ids`、`error_type`。`succeeded` 需要调用全终态并且没有清算错误。 | 保留可诊断结果。Manager 只需要每次子调用的结果或未决状态，可直接消费这个合同。场景 2 的后处理反例也说明 SDK Task 数量无法作为 POR 账本完成条件。[BatchSettlement](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_context.py#L99-L109) |
| `last_settlement` | 保存最新的本地结果。`SDKAgentRuntime` 在 `_ReasoningStopped` / `MaxTurnsExceeded` 后仅在清算成功时提取部分产物，异常还携带 `context_settlement` 和 `context_harness`。 | 当前保留。后续如果子包装器直接传递一次清算结构并覆盖现有消费者，可去掉可变镜像字段。Runtime 的本地字段不充当 journal 或 Finalizer 的执行依据。[SDKAgentRuntime.run](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_runtime.py#L310-L354) |
| 异常传播与有主 I/O | `@business_tool` 关闭默认 SDK 转换，`ToolExecutor` 把可降级普通错误变成工具结果，透传预算、SQL、DBOS 与控制错误。`is_execution_control_failure()` 检查异常组和原因。`finish_owned` / `owned_io` 保住已发出写入，某些同步工具 `drain_on_cancel` 等线程返回。 | 保留业务分类与回执写入所有权。Agent Tool 外层关闭默认转换，或通过能验证的窄格式化器仅转换普通失败。用保留控制类型的方式提交错误，场景 3、9、10 给出了默认转换、二次取消和序列化反例。[business_tool](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/tools/__init__.py#L72-L123) · [异常链与有主 I/O](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/workflow/execution.py#L76-L125) · [finish_owned](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/workflow/execution.py#L248-L264) |
| 持久化工具和模型账本 | `admit_tool_call()` 在 Run 级事务内核算配额并对同身份去重，终态不退款。模型实际尝试和 Token 有独立账本。当前工具账本 ID 为 `digest((DBOS.workflow_id, call_id))`，按角色 Workflow 分割调用命名空间。 | 保留 Run 级共同额度。切为同一个 Manager Workflow 后应验证子调用命名空间、模型物理尝试身份和原调用账本的兼容性。DBOS Step 复用和共享 SDK Usage 都无法代替物理账本，见场景 7 和 10。[工具账本](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/workflow/execution.py#L435-L510) · [调用身份](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L387-L402) |
| Operations 外部副作用保护 | 发调用前检查 Run 和 Executor 所有权并写 `operations`。记录 Actor Run ID 时仅查原 Apify Run。未知且无可用回执时停止，只有能证明未开始才允许重新发起。Finalizer 以原生 journal 和账本核对结果。 | 保留。场景 8 区分本地取消和远端状态。场景 10 表明重新生成内层 call ID 后，原身份保护不足以覆盖全部重跑。现有同输入未确认操作拦截有价值，整段恢复仍应在子循环开始前核对未决效果。[开始操作及 Actor 回执](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/workflow/execution.py#L342-L433) · [当前持久化和恢复政策](../architecture/dbos-deployment.md#故障恢复和用户重试) |
| `por.agent_tool` 细粒度 Step | 在现在的每角色 Workflow 中保存单条业务工具输出、观察和预算。子整体 Step 内 `native_step_available()` 会返回 false，因此原执行器自然落到 `_perform()`。 | 整体 Step 实验只需去掉嵌套子模型及工具检查点。只有重新执行被安全准入或跳过时，这一粒度才可迁移到正式路径。当前每角色 Workflow 仍靠 `por.agent_tool` 恢复，不宜先删生产逻辑。[native_step_available](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/workflow/execution.py#L53-L60) · [工具 Step 结果](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L221-L266) |

预算拒绝有两种现有语义。工具或搜索配额拒绝走 `_ToolRejected`，当前 SDK 轮次可以得到工具提示。模型硬配额由 `MeteredModel` 转成 `_ReasoningStopped`，当前角色循环结束，且仅在清算成功时可收集部分结果。迁移要保留这两个决定及真正的执行控制错误。 [ToolExecutor._skip](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/tool_executor.py#L338-L341) · [MeteredModel](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/runtime/sdk_runtime.py#L65-L89)

这批证据支持保留表中的本地清算组件。可简化的是父子出口的重复职责，由子 Harness 做逐调用清算，父级只等待子调用。首个可考虑删除的字段是 `last_settlement`，前提是可用一次清算返回值覆盖它的消费者。细粒度子检查点可在整段恢复通过上述业务验证后，从新的执行路径移除。

## 方案比较与建议

| 方案 | 复杂度 | 可靠性与适用条件 | 结论 |
| --- | --- | --- | --- |
| A · 仅依赖 SDK 任务管理 | 低 | 已覆盖正常批次并行与同批取消。同步回执、取消尾部和持久化控制语义仍需 POR 确认。场景 2、4、5、6 和 10 反驳完全替代清算。 | 采用其中的 SDK 调度能力。将 `_closing`、`drain`、`settle` 视为可直接删除的做法缺少证据。 |
| B · 每次子调用独立 ToolExecutor | 低至中 | 以公开 `ToolContext` 携带调用级业务对象，用现有有界清算。全 Run 持久账本仍集中，子实例可并行。场景 7 已验证上下文接线的可行性，正式绑定需带真实 Executor 回归。 | 推荐。Manager 只跟踪已启动的子调用及其清算结果，子级清算复用已有所有者。 |
| C · ExecutionScope 与 ContextVar | 中至高 | 适用于确实无法显式携带业务依赖的入口。需要额外验证上下文继承、同角色并行、取消、重放身份及隔离。它对远端效果或清算期限无额外保障。 | 等 B 的明确失败场景，再按需要评估。 |
| D · SDK 配置与公开入口 | 低 | `ToolContext`、`agent_tool_invocation`、`RunConfig.tool_execution.max_function_tool_concurrency` 和流式 `after_turn` 可用于相关性、限流及协作式停止。限流会降低同时在途数量。`after_turn` 适用于允许完成当前轮的流式入口。 | 作为 B 的补充。强制退出、持久化回执和已启动的远端工作仍由 POR 管理。[ToolExecutionConfig](https://github.com/openai/openai-agents-python/blob/v0.24.0/src/agents/run_config.py#L143-L165) |

### 拟议最小改造

1. 保留原生 `as_tool()`，在调用入口记录所属 Manager Workflow 和外层 `tool_call_id`。在本次 `ToolContext` 上显式传入新建的子 Harness 和 Executor，沿用共享的 Run 级预算及实际成本账本。SDK 的 `Usage` 继续用于当前会话观测。
2. 子 `Agent` 的工具配置保持稳定。当前 `ToolManager.bind()` 把回调静态绑定到一个 Executor，`as_tool(hooks=...)` 也捕获固定对象。可以在装配边界用 `ToolContext.context` 选取本次调用的已有执行器和 Hooks，复用 `ToolManager` 的工具目录与策略。模型输入过滤器同样可以按显式业务 Context 路由。场景 7、9 验证了传参机制，实际 POR 绑定还需回归。 [ToolManager.bind](https://github.com/kkaaakk/public_opinion_research/blob/f57c736012c1e3a8aed38c17ccfde3a9f11afeb8/src/open_deep_research/tools/manager.py#L105-L117)
3. 用最小包装器持有子 Runner、复用子 Harness 的关口与有界清算并恢复父 Context。Manager 退出时对已进入的子调用做一次有界等待，超时从其现有清算结果及 journal/账本保留未决状态。结束 Hook、局部 Future 和 `CANCELLED` journal 只能作为观测输入。逐工具调度和首轮取消交给 SDK。
4. 外层 Agent Tool 保留应当中止的控制异常。若需要持久化这类错误，在子 Step 边界从异常链提取 POR 已支持的确切类型、用量与 `unknown_outcome`，或者显式写入可序列化结构并测试恢复分类。首批集成应能从 journal 和 effect ledger 验证这一合同，再评估是否简化 `last_settlement` 的可变字段。
5. 在启用子整体 Step 自动恢复前，先验证**未提交子 Step 的重新进入**。由已提交的父模型响应提供外层调用身份，避免把重新生成的内层 ID 当作旧调用。已有受保护操作或未确认的效果按当前政策核对，只读查有权威回执的实例，未能确认时保持中断。只在重试合同、实际费用记账、保护操作和取消并存时仍满足约束的入口启用整段重跑。

## 限制与待验证项

| 项目 | 状态 | 说明 |
| --- | --- | --- |
| SDK 私有缓存和 DBOS 源码行为 | 源码确认 | 私有 Agent Tool 审批缓存、`ToolContext` 参数适配、分类错误传播和 DBOS Step 结果读取按版本审查。长期业务身份要使用公共调用信息及 POR 持久事实。 |
| 真实 ToolExecutor 多子调用集成 | 尚未验证 | 隔离用模拟业务对象。真实 Harness Hooks、工具装配、共享账本并发，以及模型与受保护工具的新身份派生需要在正式实现前一起验证。 |
| 真实 Apify、MCP、RAG 和付费模型 | 尚未验证 | 远端模拟只证明子任务不随本地取消结束。供应商取消、读回 Actor Run、ACK 丢失和账单由原有 Operations 及对应集成合同负责。 |
| 审批和流式耐久运行 | 尚未验证 | 使用纯 SDK 流式及非流式子 Agent，DBOS 实验仅运行非流式 Manager 和非流式子 Agent。未复测 SDK `needs_approval`、`RunState` 恢复、共享 SDK Session、MCP 会话退出、事件队列溢出以及后处理中止的其他工具类型。 |
| 分布式恢复和版本升级 | 尚未验证 | 本轮复现同版本独立进程的中断与恢复。跨 Executor 并发恢复、数据库服务中断、进程在副作用回执窗口崩溃、跨版本 journal 切换和 Finalizer 的真实授权走现有单独验收合同。 |

## 复现命令

从实验 Worktree 的仓库根目录执行。脚本会核对三个锁定版本，禁用 tracing 和 dotenv，允许 Python socket 使用 loopback 或本地 Unix socket。`--trace` 把每个观测点追加为一行 JSON，可省略或为每次运行指定不同的文件名。

```powershell
uv sync --extra dev --locked --python 3.11
New-Item -ItemType Directory -Force .tmp/manager-agent-settlement | Out-Null
.venv/Scripts/python.exe scripts/experiments/manager_agent_sdk.py --trace .tmp/manager-agent-settlement/sdk.jsonl
.venv/Scripts/python.exe scripts/experiments/manager_agent_dbos.py --trace .tmp/manager-agent-settlement/dbos-sqlite.jsonl
```

可给纯 SDK 脚本传用例前缀，单独重跑需要的场景。例如 `02`、`07` 或 `stream`。DBOS 实验默认使用临时 SQLite。PostgreSQL 复现使用本机独立容器与实验专用库 `por_manager_sdk_lab`。

```powershell
$container = 'por-manager-sdk-lab'
docker run --rm -d --name $container -p 127.0.0.1:15437:5432 `
  -e POSTGRES_PASSWORD=manager-sdk-lab-only -e POSTGRES_DB=por_manager_sdk_lab postgres:17.6
if ($LASTEXITCODE -ne 0) { throw 'Experiment PostgreSQL could not start' }
try {
  $ready = $false
  for ($attempt = 0; $attempt -lt 30; $attempt++) {
    docker exec $container pg_isready -h 127.0.0.1 -U postgres -d por_manager_sdk_lab
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $ready) { throw 'Experiment PostgreSQL did not become ready' }
  $env:MANAGER_AGENT_LAB_POSTGRES_URL = 'postgresql+psycopg://postgres:manager-sdk-lab-only@127.0.0.1:15437/por_manager_sdk_lab'
  .venv/Scripts/python.exe scripts/experiments/manager_agent_dbos.py --trace .tmp/manager-agent-settlement/dbos-postgres.jsonl
  if ($LASTEXITCODE -ne 0) { throw 'DBOS experiment failed' }
} finally {
  Remove-Item Env:MANAGER_AGENT_LAB_POSTGRES_URL -ErrorAction SilentlyContinue
  docker stop $container
}
```

源码和测试入口见 [SDK 实验](../../scripts/experiments/manager_agent_sdk.py) 与 [DBOS 实验](../../scripts/experiments/manager_agent_dbos.py)。`runner-exit`、`step-finally-seen`、`journal-error`、`process-abort` 和 `journal` 事件可用于核对表中快照及恢复结果。

### 仓库回归

实验 Worktree 的离线 pytest 通过 1,004 项，跳过 2 项，212 项 integration 未选中，输出 98 条警告。Ruff 的仓库 E/F/I 口径、两个脚本的格式检查和 `compileall` 均通过。

```powershell
$env:PYTHON_DOTENV_DISABLED = '1'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/experiments
.venv/Scripts/python.exe -m ruff format --check scripts/experiments
.venv/Scripts/python.exe -m compileall -q src/open_deep_research scripts/experiments
```
