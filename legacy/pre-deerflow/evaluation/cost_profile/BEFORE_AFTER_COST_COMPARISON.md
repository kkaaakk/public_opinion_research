# Before / After Single-Case Cost Comparison

## Executive Summary

- 最大 Token 消耗节点：After=public_signal_agent；Before=public_signal_agent。排序使用已知 token subtotal，缺失 usage 的调用没有被猜测。
- 最大重复上下文来源：After 的 role_reports；估算重复输入为 171151 tokens。
    - Dynamic P&E 直接新增的 Review + public/internal follow-up 调用：134；已知 token subtotal=629027（Review usage 未返回，故为下界）。After 的 risk/strategy 在 Round 2 继续执行，但 Before 也有对应下游阶段，不计入直接新增。
- 原有架构成本：Before model calls=135，tool calls=46，exact total tokens=N/A，duration=493970 ms。
- 优先检查：P0 ReAct history growth；P0 full role_reports injection；P1 compression 与 section evidence duplication。

## Test identity and configuration

| Item | Before | After |
|---|---|---|
| Commit | d0270b6be4ab09eb381db008dd62463e2d2327eb | 5989480e523909661153eb23f3dea374d842a887 |
| Case | cost_case_tesla_safety_public_opinion | cost_case_tesla_safety_public_opinion |
| Status | success | success |
| Model | deepseek:deepseek-chat | deepseek:deepseek-chat |
| max_react_tool_calls | 10 | 10 |
| max_research_rounds | N/A | 2 |

The runner did not override model, temperature, ReAct limit, research-round limit, or tool whitelist. RAG was explicitly enabled identically for both versions because the raw .env/defaults leave RAG disabled and the internal-knowledge role otherwise fails before a complete run; the RAG algorithm was not changed.

## Before vs After

| Metric | Before | After | Delta |
|---|---:|---:|---:|
| Model Calls | 135 | 233 | +98.00 |
| Tool Calls | 46 | 60 | +14.00 |
| Input Tokens | N/A | N/A | N/A |
| Output Tokens | N/A | N/A | N/A |
| Total Tokens | N/A | N/A | N/A |
| Duration (ms) | 493970 | 642458 | +148488.00 |
| Public Signal model calls | 81 | 190 | +109.00 |
| Internal Knowledge model calls | 8 | 15 | +7.00 |
| Research Review calls | 0 | 2 | +2.00 |
| Compression calls | 4 | 6 | +2.00 |
| Section Writer calls | 1 | 1 | +0.00 |

| Partial observed token subtotal | Before | After | Delta |
|---|---:|---:|---:|
| Known input tokens | 782327 | 1274421 | +492094.00 |
| Known output tokens | 45617 | 57966 | +12349.00 |
| Known total-token subtotal | 827944 | 1332387 | +504443.00 |

## Dynamic P&E incremental cost

| Component | After calls | Known total tokens | Exact? |
|---|---:|---:|---|
| Research Review | 2 | 0 | False |
| Public/internal follow-up role calls | 132 | 629027 | False |
| Direct Dynamic subset (Review + follow-up) | 134 | 629027 | estimated when any usage is missing |
| Round-2 risk/strategy downstream (not direct increment) | 22 | 327362 | estimated when any usage is missing |

Dynamic P&E cost is the After review/follow-up subset. A causal increase requires completed runs with both versions and stable provider behavior.

## Full Flow Cost Breakdown

### Before

| Stage | Model Calls | Input Tokens | Output Tokens | Total Tokens | Tool Calls | Model Duration ms |
|---|---:|---:|---:|---:|---:|---:|
| clarification | 1 | N/A | N/A | N/A | 0 | 6656 |
| research_brief | 1 | N/A | N/A | N/A | 0 | 7944 |
| report_planner | 1 | N/A | N/A | N/A | 0 | 11591 |
| internal_knowledge R1 | 7 | 91156 | 4515 | 95671 | 17 | 46175 |
| public_signal R1 | 9 | 236318 | 3292 | 239610 | 18 | 32923 |
| public_signal webpage_summarization R1 | 71 | N/A | N/A | N/A | 0 | 444904 |
| internal_knowledge compress R1 | 1 | 23949 | 5171 | 29120 | 0 | 29597 |
| public_signal compress R1 | 1 | 59993 | 8192 | 68185 | 0 | 82656 |
| risk_assessment R1 | 6 | 168712 | 2058 | 170770 | 6 | 25433 |
| risk_assessment webpage_summarization R1 | 30 | N/A | N/A | N/A | 0 | 167415 |
| risk_assessment compress R1 | 1 | 38035 | 8192 | 46227 | 0 | 67488 |
| response_strategy R1 | 4 | 105359 | 1419 | 106778 | 5 | 16247 |
| response_strategy compress R1 | 1 | 27896 | 7935 | 35831 | 0 | 65023 |
| section_writer | 1 | 30909 | 4843 | 35752 | 0 | 49328 |
| **TOTAL** | **135** | **N/A (known 782327)** | **N/A (known 45617)** | **N/A (known 827944)** | **46** | **1053380** |

### After

| Stage | Model Calls | Input Tokens | Output Tokens | Total Tokens | Tool Calls | Model Duration ms |
|---|---:|---:|---:|---:|---:|---:|
| clarification | 1 | N/A | N/A | N/A | 0 | 4816 |
| research_brief | 1 | N/A | N/A | N/A | 0 | 964 |
| report_planner | 1 | N/A | N/A | N/A | 0 | 5715 |
| internal_knowledge R1 | 7 | 62782 | 1485 | 64267 | 15 | 17185 |
| public_signal R1 | 8 | 190697 | 2235 | 192932 | 16 | 26238 |
| public_signal webpage_summarization R1 | 56 | N/A | N/A | N/A | 0 | 309344 |
| internal_knowledge compress R1 | 1 | 15760 | 4645 | 20405 | 0 | 27058 |
| public_signal compress R1 | 1 | 46337 | 8191 | 54528 | 0 | 87343 |
| research_review #1 | 1 | N/A | N/A | N/A | 0 | 39897 |
| public_signal R2 | 7 | 423296 | 3907 | 427203 | 10 | 43014 |
| internal_knowledge R2 | 6 | 76688 | 1573 | 78261 | 11 | 18866 |
| public_signal webpage_summarization R2 | 117 | N/A | N/A | N/A | 0 | 677129 |
| internal_knowledge compress R2 | 1 | 17517 | 3833 | 21350 | 0 | 24599 |
| public_signal compress R2 | 1 | 94018 | 8195 | 102213 | 0 | 81208 |
| research_review #2 | 1 | N/A | N/A | N/A | 0 | 22196 |
| risk_assessment R2 | 4 | 146045 | 1320 | 147365 | 6 | 15782 |
| risk_assessment webpage_summarization R2 | 14 | N/A | N/A | N/A | 0 | 65302 |
| risk_assessment compress R2 | 1 | 41357 | 8192 | 49549 | 0 | 62128 |
| response_strategy R2 | 2 | 75884 | 4545 | 80429 | 2 | 52037 |
| response_strategy compress R2 | 1 | 41827 | 8192 | 50019 | 0 | 50473 |
| section_writer | 1 | 42213 | 1653 | 43866 | 0 | 14107 |
| **TOTAL** | **233** | **N/A (known 1274421)** | **N/A (known 57966)** | **N/A (known 1332387)** | **60** | **1645401** |

## Top Token Consumers

| Rank | Version | Call type | Node | Agent role | Round | Total | Input | Output | Duration ms |
|---:|---|---|---|---|---:|---:|---:|---:|---:|
| 1 | After | research_compression | public_signal_agent | public_signal | 2 | 102213 | 94018 | 8195 | 81208 |
| 2 | After | react_reasoning | public_signal_agent | public_signal | 2 | 96355 | 96307 | 48 | 1720 |
| 3 | After | react_reasoning | public_signal_agent | public_signal | 2 | 95455 | 94333 | 1122 | 12140 |
| 4 | After | react_reasoning | public_signal_agent | public_signal | 2 | 83181 | 82384 | 797 | 8196 |
| 5 | Before | research_compression | public_signal_agent | public_signal | 1 | 68185 | 59993 | 8192 | 82656 |
| 6 | After | react_reasoning | public_signal_agent | public_signal | 2 | 65592 | 65302 | 290 | 4053 |
| 7 | Before | react_reasoning | public_signal_agent | public_signal | 1 | 61566 | 61202 | 364 | 3496 |
| 8 | After | research_compression | public_signal_agent | public_signal | 1 | 54528 | 46337 | 8191 | 87343 |
| 9 | Before | react_reasoning | public_signal_agent | public_signal | 1 | 51837 | 51274 | 563 | 6422 |
| 10 | After | research_compression | response_strategy_agent | response_strategy | 2 | 50019 | 41827 | 8192 | 50473 |
| 11 | After | research_compression | risk_assessment_agent | risk_assessment | 2 | 49549 | 41357 | 8192 | 62128 |
| 12 | After | react_reasoning | public_signal_agent | public_signal | 2 | 49453 | 48585 | 868 | 8988 |
| 13 | After | react_reasoning | public_signal_agent | public_signal | 1 | 47915 | 47873 | 42 | 836 |
| 14 | After | react_reasoning | public_signal_agent | public_signal | 1 | 47374 | 46747 | 627 | 8071 |
| 15 | Before | research_compression | risk_assessment_agent | risk_assessment | 1 | 46227 | 38035 | 8192 | 67488 |
| 16 | After | section_writer | section_writer | None | N/A | 43866 | 42213 | 1653 | 14107 |
| 17 | Before | react_reasoning | public_signal_agent | public_signal | 1 | 43360 | 43016 | 344 | 3754 |
| 18 | After | react_reasoning | risk_assessment_agent | risk_assessment | 2 | 42984 | 42942 | 42 | 1151 |
| 19 | After | react_reasoning | risk_assessment_agent | risk_assessment | 2 | 42560 | 42010 | 550 | 6547 |
| 20 | After | react_reasoning | response_strategy_agent | response_strategy | 2 | 42473 | 38490 | 3983 | 44301 |

## Context Duplication Analysis

### ReAct history growth

#### Before

| Agent | Round | Calls | Input tokens | Deltas | Growth observable |
|---|---:|---:|---|---|---|
| internal_knowledge | 1 | 7 | 2012, 5280, 10004, 13957, 17845, 20599, 21459 | 3268, 4724, 3953, 3888, 2754, 860 | Yes |
| public_signal | 1 | 9 | 3000, 3278, 11639, 12352, 19705, 30852, 43016, 51274, 61202 | 278, 8361, 713, 7353, 11147, 12164, 8258, 9928 | Yes |
| risk_assessment | 1 | 6 | 16521, 17046, 28108, 29280, 38138, 39619 | 525, 11062, 1172, 8858, 1481 | Yes |
| response_strategy | 1 | 4 | 23800, 25344, 27722, 28493 | 1544, 2378, 771 | Yes |

#### After

| Agent | Round | Calls | Input tokens | Deltas | Growth observable |
|---|---:|---:|---|---|---|
| internal_knowledge | 1 | 7 | 1390, 3704, 5982, 8380, 11492, 15539, 16295 | 2314, 2278, 2398, 3112, 4047, 756 | Yes |
| public_signal | 1 | 8 | 2378, 2839, 14398, 14898, 30552, 31012, 46747, 47873 | 461, 11559, 500, 15654, 460, 15735, 1126 | Yes |
| public_signal | 2 | 7 | 8170, 28215, 48585, 65302, 82384, 94333, 96307 | 20045, 20370, 16717, 17082, 11949, 1974 | Yes |
| internal_knowledge | 2 | 6 | 5892, 8686, 11860, 14498, 17277, 18475 | 2794, 3174, 2638, 2779, 1198 | Yes |
| risk_assessment | 2 | 4 | 30103, 30990, 42010, 42942 | 887, 11020, 932 | Yes |
| response_strategy | 2 | 2 | 37394, 38490 | 1096 | Yes |

An increasing exact input-token sequence is evidence that a role's prior history is resent. N/A cannot be converted into a conclusion.

### role_reports reuse

#### Before

| Consumer | Model calls | Roles | Report chars/call | Estimated tokens/call | Estimated repeated input |
|---|---:|---|---:|---:|---:|
| risk_assessment | 6 | public_signal, internal_knowledge | 51331 | 12833 | 76998 |
| response_strategy | 4 | public_signal, internal_knowledge, risk_assessment | 84091 | 21023 | 84092 |
| section_writer | 1 | internal_knowledge, public_signal, risk_assessment, response_strategy | 115240 | 28810 | 28810 |

#### After

| Consumer | Model calls | Roles | Report chars/call | Estimated tokens/call | Estimated repeated input |
|---|---:|---|---:|---:|---:|
| research_review | 2 | public_signal, internal_knowledge | 49734 | 12434 | 24868 |
| risk_assessment | 4 | public_signal, internal_knowledge | 65496 | 16374 | 65496 |
| response_strategy | 2 | public_signal, internal_knowledge, risk_assessment | 97535 | 24384 | 48768 |
| section_writer | 1 | internal_knowledge, public_signal, risk_assessment, response_strategy | 128076 | 32019 | 32019 |

Character/token estimates are explicitly estimated because Observer events do not include prompt provenance.

### Follow-up report append

- internal_knowledge: growth=-2717 chars; estimated token growth=-679
- public_signal: growth=147 chars; estimated token growth=36
- risk_assessment: growth=N/A chars; estimated token growth=N/A
- response_strategy: growth=N/A chars; estimated token growth=N/A

### Compression and Section Writer

- Before compression share: 0.2166; After: 0.2237.
- Before section-writer input tokens: 30909.
- After section-writer input tokens: 42213.

## Input/output ratios and token shares

### Before

| Component | Known tokens | Share of known tokens | Exact/estimated |
|---|---:|---:|---|
| ReAct | 612829 | 0.7402 | estimated |
| Compression | 179363 | 0.2166 | estimated |
| Research Review | 0 | 0 | estimated |
| Risk + Strategy | 359606 | 0.4343 | estimated |
| Section Writing | 35752 | 0.0432 | estimated |
| Final Report Compile | 0 | 0 | estimated |

### After

| Component | Known tokens | Share of known tokens | Exact/estimated |
|---|---:|---:|---|
| ReAct | 990457 | 0.7434 | estimated |
| Compression | 298064 | 0.2237 | estimated |
| Research Review | 0 | 0 | estimated |
| Risk + Strategy | 327362 | 0.2457 | estimated |
| Section Writing | 43866 | 0.0329 | estimated |
| Final Report Compile | 0 | 0 | estimated |

## Recommended Optimization Priority

1. P0 — ReAct history growth: verify consecutive exact input usage and full AI/tool history resend.
2. P0 — Full role_reports injection: separate assignment, review, risk, strategy, and section prompts.
3. P1 — compress_research: compare compression input/output against the context it removes.
4. P1 — Section evidence duplication: inspect multi-section prompts and final-report fallback.

## Limitations

- Only one Case and one run per version were requested.
- Current .env model is deepseek:deepseek-chat; it is not the named deepseek-v4-flash unless the environment is changed.
- No billed cost field was returned; no online price was used to invent one.
- Missing token fields remain N/A. Report-character context reuse and its share are estimated.
- The Observer HTTP sidecar and social API may be unavailable; raw JSON contains exact errors and in-process Observer events.
