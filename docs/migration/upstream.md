# DeerFlow 上游基线

- 仓库：https://github.com/bytedance/deer-flow
- 固定 Commit：`8d994cc1e7ee63c61283a71a169248beb4ed1c52`
- 引入日期：2026-10-15（Asia/Shanghai）
- 来源：该 Commit 的 GitHub codeload ZIP；全部 4000 个文件完整引入。
- 版本标识以源码为准：后端 `2.2.0-dev`，Harness `2.1.0`，Extension API `0.2.7`。
- 上游 MIT LICENSE 留在根目录；旧项目 MIT LICENSE 作为版权声明单独保留于
  `docs/licenses/pre-deerflow-LICENSE`，完整旧工程使用已有独立备份与 Git 快照。
- 原项目历史、远程备份分支与标签保留；本阶段没有推送或合并 main。

`upstream-manifest.json` 记录每个原始文件和 ZIP 的 SHA-256。完整源文件均存在；
同时记录上游可执行文件，提交时保留 Git executable mode；文本按官方
`.gitattributes` 处理换行，Windows `.cmd` 的 LF/CRLF 等价哈希单独记录。
本阶段仅对上游的 `.gitignore`、`AGENTS.md`、`README.md`、`backend/pyproject.toml`
和 `backend/uv.lock` 增加本项目配置，其余上游文件保持原始字节。
新增文件不属于上游差异；原项目根级依赖清单、旧 CI 保存在已有独立备份和 Git 快照，
详见 [备份位置](backup-location.md) 与文件变更清单。

## 后续更新

1. 在本地开发分支选定另一个不可变 Commit，完整下载或 fetch 该源码。
2. 对比两个上游 Commit，同时用 manifest 检查项目对原始文件的修改。
3. 单独审查上述五处差异和公共 Extension API 版本；不要把业务代码合入核心。
4. 用官方扩展管理器更新部署快照及锁文件，运行双包与跨模块测试。
5. 更新本文件、manifest、架构决策和验收结果后再提交评审。

参考源码：[固定版本工程](https://github.com/bytedance/deer-flow/tree/8d994cc1e7ee63c61283a71a169248beb4ed1c52)、
[Extension API](https://github.com/bytedance/deer-flow/tree/8d994cc1e7ee63c61283a71a169248beb4ed1c52/backend/packages/extension-api)、
[示例扩展](https://github.com/bytedance/deer-flow/tree/8d994cc1e7ee63c61283a71a169248beb4ed1c52/examples/deerflow-extension-example)、
[Jev classify 示例](https://github.com/bytedance/deer-flow/tree/8d994cc1e7ee63c61283a71a169248beb4ed1c52/examples/deerflow-extension-jev-classify)。
