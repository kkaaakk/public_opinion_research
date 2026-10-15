# RAG / Memory 笔记

::: tip
这个文件用于记录 RAG / memory 相关讨论结论。只有在明确要求“记一下 / 记进去 / 更新笔记”时才追加或修改。
:::

## 本页速览

| 项目     | 内容                                                                                                                  |
| -------- | --------------------------------------------------------------------------------------------------------------------- |
| 阅读目标 | 快速查找 RAG / Memory 的设计结论和后续维护约定。                                                                      |
| 记录方式 | 追加日期小节，写清背景、结论、影响范围。                                                                              |
| 相关文档 | [RAG 模块说明](rag.md)、[Memory 模块说明](memory.md)、[RAG 检索测试记录](../evaluation/rag-retrieval-test-records.md) |

这里不是完整模块文档；稳定设计应同步回模块说明，实验结果应同步到测试记录。

## 2026-05-23：文档切分逻辑

当前切分入口仍然是 `split_documents()`，输入是一组 `RAGDocument`，输出是现有的 `RAGChunk`。切分时每个 `RAGDocument` 单独处理，不会把不同来源的文档先拼成一个大文本再切。

### 统一原则

- `page_content` 只存 chunk 文本。
- `metadata` 存结构化信息，例如 `source`、`file_type`、`chunk_index`、`char_start`、`char_end`。
- `source`、`file_type` 等字段不是文本内容的一部分，所以不会被文本切分器切掉。
- 每个 chunk 最终都会带上至少这些 metadata：
  - `source`
  - `file_type`
  - `chunk_index`
  - `char_start`
  - `char_end`

### Markdown 文档

Markdown 使用两阶段切分：

1. 先用 LangChain 的 `MarkdownHeaderTextSplitter` 按 `#`、`##`、`###` 切出 section。
2. 每个 section 保留标题 metadata：
   - `h1`
   - `h2`
   - `h3`
3. 再对每个 section 内部使用 `RecursiveCharacterTextSplitter` 控制长度。

边界规则：

- 不允许跨不同 heading section 合并 chunk。
- `chunk_size` 默认按配置传入，目前推荐值是 `1200`。
- `chunk_overlap` 默认按配置传入，目前推荐值是 `200`。
- 每个 Markdown chunk metadata 保留：
  - `source`
  - `file_type=markdown`
  - `chunk_index`
  - `h1`
  - `h2`
  - `h3`
  - `char_start`
  - `char_end`

### JSON 文档

JSON 不再直接把整份 JSON dump 成字符串后切。

当前流程：

1. 先保留原始 JSON 文本。
2. 用 `json.loads()` 解析为 `dict` / `list`。
3. 递归遍历 object 和 array。
4. 为每个 leaf 字段生成 `json_path`，例如：
   - `$.posts[0].title`
   - `$.posts[0].content`
   - `$.settings.enabled`
5. 按 JSON 结构边界分组：
   - 数组里的 object item 按 item 分组，例如 `$.posts[0]`。
   - 同一个 object item 内的短字段可以合并成一个 chunk。
   - 标量数组 item 保持独立，例如 `$.tags[0]` 和 `$.tags[1]`。
6. 字符串字段：
   - 内容较短时，和同级短字段一起生成 item 级 chunk。
   - 超过 `chunk_size` 时，只对该长字段值继续使用 `RecursiveCharacterTextSplitter`。
   - 长字段切出来的每个 chunk 都会带上同级短字段作为上下文，例如 `title` + 当前 `content` 片段。
7. number、boolean、null 等非字符串字段会转成短文本保留；如果它们属于同一个 object item，可以和同级短字段合并，但不会跨 item / 跨无关对象合并。

例子：

```json
{
  "posts": [
    {
      "title": "标题",
      "content": "很长的正文..."
    }
  ]
}
```

如果 `content` 很长，会生成多个 `json_path=$.posts[0]` 的 chunk，每个 chunk 内容类似：

```text
title: 标题
content: 很长正文的一段...
```

对应 metadata：

```text
json_path = "$.posts[0]"
field_paths = ["$.posts[0].title", "$.posts[0].content"]
primary_json_path = "$.posts[0].content"
```

边界规则：

- 不跨 array item 合并。
- 不跨 object item 合并。
- 短 sibling 字段可以合并，避免 `posts[0].title` 和 `posts[0].content` 太碎。
- 长字段可以拆成多个 chunk，但这些 chunk 的 `json_path` 指向所属 item，`primary_json_path` 指向真正被切分的字段。
- 每个 JSON chunk metadata 保留：
  - `source`
  - `file_type=json`
  - `chunk_index`
  - `json_path`
  - `field_paths`
  - `primary_json_path`，仅长字段二次切分时有
  - `field_char_ranges`，短字段合并时用于记录各字段原始字符范围
  - `char_start`
  - `char_end`

### 普通文本 / 非 Markdown / 非 JSON 文档

普通文本继续使用 LangChain 的 `RecursiveCharacterTextSplitter`。

默认切分参数：

- `chunk_size=1200`
- `chunk_overlap=200`
- `keep_separator="end"`
- `add_start_index=True`

保留的 separators：

- 空行：`\n\n`
- 换行：`\n`
- 中文断句：`。`、`！`、`？`、`；`、`、`
- 英文断句：`.`、`!`、`?`、`;`、`.`、`!`、`?`、`;`
- 逗号：`，`、`,`
- 空格：` `
- 兜底：`""`

边界规则：

- 普通文本可以由 `RecursiveCharacterTextSplitter` 按正常规则合并或重叠。
- 每个普通文本 chunk metadata 保留：
  - `source`
  - `file_type`
  - `chunk_index`
  - `char_start`
  - `char_end`

### 为什么 metadata 不会被切掉

切分器只处理正文文本，不处理 metadata。

流程是：

1. loader 先生成 `RAGDocument(content, source, title, metadata)`。
2. splitter 只把 `content` 切成多个 chunk 文本。
3. 每个 chunk 再继承或补充原 document 的 `source`、`file_type` 等 metadata。
4. 最后组装成 `RAGChunk(content, source, title, chunk_id, metadata)`。

所以 `source`、`file_type`、`json_path`、`h1/h2/h3` 这些字段是挂在 chunk 上的结构化信息，不会被文本 splitter 当成正文切掉。
