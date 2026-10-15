# Public Opinion Research 架构优化日志（中文回顾版）

这份日志是给自己长期回看的版本。重点记录：系统为什么贵、真正应该先改哪里，以及改完之后要观察什么。

英文架构日志仍保留在 docs/ARCHITECTURE_OPTIMIZATION_LOG.md。

## 2026-09-02：单 Case 成本观测

### 这次看了什么

用同一个 Tesla 中国车辆安全舆情 Case，分别跑了两个版本：

- Before：d0270b6
- After：5989480
- 实际使用的模型：deepseek:deepseek-chat
- 每个版本运行 1 次

为了让 internal_knowledge_agent 能够真正执行，两边使用了完全相同的本地 RAG 配置。业务代码、Prompt、ReAct 上限、研究轮次、Tool 白名单和 RAG 算法都没有改。

### 先说结论

Dynamic Plan-and-Execute 暂时不删除。

它的基本流程已经能正常工作：

研究 → Review → 找到缺口 → 补查 → 再 Review

真正的问题不是“多了一个 Review”，而是发现缺口以后，补查做得太重，几乎又重新做了一轮完整研究。

理想状态应该是：

发现缺口 → 只查这个缺口 → 返回新增证据 → 再 Review

而不是：

发现缺口 → Follow-up Agent 再做一次大范围研究

## 这次最重要的发现

### 1. 最大成本中心是 public_signal_agent

After 一共用了：

- 233 次模型调用
- 60 次 Tool 调用
- 642.46 秒

Before 一共用了：

- 135 次模型调用
- 46 次 Tool 调用
- 493.97 秒

按目前能够可靠统计到的 Token 看：

- After：1,332,387
- Before：827,944
- 增加：504,443，约 60.9%

这里是“已知 Token 小计”，不是完整总数。很多网页摘要和结构化输出没有返回 Token 使用量，所以真实消耗只会更高，不会更低。

### 2. 一次研究产生了 187 次网页摘要调用

这是目前最异常的数字。

After 的 webpage_summarization 一共调用了 187 次，累计模型调用时间约 1,051.8 秒。Before 也有 101 次。

这意味着：系统可能搜到一批网页后，又逐篇调用模型做摘要；而且在不同 Agent、不同研究轮次中重复做了很多摘要。

这部分目前没有可靠的 Token 数，所以不能直接算出费用，但从调用数量和耗时看，它很可能是最值得优先调查的地方。

需要先弄清楚：

- 为什么一次 Case 会产生这么多网页摘要？
- 是否重复摘要了同一个网页？
- 是否把低价值网页也全部送进了模型？
- Follow-up 是否又重新摘要了大量旧网页？

### 3. ReAct 上下文会越跑越大

每次 Agent 调用 Tool 后，新的 Tool Result 会继续留在历史消息里。下一轮模型调用又把这些历史全部带上。

最明显的是 After 的 public_signal 第二轮：

- 第一次 input：约 8,170 tokens
- 后面增长到：约 96,307 tokens

其他 Agent 也有类似增长。

这说明系统不是只在“查资料”，还在反复为旧资料付输入 Token 费用。

### 4. compress_research 本身也很贵

After 一共做了 6 次压缩，已知消耗约 298,064 tokens，占已知 Token 的 22.37%。

压缩的初衷是让后面少读一些上下文，但现在压缩本身已经很贵。

后续必须确认：

压缩一次实际节省了多少后续输入 Token；

如果压缩花掉 5 万 Token，却只节省了 2 万 Token，就不划算。

### 5. 同一批 role_reports 被后面反复读取

研究报告生成后，会继续被这些流程读取：

- Research Review
- Risk Assessment
- Response Strategy
- Section Writer

After 的重复读取成本估算约为 171,151 tokens。这个数字是根据报告字符数估算的，不是模型直接返回的精确值，但足以说明问题：

同一份长报告被当成上下文，在多个后续节点反复发送。

### 6. Dynamic Research 的主要成本在 Follow-up Agent

After 的 Dynamic Loop 直接新增了：

- 2 次 Research Review
- 132 次 public/internal Follow-up 调用
- 已知 Token 至少 629,027

Review 本身不是主要成本来源。真正贵的是 Review 之后，Follow-up Agent 又执行了一轮很重的检索、网页摘要、ReAct 和压缩。

## 当前优化优先级

### P0：先调查网页摘要为什么有 187 次

这是最优先的问题。

先查清调用来源、重复网页、摘要触发条件和 Follow-up 是否重复摘要。暂时不要急着换模型或调低输出上限。

### P0：控制 ReAct 历史长度

不要让每一轮模型都重新携带完整 Tool Result 历史。

需要保留的是：

- 关键事实
- 来源链接
- 冲突信息
- 尚未解决的问题

不是所有原始搜索结果都要一直留在上下文里。

### P0：把 Follow-up 改成窄范围补查

当前 Follow-up 太像“重新研究”。

目标是让每个 Follow-up 只完成一个 Gap，例如：

- 只确认一个官方声明是否存在
- 只核对一个数字
- 只补一个监管文件
- 只验证一个内部规则

返回新增证据即可，不要重新生成完整角色报告。

### P1：重新评估 Compression 是否划算

每次压缩都要问：

压缩花费的 Token，是否小于它为后续流程节省的 Token？

如果不是，就需要减少压缩次数、缩小压缩输入，或改成更轻量的摘要方式。

### P1：减少完整 role_reports 的重复注入

后续 Agent 不一定需要整份报告。

更合理的传递方式可能是：

- 关键 Claim
- 对应来源
- 风险影响
- 冲突
- 未解决 Gap

而不是把所有角色的完整长报告再次塞进 Prompt。

### P1：检查 Section Writer 是否重复读取证据

如果多个 Section 使用同一批 role_reports，写作阶段会再次放大输入 Token。

需要确认每个 Section 实际需要哪些证据，避免每个 Section 都读取完整报告。

## 后续怎么回归

暂时不扩大 Benchmark，固定使用：

evaluation/cost_profile/single_case.json

每完成一个优化，只重新跑这个 Case，重点比较：

- Model Calls
- Tool Calls
- Input / Output / Total Tokens
- Duration
- ReAct 最大上下文长度
- 网页摘要调用次数
- Research Gap 是否还能被发现
- Follow-up 是否还能补齐关键证据
- 最终研究质量是否下降

## 最终目标

目标不是把 Token 单纯压到最低。

真正要消除的是：

- 重复搜索
- 重复网页摘要
- 重复上下文
- 过重的 Follow-up

同时保留：

- 深度研究能力
- 证据覆盖
- 冲突识别
- Research Gap 发现
- 关键证据补齐能力
