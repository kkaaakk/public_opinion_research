# Agent Execution Studio

React 19 + TypeScript（strict）+ Vite + React Flow。主链路保持 **FastAPI → DBOS 3.3.0 → OpenAI Agents SDK 0.24.0 → Research Graph Context Harness**。前端只展示事实和发送现有受保护控制请求。

## 构建与启动

在本目录使用 Node 22.12+ LTS 或 24 LTS；根 `package.json` 仍是文档工具。依赖精确固定在本目录的 `package-lock.json`。

```powershell
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

先构建，再按[DBOS 部署文档](../docs/architecture/dbos-deployment.md)启动原有 FastAPI、Executor、PostgreSQL 和业务服务。构建写入 `src/open_deep_research/web/studio/`，该目录不提交 Git。FastAPI `/`、`/studio` 提供新界面，`/studio/assets/*` 提供哈希资源；`/classic` 保留原页面。没有构建时自动回退。Web 在构建之前启动时，构建后需重启 Web，以注册静态资源挂载。

开发：`npm run dev` 后打开 Vite 输出的 `/studio/` 地址。`/api` 代理到 `http://127.0.0.1:8000`，Executor 按原架构独立启动。API Token 在「访问设置」中输入，仅保留页面内存；刷新后重新输入。无需编辑 `.env`。

部署时先执行前端构建，再打包 Python 应用。Python wheel 包含已构建的 Studio 和经典页面资源。单独 `npm run preview` 仅预览静态资源，没有生产 API 代理。

## 模块职责

| 模块                              | 职责                                                                                     |
| --------------------------------- | ---------------------------------------------------------------------------------------- |
| `protocol.ts` / `readModels.ts`   | v1 事件及 REST DTO 的严格类型、运行时校验                                                |
| `api.ts`                          | 同源认证、受保护读写、稳定错误处理                                                       |
| `observer.ts`                     | 冷回放、连续前缀、SSE 解码/补发/去重、自动重连、清算及归档观察；只有 GET                 |
| `session.ts` / `useRunSession.ts` | Tab 内 Run 选择、创建/重试的不可变请求身份、显式用户命令                                 |
| `projection.ts`                   | Live / Replay 共用的确定性 fold，按 execution / invocation / model turn / tool call 配对 |
| `inspection.ts`                   | 从真实前缀选择默认节点，按 execution/invocation/generation 定位 SDK 记录                 |
| `components/`                     | 工作流、节点检查器、SDK 追踪、Journal、证据、报告、历史、时间线                          |

使用 React 自带状态，不引入另一套状态管理或调度框架。报告、历史与高级面板按需加载。画布可切为实例列表，窄屏默认列表；完整拓扑仍可切换和缩放。时间线分页渲染，不截断已接收事件前缀。

拓扑沿箭头折行，双 Producer 并列显示。未手动选择时，检查器跟随实际活动实例，结束后选择最近终结的业务节点；手动选择只属于当前 Run，后台状态和同 Run 重新读取不会覆盖它。每个可验证的执行实例提供 Invocation、模型、工具入口；定位绑定真实 execution_id/invocation_id/generation，未知身份不按角色名猜测关联。SDK 面板可清除定位，回放只使用游标内的事件。

## 真实数据与边界

- 打开 Run 只读取；刷新从 seq 0 分页重建，随后按 `Last-Event-ID` 订阅。会话存储不保存孤立游标或 Token。
- SDK 模型/工具状态、身份、实测钩子时长与 Harness 元数据来自正式后端观测。缺失字段显示「未提供」。`returned` 工具效果回执不等于 `succeeded` 业务结果。
- 审批绑定服务端不可变 plan version；安全重试先保存 key、state_version、generation。丢 ACK 后只能确认原请求，不能自动换新 key。只有 Finalizer 应用授权。
- DBOS 终态、SDK 清算、事件交付、Neo4j 报告归档分别展示。断线和页面切换不取消 Run；归档补偿期间继续只读观察。
- 旧事件没有新身份/时间字段时保留可观测部分，不制造 turn、Prompt、工具参数/输出或账单。RAG 开关本身不证明某 Agent 已执行；跳过需要服务端 `step_skipped` 事实。
- Neo4j History 只作为报告归档；打开原 Run 后才能读取 DBOS Trace。证据与原生日志面板显示当前持久视图，不宣称是回放游标处的历史快照。
- Report Markdown 禁止 raw HTML，外部图片仅显示说明，不主动加载远程跟踪图片。

协议、接口字段及所有权详见[架构与协议说明](../docs/architecture/agent-execution-studio.md)；Review、验证和截图见[实施审计](../docs/audits/agent-execution-studio.md)。
本轮最终 Review 与合并验收见[收尾验收记录](../docs/audits/agent-execution-studio-acceptance.md)。

## 浏览器端到端合同

使用[原生合同的隔离服务](../compose.dbos.yaml)，不连接业务数据库或调用付费模型：

```powershell
# 仓库根目录：根锁与隔离测试服务
uv sync --extra dev --locked --python 3.11
docker compose -p por-studio-test -f compose.dbos.yaml up -d postgres neo4j-test --wait
# frontend/ 目录
$env:PLAYWRIGHT_BROWSERS_PATH = '../.tmp/playwright-browsers'
npx playwright install chromium
npm run build
npm run test:e2e
```

`playwright.config.ts` 启动 `tests/studio_browser_server.py`：真实 HTTP/DBOS/Neo4j/Runner/Harness，推理用官方 `ScriptedModel`，工具为可核对的合成证据。夹具创建并只删除自己的 `por_test_<uuid>` PostgreSQL 数据库。测试服务、截图数值与合成证据不能解释为真实供应商研究或费用。该入口只用于测试，不能替代生产启动方式。

`TEST_DBOS_DATABASE_URL`、`TEST_DBOS_NEO4J_URI`、`TEST_DBOS_NEO4J_PASSWORD` 可指向其他明确隔离的本地服务。截图及失败 Trace 写入根 `.tmp/`；不要上传包含真实 operator Token 的浏览器 Trace。CI 的服务和 Token 均为一次性测试配置。
