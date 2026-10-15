# Dynamic Research Plan-and-Execute Before / After Benchmark

## 状态：BLOCKED，不得据此做架构结论

本次没有完成用户要求的 10 Case × 2 版本有效 A/B Benchmark。为了避免伪造数据，Before / After 的质量、成本、Dynamic Loop 指标全部保留为 N/A，也没有选择 KEEP、KEEP_WITH_CHANGES 或 ROLLBACK。

## A. 版本与环境核验

| 项目 | 实际值 |
|---|---|
| Before | d0270b6be4ab09eb381db008dd62463e2d2327eb |
| After | 5989480e523909661153eb23f3dea374d842a887 |
| Commit check | 两个 worktree 均与用户指定 SHA 完全一致 |
| Case 配置数 | 10 |
| 重复次数 | 1（计划配置；有效完成数为 0） |
| Python | 3.11.9，C:\develop\python311\python.exe |
| Agent Observer | 0.2.0，LangGraph adapter 可导入 |
| 原始 .env 模型 | deepseek:deepseek-chat |
| fallback 尝试 | Google Gemini/Gemma；没有形成有效 paired run |
| 原始 Web 配置 | Tavily；烟测期间因成本/可用性问题，harness 后续尝试过 search_api=none |
| RAG 计划配置 | 本地 public-opinion knowledge + memory，hash embedding，memory vectorstore，simple reranker，graph disabled |

开发目录已有用户未跟踪文件未修改。Benchmark 辅助代码只放在 evaluation/plan_execute_ab/。

## B. 真实 smoke 执行证据

以下是实际运行过的命令结果，不是 Benchmark 结果：

| Artifact | 版本 | 结果 | 实际观测 |
|---|---|---|---|
| smoke_before.json | Before | 300 秒超时 | 85 model calls、41 tool calls、无 final report |
| smoke_before_2.json | Before | 240 秒超时 | 43 model calls、16 tool calls、无 final report |
| smoke_before_3.json | Before | HTTP 402 | Error code: 402 ... Insufficient Balance，发生在 write_research_brief |
| smoke_google_before.json | Before | 未完成 | Google complex graph 请求出现 HTTP 503 UNAVAILABLE |
| smoke_google31lite_before.json / smoke_google_latest_before.json / smoke_gemma_before.json | Before | 未完成 | 复杂图请求出现 503；Gemini 2.5 另外返回过 HTTP 429，明确指出 free-tier gemini-2.5-flash quota 为 20 requests |

### 实际恢复出的 Before 链路

一次 DeepSeek/Tavily smoke 的 Agent Observer span_started 顺序为：

enrich_query_images → clarify_with_user → write_research_brief → plan_report_sections → research_supervisor → internal_knowledge_agent → public_signal_agent → risk_assessment_agent → response_strategy_agent → section_writer

该运行在最终报告完成前超时，因此不能把它当作完成的 Case。它也没有进入 After，因此没有任何真实的 research_review、Follow-up task 或新增证据可以报告。

### 运行时基础设施

- 127.0.0.1:8766 Agent Observer HTTP sidecar：连接被拒绝。已使用安装的 Agent Observer v0.2 SDK + 原有 LangGraph adapter 做内存事件记录，未伪造远端事件。
- 127.0.0.1:9000 Social API：连接被拒绝。harness 准备了仓库固定 JSON fixture，但在模型阻塞前未形成有效 paired benchmark。
- DeepSeek key 在 smoke 初期曾返回成功，随后 API 明确返回余额不足。
- Google key 的简单请求和部分工具 smoke 曾成功，但复杂图请求受到 503/429，不能据此外推 20 个完整运行。

## C. Requested metrics

| 指标 | Before | After | Delta |
|---|---:|---:|---:|
| Total duration | N/A | N/A | N/A |
| Node executions | N/A | N/A | N/A |
| Agent executions | N/A | N/A | N/A |
| Model calls | N/A | N/A | N/A |
| Tool calls | N/A | N/A | N/A |
| Web/Search calls | N/A | N/A | N/A |
| RAG calls | N/A | N/A | N/A |
| MCP calls | N/A | N/A | N/A |
| Input/Output/Total tokens | N/A | N/A | N/A |
| Final report chars/tokens | N/A | N/A | N/A |
| Evidence Coverage | N/A | N/A | N/A |
| Critical Evidence Coverage | N/A | N/A | N/A |
| Unsupported Claim Rate | N/A | N/A | N/A |
| Conflict Detection | N/A | N/A | N/A |
| Final Research Quality | N/A | N/A | N/A |
| Research Review / Follow-up rounds | N/A | N/A | N/A |

Smoke 中的 85/41 和 43/16 只用于诊断供应商/成本问题，未进入上述对比。

## D. Dynamic Plan-and-Execute 判断

没有 After 运行，所以第一轮完成数、Round 2 数、Research Gap、next_tasks、Gap Detection Recall、Gap Resolution Rate、follow-up 角色分布、重复搜索和 max_research_rounds 均为 N/A。

## E. 当前阻塞与恢复条件

正式运行需要至少一个可稳定完成结构化输出、工具调用和最终报告的模型授权：

1. 恢复 DeepSeek balance，并使用原始 deepseek:deepseek-chat 配置；或
2. 提供/启用一个稳定的替代模型 quota，并明确接受它作为两版共同 Benchmark 模型。

恢复后，在仓库根目录执行：

~~~powershell
python evaluation/plan_execute_ab/run_benchmark.py --repeat 1 --case-timeout 900
~~~

如果希望保留 Tavily 实时 Web 证据，需要另外确保 Tavily quota 和摘要模型请求可用；如果只要可复现 smoke，可继续使用本地 social fixture + local RAG 配置。

## F. Artifact paths

- cases.jsonl
- rubric.json
- run_benchmark.py
- evaluate_results.py
- compare_results.py
- before_results.json（blocked，0 个有效运行）
- after_results.json（blocked，0 个有效运行）
- comparison.json（blocked，0 个有效 paired run）
- smoke_before.json（真实 Before 诊断原始记录）
- smoke_before_2.json（真实 Before 诊断原始记录）
- smoke_before_3.json（DeepSeek 402 原始记录）
- smoke_google_before.json（Google 503 诊断原始记录）
- cases/（有效 A/B 数据产生后由 comparator 生成）

以上文件位于 evaluation/plan_execute_ab/results/。

## G. 结论

BLOCKED — 不做 KEEP / KEEP_WITH_CHANGES / ROLLBACK 判断。

选择任何架构结论都会把没有完成 final report 的 smoke 或供应商错误误当成研究质量证据。Benchmark harness、10 个 Case、固定 rubric 和原始诊断记录已经就绪；补足模型余额/quota 后可直接重跑，不需要修改核心业务逻辑。
