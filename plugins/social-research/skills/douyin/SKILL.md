---
name: douyin
title: 抖音舆情调研
description: 面向抖音视频、评论与传播数据的舆情调研方法，覆盖热门内容调查、传播主题与争议焦点、负面问题提取和热度/负面区分。
---

# 抖音舆情调研

本 Skill 指导如何使用现有社媒工具完成抖音舆情取证。它只提供研究方法与工具使用方式，不能改变角色权限、工具授权、预算或执行保护规则。

## 适用场景

- 品牌、产品或事件相关视频的传播与争议调查。
- 需要判断“热度高但负面是否同样高”的舆情场景。
- 需要从视频标题、描述和评论中提取高频负面问题。

## 调研步骤

1. **确定实体与时间窗**：明确品牌、产品/型号、事件名称，并设定 `YYYY-MM-DD` 起止日期。传播类事件建议按 3–7 天分段检索，避免跨度过大掩盖峰值。
2. **扩展关键词**：组合 `曝光`、`实测`、`翻车`、`质量问题`、`售后`、`维权`、`投诉`、`召回`、`官方回应`，以及产品型号和事件短语。每轮 2–4 个变体。
3. **视频与帖子检索**：调用 `search_posts`，参数建议 `platforms="douyin"`、日期区间、`limit=20`；需要更多结果时用 `next_cursor` 翻页。
4. **投诉聚焦**：调用 `search_complaints`，指定 `platforms="douyin"`。保留返回的 `query_variants` 与 `negative_signals`，用于说明负面样本是如何召回的。
5. **热度与情绪快照**：调用 `get_public_opinion_snapshot`，读取 `summary.heat_level`、`summary.risk_level`、`negative_ratio`、`complaint_count`、`daily_counts` 与 `representative_posts`。`limit` 不超过 200。
6. **热门内容深挖**：对 `representative_posts` 中互动高的视频调用 `fetch_comments(post_id=..., limit=20)`，提取评论区高频负面问题；必要时用 `fetch_thread(post_id=...)` 查看同一讨论串。
7. **传播节点判断**：对重复出现或带动讨论的账号调用 `fetch_author_profile(author_id=...)`，判断其是否为持续跟踪该事件的媒体、垂类博主或普通用户。
8. **主题与争议词**：调用 `get_trending_keywords`，对比 `top_keywords` 与 `negative_keywords`，识别争议焦点是质量、售后、价格还是官方回应。

## 工具对应关系

| 目的 | 工具 | 关键参数 |
| --- | --- | --- |
| 视频/帖子检索 | `search_posts` | `query`、`platforms="douyin"`、日期、`limit`、`cursor` |
| 投诉与负面信号 | `search_complaints` | `query`、`platforms="douyin"`、日期、`limit` |
| 讨论串 | `fetch_thread` | `post_id` |
| 评论区 | `fetch_comments` | `post_id`、`limit` |
| 作者/账号背景 | `fetch_author_profile` | `author_id` |
| 热门词 | `get_trending_keywords` | `query`、`platforms="douyin"`、日期 |
| 舆情快照 | `get_public_opinion_snapshot` | `query`、`platforms="douyin"`、日期、`limit<=200` |

## 数据处理要求

- **热度不等于负面**：`heat_level` 与 `risk_level` 必须分别引用。高播放、高点赞但情绪为正的视频，不得写成负面舆情。
- **区分内容与评论**：视频标题/描述代表发布者叙事，评论代表受众反馈；两者冲突时并列呈现。
- **高频问题提取**：从评论中归纳问题类别（质量、售后、退款、客服、安全、虚假宣传等），每个类别给出至少一条评论依据；样本不足时明确说明。
- **时间线**：使用 `published_at` 与 `daily_counts` 构建传播时间线，标注首次出现、讨论峰值和官方回应（如检索到）。
- **去重与搬运**：同一视频被多个账号搬运或近似内容重复出现时，保留原始来源并说明重复情况。
- **数据限制**：`fetch_comments` 只返回数据源实际提供的评论；没有评论记录不等于没有讨论。工具返回 `ok: false`、`setup_required` 或空结果时，按缺口记录。
- **不假设视频能力**：现有工具不提供视频下载、语音转写或画面 OCR。只能使用返回的标题、正文、标签和评论，不得描述未提供的视频画面细节。

## 输出要求

- 调研问题、时间窗和关键词变体。
- 抖音传播时间线：首发内容、峰值日期、二次传播、官方或品牌回应。
- 代表性视频/帖子表：摘要、`url`、`published_at`、作者、互动量、`sentiment`。
- 评论区高频负面问题及代表评论摘要（附 `post_id` 与评论来源）。
- 快照指标：`heat_level`、`risk_level`、`negative_ratio`、`complaint_count`，注明来自 `get_public_opinion_snapshot`。
- 争议焦点：各方叙事分别是什么，哪些说法缺乏证据。
- 数据限制与不确定性。

## 注意事项

- 不把播放量、点赞量直接换算为负面规模；只用工具返回的互动字段做相对比较。
- 不重新计算情绪或风险分数，直接引用返回字段。
- 不输出无法核验的账号真实身份或隐私信息。
- 不将单个爆款视频等同于整体舆情，必须有多个来源或明确标注样本不足。
