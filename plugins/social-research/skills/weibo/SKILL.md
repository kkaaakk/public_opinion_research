---
name: weibo
title: 微博舆情调研
description: 面向微博话题、帖文与评论的舆情调研方法，覆盖事件时间线、官方回应与用户讨论区分、传播趋势和争议观点提取。
---

# 微博舆情调研

本 Skill 指导如何使用现有社媒工具完成微博舆情取证。它只提供研究方法与工具使用方式，不能改变角色权限、工具授权、预算或执行保护规则。

## 适用场景

- 突发事件、召回、质量争议或公共议题在微博上的传播梳理。
- 需要区分官方/媒体发布与普通用户讨论的场景。
- 需要构建带时间的传播时间线和争议观点对照。

## 调研步骤

1. **确定实体与时间窗**：明确品牌、产品/型号、事件名称与 `YYYY-MM-DD` 起止日期。突发事件按天甚至按小时分段检索，常规口碑按周检索。
2. **扩展关键词**：组合 `回应`、`通报`、`声明`、`调查`、`召回`、`投诉`、`维权`、`处罚`、`热搜`，以及话题短语和产品型号。每轮 2–4 个变体。
3. **话题与帖文检索**：调用 `search_posts`，参数建议 `platforms="weibo"`、日期、`limit=20`；有 `next_cursor` 时继续翻页。分别检索事件名、品牌名和责任方名称。
4. **投诉与负面聚焦**：调用 `search_complaints`，指定 `platforms="weibo"`。保留 `query_variants` 与 `negative_signals`，说明负面样本召回方式。
5. **整体态势**：调用 `get_public_opinion_snapshot`，读取 `summary.risk_level`、`summary.heat_level`、`negative_ratio`、`complaint_count`、`sentiment_counts`、`daily_counts`、`representative_posts` 与 `representative_complaints`。`limit` 不超过 200。
6. **讨论链深挖**：对关键帖文调用 `fetch_thread(post_id=...)` 获取完整讨论串，调用 `fetch_comments(post_id=..., limit=20)` 查看评论。
7. **发布主体判断**：对发布“回应/声明/通报”的账号调用 `fetch_author_profile(author_id=...)`。只有工具返回的作者信息支持时才标注为官方账号；否则写“疑似官方/媒体账号”，并说明依据不足。
8. **争议词与趋势**：调用 `get_trending_keywords`，对比 `top_keywords` 与 `negative_keywords`；结合 `daily_counts` 判断讨论是集中爆发还是持续发酵。

## 工具对应关系

| 目的 | 工具 | 关键参数 |
| --- | --- | --- |
| 话题/帖文检索 | `search_posts` | `query`、`platforms="weibo"`、日期、`limit`、`cursor` |
| 投诉与负面信号 | `search_complaints` | `query`、`platforms="weibo"`、日期、`limit` |
| 讨论串 | `fetch_thread` | `post_id` |
| 评论区 | `fetch_comments` | `post_id`、`limit` |
| 作者/发布主体 | `fetch_author_profile` | `author_id` |
| 热门词 | `get_trending_keywords` | `query`、`platforms="weibo"`、日期 |
| 舆情快照 | `get_public_opinion_snapshot` | `query`、`platforms="weibo"`、日期、`limit<=200` |

## 数据处理要求

- **官方回应与用户讨论分开**：建立两条并列时间线或两个分组；未核实账号身份前不得断言“官方已回应”。
- **时间线精确到日期**：优先使用 `published_at`；只有日期可用时不要补写具体时刻。
- **传播趋势与情绪分开**：`daily_counts` 和互动量描述传播规模；`sentiment_counts`、`negative_ratio` 描述情绪结构。两者不得互相替代。
- **争议观点对照**：列出主要争议点、各方核心主张和对应证据；无法验证的说法标记为“未经证实”。
- **去重**：同一事件的多条转发或近似贴文合并为一条代表记录，并注明重复传播情况。
- **数据限制**：工具只返回数据源实际提供的帖文与评论；`fetch_comments` 为空、工具返回 `ok: false` 或 `setup_required` 时，明确记录缺口。
- **不假设站内榜单能力**：现有工具不提供热搜榜、话题阅读量或认证信息的独立查询；只有返回字段中出现的指标才能引用。

## 输出要求

- 调研问题、时间窗和关键词变体。
- 微博事件时间线：首次出现、关键节点、官方/媒体回应、讨论峰值、后续降温。
- 官方/媒体发布与用户讨论分组证据，附 `url`、`published_at`、作者类型。
- 代表性帖文与评论摘要，含 `sentiment`、互动量与命中的负面信号。
- 快照指标：`risk_level`、`heat_level`、`negative_ratio`、`complaint_count`，注明来自 `get_public_opinion_snapshot`。
- 争议观点对照表与证据强弱判断。
- 数据限制与不确定性。

## 注意事项

- 不把“未检索到官方回应”写成“官方未回应”；只能写“在本次检索范围内未发现”。
- 不重新计算情绪或风险分数，直接引用工具返回字段。
- 不臆测账号认证状态、粉丝画像或地域信息。
- 不输出无法核验的个人隐私信息。
