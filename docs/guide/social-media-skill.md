# Social Research Plugin（Apify MCP）

社媒采集统一由 `plugins/social-research/` 声明，经 Apify 官方托管 MCP 进入现有业务工具链。仓库不再维护本地社媒适配器、HTTP 采集服务或各平台专用 Python 爬虫。

```text
public_signal Agent
        │
        ├── Tavily web_search                 新闻、官方公告、监管信息、公开网页
        │
        └── social-research Plugin
                │  load_skill
                ├── Apify MCP Server（https://mcp.apify.com）
                │     ├── search-actors
                │     ├── fetch-actor-details
                │     ├── call-actor
                │     ├── get-actor-run
                │     └── get-dataset-items
                │
                └── Skills
                      ├── xiaohongshu
                      ├── douyin
                      └── weibo

所有工具 → BusinessTool → ToolExecutor → por.agent_tool（DBOS Step）
```

## 插件文件

```text
plugins/social-research/
├── plugin.json        # id、版本、roles、skills；tools 为空
├── mcp.json           # 唯一一个 Apify Streamable HTTP 服务
└── skills/
    ├── xiaohongshu/SKILL.md
    ├── douyin/SKILL.md
    └── weibo/SKILL.md
```

`plugin.json` 的 `roles` 仍然只有 `public_signal`。旧的七个本地社媒工具已从清单移除，插件装配不会再因为找不到本地工具而失败。

## MCP 声明

`mcp.json` 使用 Apify 官方托管服务，通过 `tools` 查询参数只暴露社媒采集需要的五个工具：

```json
{
  "servers": [
    {
      "name": "apify",
      "transport": "streamable_http",
      "domain": "social_media",
      "effect": "protected",
      "params": {
        "url": "https://mcp.apify.com?tools=search-actors,fetch-actor-details,call-actor,get-actor-run,get-dataset-items",
        "headers": {
          "Authorization": "Bearer ${APIFY_TOKEN}"
        }
      }
    }
  ]
}
```

配置规则：

- 令牌只从环境变量 `APIFY_TOKEN` 读取，不写入插件文件、提示词或持久化目录快照。
- `APIFY_TOKEN` 未设置时，插件仍加载三个 Skills，但该 MCP 服务被跳过并记录警告；`public_signal` 必须在报告中写明社媒数据不可用，而不是编造数据。
- `domain` 与 `effect` 来自受信任的本地插件文件，MCP 服务不能自行降低保护级别。整个服务按 `protected` 执行，因此 `search-actors` 等只读发现调用也会登记操作回执。
- 服务地址的稳定身份只保留 scheme/host/port/path；查询参数和 Header 不进入 `por.role_tools` 目录。`tools` 过滤参数变化会改变发现的工具集合，从而由现有目录校验失败关闭。

## 接入现有执行链

1. `load_role_plugins()` 读取 `plugin.json` 与 `mcp.json`，解析 `${APIFY_TOKEN}`，得到 `MCPServerSpec`。
2. `load_mcp_tools()` 用官方 Agents SDK `MCPServerStreamableHttp` 做一次短连接发现，得到服务方真实工具名与 Schema。
3. `build_mcp_tool()` 把每个 MCP 工具包装为 `BusinessTool`，附加本地 `domain="social_media"`、`effect="protected"`，每次调用使用独立连接。
4. `ToolExecutor.execute()` 负责预算预占、角色领域校验、`por.agent_tool` Step 和 operations 回执；不新增 MCP 专用执行器或第二套注册表。
5. `public_signal` 的领域白名单包含 `social_media`，因此插件 MCP 工具按既有授权进入；其他角色不会获得该插件。

## 三个 Skills

`load_skill` 是普通的只读 `BusinessTool`，内容只提供研究方法，不能授予工具或改变执行策略。三个 Skill 都要求：

1. 先确认五个 Apify 工具可见；不可见时报告配置缺失。
2. `search-actors` 动态发现平台 Actor，不在代码里固定 Actor ID。
3. `fetch-actor-details` 检查输入 Schema、价格、限制和近期运行情况。
4. 按真实 Schema 构造输入，只请求任务需要的数据量。
5. `call-actor` 通过 `callOptions.maxTotalChargeUsd` 控制账单上限，必要时同时使用 `maxItems`；`waitSecs` 不超过 45。
6. 运行未到终态时用 `get-actor-run` 查询原有 `runId`，不重复启动。
7. 只读取 `get-dataset-items` 返回的真实数据集；空数据集是数据缺口。
8. 区分笔记/视频/博文与评论，保留 URL、时间、作者、正文、互动量；缺失字段标为“未提供”。
9. 去重、来源可信度和平台字段差异按各自 Skill 的规则处理。
10. 只描述观察事实和分析判断，不输出已删除工具才有的 `risk_level`、`heat_level` 或 `negative_ratio`。

## 成本与可靠性边界

- 本地 `max_tool_calls`、`max_search_calls` 和 token 预算是应用侧限制，不等于 Apify 的实际费用；只有 `callOptions.maxTotalChargeUsd` 是本轮 Actor 运行的账单上限。
- 已提交的 `por.agent_tool` Step 在恢复时直接复用结果，不会再次调用 MCP 或启动 Actor。
- 运行中取消或结果未知时，operations 回执会标记 `cancelled`/`unknown`，恢复流程停止并要求核对。
- Apify 的 `call-actor` 没有业务幂等键：如果远端运行已经启动、但本地 Step 在提交前崩溃，仍可能留下一次已计费运行。恢复时应通过 `get-actor-run`/运行历史核对，不能声称绝不会重复付费。
- MCP 连接身份参与既有恢复一致性校验；工具目录、Skill 内容、插件版本或服务地址变化都会失败关闭。

## 只读验证

`search-actors` 与 `fetch-actor-details` 可以在未配置令牌时通过受限发现连接验证：

```text
https://mcp.apify.com?tools=search-actors,fetch-actor-details
```

包含 `call-actor` 等工具的连接需要有效 `APIFY_TOKEN`；无令牌时服务端返回 401。不要在没有授权的情况下启动付费 Actor。
