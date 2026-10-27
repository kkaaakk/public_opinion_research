# PR #32 独立最终 Review 与真实 Langfuse 验收

日期：2026-10-26 至 2026-10-27（Asia/Shanghai）。原始 review HEAD：
`c633437fbae8368d668e8626480000d1c784734d`；main：
`b682ad5bf633f046df43101f012a5985f2768395`。先 fetch、核对完整 diff、独立读取实际
代码及固定 SDK/DBOS/OTel 实现，再对照历史审计；没有将历史审计或 CI success 当作
代码正确性的替代。初始评论、review submissions、未解决 threads 均为 0。

## 1. 发现、复现和处置

| 编号 | 等级 | 原始位置与问题 | 复现 / 影响 | 处置 |
| --- | --- | --- | --- | --- |
| R1 | P1 | c633437f `observability/telemetry.py:239–248` 直接复用 Span/parent/Link 的 SpanContext；只删 Link attributes | 构造带任意 vendor tracestate 的标准 Context 后，safe_span 仍保留字符串；出口白名单可被 context 字段绕过 | `_export_context` 只复制 IDs、remote/trace flags，清空全部 tracestate；不改变进程内或 DBOS 官方传播 |
| R2 | P2 | c633437f `telemetry.py:458–463` 调用 OTel once-only setter 后没有验证注册结果 | 模拟别的 initializer 在检查与注册间抢先成功，函数仍返回 True；DBOS/global 与 SDK/provider 可能分裂，不能保证统一出口 | 注册后检查 global identity，失败则关闭本管道/SDK tracing、清理自有资源，业务继续 |
| R3 | P2 | c633437f `telemetry.py:220–231` 对空格名称回退 execution | 真实服务端显示六个业务 Agent 全部匿名为 execution；API 有角色，UI 默认名称无法区分 | 使用已允许的 `por.role` 构造安全 `.agent` 显示名；不放开任意 name，不改 AgentSpec 或 Prompt |
| R4 | P2 | 018f6369 `tests/test_dbos_contracts.py:1248` / `tests/test_dbos_retry.py:521` 假定归档后的观察版本稳定 | 原生矩阵 95 passed / 2 failed；定向重复 9 passed / 1 failed 时捕获同一已清算、已投递失败的 cursor 从 55 到 57，生产 CAS 正确拒绝旧版本 | 仅测试客户端在同代安全失败、终态已投递且版本严格推进时重新观察，最多三次提交；负向拒绝、幂等和执行次数断言保留，生产 API/Finalizer 不变 |

R1–R3 均包含先失败的回归：`tracestate-red.log`、`provider-red.log`、
`labels-red.log` 各 **1 failed**；修复后三项均通过，包含全部原有用例的 tracing
单元矩阵为 **25 passed**。原始复现日志在忽略的 `.tmp/pr32-review/`，不提交
敏感数据、数据库或运行输出。修复提交：
`aad827f8edbad6b1e74c96936dff22268674b013`。

R4 初始失败、重复观察和确定性旧版本回归分别保留于
`native-initial-failed.log`、`retry-probe.log` / `retry-probe.jsonl`、
`retry-cas-red.log`。新的 `test_terminal_receipt_advancement_preserves_retry_cas`
通过真实 DBOS 消息推进 relay cursor，先断言旧版本被生产 API 拒绝，再验证重新
观察只产生一次合法 grant，Producer 工具各一次、模型调用合计四次。首次为 **1 failed**，
修正测试客户端后通过。该客户端不改变 Run 状态、放行旧 key 或给 Finalizer 授权；
代次变化、已接受请求、未清算/未投递或无版本推进时仍原样抛错。此前仅延迟 done
投递的探针没有复现失败；版本推进是实际捕获的拒绝原因，不将猜测写成结论。
R4 提交及完整新 HEAD 矩阵以冻结 PR 评论为准。

Review 处置结论：发现 P0 **0**、P1 **1**、P2 **3**，四项已修复；剩余 P0/P1/P2
可执行缺陷 **0**。下文明确列出的平台/范围限制未冒充新发现或已实现能力。

## 2. 独立审查覆盖与语义边界

- 初始化：module lock/attempt flag 管理本进程一次初始化；拒绝外部已配置 Provider 或
  Instrumentor，并补充 R2 的注册后检查。失败不启用 DBOS tracing，不启用默认
  OpenAI uploader。不做远程 auth probe，缺配置仍照常执行业务。
- Span 所有权：DBOS 生成 native Workflow/Step；OI Agents 生成 SDK trace group、
  Agent/turn/LLM/Tool；sdk_invocation 额外覆盖 settlement。框架 trace group 与 Agent
  都可能是 AGENT 类型，这是不同边界，不是两次模型/工具执行。
- SDKUsageEnrichment：固定 OI 2.6 不读取敏感数据关闭时的 ResponseSpanData.usage；
  SDK 0.24 保留该 native 数值。本桥在原 Span 结束前补字段，不生成 Span、不请求
  模型、不提交费用。真实服务端按 10 个请求收到 10 个 Generation，总计 140 tokens。
- 隐私：输入/输出和工具 schema 先由 OI 配置隐藏，最终出口再次白名单处理 attrs、
  status、Resource、Scope、Events、Links。R1 补齐了标准 Context 的 vendor 字符串。
  原生 retry event 仅保留 attempt 数字，异常文本/栈不出站。
- 并行与恢复：身份使用 ContextVar 和原有 unit/permit/invocation/round/generation，
  子流程使用 DBOS 官方 PropagateOtelContext / Client otel_context，无业务 Trace 表。
  R1 不改变 journal carrier 或 ID；没有给恢复生成替代授权。
- 业务：完整 diff 中没有 retry grant、Finalizer、reservations、effect_guard 或
  unknown-effects 分支改写。新增代次查询仍在原清算 try/finally 内；失败省略元数据，
  取消依原路径清算。SDK/预算/Graph 的原有错误、capture 和 reducer 保留。
- 输出与关闭：Langfuse 原生 BatchSpanProcessor，模型/工具路径不远程上传；整个
  SDK/provider shutdown 等待有上限。硬杀可失去尾数据；不会为补 Trace 重跑研究。
- 兼容：DBOS 3.3.0、SDK 0.24.0、Langfuse SDK 4.18.0、OI 2.6.0 和 OTel 1.45.1
  未变；应用 `dbos-phase5-studio-v1` / runtime `sdk-phase5-v1` 未变，无新增 DBOS
  命令、function ID、workflow input 或业务 schema。旧版本排空策略继续生效。
- 架构：没有实施 LangChain 审计建议、删除旧兼容接口、增加自研记录器或调度状态。
  新增的 langfuse_acceptance_fixture 仅是显式启用的本地验收资产，不进入生产包。

## 3. 真实服务端环境

现有进程和操作员文件没有 Langfuse 项目配置。只检查变量是否存在/键名，不输出其
内容。使用官方支持的 Docker Compose 与 headless initialization 建立一次性环境：

| 项目 | 隔离配置 |
| --- | --- |
| Langfuse | 官方 **4.70.1 OSS**，Web/Worker 均实际运行，health 返回 4.70.1 / OK |
| 项目 / UI | `por-pr32-review-project`，loopback `http://127.0.0.1:33080` |
| 容器项目 | `por-pr32-langfuse`，独立 PG/ClickHouse/Redis/MinIO 卷和网络 |
| 业务验收设施 | `por-pr32-native`，PostgreSQL 17.6 :25434，Neo4j 5.26.31 :27689；每例独立 por_test_UUID DB |
| 凭据 | 由本地进程随机生成、仅此测试项目使用；忽略的本地配置/环境内部传递，未显示、未提交，不使用生产密钥 |
| 推理 | 既有受控 LC 服务 + 官方 SDK Responses Model 的 httpx2.MockTransport；仅 loopback socket，**真实付费请求 0** |
| 研究 / Graph / 工具 | 实际 DBOS/Runner/Harness/Neo4j 写入与 History；搜索和模型内容为合成夹具，Review 输出驱动实际 Gap 路由 |
| 浏览器 | Browser plugin 未提供；Playwright 使用已安装 Chrome，独立 context，1600×1000；没有增加 npm 依赖 |

官方依据：[Compose](https://langfuse.com/self-hosting/deployment/docker-compose)、
[headless initialization](https://langfuse.com/self-hosting/administration/headless-initialization)。
使用 release v4.70.1 的官方 compose，不构建第三方 Trace 页面；只调整测试端口、
独立卷/资源上限和本地初始化配置。首次 MinIO registry EOF 经重试成功，没有以
本地 OTLP 接收器或其他可视化代替真实 Langfuse。

验收镜像 digest：

- Web：`sha256:71b04842ecff8fe0dd15c7bbb1acd6e0555735e393a8108c43feb4b8d0adf9e6`
- Worker：`sha256:3976a73de84a29999ff4573398e240127b8a92012421ce5eec61cd57f087d8ca`
- ClickHouse：`sha256:8a790dd3468db22b1d4e7b18a176f378ff5ff6053b9c48dd4ea1fa71a24c5ba6`
- MinIO：`sha256:268d68a2a3b6fc65f905a61f84d7eab6345ce6bff14f551e2b028ec31e6a1341`

没有操作 `public-opinion-neo4j`、mentor-match 或其他业务容器；没有修改 `.env`、
`CLAUDE.md`、生产凭据、四角色职责或业务模型配置。

## 4. UI / API 验收证据

修复后核对数据集：Run/Session `7c13cc7982944e8d9c74fc7baf34b2eb`，
Trace `e72a64dce7b265d68c6e656888ef066d`。Langfuse v2 observations API
返回 **870** 个真实观察：SPAN 844、AGENT 12、GENERATION 10、TOOL 4。
六个角色 invocation（两 Producer 各两轮 + Risk + Strategy）各有 SDK trace group
与 Agent；不能把 12 个 AGENT 观察等同 12 次角色执行。

| 验收项 | 实际结果 |
| --- | --- |
| 到站与 UI 身份 | health / UI 均为官方 Langfuse；登录、项目 Home、Tracing、Tree、Timeline 与 LLM inspector 均实际操作，无 page error / console error |
| 层级 | UI 展开 `run_submit → por.run → por.research → por.role → business_step → sdk_invocation → SDK group → public_signal.agent → turn → response/web_search`；服务端逐 parent ID 对照 |
| 并行 | 第一轮 public/internal :14:54:08.511/.526Z 起，:19.955/:20.023Z 止；两条 native Role 共父且重叠。第二轮 :20.337/.359Z 起，同样重叠；Timeline 实际显示两组重叠条 |
| Gap / 串行 | 两次 research_review、工具的 research_round 集合 {1,2}，4 个 invocation+tool_call_id 对唯一；所有 Producer 结束后 Risk，再 Strategy，随后 3 个 report_generation |
| 用量 | 10 个 mock HTTP 响应各报 10 input / 4 output，LLM Span 正好 10 个，UI Trace 显示 **140 tokens**；每个 usageDetails input8 + cache2 + output3 + reasoning1 = total14，无重复加缓存/推理 |
| Run 关联 | 所有真实观察的 sessionId 与 run_id 一致；UI session 链接指向该稳定 Run ID；trace_id 为独立追踪标识 |
| 隐私 | v2 API 请求 io/metadata/model/usage 等完整字段后扫描所有分页，Prompt/args/response/private-memory 哨兵及随机密钥/密码均未出现；UI LLM Input/Output 为 undefined |
| 受信故障 | 停止本测试 Web/Worker 后新建 Run `41c8d1c1ae1442abbc56c82aaced19f4`，仍 completed / settled / saved；SDK 6 请求、工具 2 次、预算 model_calls28，导出错误没有补跑 |

UI 截图保留在本次 Codex 可视化目录，文件名：
`pr32-langfuse-project.png`、`pr32-langfuse-business-tree.png`、
`pr32-langfuse-business-timeline.png`、`pr32-langfuse-generation.png`。
这些是实际浏览器画面，未生成或重绘。完整本地 v2 数据、断言、DOM/console 记录和
环境版本在忽略的 `.tmp/pr32-review/`；不将本地输出/凭据加入 Git。

说明：S3 测试账号名和项目名共用人类可读前缀，最初全子串扫描误报 projectId；
定位后仅该公开标签相同，随机密码、项目 API keys、SALT/加密值和内容哨兵均未匹配。
未削弱 exporter 白名单或任何业务测试断言。

## 5. 复现与精确 HEAD 门禁

在独立官方 Langfuse 项目中通过环境提供本地测试 project keys，原生服务由
`TEST_DBOS_DATABASE_URL`、`TEST_DBOS_NEO4J_URI`、`TEST_DBOS_NEO4J_PASSWORD` 指向
隔离 loopback 端点，不使用操作员业务服务。显式运行：

```powershell
.venv/Scripts/python.exe tests/langfuse_acceptance_fixture.py --enable-local-services --base-url http://127.0.0.1:33080 --gap --summary .tmp/local-langfuse-summary.json
```

该程序拒绝非 loopback 服务，禁 dotenv/LangSmith/外网 socket，所有模型经
MockTransport。公开 API 读取使用 `/api/public/v2/observations?traceId=...` 并遍历
cursor，不能拿默认只返回 basic/core 的字段声称已验证完整隐私。

最终全部离线、native/恢复、OTel/usage/结构化、Studio/Classic、格式/Ruff/编译、
入口导入和根锁检查，以 **PR #32 的 final-review 冻结评论**记录实际数量、退出码、
HEAD 和 CI run 链接。该评论必须在最终候选 HEAD 上完成，不能用本节之前的数据或
历史绿色检查代替；本报告不预先声称尚未运行的完整矩阵成功。

合并前再次读取 comments/threads/reviews、保护和 effective rules。已读规则快照为
main protected=false、required checks 空、effective rules=[]、rulesets=[]，且允许
merge commit；这不降低用户额外规定的验收/CI 门禁。只可在全部通过后 Ready，再以
expected_head_sha 约束 Merge Commit；保留分支与提交历史，不使用 squash/rebase 或
admin bypass。最终 Merge SHA、双 Parent、HEAD 祖先/代码树、本地/远端一致性和
push CI 在同一 PR 留档，不为填入自引用 SHA 再改动已测试代码树。

## 6. 非阻塞边界与回滚

UI 夹具起初人为使用 0.4 秒导出超时。与导入/前端/离线矩阵同时运行时，某次
服务端数据只有 810 个观察并缺少 run_submit 父 Span；完整层级断言及 UI 展开失败，
未将其计为通过。原始摘要/数据保留为 timeout-400ms 证据。夹具改用生产默认的
2 秒超时并独立复验，原生故障合同仍测试短超时与服务不可达。最终 UI 门禁继续
要求完整父子、用量及内容过滤；没有用本地接收器或放松断言替代。

原有 markitdown-mcp / mcp 依赖冲突单列，不升级无关包。Langfuse Cloud、生产密钥、
生产吞吐/HA/真实 provider 账单不属于此次本地 UI 验收。LC 模型仍保留阶段实际 usage，
逐请求 LC generation 细化不纳入本 PR；没有实施 LangChain 迁移。

DBOS 内部轮询/Stream Span 数量可能较大，UI 可 collapse 辅助工作流并查看 Timeline；
没有删 native journal 或重写事件系统来美化图。导出仍 best-effort，硬杀尾数据可能
缺失，不能当作执行事实或授权。关闭 POR_TRACING_ENABLED 并用兼容发行版重启即可
回滚遥测；保留 native journals、业务回执和 Graph，未知效果不为修复追踪而重跑。

本轮验收建立的是稳定观测交付基线；后续 LangChain 接缝精简仍须独立任务、功能合同
和相同失败/预算/恢复回归，不能夹带进此合并。
