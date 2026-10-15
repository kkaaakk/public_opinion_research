# Public Opinion Research — Phase 0 验收与交接

日期：2026-10-15，Asia/Shanghai。

## 结论

核心验收通过：完整官方 DeerFlow 独立启动，实际 Gateway 发现并加载自定义插件，
插件 Tool 经官方工具组装与真实 LangGraph ToolNode 执行，调用独立业务包。
真实前端工作区可访问并通过官方 Next.js rewrites 调用 Gateway。
未调用任何真实模型，未启动旧 LangGraph Agent、Neo4j、向量数据库或采集服务。

固定上游：`8d994cc1e7ee63c61283a71a169248beb4ed1c52`。
全部 4000 个上游文件保留，包括 Gateway、Harness、Extension API、Frontend、
Memory、Sandbox、Subagents、Skills、Docker、Nginx、contracts、部署与测试基础设施。
原始文件哈希、引入来源和更新方法见 [upstream.md](upstream.md)。

## 目录

```text
Public Opinion Research/
├── backend/                       # 完整官方后端
│   ├── app/                       # 官方 Gateway、channels
│   ├── packages/
│   │   ├── extension-api/          # deerflow-extension-api 0.2.7
│   │   └── harness/                # deerflow-harness 2.1.0
│   ├── extensions/sources/
│   │   ├── opinion-business/       # Docker 内可复现的业务发行包快照
│   │   └── opinion-deerflow-extension/ # 官方 CLI 管理的插件快照
│   ├── tests/                     # 官方测试
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── uv.lock
├── frontend/                      # 完整官方 Next.js 前端、pnpm 锁文件
├── opinion_business/
│   ├── pyproject.toml
│   ├── opinion_business/          # rag/research_graph/social_media/
│   │                              # sentiment/risk/evidence/reports
│   └── tests/
├── deerflow_extension/
│   ├── pyproject.toml
│   ├── opinion_deerflow_extension/
│   └── tests/
├── skills/public/                 # 所有官方 Skills + 三个舆情 Skill
├── data/                          # 保留原业务数据
├── tests/
│   ├── phase0/                    # 新跨模块集成测试，隔离旧 conftest
│   └── skills/                    # 官方 Skill 测试；原测试也保留
├── docs/
│   ├── adr/0001-deerflow-phase0.md
│   └── migration/
├── contracts/、docker/、deploy/、scripts/、examples/
├── plans/、pr-build/、.agent/、.github/
├── legacy/pre-deerflow/            # 306 文件旧项目完整备份副本
├── src/、evaluation/               # 原源码和评估资料继续保留
├── Makefile、LICENSE、AGENTS.md
├── config.example.yaml             # 官方原模板
├── config.phase0.example.yaml      # 本阶段离线模板
├── extensions_config.example.json
└── .env.example
```

原根级 `pyproject.toml`、`uv.lock`、`package.json`、`package-lock.json`、
`langgraph.json` 已归档到旧项目副本；原业务源码没有迁移或删除。
旧三份 GitHub workflow 也归档，避免继续使用已归档的根依赖清单。
新增专用离线 CI；本机已运行对应测试，远程 CI 尚未运行。
完整文件级新增/修改/删除清单见 [files-changed.tsv](files-changed.tsv)。
该清单按 Git `--no-renames` 记录：新增 4362、修改 6、删除根位置 8 个文件；
删除项均已归档，未删除业务源码。上游 47 个文件的 Git 可执行权限同时保留。

## 注册和执行链路

1. 发行包 `opinion-deerflow-extension==0.1.0` 暴露 Entry Point：
   `deerflow.extensions` → `opinion` → `opinion_deerflow_extension:install`。
2. 官方 `deerflow extensions install` 发现 Entry Point，维护后端 extensions
   依赖组、锁文件及 `config.yaml -> plugins:`。这里使用 `--required`，加载失败即启动失败。
3. Gateway `create_app()` 依照该配置调用 `@extension(api="0.2.7", name="opinion")`
   的入口，注册 `PluginContribution(namespace="public-opinion.phase0")`。
4. 官方 `get_available_tools()` → `build_plugin_tools()` 将 `ModelTool(opinion_ping)`
   转为带来源信息与输入验证的工具。真实 LangGraph ToolNode 执行后调用
   `opinion_business.ping()`。
5. 实际模型可见工具名为 `ext_public_opinion_phase_opinion_ping_8b07baee06f0`；
   输入 `{"text":"  舆情迁移验证  "}`，输出包含 `status=ok`、`phase=phase0`、
   `version=0.1.0`、`text=舆情迁移验证`。
6. 轻量状态 Action 复用官方路由：
   `POST /api/plugins/public-opinion.phase0/actions/status`，JSON body `{}`。
   真实运行的 Gateway 返回 HTTP 200，业务七个领域均报告 `interface_only`。

插件实现仅导入 stdlib、`deerflow_extension_api` 与 `opinion_business`；
AST 测试限制私有主机导入。集成测试也确认主机没有载入 `open_deep_research`。
没有第二个 Gateway 或自定义 Agent 循环。

Python 插件必须配置在 `config.yaml -> plugins:`；
`extensions_config.json` 仅用于 MCP / Skills 等官方配置，不注册 Python 插件。
主机 `plugins[].enabled` 和插件私有 `config.enabled` 是两个开关，离线模板均启用。

## 包依赖和可复现安装

| 发行包名 | Python 导入名 | 运行时依赖 |
|---|---|---|
| opinion-business 0.1.0 | opinion_business | stdlib |
| opinion-deerflow-extension 0.1.0 | opinion_deerflow_extension | opinion-business==0.1.0、deerflow-extension-api>=0.2.7,<0.3 |
| deerflow-extension-api 0.2.7 | deerflow_extension_api | 保持官方公共合同 |

DeerFlow Runtime → opinion-deerflow-extension → opinion-business。
业务包被显式加入后端 extensions 依赖组，以便 uv 从本地部署快照解析该发行包；
插件仍声明真正的传递依赖。后端 `uv.lock` 的两个路径均为
`extensions/sources/...`，没有本机绝对路径。

本次真实执行的首次安装过程（从根目录）：

```powershell
python scripts/sync_opinion_business.py
Set-Location backend
uv add --group extensions --no-workspace --no-sync ./extensions/sources/opinion-business
uv sync --locked
uv run --no-sync deerflow extensions install ../deerflow_extension --yes --required
uv run --no-sync deerflow extensions list
uv sync --locked
```

这些操作使用与官方 Docker 相同的 uv 0.11.1；常规宿主 uv 为 0.11.24。
部署快照、依赖声明和锁文件已提交，首次拉取本分支只需 `uv sync --locked`。
已安装的快照不要再次 install；后续插件更新使用官方 `extensions upgrade`。
业务更新先运行快照同步脚本，再执行 `uv sync --locked --reinstall-package opinion-business`，
版本或依赖变化同时重新用 `uv add` 声明并审查锁文件。更新后重新运行快照一致性测试。

实际发现并修复：插件自身的相对 `[tool.uv.sources]` 在官方管理器复制快照后指向错误目录，
导致 `Distribution not found .../extensions/sources/backend/packages/extension-api`。
管理器完整回滚；移除这些开发来源配置，独立测试通过显式传入三个源码包目录安装。
随后官方 install 和 locked sync 成功。

此外实际复制了**仅 backend/** 的干净源码到临时目录，在全新 venv 中
执行 uv 0.11.1 `sync --locked`；253 个锁定包解析、228 个包安装成功，
Python `-I` 模式导入双包并执行 `ping('backend-only-context')` 成功。
该验证证明无需根业务目录或开发机绝对路径；它没有代替 Linux Docker 镜像构建。
官方 Dockerfile 与 `.dockerignore` 保持原样，既有规则会带入这两个快照。

## 本机实际验证

| 验收项 | 结果 |
|---|---|
| Python | 实际后端 Python 3.12.13；系统默认 Python 3.11.9，使用 uv 选择 3.12 |
| uv | 官方锁安装及管理器使用 0.11.1；宿主 0.11.24 |
| Node / pnpm | Node 24.15.0；frontend pin 的 pnpm 10.26.2 |
| 官方依赖 | 后端 locked sync、前端 frozen-lockfile install 成功 |
| Gateway 真实进程 | Uvicorn 完成 lifespan、SQLite 迁移及 LangGraph runtime 初始化 |
| `/health` | HTTP 200、healthy |
| `/health/ready` | HTTP 200、database=ok、checkpointer=ok |
| 插件发现/状态 | 真实 GET `/api/plugins` 与 POST status action 均 HTTP 200 |
| 业务包独立测试 | 4 passed；wheel 和 editable 安装均验证 |
| 插件独立测试 | 10 passed；sdist、wheel 构建及 metadata 验证成功 |
| 跨模块测试 | 最终 14 passed，包括完整上游哈希、ToolNode、HTTP、认证及快照/锁审计 |
| 相关官方测试 | 196 passed，11 skipped |
| 官方 blocking_io | 252 passed，5 skipped；2 个依赖弃用警告 |
| 前端 | `pnpm check` 成功；5 个官方首页 E2E 全部 passed |
| 真实浏览器工作区 | `/workspace/chats/new` 可访问；相关 Gateway API HTTP 200；当前无控制台错误 |
| Skills | 官方 LocalSkillStorage 及真实浏览器 `/api/skills` 识别三个 public/enabled Skill |
| Ruff | 双包、集成测试和快照脚本 lint 与格式检查通过 |
| 付费模型完整对话 | 未执行，离线 profile 配置 models=[] |

主要复现命令（从根目录，使用已安装的 backend 环境）：

```powershell
uv run --no-project --python 3.12 --with ./opinion_business python -m unittest discover -s opinion_business/tests -v
uv run --no-project --python 3.12 --with ./backend/packages/extension-api --with ./opinion_business --with ./deerflow_extension python -m unittest discover -s deerflow_extension/tests -v
$env:PYTHONPATH = 'backend'
backend/.venv/Scripts/python.exe -m pytest -c tests/phase0/pytest.ini --confcutdir=tests/phase0 -p no:cacheprovider tests/phase0/test_integration.py -q
backend/.venv/Scripts/ruff.exe check opinion_business deerflow_extension tests/phase0/test_integration.py scripts/sync_opinion_business.py
backend/.venv/Scripts/ruff.exe format --check opinion_business deerflow_extension tests/phase0/test_integration.py scripts/sync_opinion_business.py
```

官方针对性测试（backend 目录）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_extension_api_surface.py tests/test_extension_api_contracts.py tests/test_extension_registry.py tests/test_extension_loader.py tests/test_plugin_tools.py tests/test_skills_validation.py tests/test_extension_dependency_sync.py -q
.venv/Scripts/python.exe -m pytest tests/blocking_io -q --tb=short
```

原始日志、live probe JSON 和 wheel 留在忽略的 `.tmp/deerflow-phase0/`。
详细前端命令、临时 Chrome 配置及浏览器截图记录见 [frontend-validation.md](frontend-validation.md)。

## 可直接运行的本地启动

在项目根目录的 PowerShell，保留已有本机配置后再复制模板；本次旧 `.env`
已经保存到忽略的 `.tmp/deerflow-phase0/pre-deerflow.env`，未提交。

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.tmp/uv-cache'
$env:UV_TOOL_DIR = Join-Path (Get-Location) '.tmp/uv-tools'
uv tool run --from uv==0.11.1 uv sync --project backend --locked
Copy-Item config.phase0.example.yaml config.yaml
# 新 checkout 没有 .env 时可复制官方模板；离线 profile 不使用其中的模型 Key。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
if (-not (Test-Path frontend/.env)) { Copy-Item frontend/.env.example frontend/.env }
if (-not (Test-Path extensions_config.json)) { [IO.File]::WriteAllText((Join-Path (Get-Location) 'extensions_config.json'), '{"mcpServers":{},"skills":{}}', [Text.UTF8Encoding]::new($false)) }
Set-Location frontend
pnpm.cmd install --frozen-lockfile
```

终端一，项目根目录：

```powershell
Set-Location backend
$env:DEER_FLOW_PROJECT_ROOT = (Resolve-Path ..).Path
$env:DEER_FLOW_AUTH_DISABLED = '1'
$env:PYTHONUTF8 = '1'
uv run --locked uvicorn app.gateway.app:app --host 127.0.0.1 --port 8001
```

终端二，项目根目录：

```powershell
Set-Location frontend
$env:DEER_FLOW_AUTH_DISABLED = '1'
$env:NEXT_TELEMETRY_DISABLED = '1'
pnpm.cmd dev -- --port 3000
```

浏览器打开 `http://127.0.0.1:3000/workspace/chats/new`。
这里的 auth bypass 仅用于本机 loopback 离线验收，不写入共享部署配置。
正式部署按官方 `/setup` 创建管理员并启用身份验证。

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health/ready
Invoke-RestMethod http://127.0.0.1:8001/api/plugins
Invoke-RestMethod -Method Post -ContentType 'application/json' -Body '{}' http://127.0.0.1:8001/api/plugins/public-opinion.phase0/actions/status
```

WSL2 / Docker 环境准备完成后，仍使用官方 `make dev` 或 `make up`，入口 `:2026`。
没有新增任何服务启动管理器。

## 未完成项及阻碍

- **P1，部署前验证**：Docker CLI 29.4.3 存在，但 Docker Desktop Linux Engine
  pipe 不存在；`docker info` 失败。WSL `wsl --status` 返回
  `Wsl/EnumerateDistros/Service/E_ACCESSDENIED`；本机没有 make/nginx。
  未验证 Linux Docker build/run、Nginx `:2026` 或 WSL2 全栈。
  恢复环境后执行 `docker info`、`wsl --status`、`make check`、`make up`。
- **P2，全量上游测试**：试跑官方第 1/4 个离线 shard 时，24,266 项被分流排除，
  Windows 沙箱在 `test_skill_review_core.py` 创建临时 CaseProbe 时拒绝访问，
  collection 中断，不能宣称全套通过。相关 196 项及完整 blocking_io 已在允许临时目录访问后通过。
  在 WSL/Linux 执行官方 `make test` 和 `make test-blocking-io` 完成全量回归。
- **P2，上游文档 UI**：`/en/docs` 首次渲染成功，后续导航曾出现 React
  `Element type is invalid ... undefined`。主工作区和五个首页 E2E 通过；未为此修改固定上游前端。
- **范围外，完整模型对话**：models=[]，没有模型密钥；`scripts/doctor.py` 因无模型及 nginx
  返回 2 errors。配置版本、解析、Python、Node、pnpm、uv 和本地 sandbox 检查通过。
  本阶段没有把空配置伪装成可用的模型研究服务。
- Windows 执行沙箱曾产生 uv build cleanup WinError 5、pytest 临时 ACL 拒绝和 Next spawn EPERM；
  采用仓库内缓存、正常进程/临时文件访问后完成实际验证，没有依赖冲突遗留。
- 全量导入的上游和旧归档含既有尾随空白，整体 `git diff --check` 会报告这些历史内容；
  本阶段保留原文。新业务包、适配器和集成代码已通过 Ruff；项目新文件单独检查空白。

## Git 和后续阶段

当前本地分支 `codex/deerflow-phase0`，基于 `41cbc134` 备份提交。
`main` 仍为 `c54323ba`，没有合并、推送或创建 PR。使用 `git log -1` 读取迁移交付提交。
备份分支与标签、脏工作区根因及撤销 worktree 的详情见 [git-preflight.md](git-preflight.md)。

下一阶段先迁移纯 evidence/citation 与数据契约，再评估 RAG 检索接口、社交媒体适配、
sentiment/risk 及 reports。逐项抽取到业务包并用原测试约束行为；通过扩展适配器接入官方能力。
不要迁移旧 Graph、自定义循环或替换 DeerFlow 的 Memory/Sandbox/Subagent。
完整路径和缺口见 [legacy-inventory.md](legacy-inventory.md)。
