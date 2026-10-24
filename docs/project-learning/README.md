# 项目源码学习型 PPT

> 历史材料：保留原取证提交与图表，不代表 Phase 5 运行架构。当前调用链见 [业务执行](../architecture/agent-loop.md) 和 [Phase 5 审计](../audits/openai-agents-phase5.md)。

本目录保存 Public Opinion Research 的源码与架构学习手册。内容以默认主分支 `origin/main` 的代码树为基准；分析时默认主分支为 `origin/main`，提交为 `12e2f0e`。当前工作树 HEAD 为 `41f180b`，两者文件内容无差异，差别仅来自合并提交历史。

## 交付物

- `project-learning-lines-fixed.pptx`：46 页可编辑 PowerPoint，所有流程图使用固定折线。
- `project-learning-lines-fixed.pdf`：与修订版 PPT 对应的 PDF。
- `slide-outline.md`：逐页目标与证据索引。
- `sources.md`：章节级源码、文档与 Git 提交清单。
- `source-learning-index.md`：知识点到文件、symbol、断点和观察变量的完整映射。
- `diagrams/`：九张可单独复用的 SVG 架构图。
- `archive/`：折线修订之前的 PPT 原稿与来源说明，保留用于历史对照。

## 如何更新

1. 先确认 `origin/HEAD`、`origin/main` 和当前 HEAD。
2. 重新检查 `langgraph.json`、`deep_researcher.py`、`state.py`、`runtime/`、`mcp/`、`rag/`、`memory/` 与 `observability/`。
3. 对照 `sources.md` 复核函数名、节点名、配置键和提交。
4. 按 `slide-outline.md` 更新逐页内容，并通过 Presentations 工作流重新导出候选 PPTX。
5. 运行 Presentations finalizer，再逐页渲染检查。
6. 将最终 PPTX 渲染为逐页 PNG，再装订并复核 PDF。

## 图的生成方式

PPT 中的主架构图用 PowerPoint 原生形状重建，便于编辑。`diagrams/` 下的 SVG 使用同一份结构信息手工绘制，适合文档、README 或代码评审复用。图中虚线表示可选能力或显式开关，红色标记表示未接入生产默认路径。

## 事实边界

- 生产图工厂没有内置 Checkpointer；`MemorySaver` 只在测试和评测脚本中显式传入。
- Research Graph 与知识库 GraphRAG 是两个不同子系统。
- 当前没有通用 Skill runtime；`social_media_skill` 是业务组合层的历史/文档命名，实际代码以进程内 LangChain tools 运行。
- RAG、Research Graph、MySQL 长期记忆、MCP Server 和 LangSmith 都由配置或环境变量控制，并非每次请求都会启用。
