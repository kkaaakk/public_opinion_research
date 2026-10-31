# Agent Execution Studio：事实协议与展示边界

> 2026-10-31 当前研究流程：统一 Plan → 可恢复需求澄清 → Markdown Brief 预览/编辑/版本审批 → 四角色与 Review/Gap → 默认单次综合报告。应用版本为 `dbos-brief-spec-v1`；旧 `dbos-phase5-studio-v1` journals 必须按原发行版排空。章节写作仅由显式 `report_mode="sections"` 启用。合同、前后流程及升级规则见 [Markdown Research Brief](research-brief.md)。本页其他 Phase 5/Studio 版本记录保留为历史说明。

基线：`origin/main dc070a4715f3bb1c29c5bce1cac6dfa5f3676b53`；继续 Draft PR #31，原 head `379209db4630ba3f3f56d2ca3f64dfbc5b252435`。执行架构以 [Phase 5](../audits/openai-agents-phase5.md)、[Phase 4 DBOS](../audits/openai-agents-phase4-dbos.md)和[部署文档](dbos-deployment.md)为准。没有恢复旧 LangGraph、手写 Agent Loop，或新增执行器。

## 1. 所有权与实施顺序

1. 独立审查 PR，再固定实际 API、Stream、SDK Hook、Graph 和 History 能力。
2. 在现有 observer / 领域回执接缝补充安全元数据；所有业务仍走原 Workflow / Step / Runner。
3. 实现严格 DTO、只读订阅器、Live / Replay 共用投影，再接入界面。
4. 验证刷新、故障、授权与清算，执行受控原生链路、浏览器、原有回归及 CI。

| 数据 | 权威 / 来源 | Studio 的职责 |
| --- | --- | --- |
| Run 和执行权 | DBOS `por.run`、原生子 Workflow / Journal | GET 读取；不写第二套 Run 状态机 |
| SDK Loop | `SDKContextHarness` 的原生 RunHooks / Runner 边界 | 展示开始/结束、实际模型轮次、工具身份与结果 |
| 效果与清算 | 既有 `RunFacts`、工具 operation 回执、Finalizer | 区分 returned / uncertain / succeeded；未知效果不重放 |
| 顺序事件 | 单写者 `por.events` → DBOS Durable Stream | 以 seq 重建连续前缀；去重和只读回放 |
| Evidence / Finding / Source | 每 Run 的 Research Graph | 只读 provenance，不与 RAG Knowledge Graph 混用 |
| 报告归档 | Neo4j History | 查询/阅读/导出；不冒充完整 Trace |
| 业务长期记忆 | 原 Memory 服务 | 不读取或呈现 Agent 私有记忆 |

Planning、Intake、Review、报告、抽取和压缩依旧是有界业务服务。Studio 不把它们画成额外 SDK Agent，也不以 SDK spans 推算全部业务模型用量。

## 2. 新增只读接口

所有 `/api/runs/*` 接口沿用 `trusted_owner`、单操作员 Token 和既有 Run owner 检查，响应 `Cache-Control: no-store`。客户端不能传 owner、服务 URL、凭据或不受保护的 Run 身份。

| 接口 | 分页 / 内容 |
| --- | --- |
| `GET /api/runs/{id}` | 原状态、plan、usage、错误类别、清算、报告；增加受限 request 摘要和 `approval_required` |
| `GET /api/runs/{id}/events` | 原 SSE；`Last-Event-ID` 优先于 `after`，只补发，不执行工作 |
| `GET /api/runs/{id}/event-page?after=0&limit=200` | 1–500 条持久事件，`items / next_cursor / has_more`；用于冷回放 |
| `GET /api/runs/{id}/execution` | 公共 `list_workflows(load_input=False, load_output=False)` 与 `list_workflow_steps(load_output=False)`；工作流及命令各自分页 |
| `GET /api/runs/{id}/evidence` | 1–100 个节点，按 node_id keyset 分页；q、kind、node_id、after、edge_after 均受限并参数化 |

`execution` 接受本 Run 根或 `run_id + ':'` 范围内的 workflow_id。返回 ID、函数、状态、queue、parent、版本及原生实际时间，绝不返回 native inputs、outputs、异常原文或 executor 凭据。缺少完成时间并不证明失败，函数命令也不等于一个业务 Agent。

证据只查询现有 `ResearchNode / ResearchEdge`，每个端点、关系两端均限制相同 run_id。列表与相邻关系分别按稳定 ID 分页。只开放 Source/Evidence/Claim/Finding 等节点的选定属性，排除原始工具输出、metadata、query、source_path、原始参数；文本和列表有长度上限。来源 URL 移除 userinfo、query、fragment，仅允许 HTTP(S)。继续应用既有公开出口凭据脱敏。memory Graph 后端是 Executor 进程内数据，Web 不制造另一份空图来伪装持久证据；此时明确返回不可用。

## 3. v1 事件与身份

继续使用 `protocol_version=1`，增加字段/事件，没有 SQL outbox 或游标解释器。终态 `done.expected_events` 声明本代必须交付的 settlement/usage/status 及存在的 report/error；relay 只在真实尾事件齐全后发布 done/history 回执并封闭 Stream，防止原生消息乱序造成缺尾。原 Studio 交付分支使用 `dbos-phase5-studio-v1`。本次 `dbos-brief-spec-v1` 还会等待已提交角色 artifact/error 中列明的 SDK 事件 ID，防止同毫秒乱序丢失模型/工具轨迹；这只是交付回执，不授权业务执行或重放。SDK runtime 仍为 `sdk-phase5-v1`。旧版本的 pending journals 继续由匹配发行版处理，不在新 Executor 重放；旧 Stream 仍可阅读，缺失的新字段明确为未提供。

| 字段 | 含义 |
| --- | --- |
| run_id / attempt_id | 既有业务 Run / 兼容身份；不是 SDK Trace ID |
| seq / timestamp | 原生单 Stream offset+1 / relay 记录时间（UTC） |
| observed_at | 产生公开领域事实时的数据库时间；早于 relay，不能和 relay 延迟混为耗时 |
| generation | relay 的重试代次；不是一次 HTTP 重连 |
| unit_id / step | 原业务单元与步骤名；补查和不同角色各有自己的单元 |
| execution_id | unit + 已有许可的不可逆摘要，只用于区分该单元的执行实例；不开放许可本身，不具备授权能力 |
| invocation_id / task_id / research_round | Harness 的 SDK invocation、研究任务和轮次 |
| model_turn | 已进入 SDK 输入过滤器的原生模型轮次；对应 actual model_started/completed/failed |
| tool_call_id / graph_call_id / operation_id | SDK 本地调用 / Research Graph provenance / 已有外部效果回执身份，彼此不混同 |

seq 是 relay 接收顺序，支持确定性回放；并行任务之间不能据此推断精确的物理开始先后。UI 同时区分 observed_at 与 relay timestamp。

### 事件映射

| 事件 | 展示 |
| --- | --- |
| step_started / step_completed / step_failed | Step 实例开始/完成/失败；同名步骤每次执行独立保留 |
| step_skipped + reason=role_disabled | 原角色函数依既有配置返回空 patch，服务端确认未启用；不标 Agent 完成 |
| sdk_event/session_started | SDK Session 装配；不等同于 Runner 已执行 |
| sdk_event/agent_started / agent_completed / agent_failed | 原生 SDK Agent 生命周期；Runner 结束不等于业务报告或 Run 结束 |
| sdk_event/model_started / model_completed / model_failed | 按 invocation + model_turn 配对；只记录实际响应 usage，不累加 RunResult usage |
| sdk_event/tool | 原有效果回执：started、returned、not_executed、uncertain；returned 只证明 invoker 返回 |
| sdk_event/tool_result | Harness 明确的 succeeded / failed / cancelled / unobserved；不从工具文本猜成功 |
| sdk_event/harness | input_filter、context_retrieval、working_context、context_ready、persist_batch、rolling_compact、settlement 的实际阶段及计数 |
| research_review / research_followup | 结构化 Review、Gap 决策及补查轮次/目标角色；不新增自主 Supervisor |
| retry_started | 已记录授权请求；不是已应用 rewind，不表示新研究成功 |
| settlement / usage / run_status / done | 清算、已结算用量、当前业务解释和当前代次事件交付 |
| agent_report / report / history_saved / archive_pending | 阶段产物、最终产物和独立归档事实 |

Hook 的 `duration_ms` 用 monotonic 时钟测量 SDK 钩子边界，包含该边界内已有控制/观测开销，不宣称是供应商纯推理延迟。Step 仅在开始/结束均有 observed_at 时计算差值；旧 relay timestamp 不拿来假装实际执行耗时。图规模、context_chars、压缩条数均是计数，不公开上下文内容。rolling compaction 的估算 Token 减量不当实际用量发布。

SDK 公共字段在生产 observer 中再次 allowlist：字符串长度、非负有限数值、实际 input/output/total usage。Prompt、instructions、原始参数、原始工具结果、异常文本、私有记忆不在协议里。SDK 的全零默认 usage 不能证明 provider 提供过用量，此时单次调用显示未提供。Run 总用量仍来自原预算账本，错误 `usage_complete=false` 明确标记不完整；不从 spans 重算或双计。没有定价数据，不显示估算账单。

## 4. 订阅、恢复与投影

- 冷启动、刷新、重新读取：分页读取 seq 0 起的持久前缀，再以最后完整序号订阅 SSE。sessionStorage 不保存脱离事件前缀的游标。
- 去重只消费新 seq；跨 Run、跳号、帧 id 不符、无效字段、未知协议版本停止推进。不会静默跳过坏事件或改写游标。
- SSE 支持任意分片、UTF-8、CR/LF、注释、多行 data。EOF 的残帧丢弃，完整游标保持，后续从数据库重取该事件。
- 连接与业务状态分开。网络/503 自动退避到最多 10s；401/404/409/协议错误交给用户处理。重连只有 GET，从不自动 create、retry 或 cancel。
- 原生 GET 每 1.5s 更新事实。完整结束要求终态、settlement 非 pending、当前 generation 的 events_complete、已送达 event_cursor 且没有 pending retry grant。归档 pending 时降低频率继续读取补偿结果。
- 审批暂停继续观察原 Run，另一页面提交决定后可恢复。显式取消继续等 SDK/工具清算；关闭页面或切换 Run 只关闭订阅。
- 创建先保存 key + 原参数；丢响应后确认同一请求。重试先保存 key + 原 state_version + generation；丢 ACK 后确认同一授权。前端不执行 rewind、人工核对或释放未知占位。

`projection.ts` 对同一事件前缀执行纯 fold。静态拓扑为 Intake → Planning → Approval → 并行 Producers → Review/Gap → Risk → Strategy → Report；动态 Step 实例按 execution_id 区分，SDK 调用按 invocation / turn / call 区分。不能因最终 Run 成功、旧 completed_steps、旧 report 或 RAG 选择将未开始节点填成完成。补查重新进入被指定的 Producer / Review；重试保留旧失败和成功角色，只有新开始事件才产生新执行实例。

回放只限制事件前缀，不恢复模型/工具，不读取未来报告填充过去的界面，且禁用审批/取消/重试。Journal 和 Research Graph 未提供按事件游标的历史快照，因此这两个独立面板会明确展示“当前持久视图”。History 归档不能转换成 Trace；必须打开原 Run 读取其 Stream。

## 5. 构建、验证与后续能力

独立前端的操作方式、测试入口和 wheel/Classic 集成见 [frontend README](../../frontend/README.md)。固定安装包接口优先于文档猜测；参考 [React Flow 自定义节点](https://reactflow.dev/learn/customization/custom-nodes)、[Agents SDK RunHooks](https://openai.github.io/openai-agents-python/ref/lifecycle/)、[DBOS Client](https://docs.dbos.dev/python/reference/client)。

明确未实现：原始 Prompt / tool transcript、跨 provider 账单、Planning/Graph/报告等有界业务模型的逐请求 span、SDK 任意 turn 的恢复、跨服务 exactly-once、多租户 ACL、跨主机自动接管、按历史游标还原 Graph、持久 Stream 过期后的重建、大规模事件的磁盘索引。长 Trace 目前在内存保存完整前缀，时间线仅分页/限高渲染；更大规模需单独做索引和性能验收，不能以截断前缀换取错误状态。
