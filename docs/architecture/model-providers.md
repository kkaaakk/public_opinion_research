# 模型供应商与配置契约

本项目保留 LangChain 的有界模型服务。四角色 Agent 使用官方 Agents SDK；供应商
构造能力与模型本身的工具、视觉、结构化输出能力分别验证，不承诺所有模型具有相同功能。

| 引用前缀 | LangChain 有界服务 | 原生 SDK Resolver | 凭据/范围 |
| --- | --- | --- | --- |
| `openai` | 正式构造合同 | Responses 默认，可显式 Chat Completions | `OPENAI_API_KEY` |
| `volcengine` | 正式 Ark 适配、function calling 结构化输出 | Chat Completions | `ARK_API_KEY`；保留预置 ID 和 `ep-*` |
| `deepseek` | 正式构造合同；Flash 保留 thinking 特殊处理 | Chat Completions | `DEEPSEEK_API_KEY` |
| `groq` | 正式构造合同 | Chat Completions | `GROQ_API_KEY` |
| `anthropic` | 正式构造合同 | 明确拒绝 | `ANTHROPIC_API_KEY` |
| `google_genai`、兼容别名 `google` | 正式构造合同，Google GenAI 后端 | 明确拒绝 | `GOOGLE_API_KEY`，环境模式也保留 `GEMINI_API_KEY` |
| `google_vertexai` | 正式构造合同，实际 ChatVertexAI | 明确拒绝 | 原生 `project` / `location` / ADC 或 `credentials`，不复用开发者 API key |
| `bedrock` | 正式构造合同，实际 ChatBedrock | 明确拒绝 | AWS 原生区域和凭据链；不转换成 OpenAI 参数 |
| `ollama` | 正式构造合同；保留 Classic 到 Community 的兼容回退 | 明确拒绝 | 本地 `base_url`；不承诺旧 Community 后端支持 tool binding |
| `mistralai`、兼容别名 `mistral` | 可选扩展，当前根锁未安装，服务端未验证 | 明确拒绝 | 需另行提供 `langchain-mistralai`；`MISTRAL_API_KEY` |
| `cohere` | 可选扩展，当前根锁未安装，服务端未验证 | 明确拒绝 | 需另行提供 `langchain-cohere`；`COHERE_API_KEY` |

“正式构造合同”指根依赖提供实现、真实构造及离线回归受测试约束；不表示本轮执行过
真实付费推理、通过云端配额验收或验证了所有模型功能。可选扩展保留已有安装者的分派，
缺包时抛出带安装指引的 `ModelConfigurationError`，不会返回假的可用模型。
未列出的前缀在请求前拒绝；SDK 不会因失败回退到 LangChain。

`google` 原先没有可达的 Classic 路由；现在根据已有 `GOOGLE_API_KEY` 语义映射为
`google_genai`。Vertex 使用显式 `google_vertexai`，避免开发者 API 与 ADC 混淆。
`mistral` 归一为 Classic 实际识别的 `mistralai`。模型 ID 中额外的冒号保持原样。

## 配置所有权

`models/providers.py` 是原有解析器的后端无关归属；两条路径共用 `parse_model_ref`、
`resolve_api_key` 和 `resolve_base_url`。既有 `models` / `models.factory` 导入继续可用。
仅导入 Resolver 或 SDK Model 不会装载 Classic、ChatOpenAI 或 ChatDeepSeek 后端。

- 显式 `api_key` 优先。`GET_API_KEYS_FROM_CONFIG=true` 时从
  `configurable.apiKeys` 读取；否则使用对应环境变量。SDK 支持的 key 型供应商缺少
  所选来源的凭据时，两条路径都拒绝，不以环境密钥填补空配置。
- LangChain 既有 `openai_api_key` / `openai_api_base`、Groq 的
  `groq_api_key` / `groq_api_base` 在工厂边界归一为显式参数；标准 `api_key` /
  `base_url` 同时存在时优先。这些原生字段别名不会被环境或默认地址覆盖。
- OpenAI-compatible 地址按显式 `base_url` → 可信配置 → 环境 → 默认值解析。
  Ark 支持 `ARK_BASE_URL` / `ark_base_url`；DeepSeek 用 `DEEPSEEK_API_BASE`，
  Groq 用 `GROQ_API_BASE`。OpenAI 用 `OPENAI_BASE_URL` 并保留 `OPENAI_API_BASE`。
- Ark 默认 `https://ark.cn-beijing.volces.com/api/v3`，DeepSeek 默认
  `https://api.deepseek.com`，Groq 默认 `https://api.groq.com/openai/v1`；OpenAI
  未指定地址时由官方客户端采用默认值。这些协议共用绝对 HTTP(S) URL 校验。
- Anthropic、Google、Vertex、Bedrock、Ollama 保留其原生 endpoint/凭据差异。
  不把 Vertex host 或 AWS endpoint 强行套入 OpenAI URL 策略。
- Intake、Review、Report、Graph/压缩服务及 Tavily 将完整可信 provider 配置交给工厂，
  避免先解析一次 key、再遗漏 endpoint。SDK ModelSettings 仍归 Agent/RunConfig，
  LangChain 请求参数仍归实际模型；不引入通用 ModelService。

当前根依赖与锁保持原样，包括 Vertex 和 Community。Vertex 可选化需要单独证明
默认安装与 extra 安装均可复现、导入契约兼容、锁的实际差分及平台矩阵，不能把旧锁
静态闭包估算作为本轮已节省的依赖。

回归与测量见 [接缝精简审计](../audits/langchain-seam-simplification.md)。
