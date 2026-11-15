---
layout: doc
---

# Public Opinion Research 文档站

当前生产链路由 FastAPI、DBOS、OpenAI Agents SDK 和 Research Graph Context Harness 组成。 [执行架构](./architecture/agent-loop.md) · [部署与数据库](./architecture/dbos-deployment.md)

::: tip 阅读建议
第一次接手项目时，建议按“快速开始 -> 当前技术栈 -> Agent Loop -> Tools -> RAG -> Memory”的顺序阅读；排查检索质量时直接进入评估记录。
:::

::: warning 内容边界
使用说明以根 README 为准；过时副本和已完成草稿已删除。examples 与 knowledge 是历史样例和评测语料。
:::

## 推荐阅读路径

1. 阅读 [项目 README](https://github.com/kkaaakk/public_opinion_research/blob/main/README.md)，完成环境准备和基础运行。
2. 阅读 [执行架构](./architecture/agent-loop.md)，了解五层职责、业务流程与故障恢复边界。
3. 阅读 [部署与数据库](./architecture/dbos-deployment.md)，查看配置、表结构、版本切换及回滚步骤。
4. 阅读 [Tools 模块](./architecture/tools.md)，确认 web search、RAG、MCP 和控制工具如何装配。
5. 阅读 [RAG 模块](./architecture/rag.md)，深入本地知识库、memory、hybrid retrieval、reranker 和 citation 链路。
6. 阅读 [Memory 模块](./architecture/memory.md)，理解 MySQL memory 的写入、索引和回查边界。
7. 阅读 [RAG 检索测试记录](./evaluation/rag-retrieval-test-records.md)，复现或比较检索效果。

## 文档地图

```mermaid
flowchart TD
    Home[文档首页] --> Guide[项目 README / 快速开始]
    Home --> Stack[当前技术栈]
    Stack --> Agent[Agent Loop]
    Agent --> Tools[Tools 模块]
    Tools --> RAG[RAG 模块]
    Agent --> Memory[Memory 模块]
    Memory --> RAG
    RAG --> Eval[RAG 检索测试记录]
    RAG --> Notes[RAG / Memory 笔记]
    Home --> Examples[示例报告]
    Home --> Reference[项目参考]
```

## 主题入口

| 主题         | 入口                                                           | 说明                                   |
| ------------ | -------------------------------------------------------------- | -------------------------------------- |
| 快速开始     | [项目 README](https://github.com/kkaaakk/public_opinion_research/blob/main/README.md)                       | 项目介绍、安装、运行和配置说明         |
| 架构与模块   | [执行架构](./architecture/agent-loop.md) | 五层职责、业务流程和可靠性设计 |
| 部署与验收   | [部署](./architecture/dbos-deployment.md) · [故障验收](./audits/dbos-architecture-closeout.md) | 数据库、上线流程及已验证范围 |
| 面试讲解     | [项目口述与问答](./interview/index.md) | 两分钟介绍、业务链路和追问 |
| RAG / Memory | [RAG 模块](./architecture/rag.md)                              | 本地知识库、长期记忆、检索、重排和引用 |
| 评估         | [RAG 检索测试记录](./evaluation/rag-retrieval-test-records.md) | 历次检索指标、配置、未命中样本和说明   |
| 示例         | [ArXiv 示例](./examples/arxiv.md)                              | 示例研究报告和输出样本                 |
| 知识库       | [Team Handbook](./knowledge/team-handbook.md)                  | 本地知识库样例文档                     |
| 项目参考     | [Agent Instructions](https://github.com/kkaaakk/public_opinion_research/blob/main/AGENTS.md)                      | 根目录规范开发规则        |

## 维护约定

- 模块说明优先记录当前实现；过时背景保留在 Git 历史或评估记录中。
- 新增模块文档建议按“定位 -> 流程 -> 数据结构 / 配置 -> 错误处理 -> 扩展建议”的顺序维护。
- 评估记录采用追加式写法，保留命令、配置、指标、未命中样本和解释。
- VitePress 导航变更时同步更新 `docs/.vitepress/config.mts`。
