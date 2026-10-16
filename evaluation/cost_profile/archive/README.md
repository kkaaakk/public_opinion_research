# 成本评测历史导出

以下文件从 `evaluation/cost_profile/` 原样移入，避免与仍可离线使用的分析入口混在一起。

| 归档路径 | 原文件名 | 说明 |
| --- | --- | --- |
| `reports/af_final.txt` | `af_final.txt` | v4-flash 对照最终报告导出。 |
| `reports/after_final.txt` | `after_final.txt` | 早期 After 运行的最终报告导出。 |
| `reports/ik_report.txt` | `ik_report.txt` | Round 2 内部知识角色报告。 |
| `reports/ps_report.txt` | `ps_report.txt` | Round 2 公开信号角色报告。 |
| `reports/r2_final.txt` | `r2_final.txt` | Round 2 最终报告导出。 |
| `launchers/after_v4flash_launch.cmd` | `after_v4flash_launch.cmd` | 绑定旧 Windows 目录与虚拟环境的在线评测启动器。 |
| `launchers/round2_launch.cmd` | `round2_launch.cmd` | 绑定旧 Windows 目录与虚拟环境的在线评测启动器。 |

这些内容是旧实验记录，不表示报告中的外部事实已经过本次复核。启动器及对应 runner 的历史依赖见 [目录说明](../README.md)；本次没有调用真实付费模型。

原始提交：`41cbc134f748750ffbfc7c7dde65c7552ace6296`。主要基线及其 JSON 输出保持在父目录，以保留现有分析脚本和引用的路径。
