---
name: rag-evaluation
description: >-
  用于 POR 中可能影响 RAG 检索质量、证据完整性、排序、模型调用成本或延迟的开发任务，
  包括文档处理、索引、Query Rewrite 及相关模型、配置和依赖。修改前校验 dataset-v1 并复用或建立基线，
  修改后在同等条件下评测、比较和记录。
---

# POR RAG 评测

## 判断是否触发

按实际行为影响判断。文档加载、解析、OCR/Vision、Chunking、Embedding、向量索引与检索、BM25 与分词、RRF、Metadata Boost 与过滤、Reranker、Authority、Query Rewrite、Evidence/Citation、上下文截断、Top K 与候选数、RAG 工具入口均属于评测范围。影响这些行为的依赖、模型和配置也适用。

普通文档、日志、格式、无关测试以及与 RAG 无关的 API、CLI、Workflow 或 DBOS 改动通常跳过。Internal Knowledge Agent 的查询规划、问题拆解和自主检索策略属于 Agent 层，现有 Pipeline/Tool 评测只覆盖同时改动的 RAG 能力。在 PR 中明确 Agent 层变化的覆盖缺口。

## 选择范围和题集

| 影响的行为 | 评测范围 |
| --- | --- |
| Loader、OCR/Vision、Chunking、Embedding | `pipeline` |
| 向量检索、BM25、RRF、Metadata Boost、元数据过滤 | `pipeline` |
| Reranker、Authority、Top K、候选数 | `pipeline` |
| Evidence、Citation、上下文截断 | `pipeline`，工具层也改变可见证据时补 `tool` |
| Query Rewrite、RAG 工具入口 | `tool` |

选择能覆盖本轮改动的最小范围。`pipeline` 直接评测文档处理、检索与引用。`tool` 通过生产 `rag_search` 使用的 `search_rag_context` 验证按配置执行的 Query Rewrite 和工具查询行为。需要两种范围时，为每种范围各取一组前后记录。

正式技术版本前后均使用完整 `test`（120 题）。`dev`（30 题）用于开发、调参和初步验证，禁止用 `test` 失败案例反复调参。Smoke、子样本和替身模型限于调试。比较使用同一题集，通常只在改造开始和最终完成时执行正式评测。

## 改造前

在修改相关源码、配置或依赖之前，明确改造目标、最小覆盖范围和准确的改造前版本。相关改动已存在时，从明确的改造前提交或版本建立评测条件，必要时新建 Worktree。保留并行任务的文件与暂存状态，禁止用 `reset`、`stash` 或覆盖文件取得基线。

从待测 Worktree 的仓库根目录使用项目锁文件与当前版本的 `tests/evaluate_rag_retrieval.py`。下列命令使用 `uv run --locked`，也可使用该 Worktree 中已按根锁文件安装的 `.venv` 解释器。CLI 选项以当前代码和 `--help` 为准。 [现有评测说明](../../../docs/evaluation/rag-retrieval-test-records.md)

1. 运行冻结数据集检查。命令输出 JSON，确认 `sha256` 等于 `FROZEN_DATASET_SHA256` 后才继续。退出码 0 只表示摘要计算成功。 [冻结摘要定义](../../../tests/rag_eval_records.py)

   ```powershell
   uv run --locked python tests/evaluate_rag_retrieval.py dataset-fingerprint
   ```

2. 为待测版本计算评测指纹并查历史记录。下面是 `test/pipeline` 示例，替换为选定的范围和题集。每一阶段的 `fingerprint`、`find`、`run` 使用同一组有效配置，包括所需的 `--config`、可重复的 `--set rag_FIELD=YAML` 和 `--top-k`。

   ```powershell
   uv run --locked python tests/evaluate_rag_retrieval.py fingerprint --split test --scope pipeline
   uv run --locked python tests/evaluate_rag_retrieval.py find --split test --scope pipeline
   ```

   `find` 返回 `run_id` 和 `path` 时复用该记录。JSON `null` 且退出码 1 表示本次搜索无匹配，其他错误按实际信息处理。先查当前 Worktree 的默认记录目录，再查 `origin/main` 及任务相关且明确可获取的记录。`find` 只检查一个目录。从**待测版本**运行它，可通过 `--runs-dir` 读取其他 Worktree 的记录目录，也可把 Git 引用里的原始 JSON 提取到独立临时目录再查。当前目录无匹配只说明该目录未找到记录。跨目录同样以评测器的完整性和指纹校验为准。

3. 未找到有效记录时执行改造前评测，保存真实 `run_id`、记录路径和最终指纹。确认运行成功、题集完整，且记录通过现有校验后再开始改造。模型首次下载可能补全修订并改变最终指纹，必要时以相同配置重新计算并用 `find` 核对已保存记录。

   ```powershell
   uv run --locked python tests/evaluate_rag_retrieval.py run --split test --scope pipeline --title "模块名称 · 改造前"
   ```

## 实施改造

尽量只改变一个主要技术模块。保持冻结资料、题集及实际索引语料一致，固定无关模型、配置、运行条件和评分口径。用户要求同时改动多个模块时，记录这些改动的组合效果及各项收益的归因限制。确保记录可定位到实际待测的技术版本。若运行时技术改动尚未提交，在 PR 中注明当时的 `HEAD`、待测变更和对应技术提交，供复现。

## 改造后

1. 完成功能验证后，再走一遍数据集校验、`fingerprint`、跨记录目录的 `find` 和按需 `run`。使用与基线一致的题集、范围、语料、评分规则、K 和计时条件，替换运行标题。指纹未变化时检查目标行为是否确实一致。行为有变化但指纹未覆盖，或已知模型服务修订或环境变化没有体现在指纹中时，记录原因及其对基线可比性的影响。必要时用 `run --force` 强制复测受影响的版本。
2. 用现有 `compare` 比较完整记录。命令接受两个 run ID，也接受记录 JSON 路径。下面的变量取自前后检查获得的真实 ID。

   ```powershell
   uv run --locked python tests/evaluate_rag_retrieval.py compare $beforeRunId $afterRunId
   ```

   `compare` 要求同一数据、题集、范围、K 与评分口径。如果 K 本身是实验变量，分别保存各自实际 K 的运行，报告比较限制。固定 K 的对照只支持固定 K 下的结论。
3. 保留评测器生成的 JSON。将随本轮 PR 交付的有效记录原样放入默认记录目录后，运行 `history` 更新总表，并把实验信息与结论写入 PR 和交付报告。

   ```powershell
   uv run --locked python tests/evaluate_rag_retrieval.py history
   ```

## 记录与异常

- `docs/evaluation/runs/<run_id>.json` 是权威记录，保留数据集、指纹、实际配置与 Git 版本。`history` 从默认目录中的完整 JSON 更新 `docs/evaluation/rag-retrieval-test-records.md`。合并并行实验时，保留各自有效 JSON 并重新生成总表。遇到同 ID 文件先核对内容，禁止覆盖。复用现有文件记录机制。
- 默认记录目录被 Git 忽略。随对应技术 PR 交付的正式 JSON 经检查后，逐条显式暂存，例如 `git add -f -- "docs/evaluation/runs/$beforeRunId.json"`。提交所需的正式记录和生成的总表，排除派生索引、缓存和凭据。保留失败、中断及部分运行的诊断信息，正式基线和正式对比仅使用完整成功记录。
- 冻结摘要失配、模型无法加载、OCR/Vision 不可用、凭据缺失、模型服务异常、评测不完整或评分不可比时，报告发生阶段、具体原因和已有记录。改造前无法取得有效基线时，须经用户明确允许跳过后才继续相关改动。改造后评测若失败，保留代码与失败信息，标记评测未完成、收益待验证。保持正式评测使用真实且等价的条件。
- 如实报告下降，按题型和案例分析影响，说明检索质量、性能和资源成本的取舍。由用户决定是否采纳或回滚其明确要求的改动。保留冻结数据、评分规则和失败记录，以完整结果判断整体收益。

## PR 与交付报告

- 实验信息包括改造模块、前后主要差异、数据集版本、范围、题集、K、前后 `run_id` 与指纹、基线是否复用、记录和对应 Git 版本。若无法完成其中一步，记录原因和状态。
- 根据 `compare` 和两份运行记录填写「指标｜改造前｜改造后｜变化」。证据指标采用最终可见证据对应的 `delivered` 视图，字段名称和单位以当前评测器为准。

  | 指标 | 改造前 | 改造后 | 变化 |
  | --- | --- | --- | --- |
  | Evidence Hit Rate@K | 读取实测 | 读取实测 | 读取差值 |
  | Evidence Recall@K | 读取实测 | 读取实测 | 读取差值 |
  | Evidence Precision@K | 读取实测 | 读取实测 | 读取差值 |
  | Evidence MRR@K | 读取实测 | 读取实测 | 读取差值 |
  | Evidence NDCG@K | 读取实测 | 读取实测 | 读取差值 |
  | Complete Evidence Rate@K | 读取实测 | 读取实测 | 读取差值 |
  | Excluded Source Rate@K | 读取实测 | 读取实测 | 读取差值 |
  | 平均检索延迟 | 读取实测 | 读取实测 | 读取差值 |
  | P95 检索延迟 | 读取实测 | 读取实测 | 读取差值 |
  | 索引构建耗时 | 读取实测 | 读取实测 | 读取差值 |

  补充可观测的模型调用量、Token 用量、资源成本，以及按题型和难度的变化。缺失值标记为未采集。
- 实验结论说明改善项、下降项、典型新增或修复的失败案例、质量和成本的取舍，以及是否建议保留改动。
