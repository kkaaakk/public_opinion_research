# Agent Execution Studio：最终 Review 与合并验收

日期：2026-10-26（Asia/Shanghai）。目标为原 PR #31，不另建 PR。独立核对远端 head `f69b015c6e28cd4d63ac49aeed61dc6c417b8d71`、main `dc070a4715f3bb1c29c5bce1cac6dfa5f3676b53`，初始工作区干净。GitHub review/review-thread/comment 均无既有未解决反馈。

本轮从 main → PR 完整 diff、实际 Run API / DBOS relay / SDK Runtime / Harness 调用链和前端投影重新复核，未以旧审计的通过声明替代本轮检查。执行/授权/资源预留、四角色 Agent Loop、业务模型调用与 Journal 所有权均保留；生产 Python 模块相对 f69 无改动。仅测试服务增加受控的一轮补查场景。

## 1. UI 与交互

- 280px / 84px 的节点、大字号与更高对比，紧凑折行布局和方向箭头；两个 Producer 对称并列。Intake → Planning → Approval → Producers → Review → Risk → Strategy → Report 边不变。补查使用独立 Handle 与真实轮次计数，避免与汇合线完全重叠。
- 实际节点尺寸仍由 React Flow 测量并通过 onNodesChange 保存，未用 CSS 强制可见性隐藏错误。缩放、fit、自动定位、只读 Delete 和窄屏列表/拓扑切换保留。桌面浏览器直接检查显示字号至少 12px。
- 无手动选择时，从真实活动 Step 选取检查器目标；审批等待选择 Approval；结束后选择最近终结的业务节点。失败/取消也保留其终结事实。没有实例/关联事件就显示明确空状态，不合成 Step。
- 用户选择按 Run 隔离并固定，同 Run 后台更新、审批继续及重新读取不会反复覆盖。切换 Run 后旧选择不会污染新 Run。
- 从每个真实 Step 实例进入对应 SDK Invocation / 模型 / 工具详情。定位由 execution_id + invocation_id + generation 共同约束；旧记录缺身份时不按 role 猜测。成功 Producer 在安全重试后仍定位原 generation 0 Invocation，补查的第二次 Invocation 则独立显示。

## 2. 本轮问题分级

新发现 P0：0；P1：0。与本轮直接相关的 P2 均已修复：

| 编号 | 触发与影响 | 修复与证据 |
| --- | --- | --- |
| A-01 P2 | 全流程纵向缩小，节点主字偏小、横向空间闲置 | 折行布局、大节点和文字；桌面实际字号、缩放与截图检查 |
| A-02 P2 | 有真实数据但检查器初始为空，或者显式重新订阅清除手动节点 | 由实际前缀派生默认目标，手动选择以 Run ID 固定；审批 → 运行完成的实际浏览器验证 |
| A-03 P2 | 业务实例与 SDK 页面只靠人工筛选，不便区分补查或重试 | 新增数据选择 helper 与定位入口；同 Invocation 字符串跨 execution/generation 的负向隔离测试；真实补查/重试浏览器合同 |
| A-04 P2 | 原重试 ACK 丢失后，再次确认遇到 401/403/404 或 429，会丢失请求身份 | 已有不确定请求在认证/临时限流失败后保留原 key/version/generation；401/403/429 三个回归保持原请求内容，成功 ACK 后才清除 |
| A-05 P2 | 工具业务失败的行未直接显示单独的 uncertain 外部效果回执 | 同时显示业务结果与最新效果回执；未将 failed 解释为无副作用，unknown 仍在 Run 清算/事件详情中保留；组件回归 |
| A-06 P2 | 前端 workflow 原来只在 PR 运行，合并后没有同样 UI gate | 增加 main push 的 frontend / 真实浏览器作业；不削弱现有 Python/native 作业 |

保留的非阻塞事项：

- 继承的 MarkItDown/MCP 版本冲突不变，pip check 仍失败；不改根依赖或降级 MCP。
- SDK/Harness 同步元数据写入仍有额外 PG/Journaling 开销，尚无生产吞吐/高积压 benchmark。本轮导航只读本地前缀，不增加业务模型、工具或额外后台调度。
- 生产供应商/远程 MCP、跨服务 exactly-once、多租户、Graph 历史快照、原始 Prompt/tool transcript 与账单仍是前次声明的边界；本轮未扩展。

## 3. 源码复核要点

| 域 | 结论与验证 |
| --- | --- |
| DBOS / SDK 身份 | Run/native status 为权威；Step execution、SDK Invocation、model turn、tool call 分开；实例入口不把 role 当唯一身份 |
| 并行 / Gap / 代次 | 保留确定性业务汇合与定向补查；默认选择为展示逻辑，不控制执行。重试成功角色不生成新 Invocation |
| SSE | 连续前缀、去重、冷回放、乱序配对与 terminal manifest 仍由既有协议实现；订阅器只有 GET |
| 清算 / 归档 / 取消 | native terminal、settlement、事件交付和 archive receipt 区分；pending 不关闭观察；未知效果无强制重放按钮 |
| 授权 | plan_version、key/state_version/generation 不被 UI 推算为新的授权；仅 Finalizer 可 rewind；认证拒绝不确认旧请求 |
| 用量 / 隐私 | 预算账本与 SDK 分轮用量分开，不加总双计；缺字段未提供，无实际定价。公开字段 allowlist、出口脱敏及 Graph run scope 保留 |
| React | 默认目标派生、手动目标固定、Scope 按 Run 清除；角色/Invocation 定位可显式清除，避免残留的隐式筛选；没有第二状态管理框架 |

## 4. 验证与证据

参考基线为 819 离线 / 89 原生 / 33 前端 / 4 浏览器，本轮保留全部既有用例与断言。实际已执行的本地检查：

- TypeScript、ESLint、Prettier、Vite 独立构建通过。
- 前端 **42 passed**；增加默认目标、身份隔离、回放前缀、业务结果/外部效果并存及丢 ACK 后认证/限流回归。
- Playwright **6 passed**（1600×1000、390×844）；原四条链路保留，再覆盖真实补查、多 Invocation、Generation 0 复用、手动选择固定、SDK 详情、缩放/定位、只读与窄屏切换。全部检查 page errors。
- Python 离线 **819 passed / 95 deselected / 146 warnings，101.66s**，禁 dotenv/tracing，socket guard。
- Classic Node **7 passed**；生产入口 smoke **32 passed**；既有 CI Ruff E/F/I 口径及 compileall 通过。
- 根锁同步通过；唯一继承 pip 依赖冲突仍为 markitdown-mcp 要求 mcp~=1.8.0、实际 mcp 1.23.0，未算通过。

本轮完整原生矩阵按相同五个文件重新执行，含真实 PG/Neo4j、进程树 kill/restart、容器停启、旧 key/代次、ACK 丢失及未知效果。它与最终 HEAD/合并后 CI 的具体结果、运行链接和 Merge Commit SHA 统一记录在 [PR #31 验收说明](https://github.com/kkaaakk/public_opinion_research/pull/31)，以免提交验收结果本身产生一个尚未经检查的新 HEAD；不复用 f69 的旧绿勾来授权合并。

截图来自真实受控链路，模型为官方 ScriptedModel、工具为固定合成证据，不是供应商研究/实际账单。更新后的本地 `.tmp/studio-workflow-desktop.png`、`.tmp/studio-acceptance-invocation.png`、`.tmp/studio-mobile.png` 和 CI `studio-browser-evidence` artifact 作为截图证据，不提交本地输出或 Trace 到 Git。

## 5. 合并约束

最终 HEAD 无阻塞问题且必需 CI 全绿后才 Ready for Review，使用 Merge Commit；保留 PR 分支、备份、stash 与其他工作区。合并后检查两个 parent、head 祖先关系、GitHub main / origin/main / 本地 main、一致代码树、干净工作区及 main push CI。

无生产部署、旧 journal 迁移、跨引擎重投或未知 Agent/tool 强制恢复。`dbos-phase5-v1` → `dbos-phase5-studio-v1` 的匹配发行版排空说明保持原样。
