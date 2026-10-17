# Research Graph 重复 URL 回执修复

日期：2026-10-17（Asia/Shanghai）。这是 Phase 0 后的独立缺陷修复，不包含 Phase 1。

**结论：相同研究 Run 内，同 URL、同内容版本的重复工具结果复用数据库中已存在的 Source ID；回执引用经过实际节点及溯源关系查询确认后，才允许 Micro Compact。**

## 基线与复现

开始时工作区干净，分支 `codex/openai-agents-phase0` 的 HEAD 是 `5535b8818ca3a17f39ea3f7e83042e6730ef138d`。按本任务指定，从 `2a29be4dd99e5de26c7f41197de097961012b403` 创建独立分支 `codex/fix-research-graph-duplicate-receipts`。原 Phase 0 分支及其后续真实 DeepSeek 验证提交保留，不回退或覆盖。

阅读了 [Phase 0 审计](openai-agents-phase0.md)，审查 `strategy.py`、`store.py`、`models.py`、`schema.py`、`compaction.py` 和 `runtime/sdk_context.py`。先增加领域层同批次/跨批次复现断言，并加强已有 SDK 重复 URL 用例，再修改实现。修复前这四个参数化用例全部失败，修复后全部通过。

触发条件：两次工具调用返回同 URL、同内容，但 tool call ID 不同。现有 Source ID 工厂包含调用身份，因此第二次会计算不同的临时 Source ID。例如复现中首次真实写入 `SRC_3b44d6fcf1f0daa3ce8a`，第二次计算 `SRC_c06c10b288e6f48f1d50`。

旧 Producer 只查询“是否已持久化”的布尔值，跳过第二次写入后，却把第二次临时 ID 放入 `source_ids` / `duplicate_source_ids`；其 `node_ids` 为空。旧路径可能用该回执压缩，SDK 防御检查则保留原文并记录 `compaction_deferred`。问题发生在领域层的去重与回执组装之间。

## 修改文件与职责

| 文件 | 修改职责 |
| --- | --- |
| `src/open_deep_research/research_graph/store.py` | 新增 `get_source_receipt(scope, document)`，从真实 Source 与溯源关系解析可验证回执；内存及 Neo4j 后端实现同一合同，旧 `source_is_persisted` 接口保留 |
| `src/open_deep_research/research_graph/strategy.py` | 以 URL/来源键和完整内容哈希区分版本；同批次重复先等待首次写入；新材料和重复材料均在压缩前解析真实引用，再按 tool call 合并回执 |
| `src/open_deep_research/research_graph/models.py` | 为 WriteReceipt 增加 `has_complete_node_references` 属性，检查引用完整性；没有增加序列化字段或修改 Graph Schema |
| `src/open_deep_research/research_graph/compaction.py` | 拒绝引用不完整的类型化 WriteReceipt，保留工具原文及调用关联；保留原有显式字符串回执兼容接口 |
| `tests/test_research_graph_context_harness.py` | 扩展现有领域、存储、溯源、失败、压缩及旧 AgentRuntime 回归测试，复用原测试基础设施 |
| `tests/test_sdk_context.py` | 加强已有同批次/跨 SDK invocation 重复 URL 测试，要求有效重复材料实际完成压缩，且所有引用已存在 |
| 本文档 | 记录根因、数据流、验证与剩余边界 |

`runtime/sdk_context.py` 与指定基线逐字节一致，保留 Phase 0 的最后一道回执防御检查。`schema.py`、`extractor.py`、Workflow、Web/API、预算、模型供应商配置、依赖声明及锁文件均未修改。

修改的六个 Python 文件通过完整格式检查。格式化包含既有长行换行；另以 AST 比较确认原函数的逻辑变化仅限 Producer 批次处理、两个存储后端的来源确认、内存版本缓存移除及 Micro Compact 校验，Consumer、滚动压缩、图物化、Neo4j 写入算法没有逻辑变更。

## 修复后的数据流

1. 工具原始结果在既有 SDK/旧 runtime 边界转换为 RawResearchDocument，Source ID 的生成规则不变。
2. Producer 按 `(URL 或来源键, content_hash)` 分类。数据库查询命中完整版本时，标记为重复；同批次的重复只登记待确认材料，不提前生成持久化回执。
3. 新内容继续使用原 GraphExtractor、Store.write_delta 和 ContextManager。相同 URL 的不同内容保留独立版本。内存后端从实际 Source 节点查询所有版本，修复只记住最后哈希的缓存语义，和 Neo4j 既有的所有版本查询一致；A→B→A 复用最初的 A。
4. 全部写入及 Working Context 更新成功后，再查询每个来源版本。只有真实存在的当前 Run Source，以及由现有溯源边连接的 Evidence / Claim / Finding，才进入工具回执。
5. 新材料的 `duplicate_source_ids` 为空；重复材料的该字段引用已存 Source，并且是 `source_ids` 的子集。所有命名的节点引用都包含于实际查询得到的 `node_ids`；`edge_ids` 同样来自已存在的作用域内关系。
6. 完整回执才发布到 workspace 并交给 Micro Compact。重复材料不重新抽取或写入，也能够正常压缩。工具消息的 `tool_call_id` 和 assistant 工具调用消息保持关联。

只有重复材料的批次不调用 GraphExtractor/ContextManager；它仍必须重新确认已存引用后才能压缩。查询异常、确认失败、写入失败，或 ContextManager 期间 Source 被删除，都会返回失败结果、不发布该批次回执并保留原文。写入成功但随后验证失败时，已写图数据不回滚；下一次处理可确认实际节点，不需要伪造成功。

### 作用域与溯源验证

来源匹配必须同时满足真实节点类型 `Source`、当前 `run_id`、相同 URL/来源键和完整 `content_hash`。同一研究 Run 内的角色共享证据是原有合同，复用不会改写原 Source 的 role/task 元数据；回执携带本次调用的 role、research_round、task_id。不同研究 Run 不允许复用。

Neo4j 使用一条参数化查询选出真实匹配 Source，再读取最多三跳的既有溯源节点和关系；路径中每个节点与关系都必须属于当前 Run。结果还经领域校验：Evidence `EXTRACTED_FROM` Source，Evidence `SUPPORTS` / `CONTRADICTS` / `CONTEXTUALIZES` Claim，Finding `SUPPORTED_BY` Evidence 或 `DERIVED_FROM` Claim。Source 缺失或哈希/Run 不匹配即不能生成回执。

查询使用已有 `ResearchNode` / `ResearchEdge`，没有新增 Schema 或数据库。语法参考 [Neo4j 可变长度路径](https://neo4j.com/docs/cypher-manual/current/patterns/reference/variable-length-paths/) 和 [映射表达式](https://neo4j.com/docs/cypher-manual/current/expressions/map-expressions/)，实际执行结果见下。

`has_complete_node_references` 检查结构完整性，本身不证明数据库存在性；存在性由 Store 的上述查询负责。没有把新计算的临时 ID 填入 `node_ids`，也没有绕开 SDK 校验。

## 验证结果

根 `.venv` 使用 Python 3.11.9、锁定 SDK `openai-agents==0.24.0`。本任务不改 SDK 或供应商适配，也不调用付费模型。

| 检查 | 指定基线 | 修复后 |
| --- | --- | --- |
| 根锁环境同步 | 成功 | 依赖声明与锁文件不变 |
| Bug 复现的四个参数化用例 | 4 失败 | 4 通过 |
| Research Graph + SDK 相关测试 | — | 71 通过，其中 Graph 29、SDK 42 |
| 原有 SDK Harness / Model 测试单独运行 | 42 项 | 42 通过；未减少测试数 |
| 全量离线测试 | 552 通过、6 deselected、154 warnings | 569 通过、6 deselected、154 warnings |
| 现有 Ruff E/F/I 门禁 | 通过 | 通过 |
| 六个修改 Python 文件格式检查 | — | 通过 |
| `compileall` | — | 通过 |
| 现有 Mypy 命令 | 35 错误 / 16 文件，检查 103 个源文件 | 相同 35 错误；归一化行号后诊断新增 0、移除 0 |
| `uv pip check` | 1 个已有依赖冲突 | 相同冲突：markitdown-mcp 要求 mcp~=1.8.0，已装 1.23.0 |
| Git 空白检查 / SDK 与范围外文件差异检查 | — | 通过 |

全量新增 17 个测试实例；未删除、弱化测试或改变已有 integration 排除规则。现有 Windows pyreadline3 在部分 pytest 退出后打印 Console 析构 WinError，基线也出现；这些 pytest 进程均返回 0。Mypy 与依赖检查仍返回非零，不将其报告为成功。

覆盖：首次写入、同/跨批次重复、A→B→A、同批次多个内容版本、不同 Run、同 Run 不同角色/任务、数据库查询异常、无法确认节点、ContextManager 期间节点丢失、写入失败、五种无效回执、有效重复压缩以及实际节点/边溯源。旧 `run_business_agent` / AgentRuntime 路径通过两轮重复工具调用回归；SDK 真实 Runner 循环通过加强后的重复用例及原有异常/取消/部分写入失败测试。

### 实际 Neo4j 验证

使用项目已有 Neo4j 连接运行独立合成数据验证，服务版本 `Neo4j/5.26.31`，模型调用数 **0**。测试使用随机独立 Run `duplicate-receipt-smoke-323f5c077b7e4ba2b2be828240e8ee8d`，写入前确认该 Run 不存在数据，没有创建 Schema。

实际 Store 写入同 URL 的两个版本 `SRC_smoke_a` / `SRC_smoke_b`。携带尚未写入的临时 ID 查询时，返回对应真实 Source；每个 Evidence / Claim / Finding 和边引用均通过数据库实际节点/关系查询确认；其他 Run 查不到该来源；写入 B 后仍能复用 A。清理只匹配该随机 Run 及预先列出的合成节点 ID，并验证剩余节点数 **0**。没有输出凭据或修改操作方配置。

### 可重复的离线命令

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_research_graph_context_harness.py tests/test_sdk_context.py tests/test_sdk_models.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m pytest -q tests/test_sdk_context.py tests/test_sdk_models.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m ruff format --check src/open_deep_research/research_graph/compaction.py src/open_deep_research/research_graph/models.py src/open_deep_research/research_graph/store.py src/open_deep_research/research_graph/strategy.py tests/test_research_graph_context_harness.py tests/test_sdk_context.py
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
uv pip check --python .venv/Scripts/python.exe
git diff --check
```

基线、修复前复现、修复后测试、静态检查和实际数据库验证日志留在忽略的 `.tmp/duplicate-url-receipts/`；不提交本地输出、运行数据或配置。

## 剩余边界与提交

- 确认是压缩前的查询时点证明，不是跨整个工作流的分布式事务；之后由其他进程删除节点仍可能使引用失效。本次没有引入事务引擎或修改删除策略。
- 不同进程并发首次写入相同版本时，现有 Schema 仍可能存下多个真实 Source。本修复确定性选取实际匹配节点，不新增唯一约束，也不批量清理历史重复节点。
- Neo4j 溯源查询限制三跳，但没有新增结果数上限；大型高连接图的性能未作压力验收。
- 外部自定义 ResearchGraphStore 需要实现新增的 `get_source_receipt` 能力；未实现时默认明确失败并保留原文，不退回不可信布尔去重。

独立提交信息：`fix: reuse persisted Research Graph source receipts`。本分支父提交为指定的 `2a29be4d`；不自动推送或合并 main，不迁移业务 Agent。
