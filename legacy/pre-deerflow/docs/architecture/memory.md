# Memory 模块说明

## 本页速览

| 项目     | 内容                                                                                   |
| -------- | -------------------------------------------------------------------------------------- |
| 阅读目标 | 理解原始聊天记忆如何写入 MySQL，以及哪些 memory 会进入 RAG 索引。                      |
| 关键代码 | `src/open_deep_research/memory/`、`src/open_deep_research/rag/loaders/mysql_memory.py` |
| 上游文档 | [Agent Loop 模块说明](agent-loop.md)                                                   |
| 下游文档 | [RAG 模块说明](rag.md)、[RAG / Memory 笔记](rag-notes.md)                              |

本文重点是 memory 自身的事实源、类型和写入流程；检索、切分、引用格式化等内容放在 RAG 文档中。

## 1. 模块定位

`memory` 模块负责保存和读取原始聊天记忆。它拥有 MySQL 中的真实记忆数据，而 RAG 模块只接收其中可索引的一部分，把它们转换成 `RAGDocument` 后进入向量库和关键词索引。

边界原则：

- `memory` 负责原始记录、类型、ID、MySQL 表、写入和回查。
- `rag` 负责把可索引 memory 转换为文档、切块、embedding、检索和引用。
- `deep_researcher` 只在最终报告生成后调用 memory writer，不直接操作 MySQL。

核心目录：

- `src/open_deep_research/memory/`

核心文件：

- `types.py`
- `context.py`
- `extractor.py`
- `store.py`
- `writer.py`

## 2. 记忆类型

位置：

- `src/open_deep_research/memory/types.py`

类型定义：

```python
class MemoryType(str, Enum):
    CHAT_RAW = "chat_raw"
    SUMMARY = "summary"
    PREFERENCE = "preference"
    PROJECT_FACT = "project_fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    DEPRECATED = "deprecated"
```

### 2.1 不进入 RAG 索引的类型

```python
chat_raw
```

用途：

- 保存完整聊天流水。
- 作为原始审计和回查数据。
- 不直接进入向量库，避免噪声和临时上下文污染长期知识。

### 2.2 可进入 RAG 索引的类型

```python
summary
preference
project_fact
decision
constraint
deprecated
```

用途：

- 作为长期可检索记忆。
- 被 RAG loader 转换为 `RAGDocument`。
- 切块后进入 vector store、BM25、reranker 和 citation formatter。

其中 `deprecated` 会被标记为：

```python
memory_usage = "do_not_adopt"
```

后续 RAG 引用格式化会提醒模型把它当作废弃事实或禁止项，而不是推荐项。

## 3. 原始数据结构

位置：

- `ChatMemoryRecord`

字段：

| 字段              | 含义                            |
| ----------------- | ------------------------------- |
| `memory_id`       | 稳定记忆 ID，用于 upsert 和回查 |
| `conversation_id` | 会话范围                        |
| `record_type`     | 记忆类型                        |
| `content`         | 原始内容                        |
| `title`           | 记忆标题                        |
| `metadata`        | 附加元数据                      |
| `created_at`      | 创建时间                        |
| `user_id`         | 用户范围，可为空                |
| `index_status`    | 索引状态，默认 `pending`        |

`record_type` 会通过 validator 自动规范化，兼容旧别名：

| 旧值     | 新值           |
| -------- | -------------- |
| `chat`   | `chat_raw`     |
| `memory` | `project_fact` |

## 4. 会话与用户范围

位置：

- `src/open_deep_research/memory/context.py`

### 4.1 `get_conversation_id(config)`

查找顺序：

1. `configurable.conversation_id`
2. `configurable.thread_id`
3. `metadata.conversation_id`
4. `metadata.thread_id`
5. `get_user_id(config)`
6. `"default"`

用途：

- 写入 MySQL memory。
- RAG 工具查询 MySQL memory 时过滤当前会话。
- citation source 中生成 `memory://mysql/{conversation_id}/{memory_id}`。

### 4.2 `get_user_id(config)`

查找顺序：

1. `configurable.user_id`
2. `metadata.user_id`
3. `metadata.owner`
4. `configurable.owner`

用途：

- 支持用户级 memory scope。
- 兼容旧流程里使用 `metadata.owner` 的场景。

## 5. 记忆抽取

位置：

- `src/open_deep_research/memory/extractor.py`

核心函数：

- `build_chat_memory_records(...)`
- `extract_conversation_memories(...)`

输入：

- `conversation_id`
- `user_id`
- `chat_content`
- `summary`
- `memories`
- `metadata`

输出：

- `list[ChatMemoryRecord]`

默认生成：

1. `chat_raw`：完整聊天文本。
2. `summary`：最终报告内容。
3. `project_fact` 或传入对象指定的其他记忆类型。

`memories` 支持两种形态：

```python
["plain memory text"]
```

会被当作 `project_fact`。

```python
[
    {
        "record_type": "decision",
        "title": "Architecture Decision",
        "content": "Use Milvus as the default vector store."
    }
]
```

会按传入类型保存。

## 6. 稳定 ID

位置：

- `stable_memory_id(...)`

生成方式：

```text
sha256(user_id + conversation_id + record_type + content)
```

输出格式：

```text
{record_type}-{digest前24位}
```

作用：

- 同一内容重复写入会 upsert 到同一行。
- 避免同一会话重复产生大量相同 memory。
- 给 RAG citation 和 MySQL 回查提供稳定主键。

## 7. MySQL 存储

位置：

- `src/open_deep_research/memory/store.py`

核心类：

```python
class MySQLChatMemoryStore
```

初始化：

```python
MySQLChatMemoryStore(database_url, table_name="rag_chat_memories")
```

### 7.1 表结构

表会由 `ensure_schema()` 自动创建：

| 字段              | 类型         | 说明                  |
| ----------------- | ------------ | --------------------- |
| `id`              | BIGINT       | 自增主键              |
| `memory_id`       | VARCHAR(96)  | 唯一记忆 ID           |
| `user_id`         | VARCHAR(255) | 用户 ID               |
| `conversation_id` | VARCHAR(255) | 会话 ID               |
| `record_type`     | VARCHAR(32)  | 记忆类型              |
| `index_status`    | VARCHAR(32)  | `pending` / `indexed` |
| `title`           | VARCHAR(512) | 标题                  |
| `content`         | LONGTEXT     | 原文                  |
| `metadata_json`   | JSON         | 元数据                |
| `created_at`      | DATETIME(6)  | 创建时间              |
| `updated_at`      | DATETIME(6)  | 更新时间              |

索引：

- `uq_memory_id`
- `idx_user_id`
- `idx_conversation_id`
- `idx_record_type`
- `idx_index_status`
- `idx_updated_at`

已有表会尝试 best-effort migration，补充 `user_id`、`index_status` 和对应索引。

### 7.2 写入

```python
upsert_records(records)
```

行为：

- 自动 `ensure_schema()`。
- 批量 insert。
- `memory_id` 冲突时 update。
- 写入后记录保持 `pending`，等待 RAG indexer 同步。

### 7.3 读取

```python
load_records(
    conversation_id=None,
    limit=1000,
    record_types=None,
    user_id=None,
    index_status=None,
)
```

支持按：

- 用户
- 会话
- 类型
- 索引状态

过滤。

### 7.4 加载待索引记录

```python
load_pending_indexable_records(...)
```

默认只加载 `INDEXABLE_MEMORY_TYPES`，并且 `index_status="pending"`。

### 7.5 标记已索引

```python
mark_records_indexed(memory_ids)
```

RAG indexer 完成索引后调用，将对应记录更新为 `indexed`。

### 7.6 原文回查

支持三种入口：

- `get_record_by_memory_id(memory_id, conversation_id=None)`
- `get_record_by_source(source)`
- `load_memory_record_by_source(...)`

`source` 格式：

```text
memory://mysql/{conversation_id}/{memory_id}
```

## 8. 写入流程

位置：

- `src/open_deep_research/memory/writer.py`
- `src/open_deep_research/deep_researcher.py`

主入口：

```python
persist_conversation_memory(...)
```

调用时机：

- `final_report_generation(...)` 成功生成最终报告后。
- 通过 `maybe_persist_chat_memory(...)` 异步转线程调用。

流程：

1. 检查 `rag_memory_write_enabled`。
2. 检查 `rag_memory_mysql_url`。
3. 用 `get_conversation_id(config)` 和 `get_user_id(config)` 解析范围。
4. 调用 `extract_conversation_memories(...)` 构造 memory records。
5. 创建 `MySQLChatMemoryStore`。
6. 调用 `upsert_records(records)`。
7. 如果 `rag_memory_write_sync_index=True`，启动后台线程刷新 RAG 索引。

## 9. 后台索引刷新

入口：

- `trigger_memory_index_refresh(...)`

实现：

- 启动 daemon thread。
- 在线程里构造 `RAGPipelineConfig`。
- 强制 `memory_enabled=True`。
- 传入当前 `conversation_id` 和 `user_id`。
- 调用 `pipeline.index_pending_memories()`。

注意：

- 这是非阻塞刷新。
- 即使后台刷新失败，query-time 的 `RAGIndexer.ensure_ready()` 仍会作为兜底。
- 异常只记录 warning，不影响 final report 返回。

## 10. Memory 到 RAG 的转换

转换位置：

- `src/open_deep_research/rag/loaders/mysql_memory.py`

虽然文件在 `rag` 目录下，但它消费的是 `memory` 模块定义的 `ChatMemoryRecord`。

转换规则：

```text
ChatMemoryRecord
  -> RAGDocument
  -> RAGChunk
  -> embedding + BM25
  -> RetrievalResult
  -> Citation
```

`RAGDocument.source` 格式：

```text
memory://mysql/{conversation_id}/{memory_id}
```

metadata 会保留：

- `source_type=memory`
- `memory_backend=mysql`
- `memory_id`
- `memory_type`
- `conversation_id`
- `user_id`
- `index_status`
- `created_at`

## 11. 关键配置

| 配置                                  | 作用                               |
| ------------------------------------- | ---------------------------------- |
| `rag_memory_enabled`                  | 查询时是否把 memory 作为 RAG 来源  |
| `rag_memory_paths`                    | JSON/JSONL 文件型 memory 路径      |
| `rag_memory_mysql_url`                | MySQL memory 数据库 URL            |
| `rag_memory_mysql_table`              | MySQL memory 表名                  |
| `rag_memory_mysql_limit`              | 查询/索引加载的最大行数            |
| `rag_memory_mysql_index_record_types` | 允许进入 RAG 索引的类型            |
| `rag_memory_write_enabled`            | final report 后是否写 MySQL memory |
| `rag_memory_write_sync_index`         | 写入后是否后台刷新索引             |

## 12. 设计约束

### 12.1 原始聊天和可索引记忆分离

完整聊天流水保留在 MySQL，但默认不进入向量库。

这样可以：

- 保留审计能力。
- 降低检索噪声。
- 避免临时讨论被当成长期事实。

### 12.2 MySQL 是事实源

向量库只保存可重建的索引副本。

需要完整内容时，应通过：

```text
memory_id / memory://mysql/... source
```

回查 MySQL。

### 12.3 `pending` 到 `indexed`

新写入记录默认 `pending`。

RAG indexer 同步后标记 `indexed`。

这个状态用于：

- 后台增量索引触发。
- 运维诊断。
- 避免无法确认哪些 memory 已经进入检索链路。

## 13. 扩展建议

### 新增 memory 类型

需要改动：

1. `MemoryType`
2. `INDEXABLE_MEMORY_TYPES` 或 `NON_INDEXABLE_MEMORY_TYPES`
3. `_default_title(...)`
4. RAG citation 对新类型的展示逻辑，如需要
5. 测试覆盖 normalize、写入、RAG 转换

### 新增存储后端

当前原始 memory 后端是 MySQL。

如果新增 PostgreSQL 或 SQLite，应保持：

- `ChatMemoryRecord` 不变。
- `memory_id` 生成规则不变。
- `source` 回查协议可区分后端。
- RAG loader 输出仍为 `RAGDocument`。
