# RAG 配置类按功能域拆分 Spec

## Why

`RAGPipelineConfig`（`src/open_deep_research/rag/service.py`）将 50+ 个配置字段平铺在单一类中，涵盖 embedding、vectorstore、reranker、multimodal、memory、keyword、hybrid、graph 等子系统，违反单一职责原则。项目评审文档 `docs/project/project-review-and-improvements.md` 已将其列为中期改进项。

本次重构采用**只读视图模式**：平铺字段是唯一的真实存储，保持 Pydantic schema 100% 不变；通过 `@property` 动态生成只读的子配置对象供内部消费方使用；通过 `model_validator(mode="before")` 拦截嵌套构造请求并解包为平铺字段。外部消费者（LangGraph Configuration、MCP Server）完全不受影响。

## Design: 只读视图模式

核心设计原则：**平铺字段是唯一真实存储，子配置是只读派生视图。**

```
RAGPipelineConfig
├── embedding_provider: str = "sentence_transformers"   ← 真实存储（原样不动）
├── embedding_model: str = "..."                         ← 真实存储（原样不动）
├── ...                                                  ← 其他 50 个平铺字段原样不动
│
├── @model_validator(mode="before")                     ← 输入拦截：解包嵌套构造
│   def _unpack_sub_configs(cls, data):
│       if "embedding" in data and isinstance(data["embedding"], BaseModel):
│           emb = data.pop("embedding")
│           data.setdefault("embedding_provider", emb.provider)  # setdefault: 平铺优先
│           data.setdefault("embedding_model", emb.model)
│           ...
│
├── @property                                            ← 只读视图：从平铺字段实时派生
│   def embedding(self) -> EmbeddingConfig:
│       return EmbeddingConfig(
│           provider=self.embedding_provider,
│           model=self.embedding_model,
│           ...
│       )
│
└── @model_validator(mode="after")                       ← 原有验证逻辑不动
    def apply_path_defaults(self):
        # 仍然操作 self.vectorstore_path / self.milvus_uri 等平铺字段
        ...
```

**为什么不用双向同步的字段对？**
- Pydantic 不允许 `@property` 和同名字段共存
- 双向 `model_validator` 回写会引入 `model_fields_set` 判断的边缘 case
- `@property` 只读视图天然实时反映平铺字段的值，零同步成本

## What Changes

### 新增：子配置类
在 `src/open_deep_research/rag/config.py` 中新增以下子配置类（均为 Pydantic BaseModel）：
- `EmbeddingConfig`：provider / model / device / hash_dimensions
- `VectorstoreConfig`：provider / persist_path / collection_name / milvus_uri / milvus_token / milvus_db_name / milvus_metric_type
- `RerankerConfig`：provider / model / device
- `MultimodalConfig`：enabled / provider / ocr_languages / vision_enabled / vision_model / vision_prompt / vision_max_tokens
- `MemoryConfig`：enabled / paths / json_text_fields / mysql_url / mysql_table / mysql_limit / mysql_index_record_types / conversation_id / user_id
- `KeywordSearchConfig`：top_k / backend / elasticsearch_url / elasticsearch_index
- `HybridRetrievalConfig`：rrf_rank_constant / structured_metadata_weight
- `GraphRAGConfig`：enabled / backend / max_neighbors / weight / ner_enabled / idf_enabled / idf_threshold_percentile / confidence_threshold / structural_edges_enabled / neo4j_uri / neo4j_username / neo4j_password / neo4j_database
- `ChunkingConfig`：knowledge_base_paths / chunk_size / chunk_overlap / top_k / rerank_top_n / json_text_fields / authority_rerank_enabled

### 修改：RAGPipelineConfig（平铺字段不动，新增 property + before validator）
- **平铺字段定义完全不变**：50+ 个字段保持原有定义、默认值、Field metadata
- 新增 `model_validator(mode="before")` `_unpack_sub_configs`：拦截 `embedding=EmbeddingConfig(...)` 等嵌套构造，使用 `setdefault` 解包为平铺字段（显式平铺字段优先于子配置解包值）
- 新增 9 个 `@property`（如 `embedding`、`vectorstore`、`reranker` 等）：从平铺字段实时构造只读子配置对象
- `apply_path_defaults` validator **完全不动**，仍操作 `self.vectorstore_path` / `self.milvus_uri` 等平铺字段

### 修改：内部消费方（不改外部接口）
- `rag/indexer.py`：`RAGIndexer` 各方法改为读取 `config.embedding`、`config.vectorstore` 等子配置 property
- `rag/service.py`：`RAGPipeline.__init__` 中 `create_reranker` 调用改为读取 `config.reranker`
- `create_embedding_backend` / `create_vectorstore_backend` / `create_reranker` 函数签名新增子配置对象重载（保留旧签名做过渡）

### 不改：外部消费接口
- `Configuration` 类的 `rag_` 前缀字段定义不变
- `tools/rag_tool.py` / `memory/writer.py` 的 `rag_X → X` 适配映射不变
- `rag/mcp_server.py` 的 `build_pipeline_config` 双形式兼容逻辑不变
- `rag/mcp_server.py` 的 `_config_summary` 输出字段名不变
- README / docs 中的字段引用不变
- `RAGPipelineConfig.model_fields` / `model_dump()` / `model_json_schema()` 输出不变

## Impact
- Affected specs: 无
- Affected code:
  - `src/open_deep_research/rag/config.py`（新增）
  - `src/open_deep_research/rag/service.py`（RAGPipelineConfig 新增 property + before validator）
  - `src/open_deep_research/rag/indexer.py`（适配子配置 property 读取）
  - `src/open_deep_research/rag/embeddings.py`（新增重载）
  - `src/open_deep_research/rag/vectorstore.py`（新增重载）
  - `src/open_deep_research/rag/reranker.py`（新增重载）
  - `src/open_deep_research/rag/__init__.py`（导出新类）
  - `tests/test_rag.py`（新增子配置测试，保留旧字段测试零修改）

## ADDED Requirements

### Requirement: 子配置类定义
系统 SHALL 在 `rag/config.py` 中为每个功能域提供独立的 Pydantic 子配置类，每个类的字段名和默认值与原 `RAGPipelineConfig` 中对应字段完全一致。

#### Scenario: 零参构造子配置
- **WHEN** 开发者执行 `EmbeddingConfig()`
- **THEN** 返回的对象具有与原 `RAGPipelineConfig().embedding_provider` 等一致的默认值

#### Scenario: 子配置独立构造与传递
- **WHEN** 开发者执行 `config = EmbeddingConfig(provider="hash")`
- **THEN** 该对象可独立传递给 `create_embedding_backend(config)` 并正常工作

### Requirement: RAGPipelineConfig 只读视图
`RAGPipelineConfig` SHALL 保持全部平铺字段作为唯一真实存储，通过 `@property` 提供 9 个只读子配置视图，通过 `model_validator(mode="before")` 拦截嵌套构造请求。

平铺字段是唯一真实存储意味着：
- `model_fields` 不变
- `model_dump()` 输出不变（不含子配置 property）
- `model_json_schema()` 不变
- `model_fields_set` 行为不变
- 序列化 / 反序列化行为不变

#### Scenario: 零参构造保持默认值不变
- **WHEN** 开发者执行 `RAGPipelineConfig()`
- **THEN** `config.embedding.provider == "sentence_transformers"` 且 `config.embedding_provider == "sentence_transformers"` 同时成立

#### Scenario: 平铺字段写入反映到子配置视图
- **WHEN** 开发者执行 `config = RAGPipelineConfig(embedding_provider="hash")`
- **THEN** `config.embedding.provider == "hash"` 成立（property 实时派生）

#### Scenario: 子配置构造解包到平铺字段
- **WHEN** 开发者执行 `RAGPipelineConfig(embedding=EmbeddingConfig(provider="hash"))`
- **THEN** `config.embedding_provider == "hash"` 成立（before validator 解包）

#### Scenario: 显式平铺字段优先于子配置
- **WHEN** 开发者执行 `RAGPipelineConfig(embedding=EmbeddingConfig(provider="hash"), embedding_provider="sentence_transformers")`
- **THEN** `config.embedding_provider == "sentence_transformers"` 成立（setdefault 不覆盖显式平铺字段）

#### Scenario: model_dump 不含子配置 property
- **WHEN** 开发者执行 `RAGPipelineConfig().model_dump()`
- **THEN** 返回的 dict 中不含 `"embedding"` 键，仅含 `"embedding_provider"` 等平铺字段

#### Scenario: model_fields 不含子配置 property
- **WHEN** 检查 `RAGPipelineConfig.model_fields`
- **THEN** 不含 `"embedding"` / `"vectorstore"` 等子配置 property 名称，仅含原有平铺字段

### Requirement: 内部消费方迁移
`RAGIndexer` 和 `RAGPipeline` SHALL 优先从子配置 property 读取配置，同时保留对平铺字段的读取能力以维持过渡兼容。

#### Scenario: RAGIndexer 使用子配置初始化
- **WHEN** `RAGIndexer` 初始化时读取 `config.embedding`
- **THEN** embedding_backend 的 provider/model/device 与原平铺字段读取方式结果一致

#### Scenario: 向后兼容平铺字段构造
- **WHEN** 使用原有平铺字段构造 `RAGPipelineConfig(embedding_provider="hash", vectorstore_provider="memory")` 并创建 `RAGPipeline`
- **THEN** pipeline 正常初始化，行为与重构前一致

## MODIFIED Requirements

### Requirement: RAGPipelineConfig 配置验证
`apply_path_defaults` validator SHALL 保持操作平铺字段不变。当 `vectorstore_path` 被自定义且 `milvus_uri` 未显式设置时，自动推导 milvus_uri；同时校验 `structured_metadata_weight` 在 [0, 1] 范围内。子配置 property `config.vectorstore` 实时反映推导后的值。

#### Scenario: 平铺 vectorstore_path 触发 milvus_uri 推导
- **WHEN** 开发者执行 `RAGPipelineConfig(vectorstore_path="custom/path")`
- **THEN** `config.milvus_uri == "custom/path/milvus.db"` 且 `config.vectorstore.milvus_uri == "custom/path/milvus.db"`（property 实时反映）

#### Scenario: 子配置构造触发 milvus_uri 推导
- **WHEN** 开发者执行 `RAGPipelineConfig(vectorstore=VectorstoreConfig(persist_path="custom/path"))`
- **THEN** before validator解包 `persist_path` 到 `vectorstore_path`，apply_path_defaults 推导 milvus_uri，最终 `config.milvus_uri == "custom/path/milvus.db"`

#### Scenario: structured_metadata_weight 校验
- **WHEN** 开发者执行 `RAGPipelineConfig(structured_metadata_weight=1.5)`
- **THEN** 抛出 ValueError（"structured_metadata_weight must be between 0 and 1."）
