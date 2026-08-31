# RAG 检索测试记录

本文档用于持续记录 RAG 检索评估结果。后续每次更改检索技术、知识库、评估集或参数后，都在这里新增记录。

## 本页速览

| 项目     | 内容                                                                                         |
| -------- | -------------------------------------------------------------------------------------------- |
| 阅读目标 | 比较不同 RAG 检索配置的指标变化，并定位未命中样本。                                          |
| 记录方式 | 每次评估追加一条记录，保留命令、配置、链路、指标、misses 和说明。                            |
| 相关文档 | [RAG 模块说明](../architecture/rag.md)、[当前技术栈说明](../architecture/technical-stack.md) |

优先看“对比摘要”判断趋势；需要复现或诊断时再进入单条记录。

::: warning
记录 1 是历史 baseline，当时使用了 `hash` embedding。当前检索指标评估不再允许使用 `hash`，应使用默认的真实 `sentence_transformers` embedding 链路。
:::

建议每次记录包含：

1. 测试命令
2. 测试配置
3. 技术栈与检索链路
4. 本次技术或数据变更
5. 测试结果
6. 未命中样本
7. 结果说明

## 对比摘要

| 对比项         | 记录 1：baseline                                                                                            | 记录 2：真实模型链路                                                       | 记录 3：扩充知识库后                                                       | 记录 4：误导知识库扩充后                                                   | 记录 5：Milvus 向量库最新评估                                              |
| -------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 日期           | 2026-05-18                                                                                                  | 2026-05-18                                                                 | 2026-05-19                                                                 | 2026-05-19                                                                 | 2026-05-19                                                                 |
| 命令           | `python tests\evaluate_rag_retrieval.py --embedding-provider hash --reranker-provider simple --show-misses` | `python tests\evaluate_rag_retrieval.py --show-misses`                     | `.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses` | `.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses` | `.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses` |
| Embedding      | `hash`，未使用真实 embedding 模型                                                                           | `sentence_transformers`                                                    | `sentence_transformers`                                                    | `sentence_transformers`                                                    | `sentence_transformers`                                                    |
| Embedding 模型 | 未实际使用；`Config` 中的模型路径只是参数值                                                                 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 本机缓存快照 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 本机缓存快照 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 本机缓存快照 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 本机缓存快照 |
| 向量库         | `memory`                                                                                                    | `memory`                                                                   | `milvus`                                                                   | `milvus`                                                                   | `milvus`                                                                   |
| 重排           | `simple`，未使用真实 cross-encoder                                                                          | `cross_encoder`                                                            | `cross_encoder`                                                            | `cross_encoder`                                                            | `cross_encoder`                                                            |
| Reranker 模型  | 未实际使用；`Config` 中的模型路径只是参数值                                                                 | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                          | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                          | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                          | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                          |
| 知识库规模     | 3 个文件                                                                                                    | 3 个文件                                                                   | 6 个文件，新增 data governance / integration / telemetry                   | 7 个文件，新增 misleading archive                                          | 7 个文件，misleading archive 继续扩充                                      |
| 评估集规模     | 60 条                                                                                                       | 60 条                                                                      | 133 条                                                                     | 133 条                                                                     | 133 条                                                                     |
| LLM 调用       | 否                                                                                                          | 否                                                                         | 否                                                                         | 否                                                                         | 否                                                                         |
| num_documents  | 未记录                                                                                                      | 未记录                                                                     | 33                                                                         | 34                                                                         | 34                                                                         |
| num_chunks     | 未记录                                                                                                      | 未记录                                                                     | 80                                                                         | 175                                                                        | 284                                                                        |
| num_vectors    | 未记录                                                                                                      | 未记录                                                                     | 80                                                                         | 175                                                                        | 284                                                                        |
| avg_latency_ms | 3.683                                                                                                       | 71.924                                                                     | 98.996                                                                     | 148.9805                                                                   | 189.0449                                                                   |
| hit@5          | 0.9038                                                                                                      | 1.0                                                                        | 1.0                                                                        | 1.0                                                                        | 0.9487                                                                     |
| recall@5       | 0.859                                                                                                       | 0.9455                                                                     | 0.9658                                                                     | 0.933                                                                      | 0.8561                                                                     |
| precision@5    | 0.6808                                                                                                      | 0.7269                                                                     | 0.7111                                                                     | 0.5299                                                                     | 0.4342                                                                     |
| mrr@5          | 0.8093                                                                                                      | 0.9263                                                                     | 0.9487                                                                     | 0.8013                                                                     | 0.7538                                                                     |
| Misses         | 5 条                                                                                                        | 0 条                                                                       | 0 条                                                                       | 0 条                                                                       | 6 条                                                                       |

## 2026-05-18 记录 1：Baseline，未使用真实模型

### 测试命令

```powershell
python tests\evaluate_rag_retrieval.py --embedding-provider hash --reranker-provider simple --show-misses
```

### 测试配置

| 配置项                 | 当前值                                            | 说明                                            |
| ---------------------- | ------------------------------------------------- | ----------------------------------------------- |
| 评估脚本               | `tests/evaluate_rag_retrieval.py`                 | 只评估检索效果，不调用 LLM                      |
| 评估集                 | `tests/rag_eval_cases.jsonl`                      | 共 60 条 RAG 评估样本                           |
| 知识库路径             | `examples/rag_data`                               | 小型示例知识库                                  |
| 参与检索评分样本       | 52 条                                             | 排除 `expected_sources=[]` 的不可回答样本       |
| 跳过样本               | 8 条                                              | `negative` / `unanswerable` 样本                |
| `top_k`                | 5                                                 | 最终统计前 5 个 citation                        |
| `chunk_size`           | 400                                               | 文档切块大小                                    |
| `chunk_overlap`        | 50                                                | 相邻 chunk 重叠长度                             |
| `rerank_top_n`         | 8                                                 | 进入 reranker 的候选数量                        |
| `embedding_provider`   | `hash`                                            | 使用哈希向量，不调用真实 embedding 模型         |
| `embedding_model`      | 本机 sentence-transformers 缓存路径               | 本次未实际使用，因为 `embedding_provider=hash`  |
| `embedding_device`     | `auto`                                            | 本次未实际用于模型推理                          |
| `vectorstore_provider` | `memory`                                          | 内存向量库                                      |
| `reranker_provider`    | `simple`                                          | 使用简单重排，不调用真实 cross-encoder 模型     |
| `reranker_model`       | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2` | 本次未实际使用，因为 `reranker_provider=simple` |
| `reranker_device`      | `auto`                                            | 本次未实际用于模型推理                          |
| LLM 调用               | 否                                                | 本次只测 retrieval，不测答案生成                |

### 技术栈与检索链路

| 模块           | 当前技术栈 / 配置                                     | 说明                                                       |
| -------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| RAG 管线       | `open_deep_research.rag.service.RAGPipeline`          | 本地 RAG 查询入口                                          |
| 知识库来源     | `examples/rag_data`                                   | 本地 Markdown / TXT / JSON 示例知识库                      |
| 文档切分       | `chunk_size=400`, `chunk_overlap=50`                  | 先切 chunk，再建立检索索引                                 |
| Embedding      | `HashEmbeddingBackend`                                | 轻量 baseline，不具备真实语义模型能力                      |
| Embedding 模型 | 无                                                    | 没有加载 sentence-transformers                             |
| 向量库         | `InMemoryVectorStore` / `vectorstore_provider=memory` | 内存向量库，不持久化                                       |
| 关键词检索     | 内存 BM25 / `keyword_top_k=12`                        | 与向量召回一起做 hybrid retrieval                          |
| 混合召回       | vector + BM25, RRF (`rrf_rank_constant=60`)           | 按排名位置融合                                           |
| 重排           | `SimpleReranker` / `reranker_provider=simple`         | 基于简单规则重排，不加载 cross-encoder                     |
| Reranker 模型  | 无                                                    | 没有加载 `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2` |
| 候选数量       | `max(top_k, rerank_top_n)=8`                          | 召回 8 个候选后重排，再取前 5 个 citation 计算指标         |
| 答案生成模型   | 无                                                    | 检索评估脚本不调用 LLM，不评估最终回答质量                 |

### Overall 指标（整体测试结果）

| 指标                 |   数值 |
| -------------------- | -----: |
| 参与评分样本数       |     52 |
| 跳过的不可回答样本数 |      8 |
| avg_latency_ms       |  3.683 |
| hit@5                | 0.9038 |
| recall@5             |  0.859 |
| precision@5          | 0.6808 |
| mrr@5                | 0.8093 |

### 按类别统计

| 类别          | 样本数 | hit@5 | recall@5 | precision@5 |  mrr@5 | avg_latency_ms |
| ------------- | -----: | ----: | -------: | ----------: | -----: | -------------: |
| citation      |      8 |   1.0 |      1.0 |       0.575 | 0.8125 |         3.5631 |
| cross_lingual |      8 | 0.375 |     0.25 |       0.325 | 0.3125 |         3.2487 |
| edge_case     |      3 |   1.0 |      1.0 |      0.4667 |   0.75 |         3.6691 |
| multi_hop     |     10 |   1.0 |      0.9 |        0.96 |    1.0 |         3.6416 |
| negative      |      8 |   n/a |      n/a |         n/a |    n/a |         3.5998 |
| refutation    |      8 |   1.0 |   0.9583 |         0.8 | 0.9375 |         3.4489 |
| single_hop    |     15 |   1.0 |      1.0 |        0.72 | 0.8889 |         4.1779 |

### 未命中样本

| 样本 ID        | 期望来源                       | 实际召回来源                                               |
| -------------- | ------------------------------ | ---------------------------------------------------------- |
| `atlas_zh_001` | `team_handbook.md`             | `faq.json`, `faq.json`, `faq.json`, `faq.json`, `faq.json` |
| `atlas_zh_003` | `team_handbook.md`, `faq.json` | 无                                                         |
| `atlas_zh_005` | `runbook.txt`                  | 无                                                         |
| `atlas_zh_006` | `runbook.txt`                  | 无                                                         |
| `atlas_zh_008` | `runbook.txt`                  | `faq.json`, `team_handbook.md`, `team_handbook.md`         |

### 结果说明

- 这是 baseline 记录，不应解释为真实模型效果。
- 这次没有使用 `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 做语义 embedding，也没有使用 `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2` 做 cross-encoder rerank。
- `cross_lingual` 指标明显偏低，说明 hash 向量对中英混合、同义表达和跨语言问法支持不足。
- 平均耗时很低，主要因为没有真实模型推理成本。

## 2026-05-18 记录 2：真实模型链路，终端最新输出

### 测试命令

```powershell
.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses
```

### 测试配置

| 配置项                 | 当前值                                                                       | 说明                                                                                                          |
| ---------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| 评估脚本               | `tests/evaluate_rag_retrieval.py`                                            | 只评估检索效果，不调用 LLM                                                                                    |
| 评估集                 | `tests/rag_eval_cases.jsonl`                                                 | 共 60 条 RAG 评估样本                                                                                         |
| 知识库路径             | `examples/rag_data`                                                          | 小型示例知识库                                                                                                |
| 参与检索评分样本       | 52 条                                                                        | 排除 `expected_sources=[]` 的不可回答样本                                                                     |
| 跳过样本               | 8 条                                                                         | `negative` / `unanswerable` 样本                                                                              |
| `top_k`                | 5                                                                            | 最终统计前 5 个 citation                                                                                      |
| `chunk_size`           | 400                                                                          | 文档切块大小                                                                                                  |
| `chunk_overlap`        | 50                                                                           | 相邻 chunk 重叠长度                                                                                           |
| `rerank_top_n`         | 8                                                                            | 进入 reranker 的候选数量                                                                                      |
| `embedding_provider`   | `sentence_transformers`                                                      | 使用真实语义 embedding 后端                                                                                   |
| `embedding_model`      | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 的本机缓存快照 | 多语言 sentence-transformers 模型                                                                             |
| `embedding_device`     | `auto`                                                                       | 自动选择推理设备                                                                                              |
| `vectorstore_provider` | `milvus`                                                                     | Milvus Lite 本地向量库                                                                                        |
| `milvus_uri`           | `$env:TEMP\odr_milvus_eval_<pid>.db`                                         | 脚本默认每次使用新的系统临时目录 Milvus Lite DB，避免 workspace 内 `.rag_index` 的 Windows 文件重命名权限问题 |
| `reranker_provider`    | `cross_encoder`                                                              | 使用真实 cross-encoder reranker                                                                               |
| `reranker_model`       | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                            | 本地加载 cross-encoder reranker                                                                               |
| `reranker_device`      | `auto`                                                                       | 自动选择推理设备                                                                                              |
| LLM 调用               | 否                                                                           | 本次只测 retrieval，不测答案生成                                                                              |

### 技术栈与检索链路

| 模块           | 当前技术栈 / 配置                                             | 说明                                               |
| -------------- | ------------------------------------------------------------- | -------------------------------------------------- |
| RAG 管线       | `open_deep_research.rag.service.RAGPipeline`                  | 本地 RAG 查询入口                                  |
| 知识库来源     | `examples/rag_data`                                           | 本地 Markdown / TXT / JSON 示例知识库              |
| 文档切分       | `chunk_size=400`, `chunk_overlap=50`                          | 先切 chunk，再建立检索索引                         |
| Embedding      | `SentenceTransformerEmbeddingBackend`                         | 真实语义向量模型，不再使用 hash embedding          |
| Embedding 模型 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 多语言 MiniLM 模型，适合中英文混合检索             |
| 向量库         | `InMemoryVectorStore` / `vectorstore_provider=memory`         | 内存向量库，不持久化，使用余弦相似度扫描           |
| 关键词检索     | 内存 BM25 / `keyword_top_k=12`                                | 与向量召回一起做 hybrid retrieval                  |
| 混合召回       | vector + BM25, RRF (`rrf_rank_constant=60`)                   | 按排名位置融合                                  |
| 重排           | `CrossEncoderReranker` / `reranker_provider=cross_encoder`    | 真实 cross-encoder 模型对召回候选精排              |
| Reranker 模型  | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`             | 本地 cross-encoder reranker                        |
| 候选数量       | `max(top_k, rerank_top_n)=8`                                  | 召回 8 个候选后重排，再取前 5 个 citation 计算指标 |
| 答案生成模型   | 无                                                            | 检索评估脚本不调用 LLM，不评估最终回答质量         |

### 本次技术或数据变更

| 类型      | 变更内容                                                 | 影响                           |
| --------- | -------------------------------------------------------- | ------------------------------ |
| Embedding | 从 `hash` 切换为 `sentence_transformers`                 | 提升语义匹配和跨语言检索能力   |
| Reranker  | 从 `simple` 切换为 `cross_encoder`                       | 对召回候选做真实模型精排       |
| 输出格式  | 改为纯文本输出，并关闭模型加载进度条                     | 终端不再出现 ANSI 控制字符乱码 |
| 模型文件  | 使用本机 Hugging Face 缓存和 `.rag_models/` 本地模型目录 | 避免每次评估都联网下载模型     |

### Overall 指标（整体测试结果）

| 指标                 |   数值 |
| -------------------- | -----: |
| 参与评分样本数       |     52 |
| 跳过的不可回答样本数 |      8 |
| avg_latency_ms       | 71.924 |
| hit@5                |    1.0 |
| recall@5             | 0.9455 |
| precision@5          | 0.7269 |
| mrr@5                | 0.9263 |

### 按类别统计

| 类别          | 样本数 | hit@5 | recall@5 | precision@5 |  mrr@5 | avg_latency_ms |
| ------------- | -----: | ----: | -------: | ----------: | -----: | -------------: |
| citation      |      8 |   1.0 |   0.9375 |        0.65 | 0.9167 |        68.0907 |
| cross_lingual |      8 |   1.0 |      1.0 |       0.725 | 0.7917 |        65.9248 |
| edge_case     |      3 |   1.0 |   0.8333 |         0.4 | 0.8333 |        67.1339 |
| multi_hop     |     10 |   1.0 |      0.9 |        0.98 |    1.0 |        66.0417 |
| negative      |      8 |   n/a |      n/a |         n/a |    n/a |        64.2997 |
| refutation    |      8 |   1.0 |   0.9583 |       0.775 |    1.0 |        63.9543 |
| single_hop    |     15 |   1.0 |   0.9667 |        0.64 | 0.9333 |        90.3642 |

### 未命中样本

| 样本 ID | 期望来源 | 实际召回来源 |
| ------- | -------- | ------------ |
| 无      | 无       | 无           |

### 终端最新输出

```text
Config
knowledge_base: ['examples/rag_data']
embedding_provider: sentence_transformers
embedding_model: C:\Users\zpbcool\.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2\snapshots\e8f8c211226b894fcb81acc59f3b34ba3efd5f42
embedding_device: auto
vectorstore_provider: memory
reranker_provider: cross_encoder
reranker_model: .rag_models/cross-encoder-ms-marco-MiniLM-L6-v2
reranker_device: auto
top_k: 5
rerank_top_n: 8

Overall
cases_scored: 52
cases_skipped_unanswerable: 8
avg_latency_ms: 71.924
hit@5: 1.0
recall@5: 0.9455
precision@5: 0.7269
mrr@5: 0.9263

By category
citation: count=8 hit@5=1.0 recall@5=0.9375 precision@5=0.65 mrr@5=0.9167 avg_latency_ms=68.0907
cross_lingual: count=8 hit@5=1.0 recall@5=1.0 precision@5=0.725 mrr@5=0.7917 avg_latency_ms=65.9248
edge_case: count=3 hit@5=1.0 recall@5=0.8333 precision@5=0.4 mrr@5=0.8333 avg_latency_ms=67.1339
multi_hop: count=10 hit@5=1.0 recall@5=0.9 precision@5=0.98 mrr@5=1.0 avg_latency_ms=66.0417
negative: count=8 hit@5=n/a recall@5=n/a precision@5=n/a mrr@5=n/a avg_latency_ms=64.2997
refutation: count=8 hit@5=1.0 recall@5=0.9583 precision@5=0.775 mrr@5=1.0 avg_latency_ms=63.9543
single_hop: count=15 hit@5=1.0 recall@5=0.9667 precision@5=0.64 mrr@5=0.9333 avg_latency_ms=90.3642

Misses
none
```

### 结果说明

- 真实模型链路的 `hit@5` 达到 1.0，终端输出 `Misses none`，说明所有可回答样本前 5 个 citation 中至少命中了一个期望来源。
- 相比 baseline，`cross_lingual` 从 `hit@5=0.375`、`recall@5=0.25` 提升到 `hit@5=1.0`、`recall@5=1.0`，说明多语言 embedding 对中英混合检索帮助明显。
- `avg_latency_ms` 从 3.683 上升到 71.924，主要来自 sentence-transformers embedding 和 cross-encoder rerank 的本地推理成本。
- 当前知识库包含故意加入的 `deprecated` / `retired` 干扰规则，因此这些指标反映的是带噪声知识库下的检索结果。

## 2026-05-19 记录 3：扩充知识库后的真实模型链路

### 测试命令

```powershell
python tests\evaluate_rag_retrieval.py --show-misses
```

### 测试配置

| 配置项                 | 当前值                                                                       | 说明                                      |
| ---------------------- | ---------------------------------------------------------------------------- | ----------------------------------------- |
| 评估脚本               | `tests/evaluate_rag_retrieval.py`                                            | 只评估检索效果，不调用 LLM                |
| 评估集                 | `tests/rag_eval_cases.jsonl`                                                 | 共 133 条 RAG 评估样本                    |
| 知识库路径             | `examples/rag_data`                                                          | 已从 3 个文件扩充到 6 个文件              |
| 参与检索评分样本       | 117 条                                                                       | 排除 `expected_sources=[]` 的不可回答样本 |
| 跳过样本               | 16 条                                                                        | `negative` / `unanswerable` 样本          |
| `top_k`                | 5                                                                            | 最终统计前 5 个 citation                  |
| `chunk_size`           | 400                                                                          | 文档切块大小                              |
| `chunk_overlap`        | 50                                                                           | 相邻 chunk 重叠长度                       |
| `rerank_top_n`         | 8                                                                            | 进入 reranker 的候选数量                  |
| `embedding_provider`   | `sentence_transformers`                                                      | 使用真实语义 embedding 后端               |
| `embedding_model`      | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 的本机缓存快照 | 多语言 sentence-transformers 模型         |
| `embedding_device`     | `auto`                                                                       | 自动选择推理设备                          |
| `vectorstore_provider` | `memory`                                                                     | 内存向量库                                |
| `reranker_provider`    | `cross_encoder`                                                              | 使用真实 cross-encoder reranker           |
| `reranker_model`       | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                            | 本地加载 cross-encoder reranker           |
| `reranker_device`      | `auto`                                                                       | 自动选择推理设备                          |
| LLM 调用               | 否                                                                           | 本次只测 retrieval，不测答案生成          |

### 本次技术或数据变更

| 类型       | 变更内容                                           | 影响                                                                                          |
| ---------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| 知识库扩充 | 新增 `examples/rag_data/data_governance.md`        | 增加导入窗口、schema change、数据保留、export package、partner sandbox、审批角色等事实        |
| 知识库扩充 | 新增 `examples/rag_data/integration_playbook.txt`  | 增加 Partner Sync、backfill、sync incident、post-deployment verification 等事实               |
| 知识库扩充 | 新增 `examples/rag_data/telemetry_notes.json`      | 增加 telemetry 指标、daily health summary、DR tabletop 等事实                                 |
| 干扰内容   | 新增多处 `deprecated` / `retired` 历史错误规则     | 用来测试 refutation、edge case 和检索抗干扰能力                                               |
| 评估集扩充 | `tests/rag_eval_cases.jsonl` 从 60 条扩充到 133 条 | 覆盖更多 single_hop、multi_hop、cross_lingual、citation、refutation、edge_case、negative 样本 |
| 向量库切换 | 从 `memory` 切换到 `milvus` / Milvus Lite          | 使用真实持久化向量库后端评估召回效果                                                          |

### 扩充后的评估集分布

| 类别          | 样本数 |
| ------------- | -----: |
| single_hop    |     26 |
| multi_hop     |     22 |
| cross_lingual |     22 |
| citation      |     16 |
| refutation    |     16 |
| edge_case     |     15 |
| negative      |     16 |

### Overall 指标（整体测试结果）

| 指标                 |   数值 |
| -------------------- | -----: |
| 参与评分样本数       |    117 |
| 跳过的不可回答样本数 |     16 |
| num_documents        |     33 |
| num_chunks           |     80 |
| num_vectors          |     80 |
| avg_latency_ms       | 98.996 |
| hit@5                |    1.0 |
| recall@5             | 0.9658 |
| precision@5          | 0.7111 |
| mrr@5                | 0.9487 |

### 按类别统计

| 类别          | 样本数 | hit@5 | recall@5 | precision@5 |  mrr@5 | avg_latency_ms |
| ------------- | -----: | ----: | -------: | ----------: | -----: | -------------: |
| citation      |     16 |   1.0 |   0.9375 |        0.65 | 0.9583 |          89.94 |
| cross_lingual |     22 |   1.0 |      1.0 |      0.6545 | 0.8561 |        89.7949 |
| edge_case     |     15 |   1.0 |   0.9667 |      0.7333 | 0.9556 |        88.2639 |
| multi_hop     |     22 |   1.0 |   0.9242 |      0.8455 | 0.9773 |        92.5282 |
| negative      |     16 |   n/a |      n/a |         n/a |    n/a |        85.9077 |
| refutation    |     16 |   1.0 |   0.9792 |      0.7625 |    1.0 |        86.5833 |
| single_hop    |     26 |   1.0 |   0.9808 |      0.6385 | 0.9615 |       139.7114 |

### 未命中样本

| 样本 ID | 期望来源 | 实际召回来源 |
| ------- | -------- | ------------ |
| 无      | 无       | 无           |

### 终端最新输出

```text
Config
knowledge_base: ['examples/rag_data']
embedding_provider: sentence_transformers
embedding_model: C:\Users\zpbcool\.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2\snapshots\e8f8c211226b894fcb81acc59f3b34ba3efd5f42
embedding_device: auto
vectorstore_provider: milvus
vectorstore_path: .rag_index
milvus_uri: C:\Users\zpbcool\AppData\Local\Temp\odr_milvus_eval_37860.db
milvus_metric_type: COSINE
reranker_provider: cross_encoder
reranker_model: .rag_models/cross-encoder-ms-marco-MiniLM-L6-v2
reranker_device: auto
top_k: 5
rerank_top_n: 8

Overall
cases_scored: 117
cases_skipped_unanswerable: 16
num_documents: 33
num_chunks: 80
num_vectors: 80
avg_latency_ms: 98.996
hit@5: 1.0
recall@5: 0.9658
precision@5: 0.7111
mrr@5: 0.9487

By category
citation: count=16 hit@5=1.0 recall@5=0.9375 precision@5=0.65 mrr@5=0.9583 avg_latency_ms=89.94
cross_lingual: count=22 hit@5=1.0 recall@5=1.0 precision@5=0.6545 mrr@5=0.8561 avg_latency_ms=89.7949
edge_case: count=15 hit@5=1.0 recall@5=0.9667 precision@5=0.7333 mrr@5=0.9556 avg_latency_ms=88.2639
multi_hop: count=22 hit@5=1.0 recall@5=0.9242 precision@5=0.8455 mrr@5=0.9773 avg_latency_ms=92.5282
negative: count=16 hit@5=n/a recall@5=n/a precision@5=n/a mrr@5=n/a avg_latency_ms=85.9077
refutation: count=16 hit@5=1.0 recall@5=0.9792 precision@5=0.7625 mrr@5=1.0 avg_latency_ms=86.5833
single_hop: count=26 hit@5=1.0 recall@5=0.9808 precision@5=0.6385 mrr@5=0.9615 avg_latency_ms=139.7114

Misses
none
```

### 结果说明

- 本次主要变化是扩充知识库和评估集，并将评估向量库切换为 `milvus` / Milvus Lite；embedding 仍为 `sentence_transformers`，reranker 仍为 `cross_encoder`。
- 扩充后知识库从 3 个文件增加到 6 个文件，评估集从 60 条增加到 133 条。
- `hit@5=1.0` 且 `Misses none`，说明新增可回答样本都能在前 5 个 citation 中命中至少一个期望来源。
- 相比记录 2，`recall@5` 从 0.9455 提升到 0.9658，`mrr@5` 从 0.9263 提升到 0.9487；`precision@5` 从 0.7269 降到 0.7111，主要因为知识库和干扰内容变多后，前 5 个 citation 中非期望来源比例略升。
- Milvus Lite 写入 workspace 内 `.rag_index` 时遇到 Windows 文件重命名权限问题，因此评估脚本默认改为使用 `$env:TEMP\odr_milvus_eval_<pid>.db` 作为本地 Milvus DB 路径。
- 本次索引规模为 `num_documents=33`、`num_chunks=80`、`num_vectors=80`；其中 JSON 数组会按 item 拆成多个原始 document，因此 document 数量大于物理文件数量。

## 指标含义

- `hit@5`：前 5 个召回结果中是否至少命中 1 个期望来源。
- `recall@5`：前 5 个召回结果覆盖了多少期望来源。
- `precision@5`：前 5 个召回结果中，有多少属于期望来源。当前脚本统计的是 rerank 之后最终返回的前 5 个 citation。
- `mrr@5`：第一个正确来源出现得越靠前，分数越高。
- `avg_latency_ms`：平均每条 query 完成一次 `pipeline.query()` 检索的耗时，单位是毫秒。本次统计包含可评分样本和 `negative` / `unanswerable` 样本。
- `num_documents`：知识库加载后得到的原始文档单元数量。例如 `.md` / `.txt` 通常一个文件对应一个 document，`.json` 数组会按 item 拆成多个 document。
- `num_chunks`：文档切分后得到的文本块数量，也是进入 embedding、向量库、BM25 和 reranker 的检索单元数量。
- `num_vectors`：本次索引构建时实际写入向量库的向量条数。正常情况下一个 chunk 写入一条向量，因此通常等于 `num_chunks`。

## 2026-05-19 记录 4：扩充误导知识库后的 Milvus 评估

### 测试命令

```powershell
.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses
```

### 本次技术或数据变更

| 类型       | 变更内容                                                                                | 影响                                                                                                                                           |
| ---------- | --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| 知识库扩充 | 新增 `examples/rag_data/misleading_archive.md`                                          | 按 `single_hop`、`multi_hop`、`cross_lingual`、`citation`、`refutation`、`edge_case`、`negative` 各新增 20 条误导/废弃/不可采纳条目，共 140 条 |
| 误导内容   | 每条均标记为 `deprecated`、`retired`、`do_not_use`、`misleading` 或 `unanswerable_trap` | 增加检索噪声，测试 RAG 对过期、错误、相似但非权威内容的抗干扰能力                                                                              |
| 权威锚点   | `runbook.txt` 新增一条中文 rollback owner 当前规则锚点                                  | 避免中文问题被误导 archive 完全挤出权威来源                                                                                                    |

### Overall 指标（整体测试结果）

| 指标                 |     数值 |
| -------------------- | -------: |
| 参与评分样本数       |      117 |
| 跳过的不可回答样本数 |       16 |
| num_documents        |       34 |
| num_chunks           |      175 |
| num_vectors          |      175 |
| avg_latency_ms       | 148.9805 |
| hit@5                |      1.0 |
| recall@5             |    0.933 |
| precision@5          |   0.5299 |
| mrr@5                |   0.8013 |

### 按类别统计

| 类别          | 样本数 | hit@5 | recall@5 | precision@5 |  mrr@5 | avg_latency_ms |
| ------------- | -----: | ----: | -------: | ----------: | -----: | -------------: |
| citation      |     16 |   1.0 |   0.9375 |      0.4625 | 0.8177 |       130.8341 |
| cross_lingual |     22 |   1.0 |   0.9545 |      0.4727 | 0.6212 |       129.5773 |
| edge_case     |     15 |   1.0 |   0.8889 |      0.4933 | 0.6611 |       133.3511 |
| multi_hop     |     22 |   1.0 |   0.8712 |      0.6273 | 0.8409 |       126.3651 |
| negative      |     16 |   n/a |      n/a |         n/a |    n/a |       126.5986 |
| refutation    |     16 |   1.0 |   0.9792 |        0.55 | 0.9219 |       127.0545 |
| single_hop    |     26 |   1.0 |   0.9615 |      0.5462 | 0.9167 |        231.985 |

### 未命中样本

| 样本 ID | 期望来源 | 实际召回来源 |
| ------- | -------- | ------------ |
| 无      | 无       | 无           |

### 终端最新输出

```text
Overall
cases_scored: 117
cases_skipped_unanswerable: 16
num_documents: 34
num_chunks: 175
num_vectors: 175
avg_latency_ms: 148.9805
hit@5: 1.0
recall@5: 0.933
precision@5: 0.5299
mrr@5: 0.8013

By category
citation: count=16 hit@5=1.0 recall@5=0.9375 precision@5=0.4625 mrr@5=0.8177 avg_latency_ms=130.8341
cross_lingual: count=22 hit@5=1.0 recall@5=0.9545 precision@5=0.4727 mrr@5=0.6212 avg_latency_ms=129.5773
edge_case: count=15 hit@5=1.0 recall@5=0.8889 precision@5=0.4933 mrr@5=0.6611 avg_latency_ms=133.3511
multi_hop: count=22 hit@5=1.0 recall@5=0.8712 precision@5=0.6273 mrr@5=0.8409 avg_latency_ms=126.3651
negative: count=16 hit@5=n/a recall@5=n/a precision@5=n/a mrr@5=n/a avg_latency_ms=126.5986
refutation: count=16 hit@5=1.0 recall@5=0.9792 precision@5=0.55 mrr@5=0.9219 avg_latency_ms=127.0545
single_hop: count=26 hit@5=1.0 recall@5=0.9615 precision@5=0.5462 mrr@5=0.9167 avg_latency_ms=231.985

Misses
none
```

### 结果说明

- 新增误导 archive 后，`num_chunks` 从 80 增加到 175，`num_vectors` 同步增加到 175，说明新增噪声已经进入向量库。
- `hit@5` 仍为 1.0 且 `Misses none`，说明前 5 个 citation 仍能覆盖至少一个期望来源。
- `precision@5` 从 0.7111 降到 0.5299，`mrr@5` 从 0.9487 降到 0.8013，说明误导文档显著挤入前 5 结果，达到了增加干扰难度的目的。
- `avg_latency_ms` 从 98.996 上升到 148.9805，主要来自 chunk / vector 数量增加后的检索与重排成本。

## 2026-05-19 记录 5：改用 Milvus 向量库后的最新评估

### 测试命令

```powershell
.\.venv\Scripts\python.exe tests\evaluate_rag_retrieval.py --show-misses
```

### 测试配置

| 配置项                 | 当前值                                                                     | 说明                                         |
| ---------------------- | -------------------------------------------------------------------------- | -------------------------------------------- |
| 评估脚本               | `tests/evaluate_rag_retrieval.py`                                          | 只评估检索效果，不调用 LLM                   |
| 评估集                 | `tests/rag_eval_cases.jsonl`                                               | 共 133 条 RAG 评估样本                       |
| 知识库路径             | `examples/rag_data`                                                        | 7 个物理文件，包含扩充后的误导知识库         |
| 参与检索评分样本       | 117 条                                                                     | 排除 `expected_sources=[]` 的不可回答样本    |
| 跳过样本               | 16 条                                                                      | `negative` / `unanswerable` 样本             |
| `top_k`                | 5                                                                          | 最终统计前 5 个 citation                     |
| `chunk_size`           | 400                                                                        | 文档切块大小                                 |
| `chunk_overlap`        | 50                                                                         | 相邻 chunk 重叠长度                          |
| `rerank_top_n`         | 8                                                                          | 进入 reranker 的候选数量                     |
| `embedding_provider`   | `sentence_transformers`                                                    | 使用真实语义 embedding 后端                  |
| `embedding_model`      | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` 本机缓存快照 | 多语言 sentence-transformers 模型            |
| `embedding_device`     | `auto`                                                                     | 自动选择推理设备                             |
| `vectorstore_provider` | `milvus`                                                                   | 本次明确使用 Milvus / Milvus Lite 作为向量库 |
| `vectorstore_path`     | `.rag_index`                                                               | 配置项保留；本次实际 Milvus URI 指向临时 DB  |
| `milvus_uri`           | `C:\Users\zpbcool\AppData\Local\Temp\odr_milvus_eval_38748.db`             | 本次终端输出中的 Milvus Lite 本地 DB         |
| `milvus_metric_type`   | `COSINE`                                                                   | 使用余弦相似度                               |
| `reranker_provider`    | `cross_encoder`                                                            | 使用真实 cross-encoder reranker              |
| `reranker_model`       | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`                          | 本地加载 cross-encoder reranker              |
| `reranker_device`      | `auto`                                                                     | 自动选择推理设备                             |
| LLM 调用               | 否                                                                         | 本次只测 retrieval，不测答案生成             |

### 技术栈与检索链路

| 模块           | 当前技术栈 / 配置                                             | 说明                                               |
| -------------- | ------------------------------------------------------------- | -------------------------------------------------- |
| RAG 管线       | `open_deep_research.rag.service.RAGPipeline`                  | 本地 RAG 查询入口                                  |
| 知识库来源     | `examples/rag_data`                                           | 本地 Markdown / TXT / JSON 示例知识库              |
| 文档切分       | `chunk_size=400`, `chunk_overlap=50`                          | 先切 chunk，再建立检索索引                         |
| Embedding      | `SentenceTransformerEmbeddingBackend`                         | 真实语义向量模型                                   |
| Embedding 模型 | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | 多语言 MiniLM 模型，支持中英文混合检索             |
| 向量库         | `MilvusVectorStore` / `vectorstore_provider=milvus`           | 本次核心变化，向量召回走 Milvus Lite               |
| 关键词检索     | 内存 BM25 / `keyword_top_k=12`                                | 与向量召回一起做 hybrid retrieval                  |
| 混合召回       | vector + BM25, RRF (`rrf_rank_constant=60`)                   | 按排名位置融合                                   |
| 重排           | `CrossEncoderReranker` / `reranker_provider=cross_encoder`    | 对召回候选做模型精排                               |
| Reranker 模型  | `.rag_models/cross-encoder-ms-marco-MiniLM-L6-v2`             | 本地 cross-encoder reranker                        |
| 候选数量       | `max(top_k, rerank_top_n)=8`                                  | 召回 8 个候选后重排，再取前 5 个 citation 计算指标 |
| 答案生成模型   | 无                                                            | 检索评估脚本不调用 LLM，不评估最终回答质量         |

### 本次技术或数据变更

| 类型            | 变更内容                                              | 影响                                                                                       |
| --------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 向量库          | 本次测试使用 `milvus` / Milvus Lite，而不是内存向量库 | 更接近真实向量库调用方式，可以记录实际写入向量数量                                         |
| Milvus 存储位置 | 使用 `$env:TEMP\odr_milvus_eval_<pid>.db`             | 避免 workspace 内 `.rag_index` 在 Windows 上出现文件重命名权限问题                         |
| 诊断输出        | `--show-misses` 已增强为 chunk 级诊断                 | miss 时可看到 rank、source、chunk_id、vector_score、rerank_score、page_content 和 metadata |
| 知识库干扰强度  | `misleading_archive.md` 每个类别扩充到 40 条          | `num_chunks` 从 175 增加到 284，前 5 结果更容易被误导 chunk 挤占                           |

### Overall 指标（整体测试结果）

| 指标                 |     数值 |
| -------------------- | -------: |
| 参与评分样本数       |      117 |
| 跳过的不可回答样本数 |       16 |
| num_documents        |       34 |
| num_chunks           |      284 |
| num_vectors          |      284 |
| avg_latency_ms       | 189.0449 |
| hit@5                |   0.9487 |
| recall@5             |   0.8561 |
| precision@5          |   0.4342 |
| mrr@5                |   0.7538 |

### 按类别统计

| 类别          | 样本数 |  hit@5 | recall@5 | precision@5 |  mrr@5 | avg_latency_ms |
| ------------- | -----: | -----: | -------: | ----------: | -----: | -------------: |
| citation      |     16 |  0.875 |   0.8125 |      0.3375 | 0.7083 |       167.0047 |
| cross_lingual |     22 | 0.9545 |   0.9091 |      0.4182 | 0.6098 |       159.7395 |
| edge_case     |     15 | 0.8667 |   0.7556 |      0.3333 | 0.5111 |       162.5456 |
| multi_hop     |     22 | 0.9545 |   0.6894 |      0.5091 | 0.7652 |       154.8893 |
| negative      |     16 |    n/a |      n/a |         n/a |    n/a |       158.1407 |
| refutation    |     16 |    1.0 |   0.9792 |       0.475 | 0.9219 |       159.8724 |
| single_hop    |     26 |    1.0 |   0.9615 |      0.4769 | 0.9308 |       308.5642 |

### 未命中样本

| 样本 ID              | 期望来源             | 前 5 实际召回来源                                                                                                           | 诊断标记                               |
| -------------------- | -------------------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `atlas_citation_003` | `faq.json`           | `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `team_handbook.md`, `misleading_archive.md`      | `expected_retrieved_but_low_rank=True` |
| `atlas_edge_003`     | `runbook.txt`        | `team_handbook.md`, `team_handbook.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`           | `expected_retrieved_but_low_rank=True` |
| `atlas_multi_012`    | `data_governance.md` | `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md` | 正确来源未进入前 5                     |
| `atlas_citation_011` | `data_governance.md` | `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md` | 正确来源未进入前 5                     |
| `atlas_zh_011`       | `data_governance.md` | `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md` | `expected_retrieved_but_low_rank=True` |
| `atlas_edge_007`     | `data_governance.md` | `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md`, `misleading_archive.md` | `expected_not_retrieved=True`          |

### 终端最新输出（摘要）

```text
Config
knowledge_base: ['examples/rag_data']
embedding_provider: sentence_transformers
embedding_model: C:\Users\zpbcool\.cache\huggingface\hub\models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2\snapshots\e8f8c211226b894fcb81acc59f3b34ba3efd5f42
embedding_device: auto
vectorstore_provider: milvus
vectorstore_path: .rag_index
milvus_uri: C:\Users\zpbcool\AppData\Local\Temp\odr_milvus_eval_38748.db
milvus_metric_type: COSINE
reranker_provider: cross_encoder
reranker_model: .rag_models/cross-encoder-ms-marco-MiniLM-L6-v2
reranker_device: auto
top_k: 5
rerank_top_n: 8

Overall
cases_scored: 117
cases_skipped_unanswerable: 16
num_documents: 34
num_chunks: 284
num_vectors: 284
avg_latency_ms: 189.0449
hit@5: 0.9487
recall@5: 0.8561
precision@5: 0.4342
mrr@5: 0.7538

By category
citation: count=16 hit@5=0.875 recall@5=0.8125 precision@5=0.3375 mrr@5=0.7083 avg_latency_ms=167.0047
cross_lingual: count=22 hit@5=0.9545 recall@5=0.9091 precision@5=0.4182 mrr@5=0.6098 avg_latency_ms=159.7395
edge_case: count=15 hit@5=0.8667 recall@5=0.7556 precision@5=0.3333 mrr@5=0.5111 avg_latency_ms=162.5456
multi_hop: count=22 hit@5=0.9545 recall@5=0.6894 precision@5=0.5091 mrr@5=0.7652 avg_latency_ms=154.8893
negative: count=16 hit@5=n/a recall@5=n/a precision@5=n/a mrr@5=n/a avg_latency_ms=158.1407
refutation: count=16 hit@5=1.0 recall@5=0.9792 precision@5=0.475 mrr@5=0.9219 avg_latency_ms=159.8724
single_hop: count=26 hit@5=1.0 recall@5=0.9615 precision@5=0.4769 mrr@5=0.9308 avg_latency_ms=308.5642

Misses
atlas_citation_003
atlas_edge_003
atlas_multi_012
atlas_citation_011
atlas_zh_011
atlas_edge_007
```

### 结果说明

- 本次测试明确使用 `vectorstore_provider=milvus`，终端配置中可以看到 `milvus_uri` 和 `milvus_metric_type=COSINE`。
- 扩充后的误导知识库已经进入向量库，`num_chunks` / `num_vectors` 从记录 4 的 175 增加到 284。
- `hit@5` 从记录 4 的 1.0 降到 0.9487，说明误导 chunk 已经能把部分正确来源挤出前 5。
- `precision@5` 从 0.5299 降到 0.4342，说明前 5 结果里非期望来源比例进一步升高。
- 增强后的 `--show-misses` 可以区分两类问题：正确来源完全没有召回，以及正确来源被召回但排在 top-5 之外。

## 后续记录模板

复制下面模板，追加新的测试记录。

````markdown
## YYYY-MM-DD 记录 N：配置名称

### 测试命令

```powershell
python tests\evaluate_rag_retrieval.py --show-misses
```

### 测试配置

| 配置项                 | 当前值                            | 说明 |
| ---------------------- | --------------------------------- | ---- |
| 评估脚本               | `tests/evaluate_rag_retrieval.py` |      |
| 评估集                 | `tests/rag_eval_cases.jsonl`      |      |
| 知识库路径             | `examples/rag_data`               |      |
| `top_k`                |                                   |      |
| `chunk_size`           |                                   |      |
| `chunk_overlap`        |                                   |      |
| `rerank_top_n`         |                                   |      |
| `embedding_provider`   |                                   |      |
| `embedding_model`      |                                   |      |
| `embedding_device`     |                                   |      |
| `vectorstore_provider` |                                   |      |
| `reranker_provider`    |                                   |      |
| `reranker_model`       |                                   |      |
| `reranker_device`      |                                   |      |
| LLM 调用               |                                   |      |

### 技术栈与检索链路

| 模块           | 当前技术栈 / 配置 | 说明 |
| -------------- | ----------------- | ---- |
| RAG 管线       |                   |      |
| 知识库来源     |                   |      |
| 文档切分       |                   |      |
| Embedding      |                   |      |
| Embedding 模型 |                   |      |
| 向量库         |                   |      |
| 关键词检索     |                   |      |
| 混合召回       |                   |      |
| 重排           |                   |      |
| Reranker 模型  |                   |      |
| 候选数量       |                   |      |
| 答案生成模型   |                   |      |

### 本次技术或数据变更

| 类型 | 变更内容 | 影响 |
| ---- | -------- | ---- |
|      |          |      |

### Overall 指标（整体测试结果）

| 指标           | 数值 |
| -------------- | ---: |
| num_documents  |      |
| num_chunks     |      |
| num_vectors    |      |
| avg_latency_ms |      |
| hit@5          |      |
| recall@5       |      |
| precision@5    |      |
| mrr@5          |      |

### 按类别统计

| 类别 | 样本数 | hit@5 | recall@5 | precision@5 | mrr@5 | avg_latency_ms |
| ---- | -----: | ----: | -------: | ----------: | ----: | -------------: |
|      |        |       |          |             |       |                |

### 未命中样本

| 样本 ID | 期望来源 | 实际召回来源 |
| ------- | -------- | ------------ |
|         |          |              |

### 结果说明

-
````
