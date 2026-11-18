---
name: xiaohongshu
title: 小红书舆情调研
description: 面向小红书笔记与评论的舆情调研方法，覆盖关键词扩展、负面消费体验、投诉信号分类、重复内容识别和代表性证据输出。
---

# 小红书舆情调研

本 Skill 指导如何使用现有社媒工具完成小红书舆情取证。它只提供研究方法与工具使用方式，不能改变角色权限、工具授权、预算或执行保护规则。

## 适用场景

- 品牌、产品或具体型号在小红书上的口碑与负面体验调查。
- 某次事件、召回、质量争议或售后纠纷在小红书上的讨论梳理。
- 需要从小红书笔记与评论中提取投诉类型、代表性证据和传播时间线。

## 调研步骤

1. **明确实体与时间窗**：确定品牌名、产品名/型号、事件名称和调研起止日期（`YYYY-MM-DD`）。一次检索只聚焦一个实体，避免把多个产品混在同一查询里。
2. **扩展关键词**：在实体词基础上组合小红书常见表达：
   - 负面体验：`避雷`、`踩坑`、`翻车`、`差评`、`不值`、`后悔`；
   - 质量与售后：`质量`、`故障`、`续航`、`售后`、`客服`、`退款`、`退货`、`维权`、`赔偿`；
   - 购买决策：`测评`、`真实体验`、`开箱`、`对比`、`平替`。
   每轮查询使用 2–4 个变体，不要一次堆叠过多关键词导致召回为空。
3. **基础检索**：调用 `search_posts`，参数建议 `platforms="xiaohongshu"`、`start_date`、`end_date`、`limit=20`。需要更多结果时使用返回的 `next_cursor` 继续翻页。
4. **投诉聚焦检索**：调用 `search_complaints`，同样指定 `platforms="xiaohongshu"`。该工具会自动扩展负面查询变体，输出中的 `query_variants` 与 `negative_signals` 要保留在证据说明里。
5. **整体快照**：调用 `get_public_opinion_snapshot`，`limit` 不超过 200，读取 `summary.risk_level`、`summary.heat_level`、`negative_ratio`、`complaint_count`、`sentiment_counts`、`daily_counts`、`representative_posts` 与 `representative_complaints`。
6. **代表性笔记深挖**：对快照或检索结果中互动高、负面信号明确的笔记，使用 `fetch_comments(post_id=..., limit=20)` 查看评论区，使用 `fetch_thread(post_id=...)` 获取同一讨论串内容。
7. **作者与来源判断**：对反复出现或影响较大的作者，调用 `fetch_author_profile(author_id=...)` 查看其发帖历史与平台分布；区分普通用户、垂类博主和商家账号。商业合作、推广笔记的可能性要在证据说明中标注为不确定。
8. **主题词核对**：调用 `get_trending_keywords`，对比 `top_keywords` 与 `negative_keywords`，确认负面词是否集中在质量、售后或服务环节。

## 工具对应关系

| 目的 | 工具 | 关键参数 |
| --- | --- | --- |
| 笔记/帖子检索 | `search_posts` | `query`、`platforms="xiaohongshu"`、`start_date`、`end_date`、`limit`、`cursor` |
| 投诉与负面信号 | `search_complaints` | `query`、`platforms="xiaohongshu"`、日期、`limit` |
| 讨论串 | `fetch_thread` | `post_id`（根笔记 ID） |
| 评论区 | `fetch_comments` | `post_id`、`limit` |
| 作者背景 | `fetch_author_profile` | `author_id`（来自帖子结果的 `author_id`） |
| 热门词 | `get_trending_keywords` | `query`、`platforms="xiaohongshu"`、日期 |
| 舆情快照 | `get_public_opinion_snapshot` | `query`、`platforms="xiaohongshu"`、日期、`limit<=200` |

## 数据处理要求

- **只引用真实返回结果**：URL、`post_id`、`author_id`、时间和互动数据必须来自工具输出，不得补写或推测。
- **区分笔记与评论**：笔记表达的是发布者观点；评论区反映的是讨论者反馈。分析时分别标注来源类型。
- **去重**：同一作者、近似内容、跨关键词重复命中的笔记只保留一条代表记录，并说明去重依据。
- **信号分类**：将负面内容归入质量、售后、退款、客服、安全、虚假宣传、价格/贬值等类别；一条内容可以带多个标签，但要给出原文依据。
- **热度与负面程度分开**：`heat_level` 描述传播热度，`risk_level`/`negative_ratio` 描述负面程度，不要用高互动直接推断高负面风险。
- **评论覆盖有限**：`fetch_comments` 只返回数据源实际提供的评论记录。返回为空或数量明显偏少时，明确写为数据限制，不得据此判断“无人讨论”。
- **失败即缺口**：工具返回 `ok: false`、`setup_required`、鉴权错误或空结果时，原样记录失败原因并标记数据缺口。

## 输出要求

- 调研问题与检索时间窗。
- 使用的关键词变体、工具和平台参数。
- 小红书舆情时间线：首次出现、集中讨论、官方或品牌回应（如检索到）。
- 代表性证据表：笔记/评论摘要、`url`、`published_at`、作者类型、`sentiment`、互动量、命中的负面信号。
- 投诉主题分布：每个主题下的代表证据数量与原文要点。
- 快照指标：`risk_level`、`heat_level`、`negative_ratio`、`complaint_count`，注明来自 `get_public_opinion_snapshot`。
- 研究缺口：平台覆盖、评论覆盖、时间窗、账号身份和商业内容判断的不确定性。

## 注意事项

- 不臆测小红书独有的界面信息（如收藏数、私信、商品链接状态），只有工具返回时才引用。
- 不把单条高互动笔记当作整体舆情结论；至少给出多条独立来源或明确说明样本不足。
- 不重新计算情绪或风险分数，直接引用工具返回的 `sentiment`、`risk_level` 和 `heat_level`。
- 不输出无法核验的用户身份、手机号、住址等隐私信息。
