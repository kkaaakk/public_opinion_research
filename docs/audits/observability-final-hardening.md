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
环境执行；C 本身仍独立基于 main，不夹带 A/B 生产差分。A 后续只有测试客户端更改。

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
