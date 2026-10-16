# OpenAI Agents SDK 迁移前仓库清理与验证

日期：2026-10-16（Asia/Shanghai）。仓库：`kkaaakk/public_opinion_research`。
工作目录始终为 `C:\codex_workspace\Public Opinion Research`。

**最新状态：用户随后要求释放 DeerFlow 占用并明确删除其分支、标签，已执行，见第8节。**
第1–7节记录前一轮准备交付时的事实；其中当时保留的 DeerFlow 环境与引用现已退役。

本次从指定备份恢复原舆情项目，完成 Git 清理、全仓审计、安全代码整理、历史资料归档和迁移边界准备。**未接入 OpenAI Agents SDK，未重写 Agent Runtime，未替换 LangGraph 工作流，未修改或合并到 main。**

## 1. Git 起点、本地成果与恢复

操作前实际执行 `git status`、`branch -vv`、`worktree list --porcelain`、
`fetch origin --prune`、`ls-remote --heads/--tags`、独有提交比较，读取 GitHub
全部分支、Open PR、rulesets、environments、deployments、webhooks 和 Pages 状态。
使用新获取的远程结果，不沿用此前扫描结论。

| 对象 | 操作时核实的值 / 处理 |
| --- | --- |
| 原分支 / HEAD | `codex/deerflow-phase0` / `8e4291434cf14a0f83309387d89aaa9ef0b5929d` |
| 本地 main | `c54323bac38a2d437f86705f39df33a1b109256c`，落后远程 6 个提交，没有本地独有提交；保留原样 |
| 最新 origin/main | `129d3cef23bcbe9860e7c1cb649da5b3155632d3`；最终再次读取并核对未变化 |
| 备份分支 | `origin/backup/pre-deerflow` 精确指向 `41cbc134f748750ffbfc7c7dde65c7552ace6296` |
| 备份标签 | `v1-pre-deerflow^{}` 同样指向 `41cbc134…`；`phase0-deerflow^{}` 指向 `8e429143…`，均未移动/删除 |
| 新分支 | `git switch -c codex/openai-agents-migration 41cbc134f748750ffbfc7c7dde65c7552ace6296`；切换后先核实 HEAD 与干净状态 |
| 原未提交内容 | 9 个已修改文件、4 个未跟踪文档；`stash push --include-untracked` 全部保存，共 13 个文件 |
| 持久保护 | `refs/safety/pre-openai-preparation-20261016` 指向保存的 stash commit；原有更早 stash 也保留 |
| 本地配置/开发产物 | `.env`、`config.yaml`、`extensions_config.json`、`frontend/.env`、原 `.env` 保存件记录 SHA-256 并在结束复核；忽略目录保留原地 |

没有创建 Worktree、第二项目目录或重新克隆。已有其他 Worktree 和独立备份没有移动、删除或改写；没有执行 `git clean`。
切换前将当时忽略的本地路径追加到 `.git/info/exclude`，防止旧分支较短的 `.gitignore`
使 DeerFlow 的本地配置、venv/node_modules 等误入新分支；原 exclude 备份在
`.tmp/openai-preparation/info-exclude-before.txt`。因此磁盘上仍存在的 `backend/`、
`frontend/` 等本地残留不代表本分支包含 DeerFlow 源码。源码统计以 Git 路径为准。

原本机 `.env` 当时保存于 `.tmp/deerflow-phase0/pre-deerflow.env`；后续删除DeerFlow时
已另存 `.tmp/local-config-archive/pre-deerflow.env` 并恢复到根 `.env`，见第8节。
独立备份继续位于 `C:\codex_workspace\Public-Opinion-Research-pre-deerflow-backup`；
当前源码和启动不依赖该绝对路径。

如需恢复 Phase 0 未保存成果，应先保存当时新工作区修改，再在原目录切回
旧Phase0快照，执行以下命令重建一个本地分支。原分支/标签后续已删除；
`apply` 不删除安全引用，不应在本迁移分支直接应用。

```powershell
git switch -c codex/deerflow-phase0 'refs/safety/pre-openai-preparation-20261016^1'
git stash apply refs/safety/pre-openai-preparation-20261016
```

## 2. 分支与 PR 清理结果

最新 origin 最初共有 20 个远程分支。15 个候选远程 tip 均为最新 main 的祖先，
`git rev-list --count origin/main..origin/<branch>` 均为 0。检查发现所有分支未受保护、
rulesets 为空，没有 GitHub environment/deployment/webhook，Pages 返回未配置的 404。
main 的 Actions 仅以 main 为 push 触发目标，未发现 dev 部署引用；其他 `dev` 命中是
依赖 extra 名称。Git 中没有 replace/notes 特殊引用。仓库外未登记的部署不在可验证范围内。

### 2.1 已实际删除的远程分支（14）

- `codex/agent-observer-v0.2-integration`
- `codex/ci-test-validation-hardening`
- `codex/langgraph-studio-observer-compat`
- `codex/langsmith-tracing-integration`
- `codex/observer-instrumentation-lifecycle-hardening`
- `codex/p0-p2-safe-code-cleanup`
- `codex/phase0-skill-review-baseline`
- `codex/public-opinion-dynamic-loop`
- `codex/remove-agent-observer`
- `codex/research-graph-context-harness`
- `codex/retire-legacy-package`
- `codex/separate-query-image-prompt`
- `codex/simplify-query-image-vision`
- `dev`

远程已合并 tip 与本地同名 tip 分别检查。本地有独有提交不影响已合并远程 tip 的数据安全，
对应本地分支保留，没有强制用远程覆盖。删除后实际执行 `git fetch origin --prune`。
首次 fetch 还清除了 35 个 GitHub 上已经不存在的陈旧远程跟踪引用；这不是本次新删的远程分支数。

### 2.2 已实际删除的本地分支（9）

- `codex/agent-observer-v0.2-integration`
- `codex/langsmith-tracing-integration`
- `codex/observer-instrumentation-lifecycle-hardening`
- `codex/public-opinion-dynamic-loop`
- `codex/remove-agent-observer`
- `codex/research-graph-context-harness`
- `codex/separate-query-image-prompt`
- `codex/simplify-query-image-vision`
- `dev`

这些本地 tip 均没有相对最新 origin/main 的独有提交，均非当前分支，均未被 Worktree 占用。
`codex/phase0-skill-review-baseline` 原本没有本地分支。

### 2.3 保留与未删除原因

最终 origin 保留 **7 条**：`main`、`backup/pre-deerflow`、`codex/deerflow-phase0`、
`codex/dsh-trajectory-ui`、`codex/rag-config-consolidation`、
`codex/openai-agents-migration`，以及额外保留的 `codex/workflow-utils-architecture-consolidation`。
前六条遵从明确保留名单；最后一条有仍存在的 Worktree 和本地独有提交，不在本次拆除其活动状态。

| 保留的本地候选 | 相对最新 main 的独有提交数 | 原因 |
| --- | ---: | --- |
| `codex/ci-test-validation-hardening` | 4 | 未推送本地历史，保留；远程已合并 tip 可删除 |
| `codex/langgraph-studio-observer-compat` | 1 | 同上 |
| `codex/p0-p2-safe-code-cleanup` | 1 | 同上 |
| `codex/retire-legacy-package` | 5 | 同上；原有该分支 stash 也保留 |
| `codex/workflow-utils-architecture-consolidation` | 1 | 已有 Worktree 占用；本地及远程均保留 |

其他已有本地 `codex/unified-model-factory-deepseek-structured` 不在删除候选中，且被 Worktree 占用，保留。
RAG/DSH Worktree 的本地 tip 与 PR head 不同，没有重置它们。
`open-dr` 远程及其 main 引用不属于此次 origin 清理范围，保留。

| PR | 结果 | 保留与复用决策 |
| --- | --- | --- |
| [#12 RAG 配置整理](https://github.com/kkaaakk/public_opinion_research/pull/12) | 继续 OPEN，未合并 | head `c65eeaf2300200845cf9b1221e02d75ec12152f7`；嵌套 RAGConfig、63 个外部字段兼容及测试具有复用价值，正式迁移评估后选择性迁入 |
| [#14 DSH 轨迹观测](https://github.com/kkaaakk/public_opinion_research/pull/14) | 继续 OPEN，未合并 | head `41e3ef16d0626c5a0b1ad5cd7fd510c1c5632769`；已审阅 lifecycle、JSONL ledger、lease/replay、脱敏、projection 与 Web adapter；回放不等于可恢复执行 |
| [#17 DeerFlow Phase 0](https://github.com/kkaaakk/public_opinion_research/pull/17) | 已注明新架构取代并关闭，mergedAt=null | 保留源码分支、标签及本地未保存成果；不将 DeerFlow 合入 main |

## 3. 全仓统计与审计方法

完整读取备份的 306 个 Git blob，对 158 个 Python 文件做 AST 清单、导入关系、兼容导出、
大模块及运行时耦合分析；重点链路另逐函数核对。不是只审计近期 diff。
可复现工具为 [`scripts/audit_repository.py`](../../scripts/audit_repository.py)，
逐文件路径、原始/换行归一化 SHA、全部目录规模、大文件阈值清单、历史分类、
静态候选及依赖环见 [机器清单](pre-openai-sdk-inventory.json)。

“全部文件”指全部版本控制项目文件，不包含 `.git`、忽略/未跟踪的密钥、数据库、
依赖安装和历史本机缓存。生产代码为 `src/**`，测试代码为 `tests/**`，其余 Python
为 support（包含 evaluation/scripts/tools/examples/locust）。行数为物理行，包含注释、
空行、docstring；机器清单同时给出去空行/纯注释数。不能把它误称为可执行语句数。
工作清单的新增文件必须先 stage。清单自身 bytes/hash 是写入前快照，不能作递归不变式。

<!-- inventory-summary:start -->
| 指标 | 清理前 | 清理后 |
| --- | ---: | ---: |
| 全部项目文件 | 306 | 316 |
| Python 文件 | 158 | 162 |
| 生产 Python 物理行 | 26,509 | 26,482 |
| 测试 Python 物理行 | 12,605 | 13,016 |
| 其他 Python 物理行 | 5,837 | 6,233 |
| 全部 Python 物理行 | 44,951 | 45,731 |
| 兼容导出文件 | 7 | 7 |
| 历史/生成资料分类项 | 57 | 61 |
| 无静态 importer 的源码候选 | 4 | 4 |
| Python 文件 >400 行 | 41 | 41 |
| Python 文件 >600 行 | 19 | 19 |
| Python 文件 >1000 行 | 6 | 6 |

新增文件主要为测试、审计工具和交付文档；源码保留完整，生产行数减少27。
历史分类增量包含归档说明等新记录，不表示新增了需要删除的垃圾。

| 目录（递归） | 文件数 前→后 | Python 行 前→后 |
| --- | ---: | ---: |
| `src` | 103→104 | 26,509→26,482 |
| `tests` | 43→45 | 12,605→13,016 |
| `evaluation` | 53→54 | 4,942→4,942 |
| `docs` | 60→66 | 0→0 |
| `data` | 12→12 | 0→0 |
| `examples` | 5→5 | 34→34 |
| `scripts` | 2→3 | 325→721 |
| `tools` | 1→1 | 217→217 |
| `.github` | 4→4 | 0→0 |
| `.trae` | 6→6 | 0→0 |
| `src/…/rag` | 31→31 | 7,543→7,543 |
| `src/…/research_graph` | 13→13 | 4,218→4,218 |
| `src/…/memory` | 6→6 | 1,006→1,006 |
| `src/…/workflow` | 7→7 | 1,995→1,995 |
| `src/…/runtime` | 3→3 | 953→953 |
| `src/…/web` | 6→6 | 1,096→1,096 |

全部子目录的bytes/文本行数见机器清单；目录表含父子层级，不能逐行相加。

| 超过1000行的 Python 文件 | 前→后 |
| --- | ---: |
| `tests/test_rag.py` | 2097→2097 |
| `src/open_deep_research/configuration.py` | 1833→1833 |
| `src/open_deep_research/social_media_mcp.py` | 1492→1453 |
| `src/open_deep_research/rag/loaders/knowledge.py` | 1101→1101 |
| `tests/test_token_accounting.py` | 1093→1093 |
| `src/open_deep_research/social_media_apify_api.py` | 1046→1007 |

4个无静态importer候选是`rag/memory_writer.py`、`rag/tooling.py`、`utils.py`、`security/auth.py`；动态入口/外部兼容分析后均保留。明确失效的2个机器绝对路径CMD已归档；3个历史在线成本runner及A/B旧路径runner保留为复现资料，不列作当前可运行服务。
<!-- inventory-summary:end -->

历史产物候选基线 57 项是保留审查清单，**不是 57 个垃圾文件**：9 个历史设计记录、
3 个评测 Baseline、28 个评测输出、1 个轨迹截图、16 个学习/面试产物。
兼容导出基线为 7 个：`deep_researcher.py`、`utils.py`、
`rag/{memory,memory_writer,mysql_memory,query_images,tooling}.py`。

## 4. 实际执行的安全整理

### 4.1 社交媒体纯业务 helper

- `social_media/domain.py` 统一 critical/high/medium 顺序的风险阈值选择；MCP 自定义规则、
  Apify 默认规则、各自 reasons/响应封装仍归适配器。
- 新 `social_media/records.py` 统一可选 ISO 日期与时间解析，保留原有时区墙钟日期语义。
  MCP 的 `published_at`→`created_at` fallback 保留在 MCP，Apify 不扩大字段合同。
- `social_media_mcp.py`、`social_media_apify_api.py` 删除 6 个已等价共享函数的
  同签名转发实现，用同名 import alias 保留现有名字；不改 MCP 工具 schema/operation。
- 4 个生产文件合计 **2647→2620 行，净减 27 行**。先新增跨适配器 characterization
  测试，修改前后社交套件均 **58 passed**；新增 49 个测试用例。

没有合并那些只是函数同名的不同业务：MCP/Apify cursor 接受规则不同，身份去重分别
优先 id/url，sentiment 别名及推断、metadata/tags、信号文本、engagement 输入类型亦不同。
MCP→dict、LangChain tool→JSON 文本和 HTTP→Apify actor 三条边界都有实际消费者，保留。
两个独立审计者复核了源码 diff 和这些差异，未发现行为回归。

### 4.2 历史资料归档与删除

| 原路径 | 当前归档位置 |
| --- | --- |
| `trajectory-live.png` | `docs/archive/observability/trajectory-live.png` |
| `docs/project-learning/project-learning.pptx` | `docs/project-learning/archive/project-learning-original.pptx` |
| `evaluation/cost_profile/{af_final,after_final,ik_report,ps_report,r2_final}.txt` | `evaluation/cost_profile/archive/reports/`，原文件名保留 |
| `evaluation/cost_profile/{after_v4flash_launch,round2_launch}.cmd` | `evaluation/cost_profile/archive/launchers/`，原文件名保留 |

9 个归档文件逐个与备份核对：二进制精确 SHA-256 一致，文本换行归一化后一致。
原机器绝对路径 CMD 不再作为当前启动方式宣传，保留作历史复现证据；没有把它们
改写为看似可用却缺旧 instrumentation 的新 launcher。新增 archive/评测目录说明，
更新 Round2 报告和学习目录引用。

唯一删除的实质内容是 `evaluation/cost_profile/wmi_test.txt`：8 字节，仅含 `hello`，
全仓无消费者，不是 Golden、Baseline 或可复现脚本。

保留所有成本 JSON、A/B smoke 诊断与报告：它们分别记录不同模型、超时、失败/完整
阶段，已有文档引用，不能只按文件名认作冗余。保留 `tests/rag_eval_cases*.jsonl`、
`evaluation/plan_execute_ab/{cases.jsonl,rubric.json}`、固定单案例及
`tests/expt_results/*.jsonl` 历史 Baseline。学习修订版 PPT/PDF/SVG、面试资料和 AST 图
具有独立用途，未删除；重复的四组 examples Markdown 有仓库样例与 VitePress 路由
两个消费入口，也保留。`.trae/specs` 是设计/未合并分支证据，未按未勾选 checklist 删除。

### 4.3 依赖、工具和文档

- `pytest` 从直接 runtime 依赖归入 `dev`；`ipykernel` 归入可选 `notebooks`。
  `uv.lock` 同步相应 metadata，**306 个锁定 package 的名称/版本集合完全不变**。
  当前 dev 安装可省去 23 个 notebook 相关包；其他依赖仍可能间接引入测试工具。
- CI 测试安装改用 `--extra dev --locked`，新增本迁移分支的 push 触发，保留原 Ruff
  规则、离线测试选择和全部断言。没有增加 SDK 依赖。
- 审计脚本只调用只读 Git/AST，不 import 应用、不遍历忽略目录；新增 7 项离线测试
  覆盖 tracked/ref 快照、LOC、入口候选、相对导入/循环、Golden 分类及 Windows 换行。
- 根 `package.json` 删除无消费者的 `dev: next dev`：manifest/lock均无Next依赖，
  全仓唯一引用就是该声明；保留实际VitePress的 `docs:dev/build/preview`，锁无需改动。
- README 修正仓库 clone 地址、锁定安装与开发/notebook extras，说明当前迁移状态。
  AGENTS 改为恢复后的真实结构与职责规则；`CLAUDE.md` 未编辑。
- 旧 Agent Server 重复能力审计保留原证据，顶部标明目标已被 SDK 方向取代。
  `docs/architecture/tools.md` 修正当前动态工具提示入口；正式迁移计划有 SDK 固定版本
  来源、A–E 能力矩阵、接口/目录建议、F01–F24 转换和未来验收。

## 5. 全仓职责发现与待办

| 优先级 | 范围 / 发现 | 本次决策与后续验收 |
| --- | --- | --- |
| P0，启用多人认证时 | `security/auth.py` 的 create_run owner 过滤；可信 user 映射与业务库 scope | 保留旧审计条件性证据；正式迁移必须在 API/Run/工具/数据边界做双用户拒绝测试，不把 SDK guardrail 当身份授权 |
| P0，启用人审时 | `workflow/intake.py` 规划与 interrupt 同节点，恢复可能重新规划及漏计预算 | 本次不改图；未来审批持久化并绑定 plan/version/owner，一次消费决策，展示计划必须等于执行计划 |
| P1 | `runtime/agent_runtime.py` 混合 loop、工具执行、预算、私有记忆、Workspace 与压缩；`business_agent.py` 混合角色装配和图状态协议 | 与 SDK 强耦合，只记录；复用领域策略与预算/证据不变量，替换循环和协议适配 |
| P1 | `web/server.py` 同时做参数/令牌、Semaphore、图执行、SSE与归档；`web/history.py` 管 Neo4j 档案与 LangSmith 导入 | 后续分开 Run 权威、订阅、archive投影；测试进程崩溃、取消、断线、归档补偿；Session/Trace不能代替业务历史 |
| P1 | 预算、最终报告预留和并行取证分配 | 保留 `budget.py` 与报告策略；SDK max_turns 不替代 token/搜索预算；验证重试与重复消费不漏计/重计 |
| P1 | Research Graph 入库回执后压缩、作用域与证据链 | 保留 store/context/compaction；SDK通用摘要不得丢 protected findings/conflicts/source refs |
| P1 | 依赖/类型基线 | 35 个既有 Mypy 错误、1 个已知 MCP override 不兼容；如实保留，正式接入前独立处理 SDK OpenAI/Pydantic 版本冲突 |
| P1 | 外部服务/生产恢复未验证 | MySQL/Neo4j/Milvus/ES、远程MCP连接、真实provider、worker重启与审批恢复均需正式验收，离线测试不替代它们 |
| P2 | `social_media_mcp.py` 混合协议、本地记录、查询/聚合；Apify模块混合HTTP、actor、归一化/cache | 本次仅抽共享纯helper；后续按协议/记录服务边界拆分，保持cursor/id/metadata/存储差异 |
| P2 | `configuration.py` 1833行、RAG多视图/flat兼容、重复Web extra基础约束 | PR #12先做兼容评估；不要机械替换63个字段或把完整schema拆成大量空包；Web extra的standard不同于基础依赖 |
| P2 | `rag/loaders/knowledge.py` 多格式/OCR/Vision，`splitter.py` 多类型分块 | 保留稳定RAG；将来仅在provider seam整理，保留用户图/知识图不同prompt与合同 |
| P2 | `tools/registry.py` 改全局tool.name/metadata，缓存RAG tool；`models/factory.py`依赖BaseChatModel及provider特例 | 后续每Run绑定授权集合/配置，迁移ModelProvider时保留Ark/DeepSeek错误、结构化输出、Vision/usage合同 |
| P2 | 兼容与静态“死代码”候选 | 7个兼容层均有承诺或入口/测试/外部消费者不明；不删除。`rag/memory_writer.py`与`rag/tooling.py`无静态import，但仍需对外弃用窗口 |
| P2 | 历史评测runner含旧机器路径/已退役观测依赖 | 保留脚本及固定commit证据，明确历史环境；不可宣称这些旧runner已能评测当前Runtime |

`research_graph/store.py` 的 ABC、内存/Neo4j后端共享同一领域合同，具有合理内聚性。
RAG Graph 服务知识库检索；Research Graph 服务当次研究证据与冲突，不能合并。
长期记忆、角色私有记忆、SDK未来Session和档案也不同。四角色定义/研究规划、取证、
证据复核、报告、Vision、MCP、安全、历史和可观测性均保留。

AST 找到两组静态环：`memory/store ↔ rag/loaders/mysql_memory`、
`rag/indexer ↔ rag/service`。人工核对分别是函数内延迟兼容import及 TYPE_CHECKING
反向import，不是已证实的启动循环。无静态consumer仅是候选，`security/auth.py`由
langgraph.json动态配置、工具由registry注册、CLI由entry point调用，不能删。

旧审计的24项工程问题逐项转换见[迁移计划](../architecture/openai-agents-sdk-migration-plan.md)。
SDK核实基线为官方 v0.24.0 / `17d994bd3045dae71acda676a4ef4762ed8ecd94`；
既不假定SDK自带完整生产Harness，也不忽略当前可组合Sandbox/Skills/Compaction能力。

## 6. 测试基线、回归与限制

| 检查 | 实际结果 |
| --- | --- |
| 恢复后原树，系统Python3.11.9 | **454 passed，6 deselected**；该系统环境不等于锁环境 |
| 原锁Python3.11安装 | `uv sync --extra dev --locked --python 3.11`成功；Windows沙箱build临时ACL错误在项目缓存/正常权限重试恢复，未改版本 |
| 社交新契约，生产改动前/后 | 原锁环境均 **58 passed** |
| 锁环境中间轮次 | 499 passed、1 failed、6 deselected：新inventory记录历史回归守卫文件名触发现有移除守卫；补准确历史说明后原守卫通过，不改测试/隐藏符号 |
| 最终原锁全量离线回归 | **510 passed，6 deselected**；socket guard启用，47.89秒，153项既有弃用警告 |
| Ruff | 原CI `E,F,I`规则基线/最终均通过；新审计工具及测试也通过；没有降低规则 |
| 格式 | 4个新Python文件通过 `ruff format --check`；既有大型适配器有原先风格差异，未为清理做全文件机械重排 |
| Mypy | 基线/最终均 **35 errors in 16 files**；最终检查101个生产文件，新增共享模块无新错误 |
| Compile | `src/open_deep_research`、`src/security`、`scripts`、`evaluation`全部编译通过 |
| 入口import smoke | 17个入口在禁dotenv/凭据/网络条件下导入，含graph/runtime/RAG/MCP/API/memory/auth/观测 |
| 真实本机协议smoke | 临时loopback HTTP服务 `/health`=200、未知route=404；真实MCP stdio initialize/list_tools成功列出18工具；随后关闭进程/服务，未执行采集或付费工具 |
| 评测/模型脚本入口 | `run_benchmark.py --help`与`model_provider_smoke.py --help`成功；未运行真实研究/模型 |
| 业务/基础集成合同 | 全量包含四角色/权限/循环/报告、RAG/GraphRAG/ResearchGraph、budget/token、图片、memory、MCP注册、HTTP适配/Apify归一化、API验证与history mocks |
| 依赖一致性 | 新旧锁package版本集合一致；`uv pip check`仍报原有 `markitdown-mcp`要求`mcp~=1.8.0`而安装`1.23.0`，未伪报通过 |

项目原有pytest默认排除6个标记为 integration 的真实模型/基础设施测试，本次未新增跳过、
移除测试或弱化断言。模型、Apify、搜索付费API均未调用。真实MySQL/Neo4j/Milvus/ES、
远程MCP连接、Vision/provider、LangSmith上传以及生产队列/断线/重启恢复未做实测。
现有跨模块 fake/ASGI 测试证明业务合同，不能据此宣称生产端到端验收。

`markitdown-mcp`冲突由原pyproject的显式override产生，既有说明其默认禁用、按子进程
调用；本次保留并列为待办。未来可评估隔离环境或兼容版本，不应降级主链MCP掩盖告警。
Mypy原本仅在dev声明，无项目配置/CI门禁；未将新增忽略规则或更宽选项伪装为修复。

### 6.1 可复现命令

从项目根目录运行。Windows如需避免默认uv缓存ACL，可设项目缓存；命令不输出密钥。

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
.venv/Scripts/python.exe -m pytest -q --tb=short --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName }) scripts/audit_repository.py
.venv/Scripts/python.exe -m compileall -q src/open_deep_research src/security scripts evaluation
$env:MYPYPATH = Join-Path (Get-Location) 'src'
.venv/Scripts/python.exe -m mypy --explicit-package-bases --follow-imports=skip --ignore-missing-imports src/open_deep_research src/security
uv pip check --python .venv/Scripts/python.exe
.venv/Scripts/python.exe scripts/audit_repository.py --output docs/audits/pre-openai-sdk-inventory.json
```

最后三个检查中的Mypy/pip-check目前预期返回非零，分别对应以上35项/1项既有问题。
Mypy必须用明确包根；直接按namespace文件运行会把两个tools模块误判同名，阻止检查。
完整本机日志/原Git清单/配置hash/安全stash SHA留在忽略目录
`.tmp/openai-preparation/`；敏感配置内容未提交。
`import_smoke.py`与`protocol_smoke.py`也保留在那里；后者只绑定临时loopback端口并
执行MCP握手/列工具。Windows沙箱曾拒绝stdio管道创建，正常权限下实际复测成功。

## 7. 提交与最终状态

提交按社交业务整理、历史资料/开发依赖/审计工具、迁移文档与最终清单组织。
远程分支删除没有伪造代码commit。新分支推送到origin并设置upstream；没有force push、
rebase main、修改标签或合并旧PR。提交链首个父提交仍为指定备份。
最终当前分支为 `codex/openai-agents-migration`，跟踪同名origin分支，tracked/untracked
工作区干净；既有忽略数据、两个stash及safety ref保留。
具体最终commit SHA以 `git log 41cbc134..codex/openai-agents-migration` 和远程分支为准，
避免在被描述的提交中写入自引用SHA。

本次到迁移准备为止。正式接入、供应商适配与持久Run/Harness建设须遵守迁移计划的新验收条件。

## 8. 后续按用户要求删除 DeerFlow 与释放空间

同日，用户明确要求删除已不用的 DeerFlow 部分，并进一步确认删除 GitHub 分支和标签。
本节更新最新状态，前文分支/配置保留记录属于此前审计快照。

删除前确认这些目录没有当前Git tracked文件，环境metadata确认`backend/.venv`属于
DeerFlow Python3.12，而当前根`.venv`属于原舆情项目Python3.11；
`frontend/node_modules`的pnpm store引用本仓`.pnpm-store`。所有递归删除的绝对路径先
验证在本工作区内，根路径不是reparse point；使用PowerShell7原生删除，不跟随目录链接
到其他工作区。没有使用`git clean`、全局Docker prune或清空共享uv cache。

### 删除范围

- 本地目录：`backend/`、`frontend/`、`opinion_business/`、`deerflow_extension/`、
  `.phase0/`、`.deer-flow/`、`.pnpm-store/`、`.tmp/deerflow-phase0/`、
  `.tmp/deerflow-phase0-closeout/`、`.tmp/phase0-closeout/`。
- 根`config.yaml`和`extensions_config.json`是该试验的配置，当前舆情源码无消费者；
  先保存小型配置副本再删除根位置文件。
- Docker项目`opinion-phase0-closeout`的4个容器（nginx/gateway/redis/frontend）、
  专属`redis-data`卷及`deer-flow`网络；删除gateway/frontend两个专属镜像。
- 核对构建时间、命令和确切ID，定向删除8个DeerFlow构建缓存记录，共回收约
  **7.872 GB**；其他项目及共享基础缓存不做全局清理。筛选语义依据
  [Docker官方buildx prune文档](https://docs.docker.com/reference/cli/docker/buildx/prune/)。
- 本地和origin的`codex/deerflow-phase0`分支，以及本地和origin的`phase0-deerflow`
  标签均实际删除；远程查询确认不存在。origin现保留6条分支。

### 数据保护与占用结果

原舆情`.env`保存件与独立原项目备份的`.env`逐字节hash一致，先复制到
`.tmp/local-config-archive/pre-deerflow.env`再恢复为根`.env`。原试验的根`.env`、
两个operator配置、frontend`.env`也各存一份小副本，共5个配置文件，均hash复核。
未输出秘密值，未提交这些配置。

当前业务`src/`、`data/`、根`.venv`、`.langgraph_api`、研究transcripts、DSH/query-image
资料、共享`.tmp/uv-cache`、其他Worktree及独立原项目备份完整保留。
`public-opinion-neo4j`继续运行且healthy，未改变其他项目容器。
任务前未提交修改的stash及安全ref按开发规则保留；没有清空reflog或改写Git历史。

C盘可用空间从 **27,133,726,720** 增至 **30,016,151,552** 字节，
实测净增加 **2,882,424,832字节（约2.9 GB / 2.68 GiB）**。
文件逻辑大小包含缓存硬链接，不能按目录大小简单相加推算实际释放量。
Docker构建缓存统计从25.8GB降至17.93GB；该回收量属于Docker虚拟盘内部，
没有为压缩VHDX而停机或关闭其他项目服务，不与C盘净增量相加。

删除后用根`.venv`验证17个关键入口导入，并复跑68项社交/审计/安全守卫测试；
验证关闭dotenv/tracing并阻断外部网络，不调用真实模型。生产代码、依赖锁及业务数据
没有因这次物理清理改动。清理清单、空间读数与Docker日志在`.tmp/openai-preparation/`。
