# Agent Execution Studio：实施与验收审计

日期：2026-10-25（Asia/Shanghai）。从最新 `origin/main dc070a4715f3bb1c29c5bce1cac6dfa5f3676b53` 审查，并在原 Draft PR #31 分支 `codex/agent-execution-studio` 继续。没有合并 main，没有修改 `.env`、`CLAUDE.md`、历史业务数据、旧版本安全引用或无关服务。

## 1. 原 PR 的独立 Review

原 head：`379209db4630ba3f3f56d2ca3f64dfbc5b252435`。先读实际源码及 Phase 4 DBOS、Phase 5、迁移/部署文档，再安装依赖复现，不将旧 LangGraph 材料当当前架构。

原版 4 项 Node 协议测试通过，但 TypeScript 构建实际失败（React Flow 默认导入不具备 JSX 调用签名）。GitHub 原 [frontend 检查](https://github.com/kkaaakk/public_opinion_research/actions/runs/40159405867/job/121067600306)同样失败；其他旧绿勾不能证明前端可用。

| 发现 | 影响 | 处置 |
| --- | --- | --- |
| P1：错误 React Flow 导入，未完成构建 | 页面无法交付 | 改为公开命名 API，严格类型、Lint、构建及浏览器门禁 |
| P1：刷新仅保存 cursor，事件前缀丢失 | 过去节点、Token、SDK 详情不可回放 | 刷新从 0 分页重建，随后按完整游标续订 |
| P1：截断到最近 2000 条后再投影 | 旧开始/完成事实丢失，状态不可信 | 保存完整前缀，分页/限高渲染；2401 条冷回放测试 |
| P1：重试每次生成新 key | ACK 丢失后可能授权另一失败代次 | 保存不可变 key/state_version/generation，确认原请求 |
| P1：按步骤名称集合累计完成 | 补查/重试实例与旧完成事实混合 | execution_id、invocation_id、turn、call 分开配对 |
| P2：RAG 输入直接推断 Agent disabled | 与后端实际准入/跳过事实不一致 | 只在服务端确认后发布 step_skipped；不伪装 SDK 完成 |
| P2：any + 不检查 JS 协议层 | 字段/接口错误无法在边界发现 | strict TypeScript、unknown 输入、所有 DTO 运行时校验 |
| P1：SDK 缺少可配对身份与结果语义 | 同名工具、并行角色、调用轮次无法调试 | 复用 RunHooks/Harness/operation receipt，补必要公开元数据 |
| P2：没有证据详情、完整 CI 或窄屏验收 | 主要能力未落地 | 独立面板、真实 Graph 读取、受控原生浏览器合同 |

## 2. 最终实现与边界

独立 React/TS/Vite 工程、React Flow 拓扑与实例列表、节点检查器、SDK 模型/工具追踪、原生 DBOS Journal、事件筛选与回放、证据溯源、审批/取消/安全重试、报告/History/Markdown 导出均连接实际受保护接口。原页面保留在 `/classic`；构建不存在时回退。

SDK 只公开已观测的身份、轮次、结果状态、实际 usage、钩子边界耗时和 Harness 计数。工具 `returned` 与业务 `succeeded` 分开。缺失的 Prompt、参数、原始工具响应、供应商用量和费用不生成占位数据。概览读取原业务账本，不累加 spans 双计费用。

拓扑、Live 与 Replay 使用同一纯投影；未执行节点不会按 Run 最终状态批量填成完成。SDK 内部循环仍完全由 Runner 管理，DBOS journal/status 仍是执行权威。新增后端只读视图没有调度、租约、强制重放或人工释放未知效果的能力。

协议、数据源分工、运行和部署见 [架构说明](../architecture/agent-execution-studio.md)、[前端 README](../../frontend/README.md)及 [DBOS 部署说明](../architecture/dbos-deployment.md)。

## 3. 实现完成后的复核与修复

重新读取完整 diff、固定版本安装源码和测试，而非以实现说明证明实现。另修复以下问题：

- **P1 终态事件乱序缺尾。** 首次全量原生矩阵为 **86 passed / 1 failed**：并发不同 key 重试合同中，API/History 已是 generation 1，但 Stream 最后 usage 仍是 generation 0。固定 DBOS 3.3.0 接收源码按毫秒事务时间排序，不能把归档包的到达当成其他尾事件全部交付。新增真实 DBOS/SDK 的受控乱序合同，修复前稳定 **1 failed（0 != 1）**。保留全部原断言。
- 修复只在原 relay 的交付确认：Finalizer `done.expected_events` 声明该代 settlement/usage/status 及存在的 report/error，relay 等真实事实齐全后确认 done/history 并关闭 Stream。没有重跑研究、改业务路由或新增调度器。定向原故障 + 乱序合同 **2 passed**。
- relay journal 分支变化因此明确提升 application version 为 **`dbos-phase5-studio-v1`**，SDK runtime 仍为 `sdk-phase5-v1`。旧 `dbos-phase5-v1` pending journals 必须由匹配发行版排空，新 Executor 不重放旧 relay。这不是对历史数据的迁移或重投。
- **取消后的 SDK 异常出口。** 允许已撤销取消意图下记录 `model_failed` 清算元数据，仍校验原许可，不准入新模型/工具。真实取消回归检查 model_failed、agent_failed、Harness settled，以及两个 Producer 工具各只执行一次。
- **重复终结事实。** 相同 execution_id 的迟到终结记录不会制造另一实例；新 generation / 新执行身份仍保留独立失败与重试。
- **React Flow 几何丢失。** 浏览器暴露快速状态更新时部分节点变为 hidden；改用官方 useNodesState/onNodesChange 保存实际测量尺寸，观察事实更新保留几何，未通过强制 CSS 可见性掩盖问题。检查器仅由用户点击/键盘动作单向选择，避免双向选择回调递归；Delete 不能移除只读拓扑节点。
- **回放与当前视图。** 回放不引用当前错误/审批/清算横幅填充过去；保留筛选选择，避免选项消失后“显示全部但仍隐式筛选”。当前 Graph/Journal 单独标注。
- **归档补偿。** Run 清算完成后归档 pending 时继续只读观察，不把 SSE EOF 视为研究成功，不重新创建任务。
- **依赖。** 官方 npm 审计发现 Vitest 3 的开发依赖漏洞，升级修复版 Vitest 5.0.5；官方审计为 **0 vulnerabilities**。锁文件采用公开 npm registry，避免本机镜像配置成为 CI 依赖。

## 4. 实际验证记录

| 检查 | 结果 / 口径 |
| --- | --- |
| 根锁 | `uv sync --extra dev --locked --python 3.11` 通过；DBOS 3.3.0 / SDK 0.24.0 实际核对 |
| Python 最终离线 | **819 passed / 95 deselected / 146 warnings，112.94s**；禁 dotenv/tracing，socket guard 仅允许 loopback/unix |
| 完整原生矩阵（修复后） | **89 passed / 25 deselected / 0 skipped / 0 xfailed，1150.53s**；真实 PG、Neo4j、Executor kill/restart、容器停启、SDK Runner 和回执/重试/归档故障 |
| 取消元数据定向 | **1 passed**；不重做工具，模型/Agent 异常与清算可见 |
| TypeScript / ESLint / 格式 | 通过；strict、无隐式 any，保留 React Hooks 规则 |
| 前端单元/交互 | **31 passed**；分页冷回放、SSE 分片/去重/缺口/重连、终态清算、归档、并行/补查/重试投影、审批及不可变请求身份 |
| Chromium 完整链路 | **4 passed / 48.2s**；1600×1000、390×844，全部用例检查 JS page errors；真实断线重连、审批、重试、取消、证据、报告与刷新 |
| Classic Node 合同 | **7 passed** |
| 入口 / Ruff / 编译 | **32 个生产入口 import**、既有 CI E/F/I 口径、compileall 通过 |
| 独立 Vite 构建 | 通过；主包约 475 KB / gzip 152 KB，报告与高级面板按需加载 |
| Python wheel | 构建成功；逐项核对 Studio HTML 引用的哈希 JS/CSS、lazy chunks 和 Classic 资源存在，未包含 .env/node_modules |
| npm 审计 | 官方 registry：**0 vulnerabilities**。原本机镜像 audit 返回不支持，未当成零漏洞 |
| 根 pip 一致性 | **未通过**；唯一继承冲突仍为 markitdown-mcp 要求 mcp~=1.8.0、实际 1.23.0。根锁未新增依赖冲突，未掩盖此结果 |

Windows 沙箱首轮离线缓存目录测试出现 2 个重命名权限失败（其余 817 项通过）；改用工作区内专属 `--basetemp` 后全量通过，未改断言或跳过用例。部分 Python 进程退出时有继承的 pyreadline3 控制台析构告警，pytest 汇总和 exit code 单独核对。

原生矩阵使用本次新建的 `por-studio-test` 隔离容器与每例 UUID 数据库，未使用业务 Neo4j 或生产 Runs。推理仅官方 ScriptedModel/既有固定夹具，工具为合成可核对数据，付费模型调用为 0。CI 配置继续运行原 quality/tests/native 作业，新增 frontend 和真实链路 Chromium 作业；最终结果以 [PR #31 当前 head 的检查](https://github.com/kkaaakk/public_opinion_research/pull/31/checks)及 PR 描述中的具体 run 为门禁，不能用旧 head 的绿勾替代。

## 5. 浏览器与截图

Browser 技能未在会话提供，使用 Playwright Chromium（1600×1000、390×844）。页面身份、非空内容、框架覆盖层、JS 错误、实际操作与布局均检查。认证刷新后的 401 是内存 Token 丢弃后的预期拒绝，随后输入 Token 只补发 GET，不重新 POST。

端到端路径：创建 → 不可变审批 → 真实四角色 SDK → Graph → 报告/导出 → seq 0 回放/实时切换 → 刷新同 Run → History；另覆盖安全重试保留 Producer、取消清算、真实 SSE 连接中断后自动续订。测试数据是受控 Runner/Graph 输出，不是供应商研究、实际账单或前端演示数据。

截图保存在本地 `.tmp/studio-workflow-desktop.png`、`.tmp/studio-sdk-desktop.png`、`.tmp/studio-mobile.png`；CI 上传为 `studio-browser-evidence` artifact。遵循 AGENTS.md，不将本地输出或浏览器 Trace 提交 Git。最终交付消息提供实际截图；图像概念仅作布局参考，补查连线按真实 Review → Producer 纠正。

## 6. 仍未支持

原始 Prompt / 任意 tool transcript、供应商真实账单、有界 LangChain 业务服务逐请求 spans、SDK 任意 turn 恢复、按历史游标还原 Research Graph、跨服务 exactly-once、多租户 ACL、生产 HA、过期 Stream 重建和极大 Trace 的磁盘索引仍不支持。长 Trace 保存完整内存前缀，未做高负载性能验收；不能以截断事实换取错误投影。生产供应商、远程 MCP 和旧任务实际排空不是本次受控本地测试的通过声明。

未合并 main，未部署生产。现有 Draft PR 等待用户验收。
