# 历史成本评测材料

本目录保留固定 Tesla 案例的成本、质量与上下文增长记录。`single_case.json` 是可复现输入；`before_cost_profile.json`、`after_cost_profile.json`、`after_v4flash_cost_profile.json` 和 Round 2 JSON 是不同版本/模型/完成状态的基线，不能当作同一产物删除。

`ROUND2_VERIFICATION_REPORT.md` 引用的早期超时快照用于解释 `rolling_compact` 的触发顺序；`*_reports.json` 是对原始运行结果的报告级分析。这些仍有独立诊断用途，保留原路径。

离线阅读与后处理入口包括 `render_cost_profile.py`、`postprocess_reports.py`、`compare_three.py`、`analyze_*.py`。其中部分脚本需要显式传入相应结果路径，所有结果应按记录的提交、模型和状态比较。

`run_cost_profile.py`、`run_after_v4flash.py`、`run_round2_profile.py` 和 `probe_models.py` 是历史在线测量脚本，执行时会调用真实模型。前后对比脚本依赖原 `POR-before` / `POR-after` 目录及指定历史提交；后两轮 runner 仍依赖已经退出当前源码的 `agent_observer`。它们保留历史实验的复现方法，不作为当前版本或 SDK 迁移的验收入口。本次准备没有运行这些在线脚本，也没有创建它们所需的其他 checkout。

独立导出的 TXT 报告及绑定旧机器绝对路径的 CMD 启动器移到 [archive](archive/README.md)。历史基线、Golden 输入和分析脚本保持完整。唯一删除的是全仓无引用、内容仅为 `hello` 的 8 字节调试文件 `wmi_test.txt`。
