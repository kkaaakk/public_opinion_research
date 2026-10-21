# 🔬 Public Opinion Research

基于 LangGraph、RAG、MCP 和多 Agent 协作的企业舆情风险研究与处置辅助系统。支持多模型提供商、多种搜索工具和 MCP (Model Context Protocol) 服务器。在原版 [Open Deep Research](https://github.com/langchain-ai/open_deep_research) 基础上，扩展了本地 RAG 知识库、舆情分析、社交媒体监控、长期记忆等能力。

## 当前开发方向（2026-10-21）

OpenAI Agents SDK + Python 确定性 Workflow + Research Graph Context Harness 已完成 Phase 0–4。
现有 Web/Runtime 默认保持兼容；页面可显式选择后台研究，支持计划审批、刷新后恢复进度与取消清算。
新链路需要配置受信访问令牌、持久 `RUN_DATABASE_URL`，并独立运行
`python -m open_deep_research.web.worker`。多进程/多主机可使用 MySQL/InnoDB，
本机可使用文件 SQLite；不确定工具效果的中断不会自动重放。
部署命令、恢复范围及验证证据见 [Phase 4 审计](docs/audits/openai-agents-phase4.md)。
单操作员 Token 不等于多租户身份；Phase 5 的旧 Runtime/依赖退役尚未实施。
DeerFlow 已退役，独立分支中的 RAG 配置和 DSH 轨迹成果保留。
详见[清理与测试记录](docs/audits/pre-openai-sdk-cleanup.md)和[SDK 迁移计划](docs/architecture/openai-agents-sdk-migration-plan.md)。

## 🆕 扩展功能

### 📚 本地 RAG 知识库
独立运行的本地 RAG 流程，支持文档加载、语义检索、GraphRAG 扩展和重排序：
- **知识源**: Markdown / JSON / PDF / 图片 / 源码文件
- **向量存储**: Milvus / FAISS / Chroma
- **GraphRAG**: Neo4j 图扩展 or 内存图
- **多模态**: OCR + Vision 图片理解和查询
- **MCP Server**: 暴露 `rag_search` / `rag_ensure_indexed` / `rag_status` 等工具

### 📰 舆情分析 Agent
面向品牌/产品舆情监控的多智能体系统：
- `PublicSignalAgent` — 舆情信号采集
- `InternalKnowledgeAgent` — 内部知识检索
- `RiskAssessmentAgent` — 风险评估
- `ResponseStrategyAgent` — 应对策略生成
- 内置知识库：合规文档、FAQ、历史案例、PR 预案

### 📱 社交媒体 MCP
- 社交媒体数据采集与搜索 MCP Server
- Apify 适配器对接真实平台数据（Twitter/X 等）
- 情感分析、主题提取、风险检测

### 🧠 长期记忆
- 文件记忆（JSONL）和 MySQL 双后端
- 对话摘要和持久记忆自动提取
- 记忆回溯与检索增强

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

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 API Key
```

### 4. 启动 LangGraph 开发服务器

完成 `uv sync` 后，Unix/macOS 可直接运行：

```bash
uv run langgraph dev --allow-blocking
```

Windows 若遇到 `uv` trampoline 或独立工具环境无法解析项目依赖，请使用已同步的虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m langgraph_cli dev --allow-blocking
```

项目的 `pyproject.toml` 包含 `mcp>=1.9.4` 的 uv dependency override；因此不建议使用无法应用该 override 的独立 `uvx` 环境作为唯一启动方式。

访问：
- 🚀 API: http://127.0.0.1:2024
- 🎨 Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
- 📚 API Docs: http://127.0.0.1:2024/docs

## 🔭 可观测性：LangSmith + budget.py

项目只有两套观测职责，不再有其他遥测系统：

- **LangSmith（执行链路观测）**：通过官方 `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` /
  `LANGSMITH_PROJECT` 环境变量开启。LangGraph / LangChain 原生 auto-tracing 覆盖
  Root trace、Graph node、Agent、LLM、Tool、MCP；本地 RAG 内部阶段（query rewrite、
  vector/bm25 retrieval、rerank 等）通过 `src/open_deep_research/observability/`
  的薄层 `trace_span` 记录，不重复包装 LLM/Tool 调用。
- **budget.py（Token / Usage / Budget）**：基于 LangChain 官方 callback
  （`ModelAttemptCounter` + `UsageMetadataCallbackHandler` + `add_usage`）统计
  model calls、input/output tokens、cache tokens，并执行预算策略。token 只记录
  provider 实际上报值，不做估算。

LangSmith tracing 默认关闭；未配置 API key 时自动归一化为关闭状态，业务行为不受影响。
详见 `docs/architecture/observability.md`。

## 🧩 模块结构

```
src/open_deep_research/
├── deep_researcher.py          # 主 LangGraph 流程入口
├── configuration.py            # 配置管理
├── state.py                    # 图状态定义
├── prompts.py                  # 系统提示词
├── utils.py                    # 工具函数
├── budget.py                   # 预算守卫
├── rag/                        # 🆕 RAG 知识库模块
│   ├── graph.py                #   RAG 图流程
│   ├── graph_adaptive.py       #   自适应检索
│   ├── graph_scoring.py        #   检索评分
│   ├── graph_structured.py     #   结构化检索
│   ├── graph_terms.py          #   术语图检索
│   ├── indexer.py              #   索引构建
│   ├── query_rewriter.py       #   查询重写
│   ├── memory.py / memory_writer.py  # 记忆管理
│   ├── mcp_server.py           #   RAG MCP 服务
│   ├── elasticsearch_bm25.py   #   BM25 检索
│   ├── code_languages.py       #   代码语言支持
│   └── loaders/                #   数据加载器
├── public_opinion_agents/      # 🆕 舆情分析 Agent
│   ├── registry.py             #   Agent 注册中心
│   ├── base.py                 #   基类
│   ├── risk_assessment.py      #   风险评估
│   ├── public_signal.py        #   舆情信号
│   ├── internal_knowledge.py   #   内部知识
│   └── response_strategy.py    #   应对策略
├── social_media/               # 🆕 社交媒媒体
│   └── tools.py                #   社交媒媒体工具
├── social_media_mcp.py         # 🆕 社交媒媒体 MCP Server
├── social_media_apify_api.py   # 🆕 Apify 数据适配器
├── memory/                     # 🆕 长期记忆系统
│   ├── store.py                #   存储后端
│   ├── extractor.py            #   记忆提取
│   └── writer.py               #   记忆写入
├── mcp/                        # 🆕 MCP 工具集
│   ├── tools.py                #   通用 MCP 工具
│   └── domain_filter.py        #   域名过滤
├── web/                        # 🆕 Web 管理面板
│   ├── server.py               #   FastAPI 服务
│   └── static/                 #   前端静态资源
├── tools/                      # 工具注册
│   └── rag_tool.py             #   RAG 检索工具
└── skills/                     # 🆕 技能模块
```

## ⚙️ 核心配置

### LLM 模型

| 角色 | 默认模型 | 说明 |
|------|---------|------|
| Summarization | `openai:gpt-4.1-mini` | 搜索结果摘要 |
| Research | `openai:gpt-4.1` | 搜索智能体 |
| Compression | `openai:gpt-4.1` | 研究内容压缩 |
| Final Report | `openai:gpt-4.1` | 最终报告生成 |

支持 OpenAI、Anthropic、Google、Groq、DeepSeek，以及通过 OpenAI 兼容接口接入的火山方舟。运行时代码统一通过 `open_deep_research.models.create_chat_model()` 创建 LangChain `BaseChatModel`；模型引用采用 `provider:model` 格式。

推荐的双模型路由是：网页摘要、RAG 查询改写、Research Graph 抽取、上下文管理和滚动压缩使用 `volcengine:doubao-seed-2-0-mini-260428`；研究 Agent、研究压缩和最终报告使用 `deepseek:deepseek-flash`。Volcengine 引用既支持预置 Model ID（如 `volcengine:doubao-seed-2-0-mini-260428`），也支持自定义 endpoint ID（如 `volcengine:ep-...`），工厂只剥离 provider 前缀。火山方舟需要 `ARK_API_KEY`，可用 `ARK_BASE_URL` 覆盖默认端点；DeepSeek 需要 `DEEPSEEK_API_KEY`。真实 API 冒烟测试仅在显式设置 `RUN_LIVE_MODEL_TESTS=1` 后运行，例如 `python scripts/model_provider_smoke.py --provider volcengine --model doubao-seed-2-0-mini-260428`。

### 搜索 API

默认使用 [Tavily](https://tavily.com/)，同时支持 OpenAI/Anthropic 原生搜索、DuckDuckGo、Exa，以及完整 MCP 兼容。

### RAG 配置示例

```python
config = {
    "configurable": {
        "rag_enabled": True,
        "retrieval_mode": "hybrid",          # web_only / rag_only / hybrid
        "rag_knowledge_base_paths": ["./data/knowledge"],
        "rag_memory_enabled": True,
        "rag_memory_paths": ["./data/memory/chat_memory.jsonl"],
        "rag_top_k": 4,
        "rag_chunk_size": 800,
        "rag_embedding_provider": "sentence_transformers",
        "rag_vectorstore_provider": "milvus",
        "rag_reranker_provider": "cross_encoder",
        "rag_graph_enabled": True,
        "rag_graph_backend": "neo4j",
        "rag_multimodal_enabled": True,
        "rag_query_rewrite_enabled": True,
    }
}
```

### 预算守卫

```python
config = {
    "configurable": {
        "budget_enabled": True,
        "max_model_calls": 12,
        "max_tool_calls": 20,
        "max_search_calls": 10,
        "max_input_tokens": 120_000,
        "max_output_tokens": 30_000,
        "reserve_final_report_call": True,
    }
}
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
- `rag_index_pending_memories` — 索引待处理记忆
- `rag_status` — 查看索引状态
- `rag_list_sources` — 列出知识源
- `rag_reset_cache` — 重置缓存

## 🌐 Local Web UI

### 用 Docker 启用 Research Graph

先启动 Docker Desktop，然后在项目根目录执行：

```powershell
# 生成本地数据库密码并配置连接；密码只保存在已被 Git 忽略的 .env 中
.\.venv\Scripts\python.exe scripts/neo4j_local.py configure

# 创建 Neo4j 5.26 LTS 容器，等待数据库健康检查通过
docker compose --env-file .env -f compose.neo4j.yaml up -d --wait --wait-timeout 120

# 验证真实 Neo4j 的图写入、溯源关系、检索与运行隔离
.\.venv\Scripts\python.exe scripts/neo4j_local.py verify
```

容器名为 `public-opinion-neo4j`，浏览器入口是 `http://127.0.0.1:7474`，
Bolt 地址是 `bolt://127.0.0.1:7687`。登录用户名为 `neo4j`，密码查看 `.env`
中的 `RESEARCH_GRAPH_PASSWORD`。Compose 只向本机开放端口，使用命名数据卷
保存数据库和日志，并在 Docker 重启后自动恢复容器。

验证脚本不调用模型 API，验证后清理本次生成的临时测试图。它会同时检查四个业务
Agent 使用 Neo4j 的初始化路径，但不会替代真实研究请求的模型调用验证。
节点和关系使用社区版支持的复合唯一约束，图标识字段由项目数据模型校验。

Research Graph 默认开启，连接参数由项目根目录的 `.env` 自动读取，日常启动无需
设置 `RESEARCH_GRAPH_ENABLED`。首次配置后重启 Web 服务即可；如果旧终端曾临时
关闭该功能，使用一个新终端启动，避免沿用旧的环境变量覆盖值。

停止数据库可执行 `docker compose -f compose.neo4j.yaml stop`，再次启动使用上面的
`up` 命令。命名数据卷会保留；不要使用 `down --volumes`，除非明确要删除数据库。

Research Graph 与页面的 **Enable RAG** 是独立功能：前者管理本次研究的证据图，
后者检索本地知识库。关闭 RAG 时，Web 流程会跳过依赖本地知识库的内部知识 Agent。

### 启动页面

启动独立的 FastAPI Demo 页面：

```bash
python -m open_deep_research.web.server
```

默认只绑定 `127.0.0.1:8000`。如果通过反向代理或其他方式对外提供服务，
请设置强随机的 `PUBLIC_OPINION_API_TOKEN`，并在代理层启用 HTTPS 和认证；
不要直接将未认证的 Demo 服务暴露到公网。客户端只能选择服务端允许的模型，
研究请求也有输入大小和并发保护。

### 研究历史

页面左侧的 **研究历史** 支持搜索主题或报告内容、按时间分页、重新打开完整报告、
下载和复制报告，以及通过 **沿用参数** 填入原研究主题、模型、检索方式和组织背景。
成功、失败和主动停止的运行都会保留；未生成最终报告时，可查看中断前完成的阶段报告。
刷新页面或重启 Web 服务后，已归档的记录仍可查看；打开历史记录不会重新调用模型。

历史档案使用现有 **Neo4j**，复用 `RESEARCH_GRAPH_URI`、`RESEARCH_GRAPH_USERNAME`、
`RESEARCH_GRAPH_PASSWORD` 和 `RESEARCH_GRAPH_DATABASE`，无需配置另一套数据库。
完整报告保存在独立的 `ResearchHistory` 节点中，以研究 `run_id` 对应同次研究的证据图。
每次网页研究结束或中断后自动归档，历史列表展示运行状态和时间，详情保留参数、
已有 token 用量及原始阶段报告。阶段输出从子图逐条保存，后续 Agent 失败不会让
已完成的采集或评估报告从历史记录中消失。
即使关闭研究图构建，已配置的 Neo4j 仍可用于报告归档。

已有的研究可以点击 **导入 LangSmith**，从 `LANGSMITH_PROJECT` 指定的项目中读取
已经结束的研究 trace，分批保存到 Neo4j，包含失败运行。对没有最终报告的运行，
会沿同一个 trace 查询已完成的 Agent 输出，保留不同研究轮次的原始报告，并标明
失败阶段；这些内容以“研究阶段记录”展示。导入使用现有 LangSmith
访问凭据，重复导入不会重复创建记录，也不会覆盖已保存的最终报告；可补充后来
恢复的阶段输出。旧工作流的名称变化不会导致已有最终报告被过滤。LangSmith trace 有
[数据保留期限](https://support.langchain.com/articles/8458147520-can-i-recover-deleted-traces-in-langsmith)，
因此仅作为旧报告导入来源；已导入的历史通过 Neo4j 保存。

Neo4j 暂时不可用时，页面会显示历史读取或保存失败的提示，已生成的报告仍能下载。
启用 `PUBLIC_OPINION_API_TOKEN` 后，在页面的 **访问设置** 中填写令牌；历史列表、
报告详情、LangSmith 导入和研究 API 都使用同一认证。令牌仅在当前页面使用。

## 📱 社交媒媒体 MCP Server

```bash
python -m open_deep_research.social_media_mcp --transport streamable-http --host 127.0.0.1 --port 8001
```

## 📊 数据目录

```
data/
├── knowledge/public_opinion/   # 舆情知识库
│   ├── compliance/             #   合规文档
│   ├── faq/                    #   常见问题
│   ├── historical_cases/       #   历史案例
│   ├── pr_playbooks/           #   PR 预案
│   └── product_docs/           #   产品文档
├── memory/                     # 长期记忆存储
│   ├── chat_memory.jsonl       #   对话记忆
│   └── public_opinion/         #   舆情记忆
├── social_media/               # 社交媒媒体数据
│   ├── sample_posts.jsonl      #   示例帖子
│   └── bulk_mock_posts.jsonl   #   批量模拟数据
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
python -c "import open_deep_research.deep_researcher; import open_deep_research.runtime; import open_deep_research.rag"

# 需要网络下载模型或外部基础设施的集成测试
python -m pytest -m integration

# RAG 检索评估
python tests/evaluate_rag_retrieval.py

# 舆情 Agent 测试
pytest tests/test_public_opinion_agents.py

# 社交媒体测试
pytest tests/test_social_media_skill.py

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
