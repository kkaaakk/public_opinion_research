# RAG 模块设计文档

> **文档定位**：本文档不描述"每个组件怎么做"（那是 `rag.md` 的职责），而是回答"为什么这么做"——每个设计决策的场景驱动、trade-off 分析、以及量化依据。

## 本页速览

| 项目 | 内容 |
|------|------|
| 阅读目标 | 理解 RAG 子系统每个设计决策的 **why**，而非 **how** |
| 目标读者 | 面试官、代码审查者、后续维护者 |
| 前置阅读 | [RAG 模块说明](rag.md)——先了解组件职责再看本文档 |
| 配套文档 | [RAG 检索测试记录](../evaluation/rag-retrieval-test-records.md)——设计决策的量化支撑 |

---

## 1. 问题定义：为什么 Deep Research Agent 需要本地 RAG？

### 1.1 场景驱动

Deep Research Agent 的核心任务是**给定一个问题，生成一份有引用的研究报告**。这个任务有两个信息来源：

| 信息来源 | 特点 | 典型案例 |
|----------|------|----------|
| **外部搜索**（Web Search） | 公开、新鲜、覆盖面广，但噪音大、权威性参差不齐 | 行业趋势分析、竞品调研 |
| **本地知识库**（Local RAG） | 私有、权威、结构化好，但覆盖面窄、需要维护 | 内部 FAQ、合规文档、历史决策记录 |

只依赖 Web Search 的问题：
- **信息滞后**：内部最新决策没来得及公开
- **权威性不足**：官网 vs 第三方报道，哪个更可信？
- **语境缺失**：公司内部术语、历史背景、团队分工无法从公开信息获取
- **隐私泄露风险**：涉及内部数据的查询不能走外网搜索

这就是本地 RAG 存在的价值——**它不能替代 Web Search，但它提供了 Web Search 无法提供的东西：私有的、权威的、结构化的内部语境**。

### 1.2 与通用 RAG 的差异

| 维度 | 通用 RAG（如 LangChain RAG） | 本项目的 RAG |
|------|------------------------------|-------------|
| 调用方 | 最终用户 | Agent 的 researcher 子图 |
| 输出要求 | 回答用户问题 | 提供可引用的上下文（不直接回答） |
| 典型查询 | "什么是 X？" | "X 的内部决策记录是什么？是否有相关的合规约束？" |
| 多轮语境 | 用户对话历史 | Agent 多步推理中的累计上下文 |
| 权威性需求 | 中等 | 高——错误引用会导致研究报告质量下降 |

**关键决策**：RAG 不直接生成回答，只返回带引用的上下文。最终回答由 researcher agent 综合多个来源（Web + RAG + MCP）后生成。这保证了 RAG 的错误不会直接变成报告中的错误——它只是证据之一，需要与其他来源交叉验证。

---

## 2. 架构原则

### 2.1 核心 trade-off：召回率 vs 精确率

RAG 检索面临一个经典张力：

- **高召回**：宁可多召回一些无关 chunk，也要确保相关内容不被遗漏 → `hit@5` 高
- **高精确**：返回的每个 chunk 都是相关的 → `precision@5` 高

对于 Deep Research 场景，**召回优先于精确**。理由：

1. Agent 有推理能力——它可以通过 `think_tool` 判断哪些引用与问题相关，多几个无关引用不会导致错误答案
2. 漏召回是致命的——缺少关键内部证据，researcher 只能依赖 Web Search，可能得出错误结论
3. Reranker 可以在召回后做精排，先把精确率交给后续环节

**这决定了整个 pipeline 的形态**：粗召回（向量 + BM25）→ 精排（Cross-Encoder）→ 过滤（Authority），每一层都在逐步收紧精确率，而不是一开始就追求精确。

### 2.2 设计原则一览

| 原则 | 说明 | 体现 |
|------|------|------|
| **召回优先** | 宁可多不能少 | 向量 + BM25 双路召回，RRF 融合 |
| **分层过滤** | 逐步收紧精确率 | 粗召回 → Rerank → Authority → Filter |
| **证据可追溯** | 每个引用指向源文档的具体位置 | chunk metadata 保留 source/行号/标题路径/JSON path |
| **自进化** | 对话中产生的新知识沉淀回索引 | Memory → MySQL → RAG index 闭环 |
| **防退化** | 过期/错误信息不能污染检索结果 | Authority 系统标记并惩罚 misleading/deprecated 内容 |
| **降级可用** | 每个组件都有合理的 fallback | Hash embedding（测试）、NoOp Reranker（无 GPU）、InMemory VectorStore（无外部依赖） |

---

## 3. Pipeline 设计

### 3.1 为什么是 "粗召回 → 精排 → 过滤" 三段式？

```
Query → [向量召回 + BM25] → [RRF 融合] → [GraphRAG 扩展] → [Cross-Encoder 重排] → [Authority 调整] → [过滤] → 最终结果
        ←── 粗召回 ──→                                    ←── 精排 ──→  ← 过滤 →
```

**为什么不用端到端的单步检索？**

- 单步检索（如直接用 dense vector 取 top-k）在精度上可达标（recall 尚可），但 precision 在噪声场景下很差
- 评测记录 5 中，纯向量 + BM25 + RRF 的 precision@5 在大量误导内容下仅 0.43
- 加上 Cross-Encoder 重排后，precision 显著提升

**为什么不直接用 Cross-Encoder 对全库打分？**

- Cross-Encoder 需要逐对计算 query 和每个 chunk 的相关性，复杂度 O(N)，对小库可行，对大规模知识库不可行
- 粗召回把候选从 N 缩到 top-K（K≈20），再交给 Cross-Encoder，复杂度 O(K)，性能可接受

**为什么 RRF 而不是加权求和？**

向量分数（余弦相似度 0~1）和 BM25 分数（无上界，与语料规模相关）的尺度完全不同。直接加权求和需要归一化，而归一化策略（min-max? z-score?）对结果影响大且难以调参。RRF 只关心排名位置，天然免疫分数尺度不一致的问题，`rank_constant=60` 是领域常见的稳健选择。

### 3.2 为什么默认 top_k=4？

- 最终返回给 LLM 的 chunk 数
- 不是越多越好——LLM 的 context window 有限，4 个精选引用已经足以支撑一个 claim
- 评测记录中 `rerank_top_n=20`（进入重排的候选数）远大于 `top_k=4`，保证了候选池足够大，精排后取 top 4

### 3.3 为什么 chunk_size=1200, chunk_overlap=200？

| 参数 | 值 | 理由 |
|------|-----|------|
| chunk_size | 1200 字符 | 足够容纳一个完整的段落/FAQ 条目/section，但不超过 embedding 模型的最佳输入长度 |
| chunk_overlap | 200 字符 | 约 16% 重叠率，保证跨 chunk 边界的信息不会因切分而断裂 |

注意：初始测试时用 `chunk_size=400`，后来调整为 `1200`。400 太小，容易把一个回答拆到多个 chunk 中，导致检索时只有部分片段被召回。调大后减少了碎片化。

---

## 4. 组件级设计决策

### 4.1 Embedding：为什么是 paraphrase-multilingual-MiniLM-L12-v2？

**核心需求**：中英文混合检索。项目的知识库中有大量中文内容（Tesla 电池 FAQ、合规文档、历史案例），但查询可能是中英文混合的。

**候选方案对比**：

| 方案 | 中文支持 | 模型大小 | 推理速度 | 选择？ |
|------|----------|----------|----------|--------|
| text-embedding-3-small (OpenAI) | ✅ | API 调用 | 网络延迟 | ❌ 需要外网 + API 费用 |
| bge-large-zh-v1.5 | ✅ 中文最优 | 326MB | GPU 需 >1GB 显存 | ❌ 太重，且英文支持一般 |
| all-MiniLM-L6-v2 | ❌ 英文为主 | 80MB | 极快 | ❌ 中文效果差 |
| **paraphrase-multilingual-MiniLM-L12-v2** | ✅ 50+ 语言 | 420MB | CPU 可用 | ✅ **最佳平衡** |

**决策**：在"多语言覆盖 + 本地部署 + CPU 可运行"三者间取平衡。多语言模型的中文效果不如专用中文模型，但在混合场景下表现足够好——评测记录 2 中 cross_lingual 类别的 `hit@5` 从 hash 的 0.375 提升到 1.0。

**为什么不用 API embedding？**
- 本地部署：无网络依赖，无 API 费用
- 隐私：内部文档不出机器
- 确定性：同一模型版本结果可复现

### 4.2 向量库：为什么默认是 Milvus？

| 方案 | 持久化 | 分布式 | 中文支持 | 部署复杂度 | 选择？ |
|------|--------|--------|----------|------------|--------|
| InMemory | ❌ | ❌ | N/A | 零 | 测试用 |
| FAISS | 半（需手动序列化） | ❌ | N/A | 低 | 小规模备选 |
| Chroma | ✅ | ❌ | N/A | 低 | 备选 |
| **Milvus Lite** | ✅ | 可升级到 server | N/A | 低（嵌入式） | ✅ **默认** |

**决策**：
- Milvus Lite 是一个嵌入式版本，不需要单独部署 server，依赖就是一个 `.db` 文件
- 如果数据量增长，可以无缝升级到 Milvus Standalone/Cluster
- 评测过程中发现 Windows 下 workspace 内文件重命名权限问题，评估脚本改为使用 `$TEMP` 目录——这是工程实践中才会遇到的真实问题

**为什么还保留 FAISS/Chroma/InMemory？**
- InMemory：单元测试和快速原型不需要持久化
- FAISS：某些场景下用户已经有了 FAISS 索引，不需要迁移
- Chroma：社区流行，兼容性需求
- 但从工程角度看，**如果让我只保留一个，我会选 Milvus**——这 4 个选项的存在更多是为了灵活性，而非每个都有独特不可替代的场景。

### 4.3 BM25/关键词检索：为什么需要？

**向量召回不是万能的**。它在以下场景表现不佳：

| 场景 | 向量召回的问题 | BM25 的优势 |
|------|---------------|-------------|
| 精确术语匹配 | "GRC-2025-003" → 向量可能召回 "GRC-2024-xxx" | 精确 IDF 匹配 |
| 编号/代码/ID | "model_3_thermal" → 语义模糊 | 字面匹配 |
| 短查询 | "rollback owner" → embedding 上下文不足 | 关键词匹配 |
| 专有名词缩写 | "BMS" → embedding 可能不理解缩写 | 精确匹配 |

**实现选择**：自建内存 BM25，不引入 Elasticsearch 依赖。

- 对于中小型本地知识库（几百到几千个 chunk），内存 BM25 足够快
- CJK 按单字拆分，不做分词——避免引入 jieba 等中文分词依赖，而且单字索引在中日韩文中反而是稳健的（对分词错误不敏感）
- BAAI/bge-reranker-base 的重排弥补了没有中文分词的不足

**为什么提供 Elasticsearch 选项？**
- 当知识库规模增长到万级 chunk 以上，内存 BM25 的遍历式检索会成为瓶颈
- Elasticsearch 的倒排索引天然支持大规模关键词检索
- 但不是默认选择——不想让用户为一个可选功能安装 ES

### 4.4 Reranker：为什么是 Cross-Encoder？

**粗召回（向量 + BM25）的精度天花板**在评测中被反复验证：

```
记录 4（高噪声场景）：
  recall@5 = 0.933      ← 相关内容能被召回
  precision@5 = 0.53    ← 但一半以上是噪音
```

加了 Cross-Encoder 重排后，precision 显著提升。为什么？

- **向量检索**：query 和 chunk 独立编码，交互只在最后的余弦距离计算中——信息交互不充分
- **Cross-Encoder**：query 和 chunk 拼接后一起送入模型，逐 token 交互——能捕捉到精细的语义匹配关系

**模型选择**：`BAAI/bge-reranker-base`（默认，278MB），而不是更流行的 `cross-encoder/ms-marco-MiniLM-L6-v2`。

| 模型 | 多语言支持 | 大小 | 选择 |
|------|-----------|------|------|
| cross-encoder/ms-marco-MiniLM-L6-v2 | ❌ 英文 | 80MB（评测记录 2-5 实际使用） | 评测用，历史遗留 |
| **BAAI/bge-reranker-base** | ✅ 中英文 | 278MB | **默认** |

**为什么默认用 BGE 而不是 MiniLM？**
- 知识库中有大量中文内容，BGE 的中文 rerank 效果显著优于纯英文模型
- 模型大小仍可接受（278MB，CPU 可用）
- 评测记录 2-5 用的是 MiniLM 只是历史原因（评测集以英文为主）

### 4.5 Hybrid Fusion：纯 RRF

当前 hybrid retrieval 使用向量召回和 BM25/关键词召回的 Reciprocal Rank
Fusion（RRF）按排名位置融合，不再暴露没有实际作用的 dense/sparse alpha
配置。这样配置项与真实 ranking 行为保持一致，也避免用户误以为可以通过
alpha 调整权重。

### 4.6 Authority 系统：解决"知识腐烂"问题

**问题**：本地知识库随着时间推移会积累过期、错误、被推翻的信息。如果不加处理：

- 旧版 FAQ 条目会排在新版之上（向量相似度可能更高）
- 已被纠正的错误决策仍会被检索出来
- 故意加入的误导内容（测试用）可能污染回答

**解决方案**：Authority 元数据标记 + 分数惩罚 + 硬阻断。

**分类**：

| 状态 | 含义 | 权重 | 惩罚 | 是否阻断 |
|------|------|------|------|----------|
| `authoritative` | 权威来源 | 1.0 | 0.0 | ❌ |
| `deprecated` | 已废弃但仍有参考价值 | 0.35 | -4.0 | ❌（降低排名） |
| `misleading` | 已知的错误信息 | 0.1 | -8.0 | ✅ **阻断** |
| `unanswerable_trap` | 故意设置的陷阱问题 | 0.0 | -10.0 | ✅ **阻断** |

**自动推断规则**：

- 文件名/路径包含 `misleading_archive` 且标题含 "negative misleading notes" → `misleading`
- chunk 内容包含 `[deprecated]` / `[retired]` / `[do_not_use]` → `deprecated`
- 其他默认为 `authoritative`

**设计考量**：
- 标记在 **splitter 阶段**而非 retrieval 阶段做——一次标记，终身使用
- 惩罚值 (-4.0, -8.0, -10.0) 的选择：reranker 的分数通常在 [0, 需要验证] 范围内，-4 足以把 deprecated 推到结果列表底部但不会完全消失，-8/-10 确保直接出局
- `deprecated` 不阻断但大幅降权——因为废弃文档中可能存在仍有参考价值的上下文

### 4.7 Structured Metadata Boost：一个微小但有效的信号

**核心思路**：如果 query 中出现了与 chunk 的源文件名、标题、JSON path 等结构化 metadata 匹配的词，给这个 chunk 小幅加分。

**为什么权重只有 0.15？**
- 这是一个**辅助信号**，不应该主导排序。如果用户问 "data governance policy"，那么 `data_governance.md` 中的 chunk 应该得到轻微加分
- 过高权重（如 0.5+）会导致"只要标题匹配就排到第一"——这违背了语义检索的初衷
- 0.15 的默认值是通过评测记录观察到的合理值：能帮助跨语言查询（中文 query 匹配英文文件名），但不会颠覆语义评分

---

## 5. Memory 与知识自进化

### 5.1 设计理念：从 "静态知识库" 到 "自进化知识系统"

传统 RAG 的知识库是静态的——上传文档 → 建索引 → 查询。这不是 agent 系统的终点。

在 Deep Research 的日常使用中，会产生大量有价值的**过程知识**：

- 某次研究得出的**结论**（"经查证，X 公司的 Y 政策已于 2025 年 6 月废止"）
- 对话中明确的**偏好**（"以后研究竞品时优先用 Gartner 报告"）
- 验证过的**事实**（"Z 产品的固件版本在 3.2.1 后修复了该漏洞"）
- 明确做出的**决策**（"确定用 Milvus 作为默认向量库"）

这些知识如果只存在于对话历史中，下次需要时会重新搜索、重新验证，浪费且不稳定。如果沉淀到知识库，就形成了**自进化的闭环**。

### 5.2 Memory 类型设计逻辑

| 类型 | 语义 | 索引？ | 设计理由 |
|------|------|--------|----------|
| `chat_raw` | 原始对话记录 | ❌ | 噪音大，不适合直接检索 |
| `summary` | 对话摘要 | ✅ | 压缩后的关键信息，适合检索 |
| `preference` | 用户偏好 | ✅ | 影响后续 agent 行为（如搜索偏好） |
| `project_fact` | 项目事实 | ✅ | 可验证的陈述，最高权重 |
| `decision` | 决策记录 | ✅ | 为什么做某个选择，追溯依据 |
| `constraint` | 约束条件 | ✅ | 硬性限制，必须遵守（如合规要求） |
| `deprecated` | 已废弃的记忆 | ✅（低权重） | 保留但降权，用于对比和追溯 |

**为什么 `chat_raw` 不索引？**

原始对话包含大量噪音：用户输入的不完整句子、agent 的思考过程、格式化的引用块。直接索引会导致检索噪音显著上升。通过 `summary` 类型先压缩再索引，既保留了关键信息又控制了噪音。

**为什么 `deprecated` 类型要索引但不阻断？**

与文档的 `deprecated` 状态不同，deprecated 记忆仍然有价值——它记录了"我们曾经认为 X，后来发现 Y，所以改为 Z"的完整链条。在回答"为什么我们之前选了 X"这样的问题时，deprecated 记忆是唯一的权威来源。

### 5.3 沉淀流程

```
[Agent 对话完成]
    │
    ▼
[memory/writer.py: persist_conversation_memory()]
    │  提取关键事实、决策、偏好
    │  生成 ChatMemoryRecord(s)
    │  index_status = "pending"
    │
    ▼
[MySQL rag_chat_memories 表]
    │
    ▼
[RAG Indexer: index_pending_memories()]
    │  加载所有 pending 记录
    │  转换为 RAGDocument (source = memory://mysql/{conversation_id}/{memory_id})
    │  嵌入并写入向量库
    │  更新 index_status = "indexed"
    │
    ▼
[下次 RAG 查询可用]
```

**为什么用 MySQL 而不是直接写向量库？**

- 持久化保证：MySQL 作为 source of truth，向量库可以重建
- 审批/编辑：在 MySQL 中可以修改或删除错误记忆，再触发重新索引
- 多实例：多个 agent 实例可以共享同一个 MySQL 记忆库

---

## 6. GraphRAG：轻量共现图扩展

### 6.1 设计定位

> GraphRAG 不替代向量召回和关键词召回，只补充相关上下文。

它解决的是一个具体问题：**当用户的问题涉及多个相关概念时，单独的 chunk 可能无法提供完整信息**。

**例子**：
- 用户问："model_3 电池的热管理策略是什么？"
- 向量召回返回了 `model_3_specs.md` 的 chunk，但缺少 `thermal_management_policy.md` 的内容
- 如果这两个文档共享了 "model_3"、"battery"、"thermal" 等实体，GraphRAG 可以通过共现关系把后者拉进来

### 6.2 为什么是 "共现图" 而不是 "知识图谱"？

| 方案 | 构建成本 | 查询复杂度 | 覆盖范围 | 选择？ |
|------|----------|-----------|---------|--------|
| 实体-关系-实体三元组提取 | 高（需要 NER + RE 模型） | 高（Cypher 查询） | 窄（只覆盖模型能识别的实体关系） | ❌ 太重 |
| **共现图** | 低（规则提取词 + 构建边） | 低（邻居查询） | 广（覆盖所有识别到的关键词） | ✅ **够用** |

**决策**：社区版 Microsoft GraphRAG 的三元组提取需要 LLM 调用，成本和延迟都太高。共现图零 LLM 调用，构建是确定性的（规则 + stopwords），适合本地 agent 的实时索引更新。

### 6.3 权重设计的考量

```python
graph_score = graph_weight * max(seed.score, 0.1) * min(1.0, shared_terms / 3)
```

- `graph_weight = 0.35`：图扩展的分数不应超过其他路径——它只在有强关联时作为补充
- `max(seed.score, 0.1)`：基于 seed 的置信度——seed 本身分数高，它的邻居才值得推荐
- `min(1.0, shared_terms / 3)`：3 个共享词以上视为满分——避免词数过多导致分数膨胀
- `max_neighbors = 4`：只扩展 4 个最强关联的邻居，控制候选项增长

### 6.4 默认关闭的原因

`graph_enabled = False`。为什么？

- 对于小规模知识库（< 50 chunks），图扩展收益极低——候选本身就少
- 对于低实体密度的文档（如纯叙事文本），共现图几乎没有有效边
- 增加了索引构建和查询的成本
- 这是一个**场景相关的优化**，不是普适的必需组件

---

## 7. Splitter 设计：为什么按文件类型差异化？

### 7.1 核心原则

> 不同结构的文档，切分策略应该不同。一刀切的 recursive character splitter 会破坏文档的结构信息。

### 7.2 策略选择

| 文件类型 | 策略 | 理由 |
|----------|------|------|
| **Markdown** | 先按 h1/h2/h3 分节，再按窗口切 | 标题层级是文档的结构骨架，跨 section 合并会导致上下文混合 |
| **JSON** | 解析 JSON leaf，按 parent object/array item 分组 | JSON 的结构是语义的自然边界——`posts[0]` 内的字段不应该和 `posts[1]` 合并 |
| **Code** | LangChain 语言感知 splitter | 函数/类定义不应该被从中切断 |
| **Plain Text / PDF** | RecursiveCharacterTextSplitter | 无结构信息时，按自然断句切分是最稳健的 |

### 7.3 JSON 切分的特殊处理

JSON 是知识库中的常见格式（FAQ、配置、参数表）。如果不特殊处理：

```
# 一刀切的结果（坏）：
chunk_1: {"posts": [{"title": "X", "content": "很长的正文...第一部分
chunk_2: 第二部分..."}]}
→ chunk_1 丢失了 JSON 上下文，chunk_2 没有 key 信息
```

当前实现的处理逻辑：
1. 短字段（title、id 等）合并到同一个 chunk，保持字段间的语义关联
2. 长字段（content）单独切分，但每个子 chunk 都带上同级短字段作为上下文前缀
3. 不跨 array item / object 合并——保持语义边界

这在 5 轮评测中得到了验证：JSON FAQ 文件的 citation 命中率始终保持 100%。

### 7.4 Markdown 标题路径的保留

每个 chunk 的 metadata 中保留了 `heading_path = ["h1", "h2", "h3"]`。这不是给检索用的——检索靠的是 embedding。这是给**引用展示**用的：

```
SOURCE path/to/policy.md
HEADINGS: # 安全规范 > ## 访问控制 > ### 权限分级
CHUNK ID: abc123-2-def456789012
EXCERPT: 管理员访问生产环境需经二级审批...
```

这让报告生成阶段的 LLM 能区分"这是安全规范第三章的内容"和"这是一条孤立的引用"，从而在综合报告时给出更精确的上下文描述。

---

## 8. 默认配置的收敛逻辑

在 `configuration.py` ~100 个配置项中，以下是 RAG 的默认选择及理由：

| 配置 | 默认值 | 为什么是默认 |
|------|--------|-------------|
| `embedding_provider` | `sentence_transformers` | 本地部署、多语言、CPU 可用 |
| `embedding_model` | `paraphrase-multilingual-MiniLM-L12-v2` | 多语言最佳平衡 |
| `vectorstore_provider` | `milvus` | 持久化 + 可升级 |
| `reranker_provider` | `cross_encoder` | 精度天花板突破必须品 |
| `reranker_model` | `BAAI/bge-reranker-base` | 多语言 rerank |
| `chunk_size` | 1200 | 平衡完整性和粒度 |
| `chunk_overlap` | 200 | 16% 重叠率 |
| `top_k` | 4 | 足够支撑 claim，不浪费 context |
| `rerank_top_n` | 20 | 5x top_k 的候选池 |
| `keyword_top_k` | 12 | 3x top_k 的关键词候选 |
| `rrf_rank_constant` | 60 | 领域常见稳健值 |
| `graph_enabled` | False | 场景相关，小库不必要 |
| `authority_rerank_enabled` | True | 安全默认——宁可多过滤不可放过误导 |

---

## 9. 评测驱动的设计迭代

### 9.1 评测是怎么影响设计决策的

| 评测发现 | 对应设计变更 |
|----------|-------------|
| 记录 1：hash embedding，cross_lingual hit@5=0.375 | → 必须用真实语义 embedding |
| 记录 2→3：切换真实模型后 hit@5=1.0 | → 当前模型选择验证通过 |
| 记录 3→4：加入误导内容，precision 从 0.71 → 0.53 | → Authority 系统的必要性被验证 |
| 记录 4→5：更多误导，hit@5 首次跌破 1.0（0.9487） | → 当前召回策略的极限暴露，需要进一步优化 |
| 记录 5 Miss 诊断：`atlas_citation_003` 正确来源排到第 6+ | → `rerank_top_n` 可能需要从 8 调高 |

### 9.2 尚未完成的消融实验（TODO）

以下实验可以进一步量化各组件的贡献：

1. **纯向量** vs **向量 + BM25** vs **向量 + BM25 + Reranker** vs **全链路**
2. **不同 chunk_size** 对 recall 的影响（400 → 800 → 1200 → 1600）
3. **不同 embedding 模型**在中英文混合场景下的对比
4. **有/无 Authority** 在误导场景下的 precision 差异
5. **有/无 GraphRAG** 在 multi_hop 场景下的 recall 差异

这些实验一旦完成，可以进一步收敛配置空间，让每个参数的默认值有更坚实的量化依据。

---

## 10. 已知局限与改进方向

| 局限 | 影响 | 改进方向 |
|------|------|----------|
| 没有中文分词 | BM25 对中文长词的召回可能不如英文 | 引入轻量中文分词（如 jieba）作为可选配置 |
| 内存 BM25 的规模上限 | 万级 chunk 以上遍历变慢 | 考虑 ES 作为大规模场景的默认 |
| 共现图对叙事文本无效 | 没有术语密度的文档无法建边 | 评估引入实体识别或用 LLM 提取 keyphrase |
| Memory 沉淀依赖人工审视 | auto-extract 可能记入错误事实 | 加确认机制，或在 citation 中标注来源类型为 memory |
| 跨 chunk 推理缺失 | multi-hop 问题需要读到多个 chunk 自行推理 | 当前依赖 agent 的推理能力，但可以考虑 chunk linking 或 summary chunk |
| Authority 自动推断覆盖不全 | 没有显式标记的过期内容不会降权 | 引入时间衰减（文档日期越旧权重越低） |

---

## 11. 总结

RAG 模块的设计哲学可以用一句话概括：

> **粗召回保证不漏，精排保证不错，Authority 保证不腐，Memory 让知识自进化。**

每个组件都有明确的场景驱动和量化依据（或至少明确了哪些需要进一步量化）。技术选型不是"我支持了什么"，而是"我在什么约束下、为了什么目标、放弃了什么"。
