# 配置统一与 CLI 入口整改报告

分支：`codex/config-cli-unification`。目标是把 `config.yaml` 变成唯一业务配置来源，
让终端成为主要运行入口，同时保留 DBOS、SDK Runner、ToolExecutor、预算、归档和
现有恢复语义。

## 1. 配置冲突与静默降级问题清单

| 原文件 / 函数 | 问题 | 实际运行行为 | 修复 |
| --- | --- | --- | --- |
| `configuration.py:Configuration.from_runnable_config` | 每个字段都用 `os.environ.get(field.upper(), configurable.get(...))`，环境变量优先于冻结配置 | 修改 `.env` 会改变模型、RAG、预算和角色；Run 恢复时摘要可能变化 | 业务字段只读取 `configurable` 快照；保留退役执行选择器的环境变量失败关闭 |
| `configuration.py` | 未声明 `extra="forbid"`，拼错的字段被忽略 | 配置写错时悄悄使用 Python 默认值 | `Configuration` 与 `MCPConfig` 改为 `extra="forbid"` |
| `web/runs.py:accepted_config` | 把一个请求模型同时赋给 research/compression/final/summarization，并按请求开关 RAG/角色 | Web 请求覆盖正式配置，Plan/Report/RAG 失去独立模型 | 删除该函数；Web 与 CLI 都从 `config.yaml` 构建冻结快照 |
| `.env.example` | 包含 `RESEARCH_MODEL`、`RAG_ENABLED`、`ENABLED_PLUGINS` 等业务开关 | 与 `Configuration` 默认值和 Web 默认值形成三套来源 | `.env.example` 只保留凭据、数据库连接、认证和部署项 |
| `web/server.py` | 只在 Web 进程调用 `load_dotenv()` | CLI/Executor 依赖调用方恰好预先加载 | 新增 `env.load_environment()`，在 CLI、Web、Executor、DBOS settings 和 RAG MCP 入口调用 |
| `web/durable.py:validate_request` | 用环境变量重算配置摘要 | 无关环境变化导致运行中 Run 报 `Accepted configuration/version changed` | 只从持久化快照重算；配置失效时给出明确 `RunConflict` |
| `web/durable.py:RunClient.create` | 重新读取 `allow_plan_feedback` 并写 `accepted_run` 元数据 | 配置来源不唯一；快照不是完整有效配置 | 一次规范化并冻结完整有效配置；记录 `configuration_source`；不持久化敏感字段 |
| `tools/plugins.py:load_role_plugins` | 插件目录或 id 缺失时 Warning 后继续 | 显式启用的插件静默消失 | 显式启用但目录/清单缺失改为 `ValueError` |
| `tools/plugins.py:_server_spec` | `${ENV_VAR}` 缺失时 Warning 后跳过 | 社媒 MCP 静默不可用 | 必需变量缺失改为 `ValueError`；仅 `optional: true` 的服务器允许显式降级 |
| `mcp/tools.py:load_mcp_tools` | DBHub/Feishu/MarkItDown 的构建异常和单服务发现失败被吞掉 | 显式启用的服务静默关闭 | 配置服务构建失败和必需服务发现失败向上抛出；可选插件服务记录 WARNING |
| `tools/rag_tool.py:_rag_search` | `except Exception` 返回普通文本 | Workflow 误以为检索成功、没有证据 | 抛出 `RAGRetrievalError`；RAG 未启用/未配置仍返回指导文本 |
| `rag/query_rewriter.py` | 改写失败只有普通 Warning | 降级不可识别 | 明确记录 `degraded` |
| `configuration.py` | 未校验 RAG 供应商、`rag_enabled`/`retrieval_mode` 冲突、预算和工具循环下界 | 配置声明开启但执行时被另一条件关闭 | 增加类型/范围/组合校验 |
| `budget_enabled=False` 及各限额 `None` | 正式默认无预算保护 | 完整报告可能因预算过小失败或无限消耗 | `config.yaml` 默认开启并设置上限；预检检查最小调用数和最终报告预留 |

## 2. 新的配置结构与有效优先级

```text
config.yaml           正式业务配置（唯一来源）
config.smoke.yaml     继承 config.yaml，只覆盖低成本测试字段
.env                  凭据、数据库连接、认证、部署限制
```

优先级：

1. 新建 Run 时加载 `config.yaml` 或 `config.smoke.yaml`。
2. `extends` 逐层合并（字典深合并，列表替换）；路径按所属配置文件解析。
3. `Configuration` 做类型、范围和互斥校验；未知字段报错。
4. `frozen_config()` 生成非敏感快照，`RunClient.create()` 持久化并计算摘要。
5. Executor、Agent、工具和子 Workflow 只读取该快照；不重新读取配置文件或环境业务字段。
6. `por doctor`/`por run`/`por smoke` 复用同一套预检；CLI 不提供模型、RAG、模式覆盖参数。

敏感字段 `dbhub_dsn`、`feishu_app_secret`、`rag_milvus_token` 不进入快照；
运行期分别从 `DBHUB_DSN`、`FEISHU_APP_SECRET`、`RAG_MILVUS_TOKEN` 读取。

## 3. 正式配置默认开启的业务功能

- 四个业务 Agent：`public_signal`、`internal_knowledge`、`risk_assessment`、`response_strategy`
- Plan/Brief 生成、澄清、Brief 人工审批与编辑、AI Feedback 修改
- Research Review、Gap Detection、定向补研（`max_research_rounds: 2`）
- Risk Assessment、Response Strategy、最终综合报告（`report_mode: synthesis`）
- 本地 RAG、混合检索、RAG 查询改写、上下文滚动压缩
- 用户问题图片理解与 RAG OCR/Vision 多模态
- Tavily 联网检索
- `social-research` 插件、三个平台 Skill 与 Apify MCP
- 预算保护：120 次模型调用、120 次工具调用、60 次搜索、400 万输入/40 万输出 token，
  保留最终报告调用
- 每阶段 1800 秒超时（短于 DBOS `RUN_TIMEOUT_SECONDS=3600`）
- Langfuse/OTel 默认开启（`tracing_enabled: true`；缺少密钥时预检 WARNING 并安全关闭）

非核心 MCP（DBHub、飞书、MarkItDown）默认关闭，只有显式开启才检查依赖和凭据。

## 4. 测试配置可关闭的功能

`config.smoke.yaml` 只覆盖：

- `max_research_rounds: 1`、`max_react_tool_calls: 2`
- Brief 审批与澄清关闭
- RAG、查询改写、多模态、OCR、Vision 关闭
- `social-research` 插件关闭
- `internal_knowledge` 角色关闭（与 RAG 关闭保持一致）
- 预算收紧为 24 次模型调用、16 次工具调用、8 次搜索
- 追踪关闭

仍使用真实的 `RunClient → DBOSClient → PostgreSQL/DBOS Queue → Executor → POR Workflow
→ Agents SDK`，没有模拟工作流。

## 5. CLI 使用方式

```powershell
por doctor
por run "调研某品牌近期舆情" --key request-20261119-001
por smoke "调研某品牌近期舆情"
por status <run-id>
por inspect <run-id>
por approve <run-id>
por approve <run-id> --decision reject
por approve <run-id> --decision edit --brief-file new-brief.md
por approve <run-id> --decision revise --feedback "补充竞品对比"
por clarify <run-id> --answers '{"brand": "Brand A"}'
por clarify <run-id> --skip
por report <run-id> --output report.md
por retry <run-id>
```

`run` 固定读取 `config.yaml`，`smoke` 固定读取 `config.smoke.yaml`。
`approve`/`clarify` 从当前 Run 读取版本并保留版本绑定；`retry` 使用 Run 的
`state_version` 和新的幂等键。`inspect` 显示失败阶段、错误类型、配置模型、模型调用数、
工具调用/失败数和失败 Workflow。

## 6. 启动前检查覆盖范围

`por doctor`、`por run`、`por smoke` 共享 `preflight.py`：

- 配置文件存在、YAML 合法、未知字段拒绝
- 模型引用、供应商、SDK/LangChain 路径、凭据和适配器包
- Tavily 界面与凭据
- RAG 知识路径、可索引文件、Embedding/VectorStore/Reranker/BM25/OCR 依赖
- 插件清单、Skill、MCP 声明和必需环境变量
- DBHub/Feishu/MarkItDown 的显式启用依赖
- 预算下界、最终报告预留、阶段超时与 DBOS 运行超时关系
- RAG/角色/搜索/插件互斥组合
- PostgreSQL 连通性、`por_domain.policy`、DBOS 原生目录和未完成旧 journal
- 敏感值是否误写入 `config.yaml`

输出级别为 `ERROR`（不启动）、`WARNING`（继续但提示）、`READY`。
Executor 存活在 DBOS 3.3 中没有公开 API，因此以 WARNING 提示先启动 Executor。
不打印任何 API Key、令牌或数据库密码。

## 7. 自动化测试结果

```text
pytest: 894 passed, 2 skipped, 187 deselected
Ruff (E,F,I; E501 ignored): All checks passed
compileall src/open_deep_research: passed
scripts/phase5_import_smoke.py: 30 production imports passed
```

新增/更新覆盖：

- `tests/test_config_system.py`：正式/测试配置加载、全字段覆盖、未知字段拒绝、
  环境不覆盖、快照排除敏感字段、摘要稳定性、模型路由、RAG 映射、预检、CLI 全命令、
  RAG 失败暴露
- `tests/test_web_research.py`：Web 请求不再覆盖正式配置
- `tests/test_plugins.py`：插件/必需 MCP 依赖缺失前失败、可选服务显式降级
- `tests/test_mcp_sdk_tools.py`：必需服务失败中止、可选服务不阻塞其他服务
- `tests/test_query_images.py`：视觉提示只来自配置，不受环境变量影响
- `tests/test_telemetry.py`：配置驱动的追踪开关

## 8. 真实端到端运行结果

未执行成功的真实研究。阻塞原因：

1. 当前环境没有 `DBOS_DATABASE_URL`，也没有运行中的 PostgreSQL；DBOS Executor 无法启动。
2. 正式配置启用 OCR 多模态，但本机没有 Tesseract 二进制；`por doctor` 按设计报 `ERROR`。
   `config.smoke.yaml` 已关闭多模态，因此冒烟配置的预检只剩数据库阻塞。
3. Langfuse 公钥/私钥未配置；正式配置的 `tracing_enabled: true` 会产生 WARNING，
   但不会阻止研究。

`por doctor --config config.smoke.yaml` 的静态检查全部通过（模型、Tavily、预算、超时、
插件、冲突），唯一错误是数据库不可用。没有使用 Mock 结果冒充真实研究成功。

## 9. 仍然存在的配置和运行风险

- **旧 Run 的摘要兼容性**：此前保存的是部分配置，摘要可能受旧环境变量影响。新 Run
  使用完整快照；旧 journal 应按既有版本策略在原发行版排空，不应跨本改动恢复。
- **模型/OCR 首次下载**：SentenceTransformers 与 BGE reranker 首次使用可能下载模型；
  预检只给 WARNING，未预先下载。
- **OCR 强制依赖**：正式配置启用 OCR 时缺少 Tesseract 会阻止启动；机器没有 OCR 能力时
  需要在 `config.yaml` 显式设置 `rag_multimodal_provider: disabled`。
- **Executor 存活不可静态验证**：DBOS 3.3 没有 Executor 心跳 API，预检只能检查数据库、
  策略和旧 journal，并提示先启动 Executor。
- **API 余额/配额**：静态检查无法判断；未执行付费探测。首次真实运行应先用
  `por smoke` 并观察 `por status`/`por inspect`。
- **配置快照包含绝对路径**：`plugins_dir`、RAG 路径在加载时按配置文件目录解析，
  因此快照是单机可复现的；跨主机恢复需要相同路径或重新冻结。
- **Web 次要入口**：Web 仍接受旧表单字段以兼容现有前端，但这些字段不会生效；
  旧前端显示的模型/RAG 选择不再代表实际运行配置。
- **Open Agent Platform 配置密钥**：`GET_API_KEYS_FROM_CONFIG=true` 的
  `configurable.apiKeys` 不再进入 Run 快照（它属于敏感值）。本地/CLI 部署应使用
  `.env`；OAP 需要单独的密钥注入机制，不能依赖 Run 快照。
