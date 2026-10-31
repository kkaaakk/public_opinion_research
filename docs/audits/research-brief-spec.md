# Research Brief Spec 改造与验证

日期：2026-10-31（Asia/Shanghai）。基线为当时最新 main **338c480e49881852deb0089b32ca0b1ce4124e41**，分支 `codex/research-brief-spec-workflow`。原工作区处于 `codex/langfuse-otel` 且有未提交修改，因此本次使用独立 worktree。没有搬运那些修改、改写 main、合并 PR、编辑 `.env`/凭据/`CLAUDE.md`、清理其他 worktree 或 stash。

完整前后流程图、Plan/Brief/API 合同与升级规则见 [执行架构](../architecture/research-brief.md)。下面记录实际源码审查、取舍与验收证据，不以历史 Phase 文档代替当前代码。

## 实际审查与实现

旧链路的 `por.plan` 依次运行图片处理、澄清、Brief、章节规划。澄清返回 `__end__`，顶层正常返回；Web 准入还强制关闭澄清。`por.research` 保留双 Producer、Review/Gap、Risk→Strategy，但默认还执行章节、引言/结论及编译链。四角色的真实模型/工具循环已由 SDK Runner 执行，初始任务已携带 `workflow.brief`，无须增加 Agent 或 `read_brief` 工具。

本次改动如下：

| 边界 | 实现 |
| --- | --- |
| Plan | `intake.plan_research`：同一个严格 `PlanDecision` 决定 `ready` 或 `ask_user`；输入充分只调用一次；回答或跳过后再调用一次并禁止重复提问。 |
| Brief | `workflow/brief.py`：Markdown 正文、展示问题 Schema、校验与版本哈希。正文不拆成 objective/scope/questions 等重复字段。 |
| 持久等待 | `por.run` 通过 DBOS `recv` 等待；`por.plan` 每个执行段结束即释放 BUSINESS 配额；恢复原 Run，不创建新 Run。 |
| 编辑/审批 | 复用请求行锁、既有 approvals 回执和事务消息；编辑先产生新版本，再独立确认该版本。重复 approve/reject 保持既有 409 合同，重复相同澄清/编辑可确认已提交回执。 |
| 角色上下文 | `_build_business_agent_assignment` 注入同版 Markdown/版本，作为 user 任务输入；System/AgentSpec/权限不变；原 Harness 保留初始输入头并压缩后续完整响应单元。 |
| Review | 原 Graph/Working Context 及轮次、任务去重、确定性合并保留；提示词明确对照 Brief 目标、问题和完成标准检查实际证据。 |
| 默认报告 | `generate_final_report` 单次有界综合生成，纳入四角色报告、Graph、Review 缺口及完整 Brief；先提交报告 artifact，再进入原 `compile_final_report` 外部记忆边界。 |
| 长报告 | 显式 `report_mode=sections` 复用原章节规划、并行写作、部分成果和降级代码；章节计划在 Brief 冻结后生成。只增加一个配置字段和一个业务分支。 |
| Web | Studio 与 Classic 均可澄清/跳过及编辑 Brief；Studio 支持安全 Markdown 预览、导出、未保存编辑禁止审批、研究后折叠已锁定规范。 |

实际改变生产调度的只有原有 Workflow/Step/Queue。RunFacts 没有新表、队列、租约或状态机；RAG、Research Graph、长期记忆、History 的数据所有权没有转移。保留两个已有 intake helper 的兼容导出，因为冻结参考 runner、图片合同及公开 `deep_researcher` 导出仍有消费者；它们已退出生产 `OPERATIONS`，没有失败 fallback。

## Brief 长度和持久化

- 常规 600–1200 中文字符是 prompt 软目标；2500 是 `estimate_text_tokens` 的应用估算硬界，不是特定模型 tokenizer 的精确计数。
- 超限生成最多再精简一次；精简的结构化重试也限制为一次实际尝试。仍超限、结构非法或预算不足时明确失败，不截断规范、不改为原始查询伪成功。所有实际调用保留预算与 Trace 记账。
- Markdown 仍是 `workflow.brief` 字符串。DBOS Step artifact、原生 `plan` 事件和 research 输入保留其快照，没有 `.md` 数据库。
- revision 从 1 开始；content_hash 为规范化 UTF-8 Markdown 的 SHA256；version 绑定 Run ID、revision 和 hash。编辑回旧文本也产生不同 version。
- 研究输入冻结 `brief_version`、`brief_hash`、`brief_revision`，原 reducer 显式保留这些管理元数据。角色及补查不能改写 Brief；压缩后每次 SDK 请求仍含完整 Brief，恰好一次。
- 报告模式和审批选择在准入时冻结，Workflow 分支读取已接受的数据；后续环境默认值变化不能改变待恢复 Journal 的报告拓扑。

## Review 中修复的可靠性问题

1. **等待快照竞态**：旧读取顺序先读 plan、后读 phase，跨提交时可能返回 awaiting_approval 和空 plan。现在先读取 phase，再读取其之前已提交的 payload；新增确定性 PostgreSQL 合同模拟跨提交读取。
2. **SDK 尾事件丢失**：原有终态 manifest 只要求事件类型齐全，同毫秒消息乱序可能让 History/done 先于最后的 SDK 观测。现在已提交 SDK 事件 ID 随 unit artifact/error 保存，原 relay 等清单全部交付才发布终态回执。它是交付元数据，不授予执行/重放权限。跨 retry generation 保留已交付 ID；没有为了补齐轨迹重跑角色或工具。真实合同强制让最后一个 Agent 完成事件晚于 archive，断言提前无 done、最终四个 invocation 完整。
3. **冻结执行选择**：Review 移除了 Workflow 分支对可变化环境默认值的依赖；真实合同在审批等待时改变 REPORT_MODE，仍执行原综合报告拓扑。
4. **澄清消息确定性**：回答/跳过消息 ID 绑定挑战版本，问题与答案使用规范 JSON；Workflow replay 不再随机生成消息 ID 或依赖字典插入顺序。回答、跳过及真实进程恢复的 3 项合同通过，明确禁止 Step 外的 Workflow 生成消息 UUID。
5. **编辑后过期**：复用的 expiry helper 原先遇到任何旧交互回执都会停止标记过期。现在只替换与当前 Brief 版本不同的旧回执，已消费的本版本决定保持原保护；新增 PostgreSQL 合同验证旧编辑回执不能阻止新版本过期，重复过期写入幂等。没有新增表或执行权限。

未知工具/记忆效果仍隔离，只有 Finalizer 消费绑定失败代次的 grant；旧 key、丢 ACK、取消、未清算效果不能授权下一次失败。报告生成是安全模型 Step，报告后的外部记忆写入继续使用原不确定效果保护。

应用版本为 **dbos-brief-spec-v1**，SDK/runtime 分别为 **0.24.0 / sdk-phase5-v1**。`por.run`、`por.plan`、`por.research`、`por.events` 的命令序列发生变化；旧 `dbos-phase5-studio-v1` 及更早 Journal 必须由匹配发行版/Executor 排空。回滚同样保留本版 Executor 完成本版任务，禁止复制未知 Agent/tool 单元、跨版接管或跨引擎重投。没有执行生产切换。

## 验证结果

下表是实际执行结果；中间失败保留在下文，不将 Mock 推理冒充真实外部服务或将失败运行称为通过。

| 验证 | 最终结果 |
| --- | --- |
| 根锁离线同步 | 通过；依赖锁未改变 |
| main 全量离线基线 | **1028 passed / 2 skipped / 108 deselected，272.10s**；多出的 1 项 deselected 是仅为对比加入快照的原生测量夹具 |
| 分支全量离线 | **1046 passed / 2 skipped / 121 deselected，216.04s**；集成测试另跑 |
| 原生 DBOS/PG/Neo4j/恢复/OTel/History | **114 个不同合同通过 / 0 skipped / 0 xfailed**。完整主运行 108 passed / 3 failed / 25 deselected（1729.47s）；三项精确期望/注入边界修正后，19 项补验全部通过（283.11s），包含全部原失败和新增版本/过期/记忆边界合同。原失败记录保留，不将该单次主运行称为全绿 |
| 冻结报告模式新增真实合同 | 1 passed；改变环境默认值不改变接受的拓扑 |
| Studio 单元测试 | 48 passed；包含原 42 项和新交互/投影合同 |
| Classic Node 控制合同 | 7 passed |
| 真实浏览器（Edge） | **7 passed，2.2min**；实际 FastAPI→DBOS→SDK→Graph/History，桌面 1600×1000、手机 390×844 |
| Ruff E/F/I、compileall、TS、ESLint、构建 | 通过；主要改动模块格式检查通过。Vite 保留 >500kB chunk 提示，未提高警告阈值；全量前端 Prettier 未通过（main 基线 38 个文件），本次触及的前端文件均通过，未重排无关文件 |
| `uv pip check` | **未通过**：继承的 markitdown-mcp 要求 mcp~=1.8.0，锁为 1.23.0；未修改 override、移除消费者或掩盖冲突 |

离线推理均由官方 SDK ScriptedModel/真实 LangChain callback 夹具提供。原生合同按同名用例的最新补验结果逐一核对，114 项均有通过证据；不累计重复执行数。原生矩阵实际连接本次独立 PostgreSQL 17.6 / Neo4j 5.26.31，运行真实 DBOS Journal、Queue、消息、SSE、SDK Runner、Graph 和归档；进程与物理故障合同仅操作本次创建的测试进程/容器。OTel/OTLP 使用本地受控接收端，没有连接生产 Langfuse。付费模型/API 调用为 0。

复现（在根 `.venv`，dotenv/tracing 由测试 fixture 关闭）：

```powershell
uv sync --extra dev --locked --python 3.11
.venv/Scripts/python.exe -m pytest -q --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
$env:TEST_DBOS_POSTGRES_CONTAINER = 'por-brief-spec-postgres-1'
$env:TEST_DBOS_NEO4J_CONTAINER = 'por-brief-spec-neo4j-test-1'
.venv/Scripts/python.exe -m pytest -m integration tests/test_brief_dbos.py tests/test_dbos_contracts.py tests/test_dbos_process.py tests/test_dbos_retry.py tests/test_telemetry_dbos.py tests/test_web_history.py tests/test_p1_fixes.py --disable-socket --allow-unix-socket --allow-hosts=127.0.0.1,::1
node --test tests/durable_frontend.test.cjs
cd frontend
npm ci
npm test
npm run lint
npm run build
# 已安装 Edge 时可复用，否则按现有 Playwright 配置安装 Chromium。
$env:STUDIO_BROWSER_CHANNEL = 'msedge'
npm run test:e2e
```

Windows 的子进程 kill 和缓存路径合同需要测试进程具有对应权限；不能以沙箱写入/终止失败推断业务断言成功。POSIX 信号的两个既有平台用例在 Windows 跳过，CI 的 Linux 选择与原门禁保持。

## 可复现调用/成本比较

`tests/brief_stage_profile.py --samples 20 --output .tmp/brief-stage-profile.json` 调用实际旧/新业务服务，使用相同的 7 input / 3 output synthetic tokens 和同一三章节规范。它使用测试环境与非 loopback socket guard。统计状态中的真实预算 patch，避免遗漏原章节 helper 内部嵌套 capture。

| 阶段/条件 | 旧链路模型请求 | 新默认请求 | synthetic input/output/total |
| --- | --- | --- | --- |
| 明确输入，启用澄清，含旧章节规划 | 3 | 1 | 21/9/30 → 7/3/10 |
| 明确关闭澄清，含旧章节规划 | 2 | 1 | 一条统一 Plan 路径，无原始查询 fallback |
| 需要澄清 | 第一次 1 次后 `__end__`；无同 Run 恢复 | 第一次 1 次、回答或跳过后 1 次 | 新合同完成同 Run；旧链路不可视为完整的对等交付 |
| 三章节报告（研究正文、引言、结论） | 3 | 1 | 21/9/30 → 7/3/10 |

主原生夹具只有一个研究章节，旧报告本来就只需一次写作，因此该夹具只证明 Plan 的两次请求减少，不能声称其报告请求也减少。初始夹具包含历史预算 **7 calls / 70 input / 7 output / 77 total**，比较时必须扣除：

| 原生单章节夹具 | main | 新默认 |
| --- | --- | --- |
| 本次实际模型调用（扣除初始 7） | 21 | 19 |
| 本次 input/output/total（扣除初始预算） | 165/69/234 | 151/63/214 |
| Run 累计 model_calls/total | 28/311 | 26/291 |
| Producer 工具效果 | 2 | 2 |
| 角色、证据/溯源、顺序 | 四角色、双 Producer 并行、Review/Gap、Risk→Strategy | 同样合同 |

原生 `tests/test_brief_profile.py` 在其他 DB/浏览器验收结束后，串行测量每版 5 个样本；口径为接受 Run 到 History + 终态事件交付。两版使用相同请求、四角色、工具、初始预算、根锁、真实 DBOS/PG、内存 Research Graph 和真实 Neo4j History，推理保持同一受控模型。

| 端到端本地模拟，N=5/版 | main | 新默认 |
| --- | --- | --- |
| p50 | 7.68s | 7.50s |
| p95（inclusive 分位估计） | 8.80s | 8.54s |

样本量小且无供应商网络/推理延迟，差异接近本地调度波动，不据此宣称真实端到端提速或统计显著性。20 样本阶段调度 p50/p95：Plan 12.23/13.87ms → 5.37/6.04ms；三章节报告 4.79/7.79ms → 2.01/2.31ms。同样只解释 Python 调用链，不能外推到线上。

**这些数据证明执行链及记账变化，不能证明真实 provider Token 节省、美元成本、生成质量或生产 p50/p95。** 两版推理都是可控回复，输入长度不参与 synthetic usage。真实模型报告完整性、结论准确性、引用真实性尚需带代表性 Golden Set 的独立在线验收；本次未花费模型额度。

## 维护成本与设计取舍

相对 main，仅本次改变的 15 个生产 Python 模块按同口径统计：**7395 → 7990 物理行（+595）**，AST if/for/while/try/match 控制节点 **442 → 495**，新增 **1 个** Brief 校验模块。默认 Plan/报告的业务 Step 从 4+3 降至 2+2，四角色与 Review/Gap 不变。代码总量增长来自真正可恢复的澄清、编辑/CAS/版本校验、交付竞态修复和并存的两套 Web 交互；没有宣称净删代码。

保留 sections 高级模式是明确取舍：现有并行章节、长报告和部分完成能力有现成代码及消费者，用一处显式分支可保留；移除它会牺牲功能和已有合同。历史 helper/字符串 Brief/归档读取不参与新执行链，也不作为 SDK 失败 fallback。

未解决的既有/外部事项：MarkItDown/MCP 依赖冲突；生产旧任务实际排空与部署；远程 MCP/provider 故障和真实模型质量；跨主机自动接管与跨服务 exactly-once 不在本次承诺范围。单操作员认证、预算的既有并行工程边界保持。没有新增 Spec 平台、文档库、Agent、队列或自研调度器。

## 迭代失败和审查记录

最初共享 `.venv` 缺失锁中 instrumentation，出现 4 failed/37 setup errors；改用本 worktree 独立锁环境，没有修改用户共享环境。基线初次启动目录导致一项相对路径架构断言失败，改正确目录复测；最终全量结果另记录。未靠删测试或修改选择规则消除失败。

首轮完整原生矩阵 **78 passed / 7 failed**：暴露上述两个读取/交付竞态，以及旧调用数、锁定元数据和冻结参考的精确期望差异。修复后相关 **17 项真实定向合同全部通过**。参考比较使用相同已冻结 Brief，精确计入新 Plan/章节计划成本，继续严格比较四角色、完整长报告、Workflow 与总预算；没有要求新默认报告伪装成旧章节状态。

一组混合测试调整被自动审批审查拒绝，认为会削弱重复审批断言。替代方案保留原有重复审批 409，新增相应强断言；数值期望以真实逐阶段计费与 5 次一致测量为依据。另一次命令因自动审批服务使用限额未执行，之后在工作区写权限恢复后继续。

物理故障测试初次与其他验收并行导致测试服务干扰，已恢复专用容器并改串行复验。前端曾发现报告定位与新增 Markdown 预览冲突、完成状态误等旧可选步骤；改为精确报告定位、按实际当前路径投影，并补充回归，保留原 9 节点/可读性/报告内容断言。普通沙箱下缓存/测试进程终止合同也实际失败，使用正确权限复验；失败运行未计为通过。截图、Trace、数据库和临时日志留在忽略/本地目录，不进入提交。

最终自审未留未修复 P0/P1。报告文本块通过 LangChain Core 的 `.text` 规范化；供应商 `finish_reason=length/max_tokens` 或 `stop_reason=max_tokens` 均明确失败且保留费用，不把截断响应当完整报告。对应字符串/文本块与失败计费合同保留。PR 保持 Draft/Open，不合并；远端 CI 状态以最新提交 checks 为准。

最终 CI 口径主运行还发现 3 项旧期望：审批后的 locked 标记、Risk 失败前少两次 Plan 调用、以及生成/外部记忆拆分后旧失败注入点。审批重启独立复验通过；History 合同拆成“纯综合生成失败，无报告且可安全重试”和“记忆结果未知，有已提交报告且禁止重试”，均严格校验四角色、工具效果、费用和归档。19 项补验覆盖全部原失败与新增合同；114 个不同用例按最新同名结果核对，没有以重复运行数扩大覆盖量。最终澄清消息确定性另有 3 项真实复验，关闭澄清仍一次生成 Brief 的正向合同也通过。
