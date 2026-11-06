# DBOSRunner 统一改造验收记录

2026-11-05 开始，2026-11-06 验收，基线 `origin/main b22428ac`（交付前再次 fetch 核对）。独立分支 `codex/dbosrunner-unification`。详细设计、前后架构图、8 个 Workflow、Step 映射和面试阅读要点见[架构说明](../architecture/dbosrunner-unification.md)。

本页区分真实执行结果、历史中间失败和未覆盖的生产事项。没有调用付费模型，没有修改用户原目录的 59 个未提交文件、`.env`、`CLAUDE.md`、无关 worktree、分支或 stash。

## 审计结论与归属

| 对象 | 最新 main 实际情况 | 本次接管或保留 |
| --- | --- | --- |
| Workflow / Step / Queue | 6 个 Workflow；role 进入整段 business_step；共享 business/control/archive/recovery 队列 | 8 个 Workflow，原地修改 por.role；DBOS journal/status 继续为唯一执行权威 |
| Agent 模型/工具 | 原 Runner.run；工具有许可/intent，但无每次 native 输出检查点 | 官方 DBOSRunner 模型 Step + 显式 Tool Step；官方 turnstile 保持并发调用启动顺序 |
| Plan / Review / Report | LangChain bounded services，整个阶段 checkpoint | 复杂阶段 Workflow + 动态调用 Step；结构化失败也保留真实费用及重放分类 |
| Context Harness | Python collector、Working Context、receipt、summary 可变状态 | 有限 Context/settlement Step 保存数据；从 native Tool 结果和 SDK 输入重建其余状态 |
| RunFacts 结果恢复 | 完整 Artifact 与 native 结果重复，acquire 可直接返回 Artifact 跳过业务 | 删除该恢复路径；只保留实际报告、清算、交付和费用投影 |
| 外部效果与授权 | cancel、permit、fencing、工具指纹、未知结果、资源占位 | 保留并收紧；原准入 permit 与已完成的 native 结果用途不同 |
| 数据归属 | Research Graph、RAG KG、Memory、History 分离 | 保留。没有合并图/记忆/档案；DBOS journal 不充当长期知识库 |
| 可观测性和协议 | 原 SSE manifest、History 补偿、Langfuse/OTel、CLI/审批 | 保留；native 追踪 API 仍明确不返回 input/output/error 原文 |

官方 wheel 依赖下限为 DBOS>=2.10 / Agents SDK>=0.7；实际锁定 **DBOS3.3.0 / SDK0.24.0 / dbos-openai-agents0.3.0**，不能仅凭版本下限推断行为。读取官方 runner 实现并用真正 PostgreSQL journal、进程树 kill/restart 证明模型/工具结果复用。SDK源码未修改。

## 已完成的基线与 POC

| 检查 | 真实结果 |
| --- | --- |
| main 根锁安装 | 297 packages；DBOS 3.3.0 / SDK 0.24.0 |
| main 完整离线基线 | 1078 passed、2 skipped、125 deselected；336.19s |
| main 原生 Studio/SDK 合同 | 1 passed；30.40s |
| 官方 Runner POC | 模型/工具已提交后分别 kill/restart，结果复用；2 passed |
| 正式角色故障矩阵（第一轮冻结） | 5 passed；206.29s，含 POC 与角色模型/工具/未知证据批次 |
| 前端单元/构建 | 48 passed；build 通过（现有 chunk >500kB 提示保留） |
| 原 Node 浏览器控制器 | 7 passed（从根目录运行） |

## 真实失败及修复过程

不得把中间轮次当作最终通过：

- 首轮 POC 使用错误的 DBOS 构造/查询参数，2 failed；按 3.3.0 公开源码修正后通过。
- 第一次广泛 native 矩阵 15 failed /72 passed；包括已退役的完整 Artifact 白盒合同、工具 spy 未接收新增 usage 参数，也包括真实取消、预算、消息 UUID 和追踪回归。保留业务/费用/清算断言，修复实现后复测。
- 默认 Chromium 不存在，7 个浏览器用例未能启动。改用机器已有 Chrome，未修改断言；中间 Chrome 轮次 6 passed /1 failed，暴露 Context Step 的 DBOS 原生取消清算问题。
- 独立审查报告 6 个 P1 及额外恢复窗口：Context 暂存费用未进入 SDK 准入、受控预算停止回执/重放、provider 失败重试、失败费用重复、DBOS 取消异常分类、初始化读取清算保护，以及 nested leaf rewind 的 Finalizer 崩溃窗口。后续额外费用、取消和未知批次窗口均实际修复并回归，见下表与源码。
- 结束回执变成精确幂等 ACK 后，原“重复 finished 必须报错”的白盒断言已更新为精确 ACK 不重新授权；started、不同费用及旧 permit 仍拒绝。另把 lost-receipt 注入点限定为工具，避免新增模型账单回执使原工具故障测试提前停止于 Plan。未删除或放宽未知效果、费用、证据、权限断言。

## 验证结果

本机按业务、进程、重试、追踪与新增故障窗口分组验收。CI选择集收集为 **141 个 integration 合同**（另外25个所选文件内的offline用例由offline job覆盖），最后提交由CI再次整集执行。下面逐行列真实本机输出，不将多次运行重复用例相加成一个“通过数”；最终同一提交的CI状态见PR checks。

| 验证 | 已实际记录结果 |
| --- | --- |
| 完整离线，socket guard / 禁 dotenv 与 tracing | **1087 passed /2 skipped /149 deselected**，231.66s；新增 integration 随后独立运行 |
| SDK/普通模型/工具收据提交前失败及提交后 ACK 丢失 | **6 passed /76.31s**；实际调用、26次预算/291 token、两条 tool returned 事件保持一次；旧许可和不同账单拒绝 |
| 多失败代次最终报告预算预留 | **1 passed /19.55s**；第三次重试在实际 provider 之前拒绝，费用保持已发生的两次 |
| 正式 Context/Tool/Model 故障恢复 | **4 passed /208.81s**；含恢复再次 kill、未知批次与缓存 Generic Context 错误；LC物理调用见证不增加 |
| 新版报告章节及空响应的安全重试 | **2 passed /36.56s**；只重做失败章节/最后无效响应，先前章节、阶段和四角色复用 |
| 旧 Journal / 预留及许可前取消 RPC 丢失 | **3 passed /46.19s**；旧 journal 保留且拒绝新执行 |
| prior permit/NULL 未决事实与精确 ACK | **2 passed /9.35s**；新外部清算不能绕过旧未决结果 |
| 实际 PostgreSQL 停启 | **1 passed /36.06s**；API503、权限失败不被稍后成功掩盖，无额外工具效果 |
| 完整 OTel/native trace 合同 | **8 passed /186.81s**；四角色层级、SDK6/LC13、真实 token、operation/effect 状态、OTLP关联及进程恢复 |
| 正式 Runner/失败费用/Finalizer 重启与 retry grant 组 | **22 passed /711.16s**，包含新真实进程与原 retry 合同；最终相同代码检查以 PR checks 为准 |
| 原生业务尾部与Brief完整状态基准组 | **22 passed /233.92s**；含完整State/证据基准、审批、quota公平性、Vision费用与取消、原子汇合 |
| 控制失败及模型/工具取消清算 | **3 passed /30.30s**；已知未执行故障清算与未知效果停止分别处理 |
| History/SSE独立归档、失败/取消与原有并发准入 | **8 passed /122.55s** |
| 前端 React 单元 / 构建 / 原 Node 控制器 | **48 passed** / build通过 / **7 passed** |
| 最终 Chrome 完整 HTTP→DBOS→SDK→Graph | **7 passed /2.0m**；1600×1000及390×844，pageerror断言、审批/澄清/取消/重试/断线重连/证据/报告/History |
| Ruff E/F/I、compileall、diff whitespace | 通过，未降低规则 |
| 依赖一致性 | **仍失败1项继承冲突**：markitdown-mcp要求mcp~=1.8.0，根锁1.23.0；未新增冲突 |

固定真实服务为本次专用 `por-dbosrunner-postgres-1`（15434）、`por-dbosrunner-neo4j-test-1`（17689）和 profile PostgreSQL（15435）；未停启用户已有的 MySQL/Neo4j 服务。测试创建/删除仅其 UUID `por_test_*` 数据库；外部模型/工具替身不代表供应商验收。PG与Neo4j停启使用精确容器ID、标签/挂载检查；进程 kill 仅针对夹具创建的进程树。跨日恢复时专用容器已停止，先恢复后重测；连接超时没有被当成功。

浏览器没有 Browser 插件技能，按现有 Playwright套件使用本机已有 Chrome。源码前端未修改；截图在 ignored `.tmp/studio-*.png`，CI上传 `studio-browser-evidence`。首个Chromium缺失轮次和中间取消失败均保留为失败记录。

## 性能与实际调用数

同一受控 workload、PostgreSQL17.6、隔离Neo4j，每个场景每种并发各 **6** 个完整研究，结束条件为 History saved + 终态事件完整交付。两版使用同一新 profile 脚本；main 从 b22428ac 导出，PYTHONPATH 明确指向其源码。统计 API 操作不计入每个 Run 的结束时间。`pg_stat_statements` / `track_io_timing` 提供真实 DML、WAL、SQL执行与I/O；DML为执行语句数而非成功修改行数。以下为本机小样本，不是生产 SLA，也不是 provider 质量/真实账单测试。

| 场景 | 并发 | p50秒 main→本次 | p95秒 main→本次 | 吞吐Run/s main→本次 | DML/Run main→本次 | WAL MiB/Run main→本次 |
| --- | --- | --- | --- | --- | --- | --- |
| 综合/无延迟 | 1 | 7.96→7.73 | 9.22→8.53 | .122→.127 | 1431→1620 | .90→1.09 |
| 综合/无延迟 | 2 | 9.25→9.87 | 9.42→9.97 | .216→.205 | 1372→1570 | .92→1.10 |
| 综合/SDK每次等待1秒 | 1 | 9.70→9.56 | 10.55→10.44 | .105→.103 | 1462→1659 | .91→1.09 |
| 综合/SDK每次等待1秒 | 2 | 9.54→11.16 | 10.15→12.05 | .206→.173 | 1382→1594 | .91→1.10 |
| 3章节/每章等待1秒 | 1 | 8.08→9.72 | 8.74→11.49 | .122→.102 | 1503→1746 | .94→1.15 |
| 3章节/每章等待1秒 | 2 | 9.82→12.05 | 10.20→12.44 | .202→.165 | 1448→1684 | .95→1.32 |

每组6个综合研究两版均为 **SDK36 +普通LC78 +工具12**；章节组均为 **SDK36 +LC96 +工具12**。正常负载没有额外实际模型或工具调用。API预算含夹具保存的每Run7次历史费用，不能把该起始预算误认作本次物理调用。故障见证另检查已保存Model/Tool调用一次，未知Context恢复后LC也不增加。

PostgreSQL statement执行毫秒/Run：无延迟单任务 **191.8→200.9**，并发2 **203.4→248.7**；SDK等待场景 **200.2→193.6 /246.6→221.4**；章节场景 **187.9→228.7 /227.0→290.6**。journal条目约由综合 **617–625→683–693**、章节 **651–652→728–730**。实际I/O/事务计数保留在ignored profile JSON。

**P2性能代价没有被消除或隐藏。** 默认综合单任务p50在噪声范围内，新增回执/许可与native命令增加约13–16%DML和约20%以上WAL；部分并发场景延迟/吞吐退化。SDK等待并发2 p50增加约17%，章节p50增加约20–23%。模型调用数未变；章节由原并行3次1秒调用改为顺序3次，直接增加约2秒等待。其余为新增持久化/子Workflow/确认与轮询开销，小样本不能仅凭SQL总耗时作严格因果归因。本PR优先保证成功章节保留与未知效果安全，未为降低开销删除保护；上线前应按真实任务长度/并发与保留期做容量验收。

最新冷进程恢复样本：模型checkpoint后（恢复中再次kill）完成 **23.60s**、工具checkpoint后完成 **21.70s**，未知Context停止 **25.07s**，Generic Context错误缓存后停止 **29.20s**；后两者没有增加LC物理调用。包含Windows新解释器/Executor启动；样本太少，不报告生产恢复p95或宣称跨主机自动HA。重启记录保留在 ignored JSONL。

## 数据、依赖与代码量

- 自有业务表 **6→6**，不建通用LLM检查点表；增加 `operations.usage`、`admission_permit` 和 `operations(unit)` 索引，IF NOT EXISTS增量初始化，旧数据保留。vendor `por_dbos` 仅用公开迁移/目录API。
- 根锁 package entries **297→298**，新增唯一包 `dbos-openai-agents==0.3.0`，DBOS/SDK版本不变。没有引入LangGraph、队列中间件或新 Agent Loop。
- 生产Python文件 **117→119**；非空非纯注释行 **28335→29526（+1191）**，不是净减少源码。新增主要为显式SDK clone绑定、状态快照/恢复、原生模型边界、费用和取消保护；删除了Artifact结果恢复而非删除必要业务安全。
- 自有静态 Step装饰器 **11→10**（退出整段business_step）；新增动态 `DBOS.run_step` 与官方模型Step，因此静态装饰器数量不能代表恢复粒度。
- 具体前后定义、调用入口、文件行号及统计见 [inventory](dbosrunner-inventory.json)。统计口径包含docstring，不包含空行或纯注释。

## 独立审查与风险

独立只读审查按P0/P1/P2进行，没有以测试全绿替代源码审查。发现并修复预算暂存、正常预算停止、provider重试、失败费用翻倍、native取消/ConflictID、初始化读取、叶子rewind ACK丢失、safe retry截断章节/校验响应、结束回执费用与未知Context新epoch清算等窗口。最终源码复核未发现剩余可操作P0/P1；P2为上述性能/保留期成本及生产实测边界。完整最后提交结果仍以实际分组和PR checks为准。

DBOS不为供应商/API、Graph/Memory提供跨系统exactly-once。Context内部抽取/更新/压缩仍以有限Context Step为粒度，不声称每个内部模型调用都有独立检查点；中断按未知结果停止。没有运行真实付费模型、生产数据库盘点/排空、真实MySQL或Langfuse SaaS UI验收；OTel/本机OTLP传输已测试。供应商失败未报告usage时只能保留已知尝试/用量，未知部分需权威账单核验，不能推测费用。SDK Session仅作检查投影；RAG索引/长期Memory仍归独立业务服务。

发布必须先停旧准入并在匹配旧发行版完成/取消/清算 pending、paused、unknown、archive-pending，备份两个PG schema和业务数据，再初始化、运行 `--migration-check`。没有将生产已排空写成测试结论，没有删除未完成生产状态或以重建数据库掩盖不兼容。

## 复现

沿用 AGENTS.md 根锁/根 .venv 和 socket guard。`scripts/dbosrunner_inventory.py --baseline EXPORTED_MAIN --output docs/audits/dbosrunner-inventory.json` 可重新生成静态证据。原生CI执行包含 `test_brief_dbos /test_dbos_contracts /test_dbos_process /test_dbos_retry /test_telemetry_dbos /test_dbosrunner /test_dbosrunner_migration /test_dbosrunner_report /test_dbosrunner_receipts`。

性能另运行 `tests/test_dbosrunner_profile.py`，设置 `PROFILE_SQL_STATS=1`、`PROFILE_REPORT_MODE=synthesis|sections`、`PROFILE_PROVIDER_DELAY=0|1`、`PROFILE_CHAPTER_DELAY=0|1`，用 `RUNNER_PROFILE_OUTPUT` 写ignored JSON。profile PostgreSQL启动参数为 `shared_preload_libraries=pg_stat_statements`、`track_io_timing=on`；只指向单独本地测试库。PR未提交原始日志、数据库、运行配置、缓存或截图。
