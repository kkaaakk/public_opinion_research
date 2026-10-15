# Checklist

## 子配置类定义

* [x] `rag/config.py` 中 9 个子配置类的字段名与原 RAGPipelineConfig 对应字段一致

* [x] 每个子配置类的字段默认值与原 RAGPipelineConfig 对应字段默认值一致

* [x] `MemoryConfig.mysql_index_record_types` 默认值使用 `INDEXABLE_MEMORY_TYPES`

* [x] `MultimodalConfig.vision_model` 默认值使用 `DEFAULT_RAG_VISION_MODEL`

* [x] `MultimodalConfig.vision_prompt` 默认值使用 `DEFAULT_RAG_VISION_PROMPT`

* [x] `MemoryConfig.paths` 默认值为 `["data/memory/chat_memory.jsonl"]`

* [x] `ChunkingConfig.knowledge_base_paths` 默认值为 `["data/knowledge"]`

* [x] `rag/__init__.py` 导出全部 9 个子配置类

## RAGPipelineConfig 只读视图模式

* [x] 平铺字段定义完全不变（50+ 个字段原样不动）

* [x] `RAGPipelineConfig()` 零参构造时，`config.embedding.provider == "sentence_transformers"`

* [x] `RAGPipelineConfig(embedding_provider="hash")` 时，`config.embedding.provider == "hash"`（property 实时派生）

* [x] `RAGPipelineConfig(embedding=EmbeddingConfig(provider="hash"))` 时，`config.embedding_provider == "hash"`（before validator 解包）

* [x] `RAGPipelineConfig(embedding=EmbeddingConfig(provider="hash"), embedding_provider="sentence_transformers")` 时，`config.embedding_provider == "sentence_transformers"`（显式平铺字段优先）

* [x] `RAGPipelineConfig().model_dump()` 不含 `"embedding"` / `"vectorstore"` 等子配置键

* [x] `RAGPipelineConfig.model_fields` 不含子配置 property 名称

* [x] `RAGPipelineConfig().model_json_schema()` 输出与重构前一致

* [x] `model_fields_set` 在平铺字段构造方式下行为不变

* [x] `model_fields_set` 在嵌套子配置构造方式下正确记录解包后的平铺字段（仅显式设置的字段）

* [x] `apply_path_defaults` validator 代码完全不动，仍操作 `self.vectorstore_path` / `self.milvus_uri` 等平铺字段

* [x] `structured_metadata_weight` 超出 \[0,1] 范围时仍正确抛出 ValueError

* [x] 平铺 `vectorstore_path="custom/path"` 时，`config.milvus_uri` 和 `config.vectorstore.milvus_uri` 均为 `"custom/path/milvus.db"`

* [x] 嵌套 `vectorstore=VectorstoreConfig(persist_path="custom/path")` 时，before validator 解包后 milvus\_uri 正确推导

## 内部消费方迁移

* [x] `RAGIndexer.__init__` 中 embedding\_backend 使用 `config.embedding` 初始化

* [x] `RAGIndexer.__init__` 中 vectorstore 使用 `config.vectorstore` 初始化

* [x] `RAGIndexer.ensure_ready` 中 keyword\_index 使用 `config.keyword_search` 初始化

* [x] `RAGIndexer.ensure_ready` 中 retriever 使用 `config.hybrid_retrieval` 和 `config.graph_rag` 初始化

* [x] `RAGIndexer.load_indexable_documents` 使用 `config.chunking` / `config.multimodal` / `config.memory` 读取

* [x] `RAGIndexer.source_fingerprint` 使用子配置 property 读取

* [x] `RAGPipeline.__init__` 中 reranker 使用 `config.reranker` 初始化

## 工厂函数重载

* [x] `create_embedding_backend` 支持 EmbeddingConfig 入参

* [x] `create_vectorstore_backend` 支持 VectorstoreConfig 入参

* [x] `create_reranker` 支持 RerankerConfig 入参

* [x] 三个工厂函数的旧签名仍然可用（向后兼容）

## 外部接口不受影响

* [x] `Configuration` 类的 `rag_` 前缀字段定义未改变

* [x] `tools/rag_tool.py` 的适配映射未改变

* [x] `memory/writer.py` 的适配映射未改变

* [x] `rag/mcp_server.py` 的 `build_pipeline_config` 兼容逻辑未改变

* [x] `rag/mcp_server.py` 的 `_config_summary` 输出字段名未改变

## 测试通过

* [x] `tests/test_rag.py` 中所有现有用例零修改通过（11 passed: defaults/index\_id/empty/preserves/config 相关）

* [x] `tests/evaluate_rag_retrieval.py` 的全字段构造正常工作（代码路径未修改，平铺构造兼容）

* [x] `tests/test_rag_mcp_server.py` 的兼容性测试通过（5 passed）

* [x] 新增子配置类默认值验证测试通过（test\_sub\_config\_defaults\_match\_flat\_field\_defaults）

* [x] 新增只读视图双向验证测试通过（平铺→property + 嵌套→解包 + 显式平铺优先）

* [x] 新增 model\_fields / model\_dump 不变性测试通过

* [x] 配置相关测试全部通过（18 passed, 0 failed；3 个预先存在的 Neo4j/网络相关失败与本次重构无关）

