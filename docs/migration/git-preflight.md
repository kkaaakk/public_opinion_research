# Git 与未提交修改核查记录

记录日期：2026-10-15。按照用户后续要求，Phase 0 在原本地工作区 `C:\codex_workspace\Public Opinion Research` 的开发分支执行，不使用新 worktree，不合并到 `main`。

## 大量未提交修改的原因

最初本地当前分支 `codex/deerflow-phase0` 和本地 `main` 均指向较早的 `c54323bac38a2d437f86705f39df33a1b109256c`。工作区文件已经包含较新的开发成果，而本地分支尚未对齐已经存在的远程备份快照。因此 Git 将已有开发成果显示为大量未提交修改，不能把这些差异当成迁移产生的临时文件删除。

初始检查得到：

- 145 个 tracked 文件显示修改，合计 2,879 行增加、268 行删除。
- 其中 140 个 Python 文件，按忽略注释与 docstring 的 AST 比较，123 个仅为注释/docstring/格式变化，17 个包含代码行为变化。
- 另外 5 个是 `.env.example`、`README.md` 和旧 `web/static/` 下的 JavaScript、HTML、CSS。
- 63 个当时显示未跟踪的项目文件也已经存在于远程备份快照，包含历史归档模块、测试、Neo4j 辅助文件、成本评估脚本/报告、架构学习文档和导出物。
- 已修改文件的时间主要为 2026-10-12，另有 3 个文件为 2026-10-14，早于本次迁移操作。修改时间仅作为佐证；最终判断基于文件内容与 Git 对象比较。

17 个功能性 Python 差异涉及：章节写作预算分配、用户图片 Vision 及独立提示配置、回应策略与工具权限、无可选 RAG 工具时的图证据消费、Neo4j 历史归档/Web SSE 及相应测试。完整模块分类见 [旧代码迁移清单](legacy-inventory.md)。这些工作全部保留。

## 已有备份与对齐结果

| 对象 | 核查值及含义 |
| --- | --- |
| `origin` | `https://github.com/kkaaakk/public_opinion_research.git` |
| 初始本地 `main` / 迁移分支 HEAD | `c54323bac38a2d437f86705f39df33a1b109256c` |
| 核查时 `origin/main` | `b1aa74889b4df55c500d3814c2623f1112efc0b4` |
| `origin/backup/pre-deerflow` | `41cbc134f748750ffbfc7c7dde65c7552ace6296`，消息为 `backup: snapshot local project before DeerFlow 2.0` |
| `v1-pre-deerflow` annotated tag 对象 | `eaddf3397434fcb975f7ed23be5c7621e8d9f065` |
| `v1-pre-deerflow^{}` 解引用 Commit | `41cbc134f748750ffbfc7c7dde65c7552ace6296` |

对备份快照的全部 306 个文件进行比对，当前本地对应文件均逐字节一致，允许 Git 的 LF/CRLF 换行差异。确认不存在尚未备份的项目源码后，仅将 `codex/deerflow-phase0` 快进到已有快照 `41cbc134f748750ffbfc7c7dde65c7552ace6296`。对齐后 tracked 工作区干净，再开始官方工程导入。

本地 `main` 保持 `c54323bac38a2d437f86705f39df33a1b109256c`，备份分支与标签均保留；没有 reset、clean、强制推送或修改已有历史。用户提及的独立文件夹备份未在本任务内创建或修改；在后续明确其位置后，已实际核查其完整性，见 [备份位置](backup-location.md)。

最初迁移提交 `8459ba1b` 曾另外创建 `legacy/pre-deerflow/` 副本。用户随后要求使用已有独立备份；核验其中全部 306 个项目文件及原 `.env` 一致后，移除仓库内重复副本。原 `src/` 仍在原位置，两个备份 refs 和历史提交保持不变；移除副本不改变新 Gateway 的运行依赖。旧许可证单独保留作为版权声明。

## 已撤销的新 worktree 与中断进程

本次曾生成的 worktree 路径为：

```text
C:\Users\zpbcool\.codex\worktrees\deerflow-phase0\Public Opinion Research
```

已移除其 Git worktree 注册。中断的 Git clone 当时持有残留目录，相关缓存的部分内容保留到 `.tmp/deerflow-phase0/interrupted-upstream-clone`，随后仅停止此次 clone 对应的 4 个 Git 进程，再删除已经核实路径的残留目录。`Test-Path` 验证该 worktree 路径返回 `False`；后续 `git worktree list --porcelain` 中不再包含它。

其他既有 worktree 均未删除、迁移或清理。列表中既有的 `prunable` 条目同样没有擅自处理。`.tmp/`、工具缓存、运行时配置与日志不应进入迁移提交。

## 只读复核命令

以下命令从原仓库根目录执行，不修改 refs 或工作区：

```powershell
git status --short
git branch --show-current
git log -1 --format="%H %s"
git rev-parse main
git rev-parse origin/backup/pre-deerflow
git rev-parse v1-pre-deerflow
git rev-parse 'v1-pre-deerflow^{}'
git worktree list --porcelain
git diff --stat 41cbc134f748750ffbfc7c7dde65c7552ace6296
Test-Path -LiteralPath 'C:\Users\zpbcool\.codex\worktrees\deerflow-phase0\Public Opinion Research'
```

引用快照中的单个旧文件可使用 `git show 41cbc134f748750ffbfc7c7dde65c7552ace6296:src/open_deep_research/budget.py`。查看秘密配置时避免把真实值打印到日志；本次未提交真实 `.env`、API Key、Token 或数据库密码。
