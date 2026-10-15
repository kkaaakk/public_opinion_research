@echo off
@rem 启动 V4 Flash 配置的成本测量，并保存诊断日志。
cd /d "C:\codex_workspace\Public Opinion Research"
"C:\codex_workspace\Public Opinion Research\.venv\Scripts\python.exe" evaluation\cost_profile\run_after_v4flash.py --case-timeout 5400 --max-attempts 2 --output evaluation\cost_profile\after_v4flash_cost_profile.json 1> evaluation\cost_profile\after_v4flash.out.log 2> evaluation\cost_profile\after_v4flash.err.log
