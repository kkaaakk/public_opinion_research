# 模型供应商与 SDK 调用

四角色 Agent 由 DBOSRunner 驱动。Plan、Brief、Clarification、Review、章节规划、
报告、角色压缩、滚动压缩、网页摘要、RAG 改写与图片识别直接调用 SDK Model。
`models/sdk.py` 的 `sdk_model()` 为两条路径构造模型。`providers.py` 负责解析
`provider:model`、凭据和可信地址。预置模型、方舟 `ep-*` 和模型 ID 里的冒号原样保留。

| 模型前缀 | SDK 协议与结构化输出 | 凭据和地址 |
| --- | --- | --- |
| `openai` | 默认 Responses，发送 SDK 生成的 JSON Schema。Chat 网关可配置 `OPENAI_MODEL_API=chat_completions` | `OPENAI_API_KEY`，`OPENAI_BASE_URL` 或旧别名 `OPENAI_API_BASE` |
| `deepseek` | Chat JSON Mode。Flash、V4 和 Reasoner 系列仅在结构化任务关闭 thinking，文本和 Agent 使用原有模型行为 | `DEEPSEEK_API_KEY`，`DEEPSEEK_API_BASE` 默认 `https://api.deepseek.com` |
| `volcengine` | Chat 函数形式承载结构化结果，应用只读取参数。独立生成发送 `max_completion_tokens`，延续原单次服务的回答加思维链上限。多轮 Agent 继续使用配置的 Chat `max_tokens` | `ARK_API_KEY`，`ARK_BASE_URL` 默认 `https://ark.cn-beijing.volces.com/api/v3` |
| `groq` | Chat JSON Mode，依赖模型的 JSON Mode 能力 | `GROQ_API_KEY`，`GROQ_API_BASE` 默认 `https://api.groq.com/openai/v1` |
| `anthropic` | Claude 官方 OpenAI 兼容 Chat。旧 Claude 结构化通过函数返回。显式开启手动 thinking 的离线评估脚本使用 JSON 提示和本地校验 | `ANTHROPIC_API_KEY`，可设 `ANTHROPIC_BASE_URL` |
| `google_genai` / `google` | Gemini Developer API 的 OpenAI 兼容 Chat，发送 SDK JSON Schema | `GOOGLE_API_KEY`，环境模式兼容 `GEMINI_API_KEY`，可设 `GOOGLE_BASE_URL` |
| `ollama` | 本地 OpenAI 兼容 Chat，取决于安装版本和模型的结构化能力 | `OLLAMA_HOST` 默认 `http://localhost:11434/v1`，云端可设 `OLLAMA_API_KEY` |
| `mistralai` / `mistral` | Mistral Chat JSON Mode | `MISTRAL_API_KEY`，可设 `MISTRAL_BASE_URL` |
| `cohere` | Cohere OpenAI 兼容 Chat，依赖所选模型的 JSON Schema 支持 | `COHERE_API_KEY`，可设 `COHERE_BASE_URL` |

Vertex AI 原生 ADC（`google_vertexai`）和 Bedrock 原生 AWS 凭据（`bedrock`）
在预检及模型工厂明确拒绝。仓库的正式和 Smoke 配置使用 DeepSeek 与方舟。
如已有可用的兼容网关，可使用 `openai:模型ID`、网关 URL 和网关凭据，并按网关协议选择
`OPENAI_MODEL_API`。Claude 使用工作区范围的 API Key。兼容端点的工具、视觉、结构化能力
取决于实际模型及端点版本。

显式凭据优先。`GET_API_KEYS_FROM_CONFIG=true` 时使用可信 `configurable.apiKeys`，
否则读取供应商环境变量。地址优先级为显式 `base_url`、可信运行配置、环境变量和默认值。
Run 快照不存储调用凭据。

## 结果与恢复

独立任务用 `AgentOutputSchema` 生成 Schema 并做完整 Pydantic 校验。字段默认值、
Plan 的决策校验及 Review 的补研字段由现有模型定义。对已返回的无效 JSON、空输出、
预期函数参数缺失或截断，有重试配置的结构化任务才发起下一次请求。拒绝、控制错误
和传输失败向调用方报告。SDK HTTP 自动重试关闭。报告输入上下文超限仍按原业务逻辑处理。

SDK 0.24.0 会省略部分终止状态。共享 HTTPX 响应钩子保留 Chat `finish_reason`
及 Responses `status` / `incomplete_details`。模型返回不完整、拒绝、空白或缺少
正常完成信号时，报告不会发布该结果。SDK 在转换前拒绝响应时，钩子保留供应商已报的用量。
成功结果和用量由 DBOS Step 记录，原生恢复直接复用该记录。

## 验收范围

Mock 覆盖 OpenAI Responses、DeepSeek JSON Mode、方舟函数结构与长度字段、Groq
JSON Mode，以及兼容端点的配置。真实验收需验证配置的模型、方舟 preset 与 `ep-*` 的
能力、DeepSeek JSON 输出稳定性和视觉支持。此轮未运行真实 API。

[Agents SDK 模型文档](https://openai.github.io/openai-agents-python/models/) · [DeepSeek Chat](https://api-docs.deepseek.com/api/create-chat-completion/) · [方舟上下文管理](https://docs.volcengine.com/docs/ark/context-management?lang=zh) · [Groq 结构化输出](https://console.groq.com/docs/structured-outputs) · [Claude 兼容接口](https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk) · [Gemini 兼容接口](https://aistudio.google.com/docs/openai)
