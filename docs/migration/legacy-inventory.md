# 旧项目迁移清单（Phase 0）

记录日期：2026-10-15。旧项目检查点为 `41cbc134f748750ffbfc7c7dde65c7552ace6296`，可通过远程备份分支 `origin/backup/pre-deerflow` 或标签 `v1-pre-deerflow` 读取。

Phase 0 没有迁移旧 LangGraph Graph、自定义 Agent 循环或业务实现。原 `src/` 继续保留；与官方根目录重名的旧项目文件保存在已有独立备份及上述 Git 检查点。按用户要求，核验独立备份完整后已移除仓库内重复备份。官方后端的导入路径与依赖由 `backend/` 管理，运行新的 Gateway 不要求导入旧 `open_deep_research`。

以下路径均为旧项目原始路径；在独立备份根目录下直接使用这些相对路径，或通过 Git 检查点读取。位置与完整性核验见 [backup-location.md](backup-location.md)。表中的目标表示下一阶段建议，不表示实现已经迁入。

## 可复用的业务能力

| 原始路径 | 已有职责及边界 | 下一阶段建议归属 |
| --- | --- | --- |
| `src/open_deep_research/rag/types.py`、`citations.py`、`metadata.py` | 文档、分块、检索结果、引用及元数据定义 | 优先梳理为 `opinion_business.evidence` 和 `opinion_business.rag` 的业务数据契约；保留来源与引用对应关系 |
| `src/open_deep_research/rag/loaders/`、`splitter.py`、`indexer.py` | 文档与问题图片读取、分块、索引准备 | 分离输入读取和可选提供商适配；图片问题上下文不应自动写入持久知识库 |
| `src/open_deep_research/rag/retriever.py`、`service.py`、`query_rewriter.py`、`reranker.py` | 检索、查询改写、重排及服务编排 | 逐个适配 `EvidenceRetriever`；业务层暴露检索结果，扩展层负责工具注册与参数转换 |
| `src/open_deep_research/rag/graph.py`、`graph_adaptive.py`、`graph_scoring.py`、`graph_structured.py`、`graph_terms.py` | RAG 图检索、评分及结构化抽取 | 核实与研究证据图的职责重叠后再迁移；不直接复制旧 Agent 的上下文策略 |
| `src/open_deep_research/rag/vectorstore.py`、`embeddings.py`、`elasticsearch_bm25.py`、`mysql_memory.py` | 向量、嵌入、BM25、数据库访问 | 作为可选存储/检索适配器，后续按实际使用的后端增加 optional dependencies；Phase 0 不启动这些服务 |
| `src/open_deep_research/research_graph/models.py`、`schema.py`、`store.py`、`resolver.py`、`retriever.py`、`extractor.py` | 研究证据结构、来源映射、图存储和检索 | `opinion_business.research_graph` 与 `evidence`；逐个剥离旧配置、消息及模型调用依赖 |
| `src/open_deep_research/social_media/domain.py` | 信号词、负向查询变体、风险分数、热度等级等领域规则 | `opinion_business.social_media`、`sentiment`、`risk`；先以固定输入验证语义，再决定是否复用原启发式规则 |
| `src/open_deep_research/social_media/tools.py`、`social_media_apify_api.py`、`social_media_mcp.py` | 社交数据工具、Apify 与 MCP 接口 | 采集和数据规范化归业务包；官方 Tool/MCP 注册由扩展或现有 DeerFlow MCP 配置负责 |
| `src/open_deep_research/public_opinion_agents/public_signal.py`、`internal_knowledge.py`、`risk_assessment.py`、`response_strategy.py` | 舆情、内部知识、风险核验及回应策略的职责、输入输出与提示内容 | 提炼为业务契约和三个舆情 Skills 的后续内容；不复制角色 Agent 执行循环 |
| `src/open_deep_research/workflow/reporting.py`、`prompts.py`、`state.py` 中的报告格式与章节数据 | 报告结构、角色证据映射、引用与章节汇编约定 | `opinion_business.reports`；仅迁入纯数据转换和格式规则，模型并发与运行状态交给 DeerFlow |
| `src/open_deep_research/web/history.py` | 研究运行、阶段原始输出、完成/失败状态及 Neo4j 历史归档 | 后续审定归档领域模型和存储端口；保留状态及来源，不直接迁入旧 HTTP 服务 |
| `data/social_media/` | 本地社交媒体样例 | 后续选择脱敏、可审计的离线 fixtures；Phase 0 不将样例描述为实时采集结果 |

`sentiment`、`risk`、`evidence`、`reports` 在旧仓库中并非四个独立 Python 包。相应规则散落在上述领域模块、角色规范、RAG 引用及报告流程中。新的七个模块仅建立独立接口，尚未声称具有情感分类器、风险模型或报告生成器。

## 旧框架基础设施：保留参考，不接入新运行时

| 原始路径 | 旧职责 | DeerFlow 迁移处理原则 |
| --- | --- | --- |
| `src/open_deep_research/deep_researcher.py`、`workflow/graph.py`、`workflow/intake.py`、`workflow/research.py`、`workflow/shared.py`、`workflow/state_utils.py`、`state.py` | LangGraph 入口、节点、reducer 与研究调度 | 不迁移旧 Graph；使用官方 Harness、Subagents 及 Extension API |
| `src/open_deep_research/runtime/agent_runtime.py`、`runtime/business_agent.py` | 自定义 Agent 工具循环、模型绑定、上下文策略装配 | 不复制循环；仅提取业务输入输出及权限需求供官方扩展适配 |
| `src/open_deep_research/research_graph/strategy.py`、`workspace.py`、`context_manager.py`、`compaction.py`、`transcript.py`、`metrics.py` | 模型调用前后钩子、上下文压缩、运行工作区及统计 | 区分证据数据与运行时控制；不替换 DeerFlow Memory、上下文或 Subagent 实现 |
| `src/open_deep_research/memory/`、`rag/memory.py`、`rag/memory_writer.py`、`rag/loaders/file_memory.py`、`rag/loaders/mysql_memory.py` | 长短期记忆读写及旧运行状态 | 核实哪些是企业知识、哪些是 Agent 记忆；只迁移独立业务数据，框架记忆采用官方机制 |
| `src/open_deep_research/models/factory.py`、`llm/`、`configuration.py`、`config_values.py`、`budget.py` | 多提供商模型创建、配置、调用与预算统计 | 使用官方模型/配置/工具机制；需要的业务预算规则另行界定，避免复制提供商与上下文管理系统 |
| `src/open_deep_research/tools/registry.py`、`tools/rag_tool.py`、`mcp/`、`search/tavily.py` | 自定义工具发现、MCP 包装与搜索装配 | 通过官方 ToolContribution 或官方 MCP 配置接入；不创建第二套注册表 |
| `src/open_deep_research/public_opinion_agents/base.py`、`registry.py` | 旧角色注册、提示装配与调度元数据 | 只提取职责和授权边界；不将旧注册表作为新的 Agent 基础设施 |
| `src/open_deep_research/web/server.py`、`web/static/`、`src/security/auth.py` | 旧 FastAPI、SSE、业务仪表盘与 LangGraph 部署认证 | 保留供比对，不启动第二套 Gateway，不在 Phase 0 开发业务页面；新认证与 UI 走官方机制 |
| `src/open_deep_research/observability/`、`utils.py`、`time_utils.py` | 追踪、兼容入口与通用辅助函数 | 按需提取确有业务意义的纯函数；官方追踪、服务生命周期和时间配置保持官方机制 |
| `pyproject.toml`、`uv.lock`、`langgraph.json`、`package.json`、`package-lock.json`、`compose.neo4j.yaml` | 旧依赖、LangGraph 启动和本地数据库部署 | 原样保存在独立备份和 Git 检查点；新的官方服务按 DeerFlow 锁文件与启动命令运行 |

## 测试与资料资产

- `tests/test_rag.py`、`test_query_images.py`、`test_rag_retrieval_evaluation.py`、`rag_eval_cases*.jsonl`：优先提取纯检索和引用断言，重建独立业务测试，不要求旧 Agent 导入。
- `tests/test_report_section_budget.py`、`test_role_reports_propagation.py`、`test_graph_consumer_without_rag.py`：保留预算、证据可达和角色报告完整性等回归意图；不逐行复制旧 Graph 测试。
- `tests/test_social_media_skill.py`、`data/social_media/*.jsonl`：适合建立脱离外部采集服务的规则/适配器测试。
- `tests/test_web_history.py`、`test_web_research.py`：保存归档状态和数据契约用例；HTTP 接入以后通过官方 Gateway 扩展验证。
- `evaluation/cost_profile/`、`evaluation/plan_execute_ab/`：保存原基线和实测报告。后续比较需记录框架、模型及样本差异，不能直接把旧成本结果当成新框架性能。
- `docs/audits/`、`docs/interview-architecture/`、`docs/project-learning/`、`docs/research-graph-context-harness.md`、`docs/public_opinion_agents.md`：作为旧架构证据保存；内容不自动成为 DeerFlow 的实现状态。

## 下一阶段建议顺序

1. 先确定 `EvidenceItem` 的来源、标识、引用和时间语义，迁移离线可验证的数据转换与引用规则。
2. 选择一个真实 RAG 检索后端，实现 `EvidenceRetriever`，在官方扩展中增加单一业务 Tool，并用固定 fixture 验证完整执行链路。
3. 迁移社交信号规范化与现有启发式规则，显式区分热度、情感和风险；保留不确定性及可追溯证据。
4. 迁移研究证据图的纯数据与存储层；先证明其业务职责，再决定需要哪些官方扩展钩子。
5. 迁移风险核验与报告内容规则，确保每项重要结论关联证据；最后再评估历史归档和前端需求。

每一步都独立安装 `opinion-business` 并测试，通过公开 Extension API 增加适配。旧源码保留到完成业务等价性与数据迁移验收之后；删除或退休旧运行时属于后续单独决策。
