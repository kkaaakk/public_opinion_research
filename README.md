# 🔬 Public Opinion Research

> 当前应用版本为 `por-call-ledger-v1`。Plan、可恢复澄清和 Brief 审批之后，由两个研究 Agent 并行取证。Review 定向补研，ResearchWorkflow 依次生成风险评估、应对策略和一份综合 Markdown 报告。 [Brief 合同](docs/architecture/research-brief.md)

基于 DBOS、OpenAI Agents SDK、RAG 和 MCP 的企业舆情风险研究与处置辅助系统。支持多模型提供商、多种搜索工具和 MCP (Model Context Protocol) 服务器。在原版 [Open Deep Research](https://github.com/langchain-ai/open_deep_research) 基础上，扩展了企业知识 RAG、舆情分析、社交媒体监控和可追溯来源记录等能力。

## 当前架构

CLI（`por`）经 RunClient 和 DBOSClient 将研究入 PostgreSQL 队列。独立 Executor 运行 DBOS 3.3.0 Workflow。Public Signal 和 Internal Knowledge 经 SDKAgentRuntime、Context Harness 和官方 DBOSRunner 0.3.0 调用 OpenAI Agents SDK 0.24.0 及业务工具。
可选 FastAPI 页面复用同一 Run API，但不再是运行研究的主入口。
新研究默认使用持久 Run，浏览器刷新只重新订阅，停止操作会请求取消并等待清算。
两个 Producer 并行汇合，经 Review/Gap 定向补查，再顺序执行 Risk → Strategy → Report Node。
原 LangGraph Server、业务 Graph 和手写 Agent Loop 已退役。

PostgreSQL 保存 DBOS journal 和领域事实，研究历史按 Run ID 从这些记录读取。
部署需要 `DBOS_DATABASE_URL`、操作员访问令牌及独立 DBOS Executor，详见 [部署说明](docs/architecture/dbos-deployment.md)。
旧 SQL 任务及旧版本 DBOS 任务须在匹配的发行版受控排空，不能跨引擎重投。
历史记录继续读取和导入。单操作员令牌不等于多租户身份；跨主机自动接管需要额外协调。

查看 [执行架构与可靠性设计](docs/architecture/agent-loop.md)、[测试与故障验收](docs/audits/dbos-architecture-closeout.md) 或 [面试讲解](docs/interview/index.md)。
DeerFlow 已退役；独立 RAG 配置及 DSH 分支资产保持原状。

## 🆕 扩展功能

### 📚 本地 RAG 知识库
独立运行的本地 RAG 流程，支持文档加载、语义检索、混合召回和重排序：
- **知识源**: Markdown / JSON / PDF / 图片 / 源码文件
- **向量存储**: Milvus / FAISS / Chroma
- **混合检索**: 向量 + BM25 + RRF 融合
- **多模态**: OCR + Vision 图片理解和查询
- **MCP Server**: 暴露 `rag_search` / `rag_ensure_indexed` / `rag_status` 等工具

### 📰 舆情分析 Agent
面向品牌/产品舆情监控的多智能体系统：
- `PublicSignalAgent` — 舆情信号采集
- `InternalKnowledgeAgent` — 内部知识检索
- `RiskAssessmentAgent` — 风险评估
- `ResponseStrategyAgent` — 应对策略生成
- 内置知识库：合规文档、FAQ、历史案例、PR 预案

### 📱 社媒采集（social-research 插件）
- `social-research` 插件通过 Apify 官方托管 MCP 采集小红书、抖音、微博数据
- 三个平台 Skill 指导 Actor 发现、Schema 核对、成本控制和证据提取
- 社媒工具与 Tavily 一样经过 `BusinessTool` → `ToolExecutor` → DBOS Step

### 💰 预算管理
- 可选的 token/调用次数预算守卫
- 自动降级和预算报告

## 🚀 快速开始

### 1. 克隆并激活虚拟环境

```bash
git clone https://github.com/kkaaakk/public_opinion_research.git
cd public_opinion_research
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 2. 安装依赖

```bash
uv sync --locked
```

开发与离线测试安装 `uv sync --extra dev --locked`；运行学习笔记本时增加
`--extra notebooks`。评测脚本可能调用付费模型，请先阅读对应目录说明。

开发工具默认将缓存统一写入项目 `.tmp/`，本地终端、Codex 和 CI 使用同一份 `pyproject.toml`：

| 工具 | 默认位置 |
| --- | --- |
| pytest | `.tmp/pytest-cache/` |
| pytest-benchmark | `.tmp/benchmarks/` |
| Ruff | `.tmp/ruff-cache/` |
| Mypy | `.tmp/mypy-cache/` |

无需手动传缓存参数。pytest-benchmark 使用项目 `addopts`，测试配置钩子在插件初始化前将默认位置锚定到项目根目录；显式指定其他 benchmark storage 仍有效。Mypy 使用配置目录变量，避免从子目录运行时产生另一套缓存。缓存均被 `.gitignore` 排除。

在项目根目录执行离线检查，例如：

```powershell
.venv/Scripts/python.exe -m pytest --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName })
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research
```

Mypy 的既有错误基线仍会返回非零；缓存配置不改变检查规则或测试选择。配置语义可查阅官方 [pytest](https://docs.pytest.org/en/stable/reference/reference.html#confval-cache_dir)、[pytest-benchmark](https://pytest-benchmark.readthedocs.io/en/stable/usage.html)、[Ruff](https://docs.astral.sh/ruff/settings/#cache-dir) 和 [Mypy](https://mypy.readthedocs.io/en/stable/config_file.html#confval-cache_dir) 文档。

### 3. 配置凭据与业务配置

```bash
cp .env.example .env
# 编辑 .env，只填写 API Key、数据库连接和认证凭据
```

业务配置只来自 `config.yaml`；`config.smoke.yaml` 通过 `extends` 继承正式配置，
只覆盖低成本端到端测试需要缩减的字段。`.env` 不再控制模型、RAG、插件或预算开关；
旧的 `RESEARCH_MODEL`、`RAG_ENABLED`、`ENABLED_PLUGINS` 等变量会被忽略，可以删除。
未知配置字段会直接报错，缺失的模型凭据和显式启用的插件依赖会在运行前失败。

`tracing_enabled` 是 Executor/Web 进程启动时的追踪意图，只由 YAML 决定；
`POR_TRACING_ENABLED` 不再覆盖它。Run 冻结快照会记录该值，但不会重新初始化
进程级 Tracing；正式 Run 与 smoke Run 共用一个 Executor 时共享同一追踪状态，
需要独立关闭 smoke 追踪时应使用单独的 Executor 进程和 `config.smoke.yaml`。

正式配置默认启用本地 OCR 多模态；如果机器没有 Tesseract，`por doctor` 会报
`ERROR`，此时安装 Tesseract 或在 `config.yaml` 中显式设置
`rag_multimodal_provider: disabled`。`config.smoke.yaml` 已关闭多模态。

```powershell
por doctor
```

`por doctor` 检查正式配置、模型凭据、检索服务、RAG 后端、插件/MCP、预算/超时以及
PostgreSQL/DBOS 状态，并打印最终生效配置摘要。`ERROR` 时不会提交研究。

### 4. 启动 DBOS Executor，然后从终端运行

按 [DBOS 部署说明](docs/architecture/dbos-deployment.md) 准备 PostgreSQL 并初始化，
启动一个 Executor（Web 服务不是必需入口）：

```powershell
.venv/Scripts/python.exe -m open_deep_research.web.executor --executor-id workstation-1
```

CLI 直接调用 `RunClient`，经 `DBOSClient` 入队，不经过 FastAPI：

```powershell
por run "调研某品牌近期舆情"
por status <run-id>
por inspect <run-id>
por approve <run-id>
por clarify <run-id>
por report <run-id>
por retry <run-id>
```

首次真实端到端验证使用 `por smoke "调研某品牌近期舆情"`。它固定读取
`config.smoke.yaml`，但仍然走完整的 RunClient → DBOS → Executor → Agents SDK 链路。
`run`/`smoke` 只接受研究主题、Run ID、审批和澄清内容等业务数据，不接受
`--model`、`--rag`、`--mode` 等配置覆盖参数。

### 5. 本地基线验证（第六轮验收路径）

提交真实研究前先做环境自检；`doctor` 只读配置与凭据，不写数据库：

```powershell
uv run por doctor --config config.smoke.yaml
```

如需初始化数据库，先确认 `DBOS_DATABASE_URL` 指向隔离实例，再执行：

```powershell
uv run python -m open_deep_research.web.executor --initialize-only
uv run python -m open_deep_research.web.executor --executor-id workstation-1
```

Smoke 的结论以最终结果为准，`CLI 提交成功 ≠ 研究成功`。拿到 Run ID 后依次核对：

```powershell
uv run por status <run_id> --json  # status、settlement 和 events_complete
uv run por inspect <run_id>    # 模型尝试 / 工具调用 / 操作回执是否入账
uv run por report <run_id>     # Final Report 是否存在且完整
```

验收要点：Final Report 非空且带来源；Public Signal 实际调用了检索工具；Risk /
Strategy 真实完成；Usage 完整或明确标记 `usage_complete=false`；终态交付完成
（`events_complete=true`），且所有外部副作用都已确认。失败时用 `por inspect` 加
Executor 日志定位环节，不要为跑通而无限制放宽预算。完整链路与故障诊断命令见
[执行架构](docs/architecture/agent-loop.md) 与 [部署说明](docs/architecture/dbos-deployment.md)。

第六轮已用上述路径完成两次真实端到端验收（隔离 PostgreSQL、`config.smoke.yaml`、
真实 DeepSeek 与 Tavily）：Run `b3321ea7…` 与 `d3375523…` 均达到 `completed`，
`settlement=settled`，Plan → 公开信号检索 → Review → Risk →
Strategy → Final Report 全部真实执行；账本 61 次模型尝试 / 6 次工具调用与
PostgreSQL 完全一致。已知限制见 [执行架构](docs/architecture/agent-loop.md#已知限制)。

## 🔭 可观测性与预算

DBOS journal/Step/Stream 持久化执行、阶段、错误和重试。研究历史与证据、工具操作回执共用 Run ID。
SDK hooks 提供角色执行事实。所有推理使用 SDK Usage 写入模型尝试收据，未知 token 标记为未确认。OpenTelemetry 可按进程启用，只导出经过筛选的诊断信息。LangSmith 仍可用于 RAG 等非模型业务追踪。
详见 [观测说明](docs/architecture/observability.md)。

## 🧩 模块结构

```
src/open_deep_research/
├── configuration.py            # 配置字段、类型与校验
├── config_file.py              # config.yaml / config.smoke.yaml 加载与 Run 快照
├── preflight.py                # 启动前检查（doctor / run / smoke）
├── cli.py                      # por 终端入口（直达 RunClient）
├── state.py                    # 四个业务状态域与消息
├── prompts.py                  # 系统提示词
├── utils.py                    # 旧工具导入路径的兼容导出
├── budget.py                   # 预算守卫
├── workflow/                   # DBOS 编排、持久步骤、纯状态归并与业务服务
├── runtime/                    # SDK Runner 装配、Context Harness 与滚动压缩
├── public_opinion_agents/      # 两个研究 Agent 的提示词、权限与输出合同
├── rag/                        # 企业知识加载、索引、向量/关键词检索与重排
├── models/                     # 共享 SDK Model 工厂与供应商协议
├── llm/                        # 有界 SDK 生成、上下文窗口和错误处理
├── search/                     # Tavily 搜索与网页摘要
├── mcp/                        # 协议连接、工具命名和领域准入
├── observability/              # 可选 LangSmith 业务观测
├── tools/                      # 工具注册与 RAG 工具
└── web/                        # FastAPI、Run/History/SSE、Executor、CLI 与静态 UI
```

示例报告统一保存在 [docs/examples](docs/examples/arxiv.md)；`examples/rag_local_search.py` 是本地检索示例。历史迁移、评测基线和学习资料保留原有版本边界，清理范围见 [仓库审计](docs/audits/post-sdk-repository-cleanup.md)。

## ⚙️ 核心配置

### LLM 模型

| 角色 | `config.yaml` 默认模型 | 说明 |
|------|------------------------|------|
| Research / Plan / Review / Analysis | `deepseek:deepseek-flash` | 双研究 Agent、Brief、Review、风险评估与回应策略 |
| Compression | `deepseek:deepseek-flash` | 研究内容压缩 |
| Final Report | `deepseek:deepseek-flash` | 最终报告生成 |
| Summarization / RAG rewrite / Rolling compaction | `volcengine:doubao-seed-2-0-mini-260428` | 高频辅助调用 |
| Vision | `volcengine:doubao-seed-2-0-mini-260428` | 图片理解与 RAG 多模态 |

模型引用采用 `provider:model` 格式，各任务从 `config.yaml` 读取模型。两个研究 Agent
由 DBOSRunner 完成多轮推理和工具调用。规划、Review、风险评估、回应策略、报告、
图片识别、RAG 改写和摘要直接调用 SDK Model。DBOS 保留工作流中的调用结果。两条路径通过 `sdk_model()` 创建模型，
共享预算准入和 Usage 记账。`por doctor` 检查这些模型的供应商、地址及凭据配置。
[供应商兼容范围](docs/architecture/model-providers.md)

### 搜索 API

当前 SDK 研究路径使用 [Tavily](https://tavily.com/)。`config.yaml` 中
`search_api: tavily` 且 `TAVILY_API_KEY` 缺失时，`por doctor` 与 `por run` 会在提交前失败。

### RAG 配置示例

RAG 的开关、模式、Embedding、VectorStore、Reranker、索引路径和多模态统一在
`config.yaml` 的 `rag_*` 字段中配置；Python `Configuration` 只解析和校验这一份快照：

```yaml
rag_enabled: true
retrieval_mode: hybrid          # web_only / rag_only / hybrid
rag_knowledge_base_paths: [data/knowledge/public_opinion]
rag_top_k: 4
rag_embedding_provider: sentence_transformers
rag_vectorstore_provider: milvus
rag_reranker_provider: cross_encoder
rag_multimodal_enabled: true
rag_query_rewrite_enabled: true
```

### 预算守卫

```yaml
budget_enabled: true
max_model_calls: 120
max_tool_calls: 160
max_search_calls: 120
max_input_tokens: 4000000
max_output_tokens: 400000
reserve_final_report_call: true
```

## 🖥️ RAG MCP Server

独立启动 RAG MCP 服务：

```bash
# stdio 传输
python -m open_deep_research.rag.mcp_server

# HTTP 传输
python -m open_deep_research.rag.mcp_server --transport streamable-http --host 127.0.0.1 --port 8000
```

暴露的工具：
- `rag_search` — 检索本地知识库
- `rag_ensure_indexed` — 构建/刷新索引
- `rag_status` — 查看索引状态
- `rag_list_sources` — 列出知识源
- `rag_reset_cache` — 重置缓存

## 🌐 Local Web UI（可选的次要入口）

CLI 是主要运行入口。前端源码暂时保留，但 Web 请求不再覆盖 `config.yaml` 的模型、
RAG、模式或审批开关；旧表单字段会被接受但忽略。

### 数据库

研究运行只需要 PostgreSQL。`por_dbos` 保存原生执行记录，`por_domain` 保存
请求、授权和调用回执。部署连接、初始化与 Executor 启动见
[部署说明](docs/architecture/dbos-deployment.md)。RAG 与运行存储独立；RAG 关闭时
`internal_knowledge` 必须同时从 `enabled_business_agents` 中移除，否则启动前检查报错。

### 启动页面

先按上文部署 PostgreSQL 和 DBOS Executor，再启动 FastAPI 页面：

```bash
python -m open_deep_research.web.server
```

默认只绑定 `127.0.0.1:8000`。如果通过反向代理或其他方式对外提供服务，
必须配置 `PUBLIC_OPINION_API_TOKEN` 才能接纳研究；对外服务请使用强随机令牌，并在代理层启用 HTTPS 和认证；
不要直接将未认证的 Demo 服务暴露到公网。客户端只能选择服务端允许的模型，
研究请求也有输入大小和并发保护。

### 研究历史

**研究历史** 按时间分页，可打开报告或已完成的阶段产物。Classic 页面可下载、复制报告，
也可通过 **沿用参数** 填入原研究主题、组织背景和 RAG 选项。

历史列表显示已交付终态的 Run。详情复用 Run 读取逻辑，从 DBOS 与业务账本获取请求参数、
报告、已知用量和错误信息。历史查询是只读操作，保留期取决于对应的 DBOS 记录和业务账本。
在页面的 **访问设置** 中填写 `PUBLIC_OPINION_API_TOKEN`，历史和 Run API 使用同一认证及 owner 范围。
 [历史 API 和数据保留](docs/architecture/dbos-deployment.md#历史查询与数据保留)

## 📱 社媒采集插件

社媒数据由 `plugins/social-research/` 声明的 Apify 官方 MCP 提供；设置 `APIFY_TOKEN` 后，
`public_signal` 可通过 `load_skill` 加载小红书、抖音、微博方法，并按 Actor 发现、详情、
执行、运行查询和数据集读取的顺序取证。未配置令牌时插件保留 Skills，但社媒工具不可用。
详见 [社媒采集指南](docs/guide/social-media-skill.md)。

## 📊 数据目录

```
data/
├── knowledge/public_opinion/   # 舆情知识库
│   ├── compliance/             #   合规文档
│   ├── faq/                    #   常见问题
│   ├── historical_cases/       #   历史案例
│   ├── pr_playbooks/           #   PR 预案
│   └── product_docs/           #   产品文档
└── indexes/rag/                # RAG 索引（可重建）
```

## 🧪 测试

```bash
# 默认离线验证（不读取仓库 .env，不访问真实外部服务）
python -m pytest

# Active production source lint
python -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py tests/test_*.py

# 编译与 import smoke
python -m compileall -q src/open_deep_research
python -c "import open_deep_research.runtime; import open_deep_research.rag; import open_deep_research.workflow"

# 需要网络下载模型或外部基础设施的集成测试
python -m pytest -m integration

# RAG 检索评估
python tests/evaluate_rag_retrieval.py

# 舆情 Agent 测试
pytest tests/test_public_opinion_agents.py

# 插件、Skill 与 Apify MCP 装配测试
pytest tests/test_plugins.py

# 预算守卫测试
pytest tests/test_budget.py
```

## 📖 文档

VitePress 文档站：`docs/`

```bash
cd docs
npm install
npx vitepress dev
```

## 📝 许可证

MIT License — 详见 [LICENSE](LICENSE)

## 🙏 致谢

本项目基于 [LangChain AI / Open Deep Research](https://github.com/langchain-ai/open_deep_research) 扩展开发。
