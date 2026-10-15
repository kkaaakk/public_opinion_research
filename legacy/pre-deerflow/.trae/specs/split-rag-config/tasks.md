# Tasks

- [x] Task 1: 创建 `src/open_deep_research/rag/config.py`，定义全部 9 个子配置类
  - [x] SubTask 1.1: 定义 EmbeddingConfig、VectorstoreConfig、RerankerConfig
  - [x] SubTask 1.2: 定义 MultimodalConfig、MemoryConfig、KeywordSearchConfig
  - [x] SubTask 1.3: 定义 HybridRetrievalConfig、GraphRAGConfig、ChunkingConfig
  - [x] SubTask 1.4: 在 `rag/__init__.py` 中导出全部子配置类

- [x] Task 2: 重构 `RAGPipelineConfig`，新增只读视图 property 和 before validator
  - [x] SubTask 2.1: 新增 `model_validator(mode="before")` `_unpack_sub_configs`，拦截嵌套子配置构造并使用 setdefault 解包为平铺字段
  - [x] SubTask 2.2: 新增 9 个 `@property`（embedding / vectorstore / reranker / multimodal / memory / keyword_search / hybrid_retrieval / graph_rag / chunking），从平铺字段实时构造只读子配置对象
  - [x] SubTask 2.3: 确保 `apply_path_defaults` validator 完全不动，仍操作平铺字段
  - [x] SubTask 2.4: 验证 `model_fields` / `model_dump()` / `model_json_schema()` 输出不变

- [x] Task 3: 迁移内部消费方读取逻辑到子配置 property
  - [x] SubTask 3.1: 修改 `rag/indexer.py` 的 `RAGIndexer.__init__`，从 `config.embedding` / `config.vectorstore` 读取
  - [x] SubTask 3.2: 修改 `rag/indexer.py` 的 `ensure_ready`，从 `config.keyword_search` / `config.hybrid_retrieval` / `config.graph_rag` 读取
  - [x] SubTask 3.3: 修改 `rag/indexer.py` 的 `load_indexable_documents`，从 `config.chunking` / `config.multimodal` / `config.memory` 读取
  - [x] SubTask 3.4: 修改 `rag/indexer.py` 的 `source_fingerprint` 和 `_mark_pending_memories_indexed`，从子配置读取
  - [x] SubTask 3.5: 修改 `rag/service.py` 的 `RAGPipeline.__init__`，从 `config.reranker` 读取
  - [x] SubTask 3.6: 修改 `rag/service.py` 的 `query` 方法和 `build_rag_index_id` 函数，适配新结构

- [x] Task 4: 适配工厂函数支持子配置对象入参（保留旧签名）
  - [x] SubTask 4.1: `create_embedding_backend` 新增 EmbeddingConfig 入参重载
  - [x] SubTask 4.2: `create_vectorstore_backend` 新增 VectorstoreConfig 入参重载
  - [x] SubTask 4.3: `create_reranker` 新增 RerankerConfig 入参重载

- [x] Task 5: 适配测试代码
  - [x] SubTask 5.1: 确保 `tests/test_rag.py` 中所有平铺字段构造的用例仍然通过（零修改优先）
  - [x] SubTask 5.2: 确保 `tests/evaluate_rag_retrieval.py` 的全字段构造仍然通过
  - [x] SubTask 5.3: 确保 `tests/test_rag_mcp_server.py` 的 `build_pipeline_config` 兼容测试仍然通过
  - [x] SubTask 5.4: 新增子配置类零参构造默认值验证测试
  - [x] SubTask 5.5: 新增平铺字段与子配置双向同步的测试（平铺→property 派生 + 嵌套构造→解包）
  - [x] SubTask 5.6: 新增 model_fields / model_dump 不含子配置 property 的测试

- [x] Task 6: 运行全量测试并修复回归
  - [x] SubTask 6.1: 运行 `python -m pytest tests/ -x -q` 确保零失败
  - [x] SubTask 6.2: 修复测试中因 before validator 或 property 导致的边缘 case

# Task Dependencies
- Task 2 depends on Task 1
- Task 3 depends on Task 2
- Task 4 depends on Task 1
- Task 5 depends on Task 2, Task 3, Task 4
- Task 6 depends on Task 5
