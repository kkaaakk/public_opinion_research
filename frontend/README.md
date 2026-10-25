# Agent Execution Studio

基线：main dc070a47（Phase 5 + 清理 PR #29/#30）。本目录是独立的 React + TypeScript + Vite 前端，不改变 DBOS 3.3.0 或 Agents SDK 0.24.0 的所有权。

## 运行

从仓库根目录启动既有 FastAPI 与 DBOS Executor/PostgreSQL 依赖；Web 仍使用原来的 `PUBLIC_OPINION_API_TOKEN` 单操作员模式。

```bash
cd frontend
npm install
npm run test
npm run build
```

构建产物写入 `src/open_deep_research/web/studio/`（仓库根目录相对路径）。FastAPI 自动在 `/` 和 `/studio` 提供新观测台；没有构建产物时回退旧页面；`/classic` 永远提供既有页面。前端开发可运行 `npm run dev`（Vite /api 代理到本地 8000）。

注意：构建产物是否纳入部署由部署流水线决定，部署前必须运行 npm install + npm run build，并打包 `web/studio`；不要将运行时缓存提交到仓库。根目录 package.json 是文档工具，独立于这里的依赖。

## 功能与边界

- POST /api/runs：先在 sessionStorage 固定同一幂等键和请求，再创建 DBOS Run；刷新后恢复同一 Run，不二次执行。
- GET /api/runs/{id}：权威状态、阶段、用量、审批、清算与报告。
- GET /api/runs/{id}/events：基于 Last-Event-ID 的顺序 SSE 补发、事件检查器与节点投影。EOF 不等于执行完成，终态必须等清算、done 和游标完全确认。
- 审批、取消、安全重试走原 Run API；未知副作用/中断禁止前端强制重放。
- 研究历史仍从 /api/history 读取 Neo4j 报告投影，不能伪装成完整的 DBOS 事件回放。要查看历史执行轨迹，必须提供原 Run ID 访问原始 Run API。
- 目前浏览器仅保留最近 2000 条事件用于渲染。重连游标仍按所有事件累计；服务端只发布经过过滤的元数据/报告，不把原始 Prompt、工具参数和异常原文暴露到页面。
- Agent SDK 的逐次模型/工具元数据来自后端 sdk_event；未发布的数据不会凭空生成。当前有 model_started/completed 等 SDK 生命周期事件，但完整微观 trace 不是当前 Phase 5 Run API 合同的一部分。
- 路径 /`classic` 保留原页面，遇到构建问题可以直接回滚展示，不涉及 DBOS 任务重启。

## 后续工程验收

需要在依赖可访问的环境完成 `npm run build`、真实浏览器调试和完整的 811 个离线 / 85 个原生服务合同回归。在此之前不能把 UI 视为已上线验收。可用浏览器自动化验证 SSE 截断/重连、审批、终态清算、重复重试、缺少权限、报告历史、窄屏、异常节点和大事件量行为。
