# Web 研究工作台

从仓库根目录执行以下命令。开发时在独立终端启动 Executor、FastAPI 和 Vite。

```powershell
uv sync --extra dev --locked --python 3.11
uv run deep-research-executor --initialize-only
uv run deep-research-executor --executor-id workstation-1
uv run deep-research-web
npm --prefix frontend ci
npm --prefix frontend run dev
```

Web 和 Executor 使用同一 PostgreSQL 连接配置。在系统设置中验证 `PUBLIC_OPINION_API_TOKEN`。令牌用于 Bearer 请求头，保存在当前标签页会话中。Vite 默认把 `/api` 代理到 `127.0.0.1:8000`。 [DBOS 部署与升级](../docs/architecture/dbos-deployment.md)

生产先运行 `npm --prefix frontend run build`，再启动 FastAPI。输出位于 `src/open_deep_research/web/ui/`，随 Python wheel 打包。页面位于 `/`，已注册的前端路由支持直接访问和刷新。`/assets` 托管构建资源，未知 API 和资源路径返回 404。

## 页面与接口

| 页面 | API |
| --- | --- |
| 新建研究与会话延续 | `POST /api/runs`、`GET /api/settings`、`GET /api/runs/submission` |
| 研究工作台 | `GET /api/runs/{id}`、`/images/{position}`、`/event-page`、`/events` 和已有的 `clarification`、`approval`、`cancel`、`retry` 命令 |
| 会话与历史 | `GET /api/sessions[/{id}]`、`GET /api/history[/{id}]` |
| 企业知识库 | `GET /api/knowledge`、`GET /api/knowledge/sources`、`POST /api/knowledge/index`、`POST /api/knowledge/search` |
| 执行详情 | `GET /api/runs/{id}/execution` 和 Run 快照 |
| 系统设置 | `GET /api/health`、`GET /api/system/status`、`GET /api/settings` |

创建请求使用 JSON。图片为 `images: [{ name, media_type, data }]`，`data` 是无前缀的 Base64。实际类型、数量、字节和像素限制通过设置接口返回。服务端复验文件，原图和识别结果随 Run 保留。提交中断时，工作台可按原幂等键查找已创建的 Run，或重发同一请求。

知识库使用服务端配置的目录。索引维护和检索测试需在闲置研究服务上执行，期间新 Run 或重试的准入返回冲突。索引任务状态由当前 Web 进程观察，服务重启后索引就绪状态可能需要再次确认。独立 MCP 或评测若操作同一索引，应与 Web 维护错开。系统状态展示 Web 存活、数据库连通和 DBOS 目录可读性。Executor 心跳需由部署环境监控。

## 开发验证

```powershell
.venv/Scripts/python.exe scripts/export_web_openapi.py
npm --prefix frontend run api:types
npm --prefix frontend run typecheck
npm --prefix frontend run lint
npm --prefix frontend run build
```

生成的 `src/api/schema.d.ts` 和 `package-lock.json` 随源码提交。`/docs` 提供 REST 接口说明。 [持久事件与查询协议](../docs/architecture/agent-execution-studio.md)
