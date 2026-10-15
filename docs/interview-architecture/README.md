# Public Opinion Research 架构面试版

本目录保存独立的面试型架构深挖材料。它不替代 `docs/project-learning/` 的源码学习手册，重点回答：

- 核心调用链和并发点在哪里。
- 哪些代码在成本、可靠性或安全上具有高风险。
- 当前方案为什么适合现阶段，而不是“为了简单而简单”。
- 业务规模和团队边界变化后，应该按什么条件升级。

## Files

- `public-opinion-architecture-interview.pptx`
- `public-opinion-architecture-interview.pdf`
- `code-graph-analysis.json`
- `code-graph.graphml`
- `risk-register.md`
- `architecture-decisions.md`
- `slide-outline.md`
- `interview-question-bank.md`
- `interview-source-map.md`

当前面试实战增强版共 32 页，包含 20 题 Question Bank、追问链、风险状态、Failure Scenario 和升级触发指标。

## Static graph caveat

代码图谱由 `tools/code_graph_analysis.py` 基于 Python AST 和 NetworkX 生成。LangGraph node registration、tool binding、dynamic dispatch 和 Pydantic/runtime factories 会让静态图低估部分调用边，因此 PPT 将静态图与源码追踪、测试和成本记录结合使用。
