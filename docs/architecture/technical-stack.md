# Open Deep Research 当前技术栈说明

更新时间：2026-05-27

本文档基于当前代码和 `pyproject.toml` 重写，用来替代旧版技术流程文档。它关注“现在项目实际用了什么技术、默认走哪条链路、哪些能力是可选后端”，不再保留旧实现里的过时描述。

::: tip
如果只想判断当前默认链路，优先看第 22 节“当前默认推荐栈”；如果要改代码结构，先看第 1 节总体分层和第 3 节 Agent 编排。
:::

## 本页速览

| 项目     | 内容                                                                                       |
| -------- | ------------------------------------------------------------------------------------------ |
| 阅读目标 | 建立当前项目技术栈全景，确认默认推荐链路和可选后端。                                       |
| 关键代码 | `pyproject.toml`、`src/open_deep_research/`、`tests/`                                      |
| 下游文档 | [Agent Loop 模块说明](agent-loop.md)、[RAG 模块说明](rag.md)、[Memory 模块说明](memory.md) |

这是 docs 目录的总技术背景文档。若要看阅读路径和文档地图，请先看 [文档导航](../index.md)。

## 1. 总体技术分层

当前项目可以按运行时职责分成六层：

| 层级         | 主要技术                                                                                 | 代码位置                                                           |
| ------------ | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Agent 编排层 | LangGraph、LangChain chat model、tool calling                                            | `src/open_deep_research/deep_researcher.py`                        |
| 工具层       | LangChain tools、Tavily、provider-native web search、MCP、RAG tool                       | `src/open_deep_research/utils.py`、`src/open_deep_research/tools/` |
| RAG 检索层   | loaders、splitter、embedding、vector store、BM25、GraphRAG、reranker、citation formatter | `src/open_deep_research/rag/`                                      |
| Memory 层    | MySQL 原始记忆、可索引 memory records、后台索引刷新                                      | `src/open_deep_research/memory/`                                   |
| 配置层       | Pydantic config、LangGraph RunnableConfig、环境变量                                      | `src/open_deep_research/configuration.py`                          |
| 测试与评估层 | pytest、自定义 RAG retrieval eval、deep research bench eval                              | `tests/`                                                           |

主流程仍是 deep research agent：

```text
user messages
  -> query image context
  -> clarification
  -> research brief
  -> report section plan
  -> research phase (Public Opinion subgraph)
  -> four role agents
  -> tools / RAG / web / MCP
  -> compressed research notes
  -> final report
  -> optional MySQL memory write
```

## 2. Python 与项目管理

### 2.1 Python 版本

`pyproject.toml` 当前声明：

```toml
requires-python = ">=3.10"
```

项目使用现代 Python 类型能力：

- `list[str]`
- `str | None`
- `TypedDict`
- Pydantic v2 风格 validator
- async / await

### 2.2 包管理

仓库包含：

- `pyproject.toml`
- `uv.lock`

常用命令仍以 README / AGENTS 中约定为准：

```bash
uvx langgraph dev
python tests/run_evaluate.py
ruff check
mypy
```

### 2.3 当前打包注意点

`pyproject.toml` 中 `tool.setuptools.packages` 当前显式列出：

```toml
packages = ["open_deep_research", "open_deep_research.rag", "legacy", "tests"]
```

但当前代码已经新增这些子包：

- `open_deep_research.memory`
- `open_deep_research.tools`
- `open_deep_research.rag.loaders`

如果后续要做 wheel / package 发布，需要验证这些子包是否被正确打进去。开发环境直接从源码运行通常不会暴露这个问题。

## 3. Agent 编排技术栈

### 3.1 LangGraph

项目用 LangGraph `StateGraph` 构建主图和一个 Public Opinion 子图：

- main graph：完整 deep research 工作流。
- Public Opinion subgraph：按固定业务拓扑执行四个角色 Agent。

核心文件：

- `src/open_deep_research/deep_researcher.py`
- `src/open_deep_research/state.py`

主 graph 节点：

| 节点                      | 职责                                   |
| ------------------------- | -------------------------------------- |
| `enrich_query_images`     | 将用户问题里的图片识别为临时文本上下文 |
| `clarify_with_user`       | 判断是否需要澄清                       |
| `write_research_brief`    | 生成结构化 research brief              |
| `plan_report_sections`    | 规划报告章节                           |
| `research_phase`          | 执行 Public Opinion 子图并回写结果     |
| `section_writer`          | 使用完整 role reports 撰写研究章节     |
| `write_final_sections`    | 撰写非研究型章节                       |
| `compile_final_report`    | 编译最终报告                           |

`research_phase` 不是 Supervisor Agent。它只负责把主图 `AgentState` 转换为
`PublicOpinionState`，进入并执行子图，再将 `role_reports`、`agent_memories`、
`notes`、`raw_notes` 和 `budget_usage` 转换回主图。

Public Opinion 子图的固定拓扑为：

```text
public_signal_agent + internal_knowledge_agent
    -> risk_assessment_agent
    -> response_strategy_agent
```

### 3.2 LangChain Chat Model

项目使用：

```python
from langchain.chat_models import init_chat_model
```

并创建一个可配置模型：

```python
configurable_model = init_chat_model(
    configurable_fields=("model", "max_tokens", "api_key"),
)
```

不同阶段可以使用不同模型配置：

- research model
- summarization model
- compression model
- final report model
- RAG query rewrite model
- RAG vision model

### 3.3 Structured Output

通过 Pydantic schema 约束模型输出：

- `ClarifyWithUser`
- `ResearchQuestion`
- `Summary`

通过 tool schema 驱动控制流：

- `ConductResearch`
- `ResearchComplete`

### 3.4 Budget Guard

当前项目有独立预算模块：

- `src/open_deep_research/budget.py`

支持限制：

- 模型调用次数
- 工具调用次数
- 搜索调用次数
- 输入 token
- 输出 token

关键策略：

- 可保留一次最终报告模型调用。
- 超预算工具调用会被合成 `ToolMessage` 替代。
- findings 过长时会截断以适配剩余 token budget。
- 最终报告可追加 budget summary。

## 4. 模型提供商技术栈

`pyproject.toml` 当前包含多家模型 provider 的 LangChain 集成：

| Provider                 | 依赖                                                  |
| ------------------------ | ----------------------------------------------------- |
| OpenAI                   | `langchain-openai`、`openai`                          |
| Anthropic                | `langchain-anthropic`                                 |
| Google / Gemini / Vertex | `langchain-google-genai`、`langchain-google-vertexai` |
| Groq                     | `langchain-groq`                                      |
| DeepSeek                 | `langchain-deepseek`                                  |
| AWS Bedrock              | `langchain-aws`                                       |

API key 解析逻辑主要在：

- `src/open_deep_research/utils.py`
- `src/open_deep_research/rag/query_rewriter.py`

支持从环境变量读取，也支持在 `GET_API_KEYS_FROM_CONFIG=true` 时从 RunnableConfig 的 `apiKeys` 中读取。

## 5. Web Search 技术栈

当前代码里实际配置枚举是：

```python
class SearchAPI(Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    TAVILY = "tavily"
    NONE = "none"
```

### 5.1 Tavily

依赖：

- `tavily-python`
- `langchain-tavily`

代码入口：

- `tavily_search(...)`
- `tavily_search_async(...)`

位置：

- `src/open_deep_research/utils.py`

流程：

1. 并发执行多个 Tavily query。
2. 按 URL 去重。
3. 对 raw content 调用 summarization model。
4. 返回 SOURCE / URL / SUMMARY 文本给 researcher。

### 5.2 OpenAI Native Web Search

当 `search_api=openai` 时，工具层向模型传入：

```python
{"type": "web_search_preview"}
```

这不是普通 LangChain tool，而是 provider-native tool schema。

### 5.3 Anthropic Native Web Search

当 `search_api=anthropic` 时，工具层向模型传入：

```python
{
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 5,
}
```

### 5.4 依赖存在但当前主配置未直接暴露的搜索库

`pyproject.toml` 还包含：

- `duckduckgo-search`
- `exa-py`
- `arxiv`
- `linkup-sdk`

这些依赖存在，但当前 `SearchAPI` 主枚举没有把它们作为一等 search provider 暴露。如果要启用，需要新增对应工具实现和配置项。

## 6. Retrieval Mode

当前 retrieval mode：

```python
class RetrievalMode(Enum):
    WEB_ONLY = "web_only"
    RAG_ONLY = "rag_only"
    HYBRID = "hybrid"
```

工具装配规则：

| retrieval mode | web_search                | rag_search                |
| -------------- | ------------------------- | ------------------------- |
| `web_only`     | 启用，取决于 `search_api` | 不启用                    |
| `rag_only`     | 不启用                    | `rag_enabled=True` 时启用 |
| `hybrid`       | 启用，取决于 `search_api` | `rag_enabled=True` 时启用 |

对应代码：

- `get_retrieval_tools(...)`
- `rag_requested(...)`
- `get_all_tools(...)`

位置：

- `src/open_deep_research/utils.py`

## 7. RAG 数据源技术栈

RAG 数据源统一被转换为：

```text
RAGDocument -> RAGChunk
```

核心类型：

- `RAGDocument`
- `RAGChunk`
- `RetrievalResult`
- `Citation`
- `AnswerReadyContext`

位置：

- `src/open_deep_research/rag/types.py`

### 7.1 本地知识库文件

入口：

- `load_documents_from_paths(...)`

位置：

- `src/open_deep_research/rag/loaders/knowledge.py`

支持：

| 类型     | 技术                                     |
| -------- | ---------------------------------------- |
| `.txt`   | Python 文件读取                          |
| `.md`    | Python 文件读取，后续 Markdown splitter  |
| `.json`  | `json` 标准库，后续 JSON 结构化切分      |
| `.pdf`   | PyMuPDF / `fitz`                         |
| 代码文件 | 扩展名映射 + LangChain language splitter |
| 图片文件 | Pillow + pytesseract + 可选 Vision LLM   |

### 7.2 PDF

依赖：

- `pymupdf`

技术点：

- 按页加载。
- 每页生成一个 `RAGDocument`。
- source 使用 `path#page=N`。
- metadata 保留 `page_number`、`page_count`、`path`。
- 图片型 PDF 页可渲染为图片后走 OCR / Vision。

### 7.3 图片与扫描内容

依赖：

- `pillow`
- `pytesseract`
- 可选 Vision model

配置：

- `rag_multimodal_enabled`
- `rag_multimodal_provider`
- `rag_ocr_languages`
- `rag_vision_enabled`
- `rag_vision_model`
- `rag_vision_prompt`
- `rag_vision_max_tokens`

当前图片路由会结合：

- 图像尺寸和特征
- OCR probe 文本长度
- 颜色数量
- entropy
- edge density
- 可选 Vision 分类

再决定走：

- OCR
- Vision
- OCR + Vision
- skip

### 7.4 文件型 Memory

入口：

- `load_memory_documents_from_paths(...)`

位置：

- `src/open_deep_research/rag/loaders/file_memory.py`

支持：

- `.json`
- `.jsonl`

用于把外部导出的聊天记忆或长期记忆文件接入 RAG。

### 7.5 MySQL Memory

入口：

- `load_memory_documents_from_mysql(...)`

位置：

- `src/open_deep_research/rag/loaders/mysql_memory.py`

它消费 `memory` 模块中的 `ChatMemoryRecord`，只把可索引类型转成 `RAGDocument`。

source 协议：

```text
memory://mysql/{conversation_id}/{memory_id}
```

## 8. 文档切分技术栈

位置：

- `src/open_deep_research/rag/splitter.py`

依赖：

- `langchain_text_splitters`

切分策略：

| 文件类型                   | 技术                                                     |
| -------------------------- | -------------------------------------------------------- |
| Markdown                   | `MarkdownHeaderTextSplitter` + recursive character split |
| JSON                       | 自定义 JSON leaf 遍历和 parent path 分组                 |
| Code                       | `RecursiveCharacterTextSplitter.from_language(...)`      |
| Plain text                 | `RecursiveCharacterTextSplitter`                         |
| PDF / image extracted text | 作为 plain text 切分                                     |

每个 chunk 保留：

- char range
- line range
- heading path
- page number
- json path / field paths
- source
- file type
- language
- content hash
- authority metadata

## 9. Embedding 技术栈

位置：

- `src/open_deep_research/rag/embeddings.py`

统一接口：

```python
class EmbeddingBackend:
    def embed_texts(...)
    def embed_query(...)
```

### 9.1 默认语义 embedding

依赖：

- `sentence-transformers`

默认 provider：

```text
sentence_transformers
```

默认模型：

```text
sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
```

特点：

- 支持中英文基础语义检索。
- 默认 normalize embeddings。
- 可配置 device。

### 9.2 Hash Embedding

provider：

```text
hash
```

用途：

- 单测。
- 离线诊断。
- 无模型下载环境下跑通流程。

注意：hash embedding 不具备真实语义理解能力，不应作为正式检索质量评估基线。

## 10. 向量库技术栈

位置：

- `src/open_deep_research/rag/vectorstore.py`

统一接口：

```python
class VectorStoreBackend:
    def is_ready(...)
    def add(...)
    def search(...)
```

### 10.1 默认：Milvus

依赖：

- `pymilvus`

默认 provider：

```text
milvus
```

默认本地 URI：

```text
data/indexes/rag/milvus.db
```

支持：

- Milvus Lite：本地 `.db`。
- Milvus server：`http://host:19530`。
- token。
- db name。
- metric type，默认 `COSINE`。

### 10.2 FAISS

依赖：

- `faiss-cpu`

provider：

```text
faiss
```

实现方式：

- `IndexFlatIP`
- `index.faiss`
- `chunks.json` sidecar 保存 chunk 原文和 metadata

### 10.3 In-memory Vector Store

provider：

```text
memory
```

用途：

- 单测。
- 临时实验。
- 无持久化。

### 10.4 Chroma

代码支持：

- `ChromaVectorStore`

provider：

```text
chroma
```

注意：当前 `pyproject.toml` 未列出 `chromadb` 依赖。如果要使用 `rag_vectorstore_provider=chroma`，需要额外安装 `chromadb`。

## 11. 关键词检索技术栈

### 11.1 内置 BM25

位置：

- `src/open_deep_research/rag/retriever.py`

类：

- `BM25Index`

特点：

- 无额外搜索服务依赖。
- 同时索引 chunk title 和 content。
- 英文按 word token。
- CJK 按单字切分，提供基础中文关键词召回。

默认 keyword backend：

```text
memory
```

### 11.2 Elasticsearch BM25

位置：

- `src/open_deep_research/rag/elasticsearch_bm25.py`

provider：

```text
elasticsearch
```

注意：当前 `pyproject.toml` 未列出官方 `elasticsearch` Python 包。如果要使用 `rag_keyword_backend=elasticsearch`，需要额外安装 `elasticsearch` 并启动 ES 服务。

## 12. Hybrid Retrieval 技术栈

位置：

- `HybridChunkRetriever`

流程：

1. 向量库召回 dense candidates。
2. BM25 / Elasticsearch 召回 keyword candidates。
3. 使用 Reciprocal Rank Fusion 融合排名。
4. 可选 GraphRAG 邻居扩展。
5. 结构化 metadata boost。
6. 返回候选给 reranker。

关键参数：

- `rag_keyword_top_k`
- `rag_rrf_rank_constant`
- `rag_structured_metadata_weight`

当前实现使用纯 RRF；dense 与 keyword 的原始分数不会直接相加。

## 13. GraphRAG 技术栈

位置：

- `src/open_deep_research/rag/graph.py`

支持后端：

| backend  | 技术                            |
| -------- | ------------------------------- |
| `memory` | 进程内 term co-occurrence graph |
| `neo4j`  | Neo4j 图数据库                  |

依赖：

- `neo4j`

默认配置：

```text
rag_graph_enabled = True
rag_graph_backend = "neo4j"
rag_neo4j_uri = "bolt://localhost:7687"
rag_neo4j_username = "neo4j"
```

作用：

- 不替代向量/BM25。
- 只在已有 seed results 的基础上扩展共享术语的邻居 chunk。
- 给复杂项目知识或实体关系补一点上下文。

如果没有可用 Neo4j 服务，需要把 `rag_graph_enabled=False` 或改成 `graph_backend=memory`，否则构建 graph index 会依赖外部 Neo4j。

## 14. Reranker 技术栈

位置：

- `src/open_deep_research/rag/reranker.py`

支持：

| provider                                 | 技术                                 |
| ---------------------------------------- | ------------------------------------ |
| `cross_encoder` / `bge` / `bge-reranker` | sentence-transformers `CrossEncoder` |
| `simple`                                 | keyword overlap reranker             |
| `none` / `disabled`                      | 不重排                               |

默认：

```text
rag_reranker_provider = "cross_encoder"
rag_reranker_model = "BAAI/bge-reranker-base"
```

reranker 输入不仅包含 chunk content，也包含 source、title、metadata 等结构化上下文。

## 15. Authority Rerank

位置：

- `src/open_deep_research/rag/splitter.py`
- `src/open_deep_research/rag/service.py`

技术点：

- splitter 推断 `source_status`。
- service 在 rerank 后做 authority adjustment。
- misleading / unanswerable trap 会被过滤。
- deprecated 会被降权。

状态：

- `authoritative`
- `deprecated`
- `misleading`
- `unanswerable_trap`

该机制用于避免废弃文档、误导性归档或测试陷阱进入最终引用。

## 16. Citation 与 Grounding 技术栈

位置：

- `src/open_deep_research/rag/citations.py`

输出：

- SOURCE 编号
- SOURCE path / memory URI
- CHUNK ID
- METADATA
- EXCERPT
- Sources 列表

关键设计：

RAG 工具返回的文本会明确要求：

```text
Use only the cited excerpts below for claims about local knowledge or chat memory.
```

这样约束会经过 researcher、compression、final report 多阶段传递，降低本地知识无引用扩写。

## 17. Query Rewrite 技术栈

位置：

- `src/open_deep_research/rag/query_rewriter.py`

默认模型：

```text
openai:gpt-4.1-mini
```

作用：

- 将用户问题改写成单条 standalone retrieval query。
- 保留专有名词、ID、路径、JSON path、表名等关键字。
- 不回答问题。
- 失败时回退原 query。

配置：

- `rag_query_rewrite_enabled`
- `rag_query_rewrite_model`
- `rag_query_rewrite_max_tokens`

## 18. Memory 技术栈

位置：

- `src/open_deep_research/memory/`

### 18.1 原始存储

依赖：

- `sqlalchemy`
- `pymysql`

后端：

- MySQL

默认表：

```text
rag_chat_memories
```

表里保存：

- chat raw transcript
- final report summary
- project facts
- preferences
- decisions
- constraints
- deprecated memory

### 18.2 可索引类型

默认可进入 RAG：

- `summary`
- `preference`
- `project_fact`
- `decision`
- `constraint`
- `deprecated`

默认不进入 RAG：

- `chat_raw`

### 18.3 写入时机

在 `final_report_generation` 成功后，如果：

```text
rag_memory_write_enabled=True
```

会调用：

```text
maybe_persist_chat_memory -> persist_conversation_memory
```

写入 MySQL。

### 18.4 后台索引

如果：

```text
rag_memory_write_sync_index=True
```

写入后会启动 daemon thread，调用 RAG pipeline 刷新 pending memory。

query-time `RAGIndexer.ensure_ready()` 仍是兜底。

## 19. MCP 技术栈

### 19.1 外部 MCP 工具客户端

依赖：

- `mcp`
- `langchain-mcp-adapters`
- `aiohttp`
- `supabase`

位置：

- `src/open_deep_research/utils.py`

功能：

- 读取 `mcp_config`。
- 通过 `MultiServerMCPClient` 拉取工具。
- 支持 auth required 的 token exchange。
- 避免工具名冲突。
- 包装 MCP authentication error。

### 19.2 RAG MCP Server

位置：

- `src/open_deep_research/rag/mcp_server.py`

依赖：

- `mcp.server.fastmcp.FastMCP`

暴露工具：

- `rag_search`
- `rag_ensure_indexed`
- `rag_index_pending_memories`
- `rag_status`
- `rag_list_sources`
- `rag_reset_cache`

支持 transport：

- `stdio`
- `sse`
- `streamable-http`

## 20. 配置技术栈

位置：

- `src/open_deep_research/configuration.py`

技术：

- Pydantic `BaseModel`
- `field_validator`
- `model_validator`
- LangGraph `RunnableConfig`
- 环境变量

配置来源：

- `.env`
- LangGraph Studio UI
- RunnableConfig 的 `configurable`

关键配置组：

- agent general config
- search config
- budget config
- RAG config
- multimodal config
- memory config
- model config
- MCP config

## 21. 测试与评估技术栈

测试工具：

- `pytest`
- `ruff`
- `mypy`

RAG 相关测试：

- `tests/test_rag.py`
- `tests/test_rag_mcp_server.py`
- `tests/test_rag_retrieval_evaluation.py`
- `tests/evaluate_rag_retrieval.py`
- `tests/rag_eval_cases.jsonl`
- `tests/rag_eval_cases_github.jsonl`

Budget 测试：

- `tests/test_budget.py`

Deep research bench：

- `tests/run_evaluate.py`
- `tests/evaluators.py`
- `tests/pairwise_evaluation.py`

## 22. 当前默认推荐栈

如果按当前配置默认值理解，本地 RAG 推荐栈是：

| 能力                | 默认技术                                                 |
| ------------------- | -------------------------------------------------------- |
| Agent orchestration | LangGraph                                                |
| Model invocation    | LangChain `init_chat_model`                              |
| Web search          | Tavily                                                   |
| Retrieval mode      | `web_only`，需要显式切到 `rag_only` 或 `hybrid` 才用 RAG |
| Embedding           | SentenceTransformers multilingual MiniLM                 |
| Vector store        | Milvus / Milvus Lite                                     |
| Keyword search      | 内置 BM25                                                |
| Reranker            | BGE cross-encoder                                        |
| GraphRAG            | Neo4j，需外部服务                                        |
| PDF                 | PyMuPDF                                                  |
| OCR                 | pytesseract + Pillow                                     |
| Memory store        | MySQL + SQLAlchemy + PyMySQL                             |
| MCP                 | FastMCP / LangChain MCP adapters                         |

## 23. 可选能力与额外依赖

这些能力当前代码支持，但需要注意依赖或外部服务：

| 能力                          | 需要                                                       |
| ----------------------------- | ---------------------------------------------------------- |
| Chroma vector store           | 额外安装 `chromadb`                                        |
| Elasticsearch keyword backend | 额外安装 `elasticsearch` Python 包并启动 ES                |
| Neo4j GraphRAG                | 安装并启动 Neo4j，配置密码                                 |
| OCR                           | 本机安装 Tesseract 可执行程序，Python 侧已有 `pytesseract` |
| Vision image extraction       | 配置可用的 vision-capable model 和 API key                 |
| MySQL memory                  | 可连接 MySQL，配置 `rag_memory_mysql_url`                  |

## 24. 与旧文档相比的关键变化

这份文档已按当前代码修正以下点：

- 默认向量库是 Milvus，不再把 Chroma 写成默认生产后端。
- RAG loader 已拆到 `rag/loaders/` 子包。
- Memory 已拆成独立 `memory/` 模块，MySQL 是原始记忆事实源。
- tools 已有独立 `tools/rag_tool.py`，旧的 `rag/tooling.py` 只是兼容导出。
- 当前 SearchAPI 主配置只暴露 Tavily、OpenAI native、Anthropic native、None。
- GraphRAG 当前支持 memory 和 Neo4j 两类后端。
- 多模态 RAG 已覆盖图片文件、图片型 PDF 页和用户问题图片临时上下文。
- Budget Guard 已成为 agent loop 的正式控制层。
