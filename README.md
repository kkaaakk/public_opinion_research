# 🔬 Public Opinion Research

基于 LangGraph、RAG、MCP 和多 Agent 协作的企业舆情风险研究与处置辅助系统。支持多模型提供商、多种搜索工具和 MCP (Model Context Protocol) 服务器。在原版 [Open Deep Research](https://github.com/langchain-ai/open_deep_research) 基础上，扩展了本地 RAG 知识库、舆情分析、社交媒体监控、长期记忆等能力。

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
git clone https://github.com/kkaaakk/open_deep_research.git
cd open_deep_research
uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
```

### 2. 安装依赖

```bash
uv sync
```

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

## 🔭 Agent Observer 可观测性

可选的 Observer 集成默认关闭；开启后通过 `AGENT_OBSERVER_ENABLED=1` 或
Studio 配置中的 `agent_observer_enabled=true` 上报到本地 Observer（默认
`http://127.0.0.1:8766`）。集成层保持 fail-open，不会改变业务 Agent 的返回值、
工具 schema 或 LangGraph 图导出。

Model execution 统一经过 `src/open_deep_research/observability/` 的 boundary，覆盖：

- 主图模型调用、RAG query rewrite、Vision/multimodal loader、网页摘要和 structured output；
- structured output 优先使用 provider 实际返回的 usage；wrapper 没有 raw usage 时保持 `N/A`，不估算 token；
- ContextVar guard 抑制 wrapper 嵌套造成的 Model 双计数，Model Call 以逻辑/provider boundary 为准。

Tool execution 统一由 researcher 的 `execute_tool_safely` 调用
`observe_tool_ainvoke`，覆盖内置 RAG、MCP、社交媒体及其他运行时 LangChain tools。
Observer 默认只记录工具名、参数摘要、duration、成功/失败和 bounded result bytes，
不记录完整工具结果；MCP namespace wrapper 保留原有 schema、metadata 和行为。

生命周期语义分开记录：

- 每次 LangGraph node attempt 都有独立 Span，并带 `node_name`、`attempt`、`retry`；
- `interrupt()` 记录为 `INTERRUPTED`，不会变成 `FAILED` 或 `COMPLETED`；
- resume 是新的 runtime invocation/physical segment，通过同一个
  `logical_run_id` 与原工作流关联，避免把 Python Run 对象写入 checkpoint/state；
- `thread_id` 与 LangGraph checkpoint 用于 correlation，normal/failure/retry/interrupt/resume
  路径都会清理当前 Run/Span Context。

独立启动的 RAG/MCP server 没有当前 Public Opinion Run 时不会强行绑定 Observer；
只有实际嵌入一次研究运行的调用才会出现在该 Run 中。P0-2 的 `role_reports` 仍是
Section Writer 的正式当前运行输入，`agent_memories` 只作为 compact/private memory。

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

支持 OpenAI、Anthropic、Google、Groq、DeepSeek 等所有通过 `init_chat_model()` 接入的模型。

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
# 运行所有测试
pytest

# RAG 检索评估
pytest tests/evaluate_rag_retrieval.py

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
