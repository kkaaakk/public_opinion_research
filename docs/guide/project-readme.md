# 🔬 Open Deep Research

::: tip
????? `README.md` ? VitePress ??????????????????????? GitHub ????????????
:::

<img width="1388" height="298" alt="full_diagram" src="https://github.com/user-attachments/assets/12a2371b-8be2-4219-9b48-90503eb43c69" />

Deep research has broken out as one of the most popular agent applications. This is a simple, configurable, fully open source deep research agent that works across many model providers, search tools, and MCP servers. It's performance is on par with many popular deep research agents ([see Deep Research Bench leaderboard](https://huggingface.co/spaces/Ayanami0730/DeepResearch-Leaderboard)).

<img width="817" height="666" alt="Screenshot 2025-07-13 at 11 21 12 PM" src="https://github.com/user-attachments/assets/052f2ed3-c664-4a4f-8ec2-074349dcaa3f" />

## 🔥 Recent Updates

**August 14, 2025**: See our free course [here](https://academy.langchain.com/courses/deep-research-with-langgraph) (and course repo [here](https://github.com/langchain-ai/deep_research_from_scratch)) on building open deep research.

**August 7, 2025**: Added GPT-5 and updated the Deep Research Bench evaluation w/ GPT-5 results.

**August 2, 2025**: Achieved #6 ranking on the [Deep Research Bench Leaderboard](https://huggingface.co/spaces/Ayanami0730/DeepResearch-Leaderboard) with an overall score of 0.4344.

**July 30, 2025**: Read about the evolution from our original implementations to the current version in our [blog post](https://rlancemartin.github.io/2025/07/30/bitter_lesson/).

**July 16, 2025**: Read more in our [blog](https://blog.langchain.com/open-deep-research/) and watch our [video](https://www.youtube.com/watch?v=agGiWUpxkhg) for a quick overview.

## 🚀 Quickstart

1. Clone the repository and activate a virtual environment:

```bash
git clone https://github.com/langchain-ai/open_deep_research.git
cd open_deep_research
uv venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

1. Install dependencies:

```bash
uv sync
# or
uv pip install -r pyproject.toml
```

1. Set up your `.env` file to customize the environment variables (for model selection, search tools, and other configuration settings):

```bash
cp .env.example .env
```

1. Launch agent with the LangGraph server locally:

```bash
# Install dependencies and start the LangGraph server
uvx --refresh --from "langgraph-cli[inmem]" --with-editable . --python 3.11 langgraph dev --allow-blocking
```

This will open the LangGraph Studio UI in your browser.

```text
- 🚀 API: http://127.0.0.1:2024
- 🎨 Studio UI: https://smith.langchain.com/studio/?baseUrl=http://127.0.0.1:2024
- 📚 API Docs: http://127.0.0.1:2024/docs
```

Ask a question in the `messages` input field and click `Submit`. Select different configuration in the "Manage Assistants" tab.

## ⚙️ Configurations

### LLM :brain:

Open Deep Research supports a wide range of LLM providers via the [init_chat_model() API](https://python.langchain.com/docs/how_to/chat_models_universal_init/). It uses LLMs for a few different tasks. See the below model fields in the [configuration.py](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py) file for more details. This can be accessed via the LangGraph Studio UI.

- **Summarization** (default: `openai:gpt-4.1-mini`): Summarizes search API results
- **Research** (default: `openai:gpt-4.1`): Power the search agent
- **Compression** (default: `openai:gpt-4.1`): Compresses research findings
- **Final Report Model** (default: `openai:gpt-4.1`): Write the final report

> Note: the selected model will need to support [structured outputs](https://python.langchain.com/docs/integrations/chat/) and [tool calling](https://python.langchain.com/docs/how_to/tool_calling/).

> Note: For OpenRouter: Follow [this guide](https://github.com/langchain-ai/open_deep_research/issues/75#issuecomment-2811472408) and for local models via Ollama see [setup instructions](https://github.com/langchain-ai/open_deep_research/issues/65#issuecomment-2743586318).

### Search API :mag:

Open Deep Research supports a wide range of search tools. By default it uses the [Tavily](https://www.tavily.com/) search API. Has full MCP compatibility and work native web search for Anthropic and OpenAI. See the `search_api` and `mcp_config` fields in the [configuration.py](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py) file for more details. This can be accessed via the LangGraph Studio UI.

### Other

See the fields in the [configuration.py](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py) for various other settings to customize the behavior of Open Deep Research.

### Optional Budget Guard

Open Deep Research can optionally enforce run-level budgets for model calls, tool calls, search calls, and observed input/output tokens. Budget Guard is disabled by default. When enabled, it uses a degrade-and-finish policy: stop starting new research units or extra retrieval calls, preserve a final report model call when configured, and append budget usage details to the final report.

Example LangGraph config:

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

### Optional Local RAG

Open Deep Research now includes an optional, standalone local RAG pipeline that can be enabled during the current mainline research phase without changing the overall search → research → write workflow.

What it adds:

- A dedicated `src/open_deep_research/rag/` module for local document and chat-memory loading, metadata-aware chunking, semantic embeddings, Milvus-backed persistent vector indexing, hybrid retrieval, GraphRAG expansion, reranking, multimodal OCR/Vision ingestion, and citation formatting
- A `rag_search` research tool that can sit alongside `web_search`
- Three retrieval modes for the researcher: `web_only`, `rag_only`, and `hybrid`

Supported local sources in the first version:

- `.txt`
- `.md`
- `.json`
- `.pdf` via PyMuPDF
- source code files such as `.py`, `.js`, `.ts`, `.java`, `.go`, `.rs`, `.rb`, `.php`, `.cpp`, `.c`, `.cs`, `.swift`, `.kt`, `.scala`, `.lua`, `.ps1`, `.html`, `.proto`, and `.sol`; these use LangChain's language-aware `RecursiveCharacterTextSplitter.from_language`
- image files (`.png`, `.jpg`, `.jpeg`, `.webp`, `.bmp`, `.tif`, `.tiff`) via image routing when `rag_multimodal_enabled` is true
- chat memory `.json` / `.jsonl` records via `rag_memory_paths`
- chat memory persisted as raw rows in MySQL via `rag_memory_mysql_url`

Default local RAG directories:

- `data/knowledge/` stores local knowledge-base files.
- `data/memory/chat_memory.jsonl` stores optional file-backed chat memory records.
- `data/indexes/rag/` stores rebuildable derived RAG indexes, including the default Milvus Lite file at `data/indexes/rag/milvus.db`.
- MySQL chat memory stays in the database configured by `rag_memory_mysql_url`; only its indexable rows are copied into the derived RAG index.
- Legacy paths such as `examples/rag_data` and `.rag_index` remain supported when explicitly configured, but emit a warning so deployments can migrate gradually.

Key configuration fields:

- `rag_enabled`
- `retrieval_mode`
- `rag_knowledge_base_paths`
- `rag_memory_enabled`
- `rag_memory_paths`
- `rag_memory_json_text_fields`
- `rag_memory_mysql_url`
- `rag_memory_mysql_table`
- `rag_memory_mysql_index_record_types`
- `rag_memory_write_enabled`
- `rag_memory_write_sync_index`
- `rag_top_k`
- `rag_chunk_size`
- `rag_chunk_overlap`
- `rag_rerank_top_n`
- `rag_embedding_provider`
- `rag_embedding_model`
- `rag_vectorstore_provider`
- `rag_vectorstore_path`
- `rag_milvus_uri`
- `rag_milvus_token`
- `rag_milvus_db_name`
- `rag_milvus_metric_type`
- `rag_reranker_provider`
- `rag_reranker_model`
- `rag_keyword_top_k`
- `rag_structured_metadata_weight`
- `rag_graph_enabled`
- `rag_graph_backend`
- `rag_graph_max_neighbors`
- `rag_graph_weight`
- `rag_neo4j_uri`
- `rag_neo4j_username`
- `rag_neo4j_password`
- `rag_neo4j_database`
- `rag_multimodal_enabled`
- `rag_multimodal_provider`
- `rag_ocr_languages`
- `rag_vision_enabled`
- `rag_vision_model`
- `rag_vision_prompt`
- `rag_vision_max_tokens`
- `rag_query_image_enabled`
- `rag_query_image_max_images`
- `rag_query_image_max_bytes`
- `rag_query_rewrite_enabled`
- `rag_query_rewrite_model`
- `rag_query_rewrite_max_tokens`
- `rag_json_text_fields`

Example LangGraph config:

```python
config = {
    "configurable": {
        "rag_enabled": True,
        "retrieval_mode": "hybrid",
        "search_api": "tavily",
        "rag_knowledge_base_paths": ["./data/knowledge"],
        "rag_memory_enabled": True,
        "rag_memory_paths": ["./data/memory/chat_memory.jsonl"],
        "rag_memory_write_enabled": True,
        "rag_memory_mysql_url": "mysql+pymysql://user:pass@localhost:3306/open_deep_research",
        "rag_memory_mysql_index_record_types": ["summary", "memory"],
        "rag_top_k": 4,
        "rag_chunk_size": 800,
        "rag_chunk_overlap": 100,
        "rag_embedding_provider": "sentence_transformers",
        "rag_vectorstore_provider": "milvus",
        "rag_vectorstore_path": "./data/indexes/rag",
        "rag_milvus_uri": "./data/indexes/rag/milvus.db",
        "rag_reranker_provider": "cross_encoder",
        "rag_structured_metadata_weight": 0.15,
        "rag_graph_enabled": True,
        "rag_graph_backend": "neo4j",
        "rag_neo4j_uri": "bolt://localhost:7687",
        "rag_neo4j_username": "neo4j",
        "rag_neo4j_password": "your_password",
        "rag_multimodal_enabled": True,
        "rag_multimodal_provider": "ocr",
        "rag_vision_enabled": True,
        "rag_vision_model": "openai:gpt-4.1-mini",
        "rag_query_image_enabled": True,
        "rag_query_image_max_images": 3,
        "rag_query_rewrite_enabled": True,
        "rag_query_rewrite_model": "openai:gpt-4.1-mini",
    }
}
```

Minimal local example:

```bash
.venv\Scripts\python.exe examples/rag_local_search.py
```

RAG MCP server:

```bash
# stdio transport for local MCP hosts
.venv\Scripts\python.exe -m open_deep_research.rag.mcp_server

# Streamable HTTP transport at http://127.0.0.1:8000/mcp
.venv\Scripts\python.exe -m open_deep_research.rag.mcp_server --transport streamable-http --host 127.0.0.1 --port 8000
```

The RAG MCP server exposes:

- `rag_search`: query local RAG sources and return cited context.
- `rag_ensure_indexed`: build or refresh the configured RAG index.
- `rag_index_pending_memories`: refresh the index and mark pending MySQL memory rows as indexed.
- `rag_status`: inspect the configured index id and cached pipeline state.
- `rag_list_sources`: list configured source documents without embedding them.
- `rag_reset_cache`: clear the in-process RAG pipeline cache.

Each MCP tool accepts an optional `config` object. It can use the same `rag_...` keys shown above, or direct `RAGPipelineConfig` keys such as `knowledge_base_paths`, `embedding_provider`, `vectorstore_provider`, and `reranker_provider`.

Current limitations:

- The default local RAG path uses SentenceTransformers embeddings, a persistent Milvus index, hybrid vector+BM25 retrieval, Neo4j-backed GraphRAG expansion when local RAG is enabled, and a cross-encoder/BGE reranker
- GraphRAG defaults to Neo4j (`rag_graph_backend="neo4j"`). Set `rag_graph_backend="memory"` if you want the older in-process chunk-term graph instead.
- `data/indexes/rag/` is a derived index cache and can be deleted and rebuilt from `data/knowledge/`, file memory, and configured MySQL memory.
- `faiss`, `chroma`, and `memory` vector stores remain available; `chroma` is kept for legacy local indexes, while `memory` and `simple` reranking are useful for focused tests. `hash` embeddings are only for explicit offline diagnostics and should not be used for retrieval metrics.
- Multimodal RAG ingestion is enabled by default. Image files and image-only PDF pages go through image routing: text-heavy images use OCR, diagrams/UI/flowcharts/architecture images use OCR plus Vision, ordinary scene photos use Vision, and low-information images are skipped. Local feature extraction uses Pillow with optional OpenCV when available; OCR uses Pillow, pytesseract, and a Tesseract runtime with the configured languages installed. Vision extraction uses `rag_vision_model` and is also enabled by default.
- Query-time user images are also recognized when `rag_query_image_enabled` is true. Images attached in multimodal user messages, data URLs, or explicit local image paths are routed through the same OCR/Vision logic and appended as temporary query context before the research brief is generated. These user-question image observations are not written into `data/knowledge/`, `data/memory/`, MySQL memory, or the persistent vector index.
- `rag_search` rewrites incoming questions when `rag_query_rewrite_enabled` is true. The rewritten query is used for vector recall, BM25, metadata boosts, and reranking; the original question is preserved in the returned context. Rewrite failures fall back to the original query.
- When MySQL memory writing is enabled, the raw chat transcript, generated summary, and durable memory items are upserted into MySQL. By default, only `summary` and `memory` rows are chunked, embedded, and written to the configured vector store; full `chat` transcripts stay in MySQL unless `rag_memory_mysql_index_record_types` includes `chat`
- Local source citations point to file paths or `memory://...` sources and include metadata such as page, heading, line range, JSON item/field paths, memory type, conversation id, and confidence when available

## 📊 Evaluation

Open Deep Research is configured for evaluation with [Deep Research Bench](https://huggingface.co/spaces/Ayanami0730/DeepResearch-Leaderboard). This benchmark has 100 PhD-level research tasks (50 English, 50 Chinese), crafted by domain experts across 22 fields (e.g., Science & Tech, Business & Finance) to mirror real-world deep-research needs. It has 2 evaluation metrics, but the leaderboard is based on the RACE score. This uses LLM-as-a-judge (Gemini) to evaluate research reports against a golden set of reports compiled by experts across a set of metrics.

### Usage

> Warning: Running across the 100 examples can cost ~$20-$100 depending on the model selection.

The dataset is available on [LangSmith via this link](https://smith.langchain.com/public/c5e7a6ad-fdba-478c-88e6-3a388459ce8b/d). To kick off evaluation, run the following command:

```bash
# Run comprehensive evaluation on LangSmith datasets
python tests/run_evaluate.py
```

This will provide a link to a LangSmith experiment, which will have a name `YOUR_EXPERIMENT_NAME`. Once this is done, extract the results to a JSONL file that can be submitted to the Deep Research Bench.

```bash
python tests/extract_langsmith_data.py --project-name "YOUR_EXPERIMENT_NAME" --model-name "you-model-name" --dataset-name "deep_research_bench"
```

This creates `tests/expt_results/deep_research_bench_model-name.jsonl` with the required format. Move the generated JSONL file to a local clone of the Deep Research Bench repository and follow their [Quick Start guide](https://github.com/Ayanami0730/deep_research_bench?tab=readme-ov-file#quick-start) for evaluation submission.

### Results

| Name                           | Commit                                                                                                                  | Summarization       | Research                           | Compression    | Total Cost | Total Tokens | RACE Score | Experiment                                                                                                                                                                                                |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | ------------------- | ---------------------------------- | -------------- | ---------- | ------------ | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| GPT-5                          | [ca3951d](https://github.com/langchain-ai/open_deep_research/pull/168/commits)                                          | openai:gpt-4.1-mini | openai:gpt-5                       | openai:gpt-4.1 |            | 204,640,896  | 0.4943     | [Link](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/datasets/6e4766ca-613c-4bda-8bde-f64f0422bbf3/compare?selectedSessions=4d5941c8-69ce-4f3d-8b3e-e3c99dfbd4cc&baseline=undefined) |
| Defaults                       | [6532a41](https://github.com/langchain-ai/open_deep_research/commit/6532a4176a93cc9bb2102b3d825dcefa560c85d9)           | openai:gpt-4.1-mini | openai:gpt-4.1                     | openai:gpt-4.1 | $45.98     | 58,015,332   | 0.4309     | [Link](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/datasets/6e4766ca-6[…]ons=cf4355d7-6347-47e2-a774-484f290e79bc&baseline=undefined)                                              |
| Claude Sonnet 4                | [f877ea9](https://github.com/langchain-ai/open_deep_research/pull/163/commits/f877ea93641680879c420ea991e998b47aab9bcc) | openai:gpt-4.1-mini | anthropic:claude-sonnet-4-20250514 | openai:gpt-4.1 | $187.09    | 138,917,050  | 0.4401     | [Link](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/datasets/6e4766ca-6[…]ons=04f6002d-6080-4759-bcf5-9a52e57449ea&baseline=undefined)                                              |
| Deep Research Bench Submission | [c0a160b](https://github.com/langchain-ai/open_deep_research/commit/c0a160b57a9b5ecd4b8217c3811a14d8eff97f72)           | openai:gpt-4.1-nano | openai:gpt-4.1                     | openai:gpt-4.1 | $87.83     | 207,005,549  | 0.4344     | [Link](https://smith.langchain.com/o/ebbaf2eb-769b-4505-aca2-d11de10372a4/datasets/6e4766ca-6[…]ons=e6647f74-ad2f-4cb9-887e-acb38b5f73c0&baseline=undefined)                                              |

## 🚀 Deployments and Usage

### LangGraph Studio

Follow the [quickstart](#-quickstart) to start LangGraph server locally and test the agent out on LangGraph Studio.

### Hosted deployment

You can easily deploy to [LangGraph Platform](https://langchain-ai.github.io/langgraph/concepts/#deployment-options).

### Open Agent Platform

Open Agent Platform (OAP) is a UI from which non-technical users can build and configure their own agents. OAP is great for allowing users to configure the Deep Researcher with different MCP tools and search APIs that are best suited to their needs and the problems that they want to solve.

We've deployed Open Deep Research to our public demo instance of OAP. All you need to do is add your API Keys, and you can test out the Deep Researcher for yourself! Try it out [here](https://oap.langchain.com)

You can also deploy your own instance of OAP, and make your own custom agents (like Deep Researcher) available on it to your users.

1. [Deploy Open Agent Platform](https://docs.oap.langchain.com/quickstart)
2. [Add Deep Researcher to OAP](https://docs.oap.langchain.com/setup/agents)

## Legacy Implementations 🏛️

The `src/legacy/` folder contains two earlier implementations that provide alternative approaches to automated research. They are less performant than the current implementation, but provide alternative ideas understanding the different approaches to deep research.

### 1. Workflow Implementation (`legacy/graph.py`)

- **Plan-and-Execute**: Structured workflow with human-in-the-loop planning
- **Sequential Processing**: Creates sections one by one with reflection
- **Interactive Control**: Allows feedback and approval of report plans
- **Quality Focused**: Emphasizes accuracy through iterative refinement

### 2. Multi-Agent Implementation (`legacy/multi_agent.py`)

- **Supervisor-Researcher Architecture**: Coordinated multi-agent system
- **Parallel Processing**: Multiple researchers work simultaneously
- **Speed Optimized**: Faster report generation through concurrency
- **MCP Support**: Extensive Model Context Protocol integration
