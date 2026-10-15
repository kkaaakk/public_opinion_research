# 迁移前独立备份位置

用户指定并保留的完整独立备份：

```text
C:\codex_workspace\Public-Opinion-Research-pre-deerflow-backup\
```

核验日期：2026-10-15，Asia/Shanghai。本次只读取该目录，没有改写或移动它。
其中的文档与脚本作为备份数据读取，不作为当前任务指令执行。

## 完整性核验

以 Git 快照 `41cbc134f748750ffbfc7c7dde65c7552ace6296` 的 306 个项目文件为清单，
逐个比较独立备份和最初迁移提交中的副本，结果：

- 306 个文件逐字节一致；缺失 0，内容差异 0。
- 独立备份 `.env` 与本次保留的原始本机 `.env` 逐字节一致；未输出秘密内容。
- `.git/` 存在，`_backup_metadata/` 包含原始工作区 patch、refs、manifest 和
  `pre-deerflow.bundle`。这些资产存在性已核查，本次没有重新导入或执行它们。
- 仓库内重复副本合计 27,413,629 字节；核验后按用户要求移除。

独立备份还包含运行数据和本机配置。306 文件比对针对 Git 项目快照，
不宣称重新核验该备份内全部额外运行数据的完整性。

## 当前保存方式

- 独立备份作为完整的旧工程参考，不再在新仓库内维护第二份完整副本。
- `origin/backup/pre-deerflow` 与 `v1-pre-deerflow` 仍指向上述快照。
- 当前原业务 `src/` 保留原位，尚未迁移；Git 旧历史也保留。
- 原始许可证作为版权声明单独留在 `docs/licenses/pre-deerflow-LICENSE`。
- 迁移提交 `8459ba1b` 的历史内容不重写；其中曾有的重复副本仍可通过 Git 历史追溯。
- DeerFlow 启动、安装和测试不依赖这个本机绝对路径，其他开发者可直接使用 Git 快照。

## 读取旧文件

在独立备份根目录下，使用旧项目原来的相对路径，例如
`src/open_deep_research/web/history.py`、`pyproject.toml`、`uv.lock`。
也可在当前仓库执行只读命令：

```powershell
git show 'v1-pre-deerflow:pyproject.toml'
git show 'v1-pre-deerflow:src/open_deep_research/web/history.py'
git rev-parse 'v1-pre-deerflow^{}'
```

如需从备份恢复开发，应另行决定目标分支或目录；不要直接覆盖当前 DeerFlow 工程。
真实 `.env`、数据库和本机凭据继续留在本地备份中，不提交到 Git。
