@echo off
@rem 启动第二轮成本测量，将标准输出和错误分别保存到日志。
cd /d "C:\codex_workspace\Public Opinion Research"
"C:\codex_workspace\Public Opinion Research\.venv\Scripts\python.exe" evaluation\cost_profile\run_round2_profile.py --case-timeout 7200 --max-attempts 2 --output evaluation\cost_profile\round2_full_cost_profile.json 1> evaluation\cost_profile\round2_full.out.log 2> evaluation\cost_profile\round2_full.err.log
