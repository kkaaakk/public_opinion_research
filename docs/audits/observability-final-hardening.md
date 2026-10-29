# 可观测性最终完善与边界审计

日期：2026-10-28，Asia/Shanghai。基线 main：
`9b479fa4fd4ee412305d81ddba3bf2ecb62b364c`（PR #33）。开始及发布前均复核 main；
三个分支独立从该 main 建立，没有堆叠到未合并 PR。原工作目录、未提交修改、其他
Worktree、分支、Stash、测试资产和 `.env` 均保留；未修改 `CLAUDE.md`，未部署生产。

**结论：观测和故障隔离基础已可用于本项目的有界研究与排障，但本轮不能认定整套体系
已达到默认四并发下的完整长期运行成熟度。** 单 Run 的正常/不可达开销门槛通过，真实
Langfuse 的 Generation、用量、脱敏和层级通过；四并发存在实测显著开销并未通过预设
容量门槛。不能以 UI 完整或零队列丢弃替代这一性能失败。生产云端、长时间大内容量、
HA、磁盘故障和真实 provider 费用仍未验证。

## 1. 分阶段交付及真实缺口

| 阶段 | 独立分支 / PR | 内容和状态 |
| --- | --- | --- |
| A | `codex/observability-langchain-generations` / [#34](https://github.com/kkaaakk/public_opinion_research/pull/34) | Core 逐请求 Generation；Draft、未合并 |
| B | `codex/observability-reliability` / [#35](https://github.com/kkaaakk/public_opinion_research/pull/35) | 原生业务视图、交付诊断和有界退出；Draft、未合并 |
| C | `codex/observability-benchmarks` / [#36](https://github.com/kkaaakk/public_opinion_research/pull/36) | 可重复基准及本报告；Draft、未合并 |

原有架构实际为单 Provider → LangfuseSpanProcessor/BatchSpanProcessor → 安全 Exporter，
DBOS 原生 Workflow/Step、OI Agents 和业务边界共用它。非 Agent Core 模型只有阶段
用量，没有逐请求 Generation；内部 Stream/Event/Polling 观察占多数。旧审计没有被当作
现状证明，已重新读取源码、锁和安装的版本：DBOS 3.3.0、Agents SDK 0.24.0、Langfuse
Python 4.18.0、OI Agents 2.6.0、OTel 1.45.1、Core 1.6.11。

A 唯一新增锁定依赖是 OI LangChain **0.1.80**，使用已有 OI instrumentation 0.1.72 / 
semconv 0.1.42；根锁 296 → 297 条目。没有恢复顶层 LangChain Agent/LangGraph、另建
TracerProvider、独立上传通道、Trace 数据库或工作流引擎。

## 2. Generation、去重和用量

OI LC 内存 PoC 的真实 Core chain/tool/model 一次调用产生 **1 LLM + 1 TOOL + 2 CHAIN**，
所以直接全局插桩不符合工具去重要求。A 通过公开 instrument/uninstrument、
CallbackManager.handlers 和 configure hook 取得官方 handler，只转发模型生命周期。
OI 仍负责 Span/时间/成功用量；没有补写私有 `_tracer`、包装模型请求或实现另一条循环。

SDK Models 继续只由 OI Agents 记录；Core 链/检索器/工具不再次记录。模型返回的名称
只有在可观察时补到原 Span；部分失败的已报告 usage 也只补到同一 Span。结构化解析
重试：**2 次真实 Core 请求 / 2 Generations / 21 tokens**，账本仍为 **2 calls / 21 tokens**。
混合 SDK+Core 工具：**2 SDK + 1 Core Generations / 1 Tool**。没有追踪用的补跑或费用提交。

直接取消 awaiter 时，固定 Core 可能不发错误回调。适配只在原 inline callback Task
结束时清理其已经开始的观察，标记 `cancelled`、unknown effect、`model_callback_missing`；
不创建缺失 Generation、虚构返回/usage、取消或重试模型。原预算 admission callback 也
改为 inline，先执行原谓词；拒绝用例为 **0 请求 / 0 calls / 0 tokens / 0 Generations**。
没有改变计费算术、capture/reducer、Finalizer、授权、取消、回执、业务角色或 Prompt。

真实 Langfuse Gap Run `d52075990da24412aba441cc0604e2b6` / Trace
`eca6f20515d57c3b96de5769a8d5e4d9`：**892 observations、30 Generations（10 SDK +
20 Core）、4 Tools、340 tokens**。Run 账本 417 减夹具初始 77 正好为 340；20 个 Core
名称为 fixture-service，10 个 SDK 为 pr32-offline-model。详细证据见 A 的独立审计。

边界：Core 生命周期不等于 vendor SDK 内部透明 HTTP 重试；这里不细分后者。外部安装的
Core cache、绕过正常回调的自定义模型不在已验证范围；本项目没有配置该模型缓存。
未知 token 不估算为费用；采样/硬杀时不为 Trace 重放模型或工具。

## 3. 原生视图降噪，同一完整研究前后比较

使用官方 **Langfuse 4.70.1 OSS Web/Worker**，独立本地 Docker 项目、随机测试项目
凭据、PostgreSQL/ClickHouse/Redis/MinIO 卷和网络。UI 是真实 Tree/Timeline/Tracing，
没有新建页面或生成示意截图。浏览器使用既有 Playwright 和 Chrome、独立 context，
1600×1000；本地登录材料仅保存在忽略目录，未输出或提交。

B 只加固定 `por.span_layer=business|framework` 标签，按 OI Agent/LLM/Tool 类型及正向
业务名称分类。动态工具名也保留。原生查询为
`metadata.attributes.por.span_layer:business`，加 `metadata.run_id:RUN_ID` 限定研究。
已在本地项目创建 **POR Business path** 保存视图。历史无标签数据仍用全量视图。

同一个完整 normal Run `e4687d697ae94fbd918aa8ac5c19b682` / Trace
`08664f56f2043cf8e195cfa15783e9e5`：

| 比较 | 全量视图 | 业务视图 |
| --- | ---: | ---: |
| UI 可见观察 | 612 | 82 |
| 框架辅助观察 | 530（86.60%） | 0 |
| 服务端保留观察 | 612 | 612 |
| 服务端 Generation / tokens | 21 / 234 | 21 / 234 |
| 缺失父 Span | 0 | 0 |

UI 操作和完整字段 API 均验证了这些数字。没有名称黑名单丢弃、重挂父子、改写 DBOS
工作流或 SSE；授权/重试/恢复辅助数据仍可清除过滤后查看。Tree 的折叠/搜索和 Timeline
用于定位，过滤表格不代表原始 Trace 被裁剪。初始 Gap Trace 另有 894/119 的语义分类，
其轮询数不同，没有拿它冒充同一负载的性能前后对照。

截图保存在本次可视化目录：`obs-same-run-all.png`、`obs-same-run-business.png`、
`obs-native-business-view.png`、`obs-before-tree.png`。它们是实际浏览器截图。

## 4. 退出、故障、恢复与可持久化事实

B 增加 process-local 数值交付计数，区分 attempted / acknowledged / failed spans 和
batches；返回 FAILURE 与抛异常均统计。ACK 仅指 HTTP 导出成功，不等于后端持久化。
正常 explicit finally 和 atexit 都进入同一个有界 daemon close worker，整个等待默认
3s；Exporter shutdown 并发幂等，flush/provider/close 异常均隔离，不影响 Finalizer。
固定 OTel 的 force_flush 本身忽略 timeout 参数，因此仍需要外层截止时间。

| 验证 | 真实结果与边界 |
| --- | --- |
| 正常解释器退出 / 未捕获异常 | 真实子进程的已结束 Span 被 Flush，返回码按原异常保持 |
| 卡住/抛错的关闭 | 调用者等待有界，异常只产生通用诊断；不授权或补跑研究 |
| Windows 进程树强杀 | 未导出的内存观察丢失；使用原生 fault helper 结束 venv redirector 及真实解释器 |
| SIGINT / SIGTERM | Windows 明确跳过两个 POSIX 用例；实际 Linux CI 结果单列，不能把 skipped 当通过 |
| DBOS Worker kill/restart | 原生检查点、未知回执、取消、安全重试合同验证；事实仍可查，效果不重复 |
| 有界队列溢出 | 官方指标测得容量 8：24 ended = 12 exported + 12 dropped |
| OTLP 超时 / endpoint 不可达 | 模型/工具继续，无额外 grant、请求或费用，无无界队列；失败批次不保留重放 |
| 真正 Langfuse Web 停止后恢复 | 下文同一研究进程实测通过 |

真实服务故障 probe：停止仅本任务 Web 容器，Run
`4da31ba6492a41c4812d4c8f2ca9db12` 仍 completed / settled / saved，账本 **28 calls /
311 tokens / 2 tools**；**614 spans / 4 batches 导出失败、ACK 0**。恢复 Web health 后
释放同一进程第二个 Run `1151eeb4c3e843a7b5e49be66461365b`：服务端 **612 observations /
21 Generations / 234 tokens / 0 parent gaps**，ACK 及服务端完整率均 100%，内容哨兵无泄漏。
原进程只执行两套正常研究，没有补跑第一套来修复 Trace。

必须区分：已持久化的 DBOS journal/回执，已结束并导出的 Span，仍在内存中及尚未结束的
Span。SIGKILL、TerminateProcess、os._exit、崩溃和断电可以跳过 atexit 并丢失尾数据。
用 Run/session ID 关联持久历史；Trace 缺失不是未执行证明，遥测不能授权恢复或重试。

评估了官方 Collector sending queue + file_storage：可保护 Collector 已接受的数据，
不能保存应用未发送的 Span，仍有磁盘、队列和重试边界。本轮未加入强制 Collector 或
自研 spool；它不是解决应用 SIGKILL 零丢失的方案。现有失败批次丢弃、后续数据恢复导出
的策略已明确。见 [官方边界](https://opentelemetry.io/docs/collector/resiliency/)。

## 5. 可重复性能方法与预先门槛

入口：`evaluation/observability/run_benchmark.py` 和相邻 README。每轮/配置为新解释器、
独立 `por_test_UUID` 数据库和完整预热 Run，配置顺序轮换。**每配置 3 轮 × 4 Run，48 个
测量样本、12 个预热**。真实 DBOS/Run API/Runner/Harness/Neo4j Graph/History；SDK
为官方 OpenAIResponsesModel + MockTransport（2ms 固定模拟回复），Core 为真实生命周期
Fake model。每 Run **6 SDK + 15 Core 请求、2 tools**，同一数据和完整业务路径。

每个 Run 都验证 completed/settled/saved 和 **28 calls / 311 tokens**（含固定初始
7 / 77）；实际响应为 **21 calls / 234 tokens**。付费模型请求 **0**。

环境：Windows、Python 3.11.9，AMD Ryzen 7 8845H，8 cores/16 logical CPUs；Docker VM
16 CPUs / 7,950,495,744 bytes。基准专属 PG 17.6（512MiB cap）、Neo4j 5.26.31（1GiB
cap、heap 256–512MiB/pagecache128MiB），独立 Langfuse 项目。其他本轮 native/browser
进程及服务已结束/停止；用户原有业务容器未动。硬件和 Docker VM 仍共享，不宣称专用
生产压测机器或无其他系统活动。

采样 1.0，队列 2048，batch512，interval5s，OTLP2s，shutdown3s。CPU/Run 为 Run 窗口
进程 CPU，RSS 每25ms采样、包含库/夹具/校验 probe。Agent/LLM/Tool 计时在开关两组都
存在；关闭/drain 单列。服务端 API 完整性核查在计时窗外。Span ACK/Run-window 吞吐
不是网络最大容量。没有第二条上传热路径。

预先门槛保留在 `LIMITS`：p95 ≤ off×1.25+100ms、额外峰值 RSS≤256MiB、健康队列丢弃0、
关键 Span ACK/服务端完整、shutdown≤3.5s；四并发 p95≤健康串行×2。**未在看到结果后
调整门槛**，最后一项实际失败。

测的是 A+B 的本地组合副本（生产代码同 A 本地 `1bfa5f32` + B `d6a9b696`），以 A 根锁
环境执行；C 本身仍独立基于 main，不夹带 A/B 生产差分。这是早期冻结组合的历史数字。
A 后续除测试客户端外，还修复了继承 Counter 的 admission 顺序，见第 8 节；本次第 9 节
的 original 使用三个 PR 最终 HEAD 的实际组合，不能把这两套生产版本视为相同。

## 6. 正式结果：性能门槛有一项失败

Run 单位秒；RSS MiB；CPU 是秒/Run。分位数由全部原始样本合并计算，不平均各轮分位数。

| 配置（各12 Run） | p50 | p95 | p99 | CPU/Run | 峰值 RSS |
| --- | ---: | ---: | ---: | ---: | ---: |
| A tracing off，串行 | 8.753 | 9.226 | 9.235 | 5.329 | 807.6 |
| B tracing on，正常 Langfuse | 9.499 | 10.385 | 10.514 | 6.867 | 825.8 |
| C tracing on，不可达 endpoint | 9.657 | 10.849 | 11.599 | 6.779 | 839.4 |
| D tracing on，四并发 | 25.105 | 25.368 | 25.370 | 8.107 | 828.6 |

| 配置 | Span 总数 | ACK/秒（Run窗） | 失败 batch | 队列峰值 / 2048 | queue-full 丢弃 | shutdown 最大秒 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| off | 0 | 0 | 不适用 | 不适用 | 不适用 | 0.0000054 |
| healthy | 7,228 | 62.70 | 0% | 370 | 0 | 0.0760 |
| unavailable | 7,326 | 0 | 100%（预期故障） | 487 | 0 | 2.0488 |
| concurrent | 7,428 | 104.27 | 0% | 511 | 0 | 0.0739 |

healthy/concurrent 的关键 ACK、完整服务端 Span、Generation 数、token 数和父子闭合检查
全部通过；服务端内容哨兵无泄漏。不可达时导出失败不等于 queue-full 丢弃：批次出队
后失败并丢失，不能以 queue_dropped=0 声称零丢失。off 的 Trace 指标为不适用。

各轮 Run p95 波动范围（秒）：off **7.682–9.234**，healthy **9.894–10.502**，
unavailable **9.869–11.428**，concurrent **20.671–25.370**。12 个样本的 p99 接近最大值，
不是可靠的生产尾延迟 SLO；固定模型延迟也不是云端模型性能。

| 阶段 | off p50/p95/p99 | healthy | unavailable | concurrent |
| --- | --- | --- | --- | --- |
| Agent，秒 | .355/.658/.783 | .453/.760/.791 | .588/1.135/1.349 | 1.920/4.454/4.643 |
| LLM，毫秒 | 2.116/11.731/14.548 | 3.279/13.551/16.470 | 3.325/13.241/17.607 | 27.259/131.207/332.475 |
| Tool，毫秒 | .579/.692/.710 | .634/.884/1.649 | .599/1.425/2.007 | .830/13.092/24.694 |

正常串行 p95 开销 **12.57%**，CPU/Run **+28.85%**，RSS峰值 **+18.26MiB**。串行延迟/RSS/
队列/关闭门槛通过，但不能把 CPU 增量忽略。四并发 p95 **25.368s > 20.770s 门槛**，
原总 gate 保持 false。

追加同并发 tracing-off 分组对照（另3轮×4 Run，不替代原实验，非同期随机配对）：p50 **18.467s**、
p95 **18.541s**、p99 **18.543s**、CPU/Run **6.368s**、RSS **806.91MiB**。
对应 tracing-on p95 **+36.82%**、CPU/Run **+27.30%**。它提示本环境下有显著追踪开销，
但非同期实验不能排除后台负载/时段效应，不能当作严格生产因果证明。原门槛没有改成事后有利的值。

检查了 native helper 数量、2048/512 batch 配置、无队列积压丢弃、元数据出口及采样。
额外 py-spy 诊断出现最多约22秒 sampling lag，不能用它给热点分配可靠百分比；未据此
改写生产执行或做未经证明的优化。降低整 Trace 采样率是现有配置能力，但本轮未把
未测配置说成已经解决四并发开销。后续应在可控长期环境做低速准确 profiling 和采样/
batch 对照，再决定容量；不重写 DBOS 或靠丢父 Span 美化数据。

## 7. 安全、门禁与剩余工作

TraceConfig 隐藏输入/输出、消息、prompt、工具 schema 和检索文档；最终出口对白名单
scope、attributes、events、links、resource、status、tracestate 全面处理。没有内容开关。
原始 Prompt/Response/Tool Arguments/证据/private Memory 哨兵和测试凭据均未在完整字段
API 中出现；API keys、登录材料、数据库、日志、截图和原始 benchmark JSON 不入 Git。

| 门禁 | 实际本地结果 |
| --- | --- |
| A 全量 offline / native | 927 passed / 101 passed |
| B 全量 offline | 925 passed、2 POSIX skipped（Windows） |
| B native | 100 passed/1 测试等待边界失败；等待 History+manifest 后定向2 passed；最终 Linux CI 单列 |
| C 全量 offline | 918 passed / 107 deselected |
| Studio | 各分支 TS/lint/42 unit/build通过；各6个 Chrome E2E通过；Classic各7通过 |
| Ruff / 格式 / compile / 入口 / 锁 | 相应候选检查通过，32入口无退役引擎/网络；根锁可复现 |
| pip check | 原有 markitdown-mcp 要求 mcp~=1.8.0 与根锁1.23.0不兼容，仍 exit1，未粉饰 |

早期失败也保留：A原生99/2后修复旧断言及测试CAS；中断矩阵不计成功；沙箱缓存/浏览器
权限失败后在相同本机环境重跑，不改断言；C最初 ScriptedModel 不提供完整 native trace
usage，改用官方 Models + MockHTTP，校验器也改为等待所有 ACK Span 而非只等业务子集。
这些预演没有混入正式数据。A Linux browser CI 后又复现旧观察版本重试冲突，修复的是
最多三次同代安全失败的测试客户端，保留旧 key/只读/Producer不重复断言，不改生产授权。

发布使用已连接 GitHub API，因为本机 Git HTTPS 不可达。每次远端 tree SHA 与本地
冻结 tree 校验相等；提交作者/时间导致 SHA 可不同。A/B/C全部保持 Draft，未合并。
仓库 Claude Review 工作流虽然显示 success，其实际模型 Review step 为 **skipped**；
没有把它当作独立 Review 完成。最终 CI、独立 Review及剩余事项以各 PR 的冻结记录为准。

## 8. 已授权独立 Review 及修复记录

三个子代理分别只读审查 A/B/C，没有读取凭据、编辑文件或调用业务模型。B 未发现
P0/P1/P2；A 找到 1 个 P1，C 找到 1 个 P1 和 5 个 P2，均已修复并复审确认闭环。

A：父 Tool/Runnable 先继承模型 observer，再合并预算 Counter 时，inline 本身不能保证
admission 先运行。只前置新 Counter 也不能覆盖继承的更严格 Counter。现在只对合并副本
稳定前置**全部** ModelAttemptCounter，保留它们及其他 handler 的相对顺序、local/继承
成员资格和调用者配置。新增8个 sync/async/配置回归先失败，修复后目标矩阵106 passed；
拒绝同时验证0请求、0费用、无Generation、无遗留OI Run。最终计数以更新CI为准。

C：修复了继承业务环境可覆盖测试 Graph/目录/SDK session、libpq query 可覆盖校验host、
单 `--case` 忽略 `--source-root`、内容哨兵未覆盖实际 Memory/SDK/tool、聚合完整率掩盖跨
Run错归，以及Windows timeout只杀venv redirector的问题。入口现仅保留最小OS/显式TEST
输入；不读取被剔除变量的值。DSN必须显式测试用户/密码/数据库且无query/fragment；本地
PG禁SSL/GSS并只允许已提供密码认证，Requests不发现netrc/proxy。子进程超时/中断清理
整个自有树。逐Run强制唯一不共享Trace、双run_id/session、21/234、8Agent2Tool、业务
阶段数量和同Run父关系。26个纯离线隔离/统计/归属/清理回归通过。

初版正式数字已被独立用标准库复算且一致，原数据的每Run实际也是唯一Trace、21/234，
并未发现这批数据真实错归。修复后单Run真实服务端612/21/234、全部逐Run校验通过；
随后完整多Run执行验证单列保存。第6节数字仍明确对应最初冻结组合，未拿功能验证
期间的数据或新配置替换原性能失败。新加内容哨兵和更严验证不构成“性能已优化”的证明。

A初版最终Linux CI和Studio已通过，B Linux为927 offline（含两个POSIX信号）+101 native，
C初版Linux CI也通过。修复后的最终HEAD及独立复审结论统一在各PR冻结评论记录，不能用
旧HEAD绿灯代替新HEAD；所有PR仍保持Draft、未合并。

本次已经证明：逐请求观测和去重、实际用量/失败隔离、真实服务端脱敏与原生可读视图、
有界退出/队列及不重放的恢复边界。尚未证明：四并发性能达标、生产长期内容量/吞吐、
Cloud/HA/持久 Collector以及 SIGKILL 零丢失。没有以“Trace 页面完整”替代这些条件。

## 9. 2026-10-29 组合验收与性能复核

本节记录后续明确授权的性能诊断、组合验收及条件合并任务；前述失败数据完整保留。
只有原数值门槛、完整性、安全、Review 与最终 CI 全部通过才允许按 #34 → #35 → #36
使用 Merge Commit 合并；否则保留 Draft，不部署生产、不接受未解决的容量风险。

### 9.1 实际集成基线

重新 fetch 和读取完整 PR 差异、评论、Review、CI、固定版本实现。main 仍为
`9b479fa4fd4ee412305d81ddba3bf2ecb62b364c`，三个 PR 均 open / Draft / 未合并：

| PR | 本轮 original 使用的远端 HEAD |
| --- | --- |
| #34 | `ef282a3a25dddc6add6187935dc7e95a726823fe` |
| #35 | `a831f36f656af2a5babce83d6dadd92ef9679b58` |
| #36 | `7592a1f7365ec35c762291892fed1af637e92906` |

独立 managed Worktree / `codex/observability-integration` 从最新 main 建立，用本地
Merge Commit 依次组合实际远端提交。`telemetry.py` 与架构文档由 ort 自动合并，文本
冲突为 0；源码复核及定向回归确认 LC 初始化、脱敏出口、业务标签和有界关闭均保留。
没有用一边覆盖另一边。组合冻结 HEAD 为
`5705768fb954787fbcef2c5b0a6d11aba3ce5ed5`，tree
`5d31cd7b9f67f61d71bb1c7ce5dda74d82470b0f`。原始生产 `src` 在优化前保存在忽略快照，
每个正式子进程再记录规范化 Python 源码 SHA-256。原 PR/Worktree/未提交资产均保留。

### 9.2 分组诊断证据与最小优化

以下为专门诊断，包含 1 次预热和 4 次测量，不能混入正式分位数。函数计时是跨线程、
可嵌套的 inclusive wall；相互重叠，不能相加为 CPU 百分比。

| 热点/组件 | 实测证据 | 口径与处置 |
| --- | --- | --- |
| 完整 Run 状态观察器 | 原 50ms 轮询：611 次 get / 14,053 SQL；归档回执观察：10 次 get / 230 SQL | 原生工作流仍另有 25,153 / 23,852 SQL，业务投影 62 / 41 次 get；不能把所有 SQL 算作观察器 |
| 原 `CorrelationProcessor.on_start` | 3,096 次合计 98.8ms | 未证明是秒级开销的主要来源；不改变身份传播 |
| Langfuse on_start / on_end | 各 3,096 次，114.4 / 66.5ms | 保留官方 Processor、项目隔离与完整 Span |
| 原出口白名单与 safe_span | 属性过滤 7,152 次 / 69.3ms，Span 清洗 3,096 次 / 90.5ms | 去除重复键前缀解析、常量集合/Resource 构造和多余 dict 副本 |
| LC start / finish | 各 75 次 / 38.5ms、53.0ms | 现有官方模型生命周期保留；不动 Counter 顺序、取消或用量 |
| DBOS start_span / OTel get_tracer | 2,736 次 / 693.2ms；2,894 次 / 159.8ms | 第三方内部成本，未做私有 Patch 或删除诊断 Span |
| SafeExporter | 13 批 / 4.96s | 包含清洗、编码、网络及调度，不能解释为纯 CPU；仍在后台线程 |

观察器放大来自 `NativeCase.wait` 每 50ms 调用完整 Run projection：读取 request、units、
多个 DBOS events，再重建预算、报告及未清算投影。基准现在使用公开
`DBOSClient.get_event(run_id + ':events', 'history', timeout_seconds=120)` 等待实际归档
回执，再读取完整 Run 一次；创建和最后完整 get 仍在 Run 时间/CPU 窗内。
`events_workflow` 只有在 History 保存且终态 manifest 齐全后才发布该回执。仍核对同代、
completed / settled / saved / events_complete、匹配 History ID 及 28 calls / 311 tokens。
正常固定夹具无 retry；这不是生产等待器或调度修改，原生合同仍使用原来的观察 helper。

旧观察/新观察的单次诊断 p95 为 31.462s / 19.847s，CPU 总量 41.25s / 21.875s。
这是非同期、带探针的测量方法对照，不是生产优化收益。原 50ms 数据仍有效描述其负载，
不能用新方法数字覆盖第 6 节旧门槛失败，也不能把差值全部归给 OTel。

唯一生产优化是 `observability/telemetry.py`：原数值/字符串字段和 scope 精确集合预计算，
共享公开不可变的安全 Resource，直接读取结束 Span 的属性视图。仍逐值检查有限非负
数值、严格字符串标签；原 Resource 内容全部丢弃，事件、Link、status、tracestate 规则
不变。没有缓存 Run 事实、修改业务读取或新增 tracing 组件。候选提交 `0c143b4b`。
交替 3 轮微基准的清洗耗时原为 13.4–17.7µs、候选 8.7–11.9µs；完整模型调用波动较大，
尚不能证明其端到端显著收益。没有据此修改 batch/queue 或采样率。

### 9.3 正式实验方法与 Review

基准提交 `68023be6`：固定 seed 20261029、平衡随机块，original/candidate 各测 serial
OFF/ON、concurrent OFF/ON、unavailable。每版本每配置 3 轮 × 4 Run；总计 120 个测量
Run、30 次独立完整预热。每单元是新解释器及独立随机库；同一锁、数据、模型响应、
工作流、资源上限、采样 1.0、2048/512/5s batch、2s OTLP、3s shutdown。正式运行期间
不并行执行离线测试、浏览器、build 或 profiling。诊断和历史样本单独保留。

仍使用第 5 节全部数值门槛；补齐原 summary 遗漏的并发 RSS 检查，阈值同为 256MiB，
对照同并发 OFF。报告 Run/阶段原始样本、p50/p95/p99、Runs/s、CPU/Run、RSS、语义类型/
业务 Span、ACK 及完整字段服务端逐 Run 校验、batch/OTLP/flush、queue/drop/失败。
batch/flush 是含探针锁等待的 inclusive wall；不能拿它分配纯 Exporter CPU。
RSS 采样含 destroy/drain/shutdown，CPU 截止测量 Run 结束，两者窗口明确不同。
Core callback 与 SDK get_response 的 LLM 时钟边界略不同，仅用于同方法组间比较，
不能把混合分位数当作供应商纯推理耗时。12 Run/config 的尾分位仍有较大不确定性。

本轮独立只读 Review 已完整读取 A/B/C 及优化差异和固定依赖实现：P0=0、P1=0；P2=1，
旧并发 RSS 漏门禁已修复并复核。反例曾使并发 RSS 9000MiB / OFF 100MiB 仍通过，现拒绝。
新旧字段过滤另做 2000 组、54 种键差分验证，输出一致。新增 9 个基准回归先失败，
修正后基准合同 35 passed；包含新增类型/Resource 隔离测试的目标矩阵 156 passed /
2 POSIX skipped（Windows）。最终全量、真实 UI、故障及性能结果下文单列，不预宣成功。

### 9.4 正式结果：两版本均未通过容量门槛

下表每行均为 3 轮 / 12 个测量 Run；分位数直接合并原始样本计算。Run 单位秒，CPU 为秒/Run，RSS 为 MiB。

| 版本/配置 | p50/p95/p99 | CPU/Run | 峰值 RSS | 完成 Run/s |
| --- | --- | ---: | ---: | ---: |
| original / 串行 OFF | 10.317/12.842/13.289 | 4.622 | 791.76 | 0.0982 |
| original / 串行 ON | 10.330/11.436/11.505 | 5.027 | 808.62 | 0.0997 |
| original / 四并发 OFF | 18.664/19.758/19.758 | 5.727 | 790.88 | 0.2101 |
| original / 四并发 ON | 21.514/23.190/23.190 | 6.426 | 810.46 | 0.1855 |
| original / ON 不可达 | 10.307/13.513/14.583 | 5.406 | 824.61 | 0.0940 |
| candidate / 串行 OFF | 9.341/11.261/12.135 | 4.536 | 792.20 | 0.1026 |
| candidate / 串行 ON | 10.339/11.340/11.355 | 5.154 | 809.36 | 0.0971 |
| candidate / 四并发 OFF | 19.642/19.800/19.812 | 5.855 | 791.66 | 0.2042 |
| candidate / 四并发 ON | 22.880/27.205/27.206 | 6.979 | 812.33 | 0.1682 |
| candidate / ON 不可达 | 10.332/11.838/12.265 | 5.277 | 821.44 | 0.0959 |

| 版本/配置 | Span 总数/业务 | LLM/AGENT/TOOL/其他 | ACK span/s（Run窗） | 失败批次/批次 | Queue峰值/丢弃 | shutdown最大秒 |
| --- | ---: | --- | ---: | --- | --- | ---: |
| original / 串行 OFF | 0/0 | 0/0/0/0 | 0.00 | — | — | 0.0000 |
| original / 串行 ON | 7100/984 | 252/96/24/6728 | 59.00 | 0/26 | 399/2048 / 0 | 0.0473 |
| original / 四并发 OFF | 0/0 | 0/0/0/0 | 0.00 | — | — | 0.0000 |
| original / 四并发 ON | 7424/984 | 252/96/24/7052 | 114.75 | 0/18 | 511/2048 / 0 | 0.0576 |
| original / ON 不可达 | 7324/984 | 252/96/24/6952 | 0.00 | 21/21 | 492/2048 / 0 | 2.0316 |
| candidate / 串行 OFF | 0/0 | 0/0/0/0 | 0.00 | — | — | 0.0000 |
| candidate / 串行 ON | 7116/984 | 252/96/24/6744 | 57.60 | 0/26 | 353/2048 / 0 | 0.0468 |
| candidate / 四并发 OFF | 0/0 | 0/0/0/0 | 0.00 | — | — | 0.0000 |
| candidate / 四并发 ON | 7427/984 | 252/96/24/7055 | 104.13 | 0/19 | 510/2048 / 0 | 0.0945 |
| candidate / ON 不可达 | 7220/984 | 252/96/24/6848 | 0.00 | 20/20 | 493/2048 / 0 | 2.0463 |

OFF 没有导出队列，相关指标为不适用。不可达的失败批次是预设故障，不能用 queue-full=0 声称没有丢失。Queue峰值来自25ms原生指标采样，非每个瞬时值；累计丢弃使用官方计数。

| 版本/配置 | batch p50/p95/p99，ms | flush p50/p95/p99，ms | 各轮 Run p95范围，s |
| --- | --- | --- | --- |
| original / 串行 OFF | — | — | 10.312–13.112 |
| original / 串行 ON | 58.557/113.260/142.867 | 30.544/44.547/45.792 | 10.227–11.362 |
| original / 四并发 OFF | — | — | 18.656–19.758 |
| original / 四并发 ON | 106.130/209.706/439.730 | 40.641/54.084/55.279 | 19.713–23.190 |
| original / ON 不可达 | 2049.534/2072.689/2444.296 | 2025.571/2030.151/2030.559 | 10.317–14.486 |
| candidate / 串行 OFF | — | — | 10.181–12.054 |
| candidate / 串行 ON | 64.164/153.743/392.726 | 0.032/40.958/44.596 | 10.346–11.353 |
| candidate / 四并发 OFF | — | — | 19.159–19.811 |
| candidate / 四并发 ON | 108.586/257.065/441.612 | 67.900/90.710/92.738 | 21.167–27.205 |
| candidate / ON 不可达 | 2038.295/2085.667/2396.621 | 2028.791/2043.594/2044.910 | 10.328–12.226 |

阶段时钟单位为 ms；每行分别48个Agent、252个LLM、24个Tool样本。保持第9.3节对时钟边界和探针的限制。

| 版本/配置 | Agent p50/p95/p99 | LLM p50/p95/p99 | Tool p50/p95/p99 |
| --- | --- | --- | --- |
| original / 串行 OFF | 500.586/907.649/1700.005 | 2.176/13.506/19.287 | 0.739/0.994/1.108 |
| original / 串行 ON | 544.137/821.775/897.710 | 2.797/14.275/20.728 | 0.707/0.989/1.031 |
| original / 四并发 OFF | 1437.198/3083.185/3234.084 | 15.430/74.069/112.770 | 1.175/27.966/34.959 |
| original / 四并发 ON | 1771.117/3593.310/3932.950 | 23.276/100.251/144.002 | 1.894/24.853/111.995 |
| original / ON 不可达 | 443.926/1125.112/1295.713 | 2.973/15.025/22.304 | 0.719/1.033/1.066 |
| candidate / 串行 OFF | 396.291/736.020/774.053 | 2.017/11.754/16.289 | 0.633/0.918/0.974 |
| candidate / 串行 ON | 413.939/684.053/827.184 | 2.993/12.498/16.052 | 0.707/0.930/0.973 |
| candidate / 四并发 OFF | 1112.249/3395.298/3504.888 | 18.448/93.183/139.214 | 1.379/20.371/24.558 |
| candidate / 四并发 ON | 2346.018/4608.700/4872.944 | 24.581/105.644/256.021 | 1.576/83.222/92.750 |
| candidate / ON 不可达 | 400.454/870.626/1311.748 | 2.757/12.820/15.081 | 0.747/3.023/4.471 |

| 容量门槛 | 原始组合 | 候选组合 |
| --- | --- | --- |
| 四并发 p95 ≤ 健康串行×2 | **23.189681 > 22.871936s，失败** | **27.204602 > 22.680916s，失败** |
| 其余原有门禁及补齐的并发RSS门禁 | 全部通过 | 全部通过 |
| 总 gate | false | false |

同并发 ON 相对 OFF 的实测 p95 / CPU增量：
- original：p95 **+17.37%**，CPU/Run **+12.21%**。
- candidate：p95 **+37.40%**，CPU/Run **+19.19%**。

候选四并发p95在本轮比原始组合高 **17.31%**。微基准的清洗收益没有在端到端兑现，不能宣称优化成功；样本小且宿主/Docker共享，也不足以证明该小改动造成全部回退。原始串行ON的p95甚至低于OFF，进一步说明不能把小样本差值当稳定因果。全部结果保留，没有择优替换或修改阈值。

48个健康/并发测量Run的服务端逐Run校验全部valid，均21 Generations/234 tokens，关键ACK、所有ACK到站、parent闭合和内容检查通过；72个开启追踪的Run各有独立Trace。30记录与计划、样本守恒、两源116个Python文件及所有汇总经独立标准库复算一致。原始指纹 `d16878720c457796908a0b2f0d0a48276252188caf8ee0ff11a5ac3e75c88c9d`，候选 `358b218c2e0f1a260b9b30f0395473e986a0acad623a762dd7573199544e7614`。

### 9.5 追加低频诊断与瓶颈结论

正式测量结束后，另用5Hz文件/函数名栈采样、公开方法计时和GC callbacks，不采集locals。
实际采样间隔中位约0.214s、p95约0.217s、最大0.518s；没有沿用旧py-spy滞后样本。
以下包含初始化、1次预热及4次测量，Windows thread CPU有15.625ms量化，嵌套计时有重叠，
不得相加为CPU份额，也不用于替代正式样本。

| 组件 | ON诊断 | OFF诊断 | 结论边界 |
| --- | --- | --- | --- |
| SQLAlchemy native execute | 24,140次，inclusive wall81.607s / thread CPU15.016s | 24,152次，85.558s / 16.922s | 原生SQL执行/等待是较大已测路径；不能据此分摊服务端CPU与Windows–Docker网络开销 |
| OI SDK span start/end | 110次，wall33.6/16.4ms | 不适用 | 未证明是秒级主要热点；保留官方插桩 |
| LC start/finish | 75次，wall41.7/61.2ms | 不适用 | 保留模型生命周期和准入顺序 |
| SafeExporter | 10批，wall0.780s / thread CPU0.250s | 不适用 | 包含嵌套清洗/OTLP；后台成本，未证明应引入新队列或Collector |
| 指标收集 | 766次，wall0.654s | 740次，wall0.110s | 测量工具有成本；不能把所有差值认作生产插桩损耗 |
| generation2 GC | 7次，wall1.769s | 6次，wall1.423s | 有停顿但未证明可安全全局冻结/调整；未改变业务对象清理语义 |

采样叶节点主要是等待及psycopg调用；未观察到连接池等待成为主导路径。现有DBOS池为官方
默认20，未发现关闭SQL编译缓存的配置。健康队列未满/无丢弃，故没有证据支持靠扩大队列或
改batch来解决本轮容量，也没有关闭LC插桩、降低采样或删Span。

同一完整Trace的native por.run为15.261s、por.events为26.793s、por.finalize为17.686s。
业务根结束不等于终态投递/归档观察完成，约11.5s尾段仍涉及原生事件交付；不能把它隐藏掉。
这些证据缩小到原生SQL/协议/交付路径，但尚不能把剩余因果精确分摊到服务器执行、客户端
序列化、调度和网络。因此本轮不猜测修改业务工作流或接纳容量风险。

下一步最小方案是一次隔离的SQL/事件relay专项实验：以SQL指纹/次数/行数/耗时及服务器
原生统计拆分send/recv、stream/event、状态投影和网络RTT，再评估官方支持的连接参数。
保持事务、fsync、授权/清算和原门槛；涉及native命令序列批量化时必须另做版本/排空审计。
无需为此新增追踪基础设施；Collector无法消除本地原生执行成本或应用发送前的SIGKILL窗口。

### 9.6 最终组合的真实UI、故障和完整性

真实官方Langfuse4.70.1，1600×1000 Chrome，合成Run
`a8b5e90769404de1a93338e3213cb9e0` / Trace `42c7f973bf8478947337eaca892abff5`：
全字段API准确621 observations，业务82、框架539（86.80%），21 Generations/234 tokens、
8 AGENT（4组+4角色）/2 TOOL，所有父关系有效。UI全量≈621与业务≈82一致；原数据仍621，
没有重挂父子。原生Tree及Generation inspector实际显示fixture-service、10 tokens、
Input/Output undefined；Trace总量234，与311减夹具初始77一致。两Producer为同父不同
Role span，start为03:06:07.474Z/.527Z，end为03:06:11.007Z/.970Z，确有重叠。
完整API的Prompt/响应/工具参数/证据/Memory哨兵及测试API keys均无泄漏。

普通metadata双条件曾触发一次events.filterOptions侧栏统计超时422，表格仍82；证据保留。
改用Trace ID限定后复验无相关页面错误，表格/Tree/Generation均正常。它是本地Langfuse
查询边界，不把初次超时隐藏，也没有为美化图而修改业务。
截图为实际浏览器：obs-integration-all.png、business.png、tree.png、generation.png
（均有obs-integration-前缀），在本次可视化目录；无新设计/生成截图。

最终同进程故障控制器 v3 实测：停止本任务Web后Run
`897f9a19a264434fbfe48f8f026651c4`仍completed/settled/saved/events_complete，28 calls/
311 tokens；614 spans /4 batches失败，ACK0。自动恢复health后，同一进程新Run
`c04d2c4f9cfa42df9cc7e1b0687175d2`到站612 observations、21 Generations/234 tokens，
逐Run/父关系/内容检查通过，ACK及服务端完整率1，shutdown0.0013s。严格只执行两项研究，
旧失败批次未重发，模型工具未为Trace补跑。更早的基础库停止/聊天中断gate超时尝试单列
保留，未计成功；控制器现在独立恢复服务，不依赖下一条聊天消息。

Linux101项原生矩阵继续验证kill/restart、取消、未知效果、安全重试和归档，额外退出/
队列合同验证有界等待、24=12导出+12丢弃和POSIX信号。SIGKILL仍可丢内存尾部；DBOS是
执行事实来源，不用Trace缺失推断未执行或授予重试。

Classic另用现有浏览器夹具完成实际POST→native研究→报告/用量呈现→刷新：仅1次创建，
20 calls/224 tokens保持不变；再打开保存历史仅GET、0新创建，费用未变。该夹具本来关闭
clarification且无正式基准初值，不混入120个性能样本。无令牌401是预期保护；只读复验将404定位为
/favicon.ico静态资源缺失，没有页面异常或报告故障，不把预期认证错误及资源404报作console全绿。既有Classic协议7项
通过；Studio完整构建/单元/6条原生浏览器矩阵由独立GitHub机器通过。

### 9.7 最终检查、发布状态及明确结论

| 最终组合代码68023be6的验证 | 实际结果 |
| --- | --- |
| Linux CI40810614101 | quality通过；985 offline /107 deselected，101 native /25 deselected |
| Studio CI40812344128 | type/lint/build、42 unit、6真实浏览器通过 |
| Windows根venv全offline | 983 passed /2 POSIX skipped /107 deselected，272.73s |
| Classic / import / compile / format / lock | 7协议通过；32入口通过；compile、4变更Python格式、297项锁通过 |
| pip check | 既有markitdown-mcp要求mcp~=1.8.0、当前1.23.0，仍exit1；无新增依赖 |

本地最小环境首次漏Windows home导致收集失败，补home后又因CPU架构变量缺失使2个缓存
合同失败（981 passed）；补标准OS路径/架构变量后4个缓存定向通过，再全量983通过。
没有保留业务凭据/读取生产.env，也没有改断言或删除用例。uv沙箱cache原子写失败由独立
授权执行复验，不能把首次失败算通过。测试账号/API keys仅按用户具体授权用于loopback，
未输出、提交或部署。截图、原始样本、数据库/缓存均不入Git；历史失败与worktree都保留。

独立Review再次复算全部30文件/120Run、计划及两源116个Python文件，全部匹配冻结提交；
3000组×57种键/scope额外等价复核通过。没有新增P0/P1/P2；原漏并发RSS的P2已修复。
最终CI链接：[组合CI](https://github.com/kkaaakk/public_opinion_research/actions/runs/40810614101)、
[Studio](https://github.com/kkaaakk/public_opinion_research/actions/runs/40812344128)。

**合并决定：不合并。** #34/#35保留原HEAD和Draft；#36只更新基准/本审计，最终发布HEAD及
该HEAD的CI在PR冻结记录列出，不能用旧HEAD绿灯代替。最小生产候选保存在独立验证分支
`codex/openai-agents-observability-integration`（代码68023be6），没有注入原PR或main。
main保持9b479fa4；没有Ready、Merge、Squash/Rebase、生产部署或生产模型付费请求。

在已测路径上逐请求追踪、隐私/归属/预算及故障隔离功能完整，可靠性是有界best-effort，
适合继续本地开发与排障；四并发容量明确未达标。没有证据需要新的可观测性基础设施。
可以继续独立业务开发与源码学习，但**不能按本轮原验收标准宣布可观测性改造结束、
达到长期生产成熟度或批量合并**。下一步应先完成上述最小SQL/交付专项验证，而非降低门槛。
