# SDK 迁移后的仓库清理审计

日期：2026-10-24（Asia/Shanghai）。基线：`origin/main` 的 `1f80b4819523d209e2eba92d9ab66d12f7259706`。清理分支：`codex/post-sdk-repository-cleanup`。分类清单在删除前建立；本报告区分初始证据、实际改动与验证结果。

historical note: removed runtime and integration names in this audit describe frozen evidence and regression guards, not current execution paths.

## 1. Git 与数据边界

开始时位于 `codex/openai-agents-phase5` / `ddbc56a9`，tracked/untracked 工作区干净；fetch 后确认最新 main 已合入该分支。直接在当前干净 checkout 从最新 main 建立独立分支，没有移动现有其他 Worktree、Stash 或分支。保留 `refs/safety/pre-openai-preparation-20261016` 和两个既有 Stash；`.tmp/trajectory-ledger` 是另一个 Worktree，不能作为临时目录清除。

审计区分 Git 已跟踪源码与未跟踪/忽略的本机资产；不读取或提交 `.env` 的秘密正文，不改 `AGENTS.md` / `CLAUDE.md`。旧 checkpoint、SDK Session、研究 transcript、数据库和配置副本不是可再生缓存。

## 2. 删除前分类清单

A = 证据充分的废弃/重复内容；B = 已验证可再生的缓存；C = 消费者或历史价值未确定，保留；D = 当前有效或明确受保护，保留。目录内可有不同类别，不能整目录套用删除结论。

| 根项目 | 类别 | 证据与处置计划 |
| --- | --- | --- |
| `.benchmarks/` | B | 空目录、无 tracked 文件或运行消费者；删除空目录 |
| `.claude/` | D | 未跟踪的 `settings.local.json`，个人配置，保留 |
| `.codex-finalizer/` | C | 四份学习/面试 PPTX 验证记录，历史产物证据，保留 |
| `.git/` | D | Git 元数据、Worktree、Stash、安全引用，保留 |
| `.github/` | A/D | 保留 CI、DBOS 服务合同、Claude 自动化、Dependabot；无消费者且绑定已退役 DeerFlow skills 的豁免 JSON 迁入 `docs/archive/deerflow/`，保留原始内容 |
| `.idea/` | D | IDE 个人配置，保留 |
| `.langgraph_api/` | D | 七份历史 pickle checkpoint/store，不被当前引擎消费但不能再生，保留 |
| `.playwright-mcp/` | C | 历史截图、页面快照、控制台日志及原配置；包含独立观测证据，保留 |
| `.tmp/` | C/D | 配置归档、历史审计、源码下载、transcript、SQLite Session、另一个 Worktree 和共享 uv cache；原有内容保留，只清理本次新建的 npm cache / uv build 临时目录 |
| `.tmp-git-sync-20261012-090013/` | D | 原未提交文件副本、manifest、patch、校验记录；恢复资产，保留 |
| `.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/` | B | 工具生成且无 tracked 文件；检查缓存标记及普通目录后删除，验证可能重新生成 |
| `.venv/`、`.env` | D | 根锁环境与操作员配置，保留；仅按锁同步安装依赖 |
| `node_modules/` | B | 开始时不存在，本次由根 `npm ci` 为文档验证生成；验证后与本次 npm cache/uv build 临时目录一起清理 |
| `data/` | D | 12 个 tracked 文件含知识样本、记忆、社媒数据及索引占位；本地派生索引可能不可无损重建，均保留 |
| `docs/` | D | 历史审计、迁移记录、ADR、学习资产、评测语料、文档站；保留证据，修复活动导航/已失效说明 |
| `evaluation/` | D | Golden 输入、跨版本/模型基线和历史 runner/后处理共 54 个文件；旧 runner 依赖固定提交，保留并说明历史环境 |
| `examples/` | A/D | 四个 Markdown 与 `docs/examples/` 逐字节（归一化 CRLF）一致，当前导航仅指 docs，删除根副本；保留可运行的 `rag_local_search.py` |
| `scripts/` | D | 10 个依赖/审计/原生合同/显式付费 smoke/本地 Neo4j 运维入口均有用途，保留 |
| `src/` | A/D | 当前 SDK/DBOS/Harness/RAG/Graph/Memory/四角色及 API 保留；仅删除下节列明的私有无消费者实现 |
| `tests/` | D | 旧引擎命名测试已是禁止旧执行路径的回归守卫；reference workflow 为离线比较基准，均保留 |
| `tools/` | D | `code_graph_analysis.py` 是固定版本学习图生成器，有文档消费者，保留 |
| `.env.example` | D | 当前部署配置模板，保留；旧 selector 的拒绝语义不改 |
| `.gitignore` | D | 增补本机 cache/工具产物忽略规则，不扩大到有效源码或 tracked 数据 |
| `.markdownlint-cli2.jsonc` | A/D | 删除已不存在的 `src/legacy/**/*.md` 和清理后空的根 `examples/**/*.md` glob；保留 lint 规则 |
| `.prettierrc` | D | npm 文档格式命令消费者，保留 |
| `AGENTS.md`、`CLAUDE.md`、`LICENSE` | D | 开发约束、个人/历史说明和许可证，原样保留 |
| `compose.dbos.yaml`、`compose.neo4j.yaml` | D | 当前持久工作流及证据/History 服务，保留；旧 phase4 项目/label 被合同测试使用，不能机械重命名 |
| `locustfile.py` | D | 已迁移 Run API 的压测客户端，保留 |
| `package.json`、`package-lock.json` | A/D | 保留 VitePress/Markdown 命令；移除无配置/命令消费者的两个 remark lint preset，同步锁 |
| `pyproject.toml`、`uv.lock` | A/D | 保留当前运行依赖和 provider 动态装配；删除无项目直接消费者的 pandas/rich 顶层声明，合并 web extra 的冗余 FastAPI 约束；为实际 YAML 配置回归显式声明 dev PyYAML，同步锁 |
| `README.md` | D | 修正不存在的 skills 目录、缺失的当前 runtime/workflow 目录和旧单一 LangChain 模型说明 |

## 3. 源码候选与证据

| 类别 | 对象 | 判断 |
| --- | --- | --- |
| A | `workflow/shared.py::_with_correlation_metadata` | 旧 Graph 调用的私有助手；全仓 AST/文本无消费者，当前单次业务调用自有观测边界；仅保留共享 `configurable_model` |
| A | `research_graph/store.py::_scope_value` | 私有范围兼容助手无任何调用；当前写入/检索使用显式 `ResearchGraphScope` 和参数，删除不改变范围校验 |
| A | `rag/loaders/knowledge.py::_extract_json_text`、`_extract_field_path`、`_collect_text_fragments` | 旧 JSON 扁平化闭包；现在 loader 保存原始 JSON，splitter 负责 json_path；不在公开 loader exports 或 patch registry 中。file_memory 的同名实现有真实调用，必须保留 |
| C | `rag/memory_writer.py`、`rag/tooling.py` | 无内部静态消费者，但明确承诺旧导入路径，仓库外调用者未知；保留，不擅自结束兼容期 |
| C | `budget_usage_with_warning`、`budget_from_native_search`、`memory.types.is_indexable_memory_type` | 无当前内部调用，但为公开辅助接口，仓库外使用不明；保留 |
| C | `context_warning_ratio`、`public_opinion_monitoring_window` | 未找到配置校验以外的业务读取；仍属于公开配置/schema，操作员和外部客户端使用不明，保留并列为后续弃用候选 |
| D | `research_graph_neo4j_*` 配置别名 | 虽没有模块外逐字引用，Configuration 自身归一化会消费它们；保留现有有效输入语义 |
| D | 其余五个兼容模块及 `workflow.get_workflow` | 业务/测试/历史脚本/文档消费者存在；factory stub 是明确拒绝旧执行入口的合同 |
| D | `web/history.py` 的旧来源名称/导入、纯状态 reducer、Graph/MCP 适配 | 当前历史读取、证据处理、工具注册和动态入口仍使用；不是旧调度器 |
| D | `tests/reference_workflow.py`、`test_langgraph_runtime_compat.py` 等 | 固定比较 oracle 与旧引擎缺失/拒绝合同，不在生产执行链 |

模块级 AST 引用审计覆盖全部 tracked Python；同时核对 entry points、FastAPI/DBOS 装饰器、字符串注册、`__all__` / lazy export、配置和动态 provider 工厂。无静态引用不等于废弃；FastAPI handlers、DBOS events/finalize workflow 正是动态注册，全部保留。

## 4. 实际清理与引用修复

生产代码仅修改三个已有模块，删除上表五个无消费者的私有函数及其专属 import。没有移动业务模块或新增抽象层；Agent/DBOS 版本、角色策略、提示词、恢复命令、工具协议、SSE 和 History 的实现均未修改。`runtime/agent_runtime.py`、`workflow/graph.py`、`langgraph.json`、旧 SQL Worker/RunStore 以及 DeerFlow 源码在本次基线已不存在，不能算成本次删除成果。

四份根示例报告删除前与 `docs/examples/` 逐字节比较（归一化 CRLF）一致，后者及其导航保留。DeerFlow 豁免文件迁至 `docs/archive/deerflow/`，Git 识别为 **100% rename**，原 JSON 内容不变，另加历史边界说明。所有历史审计、迁移规划、ADR、Golden Set、评测记录、后处理和独立学习资料继续保留。

移除 `remark-preset-lint-consistent` / `remark-preset-lint-recommended`：没有 `.remarkrc`、package 配置或 CLI 参数启用它们；现有 `md:toc` 只调用 `remark-toc`，Markdown lint 由 `markdownlint-cli2` 执行。同步 npm 锁后退出 45 个包条目，保留包的整个锁条目未变。

Python 删除 `pandas` / `rich` 的顶层声明，项目没有直接 import 或配置消费者。它们仍分别被 `pymilvus`、`typer` 等有效传递依赖需要，**没有把锁包数不变说成卸载成功**。合并 web extra 重复的 FastAPI 约束，保留 `uvicorn[standard]` 能力；为 YAML 回归测试显式声明 dev `pyyaml`，原锁已包含它。281 个 Python 锁记录及版本集合完全不变，SDK 0.24.0 / DBOS 3.3.0 不变。

修复两项已证实的配置/测试隔离问题：

- Dependabot 的第二个 `updates` 键覆盖了 pip 任务；合并到一个列表，原 pip / github-actions 的每周频率不变。新增用例在原配置 **1 failed**，修复后通过。
- 原生 Session 合同写死 `.tmp/dbos-sdk-sessions.sqlite`。夹具现在强制把 Session 文件放入该用例的 `tmp_path`，保留四角色独立会话断言，并新增路径隔离断言。清理前原生测试追加的本次 Run 四条 Session / 16 条消息，已通过该用例独立 transcript 的 Run ID 核对，仅撤回这些新增记录；原有 **12 条 Session / 48 条消息逐条不变**，SQLite integrity check 成功，操作前备份留在本次忽略目录。没有删除原有会话或把整个数据库当缓存。

增强 JSON 入口回归：原文仍完整保留，未配置过滤、`faq.answer` 和 `faq[0].answer` 三种路径都经过实际 loader → splitter，验证字段过滤与 citation path，file_memory 的有效 helper 未动。

README 改为当前模块地图，删除不存在的 skills 目录说明，区分 SDK Model 和 LangChain 单次模型边界，说明当前 Web 搜索准入。VitePress 导航指向 main，本项目仓库链接替换原模板链接；两个审计脚本链接改为固定基线 GitHub 源码，保留原文证据和 dead-link 检查。Markdown 工具删除失效的 legacy glob 和已无 Markdown 的根 examples glob，`docs/examples/` 继续由 docs glob 覆盖；`.gitignore` 补上本机工具输出、`.tmp/` 和 Mypy cache，不误忽略源码或现有 tracked 数据。

## 5. 数量变化

统计使用既有 `scripts/audit_repository.py` 的 Git blob / tracked working-file 快照；SLOC 使用既有 `scripts/dbos_inventory.py::count`，排除空白、独立注释及 docstring。新增报告、归档说明和回归测试计入，缓存/本地数据不混入 Git 文件数。

| 指标 | 清理前 | 清理后 | 变化 |
| --- | ---: | ---: | ---: |
| Git 跟踪文件 | 343 | 341 | -2（删除 4 个副本，新增 2 份说明；归档移动不增减） |
| Python 文件（全仓） | 195 | 195 | 0，没有删除有效测试或模块 |
| Python 物理行（全仓） | 61,124 | 61,039 | -85 |
| 生产 Python 物理行 | 30,926 | 30,811 | -115 |
| 生产 Python SLOC | 24,243 | 24,165 | -78 |
| 测试 Python 物理行 | 22,830 | 22,860 | +30 |
| Python base / dev / web / notebooks 声明 | 36 / 4 / 2 / 1 | 34 / 5 / 1 / 1 | 删无用直接声明、去重 extra、显式测试依赖 |
| Python 锁 package records | 281 | 281 | 0，版本变化 0 |
| npm 直接 dev 依赖 | 7 | 5 | -2 |
| npm 锁包条目（不含根，含平台可选包） | 449 | 404 | -45，保留条目变化 0 |
| 本机 Windows `npm ci` 安装包 | 403 | 358 | -45 |

本机 B 类实际清理：`.mypy_cache` / `.pytest_cache` / `.ruff_cache`、源码和支持目录的 `__pycache__` 共 **1,053 个文件 / 44,677,674 逻辑字节**；其中包括已退役 Auth、旧 Runtime、wizard / Phase 0 测试的字节码。空的 `.benchmarks`、`src/security`、`scripts/wizard[/steps]` 和 `tests/phase0` 一并移除。保留当前 editable 安装依赖的 `src/open_deep_research.egg-info`。

本次验证新建的 `node_modules`、npm cache、uv build 临时目录和 VitePress dist 首轮清单为 **9,027 个文件 / 145,280,421 逻辑字节**，验证结束后清除；为复验 Windows 构建权限而重新生成的同类输出也再次清除，不重复累计文件数。首轮总计 **10,080 个再生文件 / 189,958,095 逻辑字节**；硬链接/文件系统分配可能不同，不冒称实际磁盘释放量。这些是本机操作，不伪装为 Git 删除。

删除脚本逐项解析绝对路径，要求仍在当前仓库内、没有 reparse point、无 tracked 文件；工具 cache 检查 CACHEDIR.TAG 签名，字节码目录只允许 `.pyc`，空目录必须确实为空。没有用 `git clean`，对 `.langgraph_api`、`.tmp` 历史/配置/Session/transcript、`.tmp-git-sync-*`、`.playwright-mcp` 和 `data/` 无整目录清理。另有 **142 个受保护本地文件 SHA256 不变**；已恢复的 Session 单独逐条核对并复核恢复后的 hash。

## 6. 清理前后验证

所有本地模型推理使用官方 SDK ScriptedModel、模拟 HTTP 或受控业务模型；禁 dotenv/tracing 和非 loopback socket，付费模型/API 调用为 **0**。全量默认 pytest 的 91 deselected 包含外部环境/原生合同，原生矩阵另行显式执行，未新增 skip/xfail。

| 检查 | 清理前 | 清理后 |
| --- | --- | --- |
| 根锁同步 | 281 records / 216 installed，通过 | locked/offline 同步通过，仅重建当前 editable 包；版本集合无变化 |
| 完整离线 pytest | **804 passed / 91 deselected / 146 warnings，78.39s** | **807 passed / 91 deselected / 146 warnings，63.77s**，新增 1 个配置回归和 2 个 JSON 参数化场景 |
| SDK/DBOS 原生矩阵 | **85 passed / 25 deselected / 0 skipped / 0 xfailed，1051.16s** | **85 passed / 25 deselected / 0 skipped / 0 xfailed，1023.84s**，含 Session 路径隔离断言 |
| Node 前端合同 | **7 passed / 0 failed / 0 skipped** | **7 passed / 0 failed / 0 skipped** |
| CI Ruff E/F/I | 通过 | 通过 |
| 全仓启用规则 Ruff（src/tests/scripts/evaluation/examples/tools/locustfile） | 422 diagnostics，未通过 | 421 diagnostics，未通过；按文件/规则/消息比较新增 0 |
| 全仓格式 src/tests/scripts | 68 个待格式化，未通过 | 67 个待格式化，未通过；没有新增格式不符文件 |
| 本次改变结构且原已格式化的 Python | 基线记录在本地 | shared / dbos_support / dbos_contracts / repository_audit 四文件通过；其余旧文件仅删除/局部增强，未机械重排 |
| Mypy 原审计命令 | **34 errors / 16 files / 112 checked**，未通过 | **同 34 项**，忽略行号逐条比较新增 0；历史 35 项不是当前基线 |
| Compile | 生产与支持目录通过 | src/tests/scripts/evaluation/examples/tools 通过 |
| 无旧引擎/网络的入口 import | 32 个通过 | 同 32 个通过 |
| pip check | MarkItDown MCP 要求 mcp~=1.8.0，实际1.23.0，未通过 | 同一继承冲突，新冲突 0 |
| Python 锁一致性 | locked sync 通过 | `uv lock --check --offline`、locked sync 通过 |
| npm 锁安装 | `npm ci` 通过 | `npm ci --offline` 通过 |
| VitePress docs:build | **失败**，两处指向仓库外 scripts 的相对链接 | **通过**，修复链接；仍保留继承的 chunk size 提示 |
| 实际 CLI/API 启动 | 原生矩阵覆盖入口 | Executor `--initialize-only` 两次与 `--health` 成功；Uvicorn 实际 HTTP 的 health/index/schema/鉴权成功 |

原生套件用本次新建 `por-repository-cleanup` 项目的 PostgreSQL 17.6 / Neo4j 5.26.31；只停启该项目精确容器，PG 只创建/删除 fixture 独立 `por_test_<uuid>` 库。现有业务 Neo4j/MySQL 服务未参与。覆盖 `/api/runs`、`/api/research`、CLI 进入真实 SDK Runner / DBOS 持久化，SSE 尾事件及 History、取消、审批、幂等、旧 retry key、失败代次、未知效果保护、Executor kill/restart、PG/Neo4j 实际停启；没有以正常路径替代恢复验收。

边界说明：原生 API/CLI 完整业务链使用受控 ASGI/HTTP transport 和实际 DBOS/数据库；额外 Uvicorn smoke 验证真实 TCP 启动。没有调用线上 provider、远程 MCP、生产数据库，没有执行生产排空或跨主机 HA 验证。

迭代问题如实记录：Python 锁重算首次 offline 失败于跨平台元数据缓存不完整，联网获取元数据后成功且没有版本变化；首次 editable 构建遇 Windows 临时 PE 资源 ACL，改用本次工作区临时目录后原命令成功。最后一次 docs 构建遭遇沙箱目录 mkdir EPERM，在新生成依赖/输出上以正常权限重跑成功，没有关闭链接检查。独立启动 smoke 最初误把“未配置操作员 Token”的 fail-closed 503 期望成 401，改为显式合成测试 Token 后验证无授权请求 401，未改生产鉴权。Windows pyreadline/SWIG 退出提示和既有弃用警告继续记录，不冒充断言失败或环境全绿。

复现（PowerShell，先准备独立本地测试服务）：

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
uv sync --extra dev --locked --python 3.11
$env:PYTHON_DOTENV_DISABLED = '1'
$env:LANGSMITH_TRACING = 'false'
$env:LANGCHAIN_TRACING_V2 = 'false'
$env:OPENAI_AGENTS_DISABLE_TRACING = '1'
.venv/Scripts/python.exe -m pytest -q --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
$env:TEST_DBOS_POSTGRES_CONTAINER = 'por-repository-cleanup-postgres-1'
$env:TEST_DBOS_NEO4J_CONTAINER = 'por-repository-cleanup-neo4j-test-1'
.venv/Scripts/python.exe -m pytest -q -m integration tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py tests/test_web_history.py tests/test_p1_fixes.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
node --test tests/durable_frontend.test.cjs
.venv/Scripts/python.exe -m ruff check --select E,F,I --ignore E501 src/open_deep_research tests/conftest.py (Get-ChildItem tests/test_*.py | ForEach-Object { $_.FullName })
.venv/Scripts/python.exe -m compileall -q src/open_deep_research tests scripts evaluation examples tools
.venv/Scripts/python.exe scripts/phase5_import_smoke.py
uv lock --check --offline
uv pip check --python .venv/Scripts/python.exe
npm ci
npm run docs:build
.venv/Scripts/python.exe scripts/audit_repository.py --base-ref 1f80b4819523d209e2eba92d9ab66d12f7259706 --output .tmp/post-sdk-cleanup/reproduced-inventory.json
```

所有本机原始日志、完整静态清单、缓存删除 manifest 与数据保护记录留在忽略的 `.tmp/post-sdk-cleanup/`；不提交运行输出、秘密、数据库或下载源码。

## 7. 保留的遗留问题与后续建议

- 保留七个明确的兼容导出模块和拒绝旧工厂的 stub；其中 `rag/memory_writer.py` / `rag/tooling.py` 及少量公开 helper 缺少内部消费者，建议下一次基于对外调用清单决定弃用窗口，本次不推断外部使用为零。
- `context_warning_ratio` / `public_opinion_monitoring_window` 的业务消费者仍需确认；四个 `research_graph_neo4j_*` 是有效别名，不应与真正无消费者字段一起删除。
- `.langgraph_api`、Session、历史 transcript、截图与配置归档需要明确数据保留/导出政策；不用缓存清理替代数据生命周期决策。`.tmp/trajectory-ledger`、prunable Worktree 记录、其他分支/Stash/安全 ref 原样保留。
- 历史 evaluation runners、学习/面试图仍引用旧架构，这是固定版本复现证据；当前验收使用 SDK/DBOS 套件，不能直接以历史 runner 证明新版本效果，也不为消除引用而删除基线。
- MarkItDown/MCP 继承版本冲突宜单独隔离或兼容升级；Mypy、剩余 Ruff/格式欠账另开范围处理。本次没有通过新 ignore、降级依赖、删除有效功能或放宽测试掩盖问题。
- 不自动合并 main；最终提交、分支和指向 main 的 PR 交由独立 Review。
