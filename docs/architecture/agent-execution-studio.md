# 研究工作台的查询与事件协议

工作台读取 RunClient 的所有者范围内快照、DBOS 阶段结果、业务账本和已保留 Stream。页面及接口的对应关系见 [前端说明](../../frontend/README.md)。有关审批与故障恢复，见 [Research Brief 合同](research-brief.md) 和 [DBOS 部署](dbos-deployment.md)。

## 读取与提交

- 新建研究接受主题、企业背景、本次 RAG 开关、已有会话 ID 和可选图片。创建使用 `Idempotency-Key`。通信中断时，`GET /api/runs/submission` 使用同一请求头查询已接受的 Run。
- 图片正文在准入事务内写入 `por_domain.request_images`，与原请求一起保留。请求记录只保存顺序、文件名、媒体类型、大小及参与幂等校验的内容摘要。规划加载已验证的图片，复用既有 Vision 识别步骤。可公开的识别结果保存在规划 Workflow 的原生 Event。
- 澄清和 Brief 操作携带当前交互版本。AI 修改携带幂等键，只有成功修改消耗次数。重试携带观察到的 `state_version` 和幂等键，Finalizer 应用重试授权。
- 会话查询基于已有请求的 `research_session_id`。延续会话创建独立 Run。历史主题及终态筛选在服务端应用，分页使用服务端返回的 `next_offset`。
- 执行详情只读取公开 DBOS Catalog API 和业务调用账本。Workflow、Step 和调用分别分页。用量不完整、Step 完成时间缺失以及旧版账本不可读取时，界面显示对应的未知状态。

## 持久事件

`GET /api/runs/{id}/event-page?after=0&limit=200` 读取有界的持久前缀，返回 `items`、`next_cursor` 和 `has_more`。SSE 使用 Bearer 请求头，`Last-Event-ID` 优先于 `after`。

| 事件字段 | 含义 |
| --- | --- |
| `seq` | 单 Stream 的原生 offset + 1。 |
| `generation` | 持久重试代次。 |
| `timestamp` 和 `observed_at` | 公开事件时间和生产者记录的事实时间。 |
| `unit_id`、`execution_id`、`step` | 阶段、执行实例和名称。 |
| `sdk_event` | 经过筛选的调用身份、状态和已观察用量。 |
| `research_review`、`research_followup` | Review 结果及实际补研轮次。 |
| `agent_report`、`report` | 已产生的阶段报告和最终报告。 |
| `settlement`、`usage`、`run_status`、`done` | 清算、用量、Run 状态和本代次交付。 |

刷新后先读快照和持久前缀，再从最后一个完整序号订阅。断线后保留相同游标，丢弃残帧。等待交互时切换到快照轮询。连接结束后重新核对 Run 快照。历史交付完成要求当前代次终态、非待清算、`events_complete` 为真且没有待应用重试。

工具活动只展示服务端公开的元数据。报告内容及来源由已保留阶段产物提供。 [运行状态与架构](agent-loop.md#交付与执行状态)
