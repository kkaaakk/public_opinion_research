# POR Manager Agent POC

实验使用真实 OpenAI Agents SDK 的 `Runner`、`Agent.as_tool()` 和 DBOS。独立脚本使用 SDK 提供的 `ScriptedModel`，供应商协议测试使用进程内 HTTP transport，外部采集效果由隔离 PostgreSQL 中的另一笔事务模拟。运行时禁用 dotenv 和 tracing，Python socket 仅允许回环连接。实验所用代码和依赖配置都在本目录。 [架构与验证报告](../../docs/audits/manager-agent-feasibility.md)

## 准备 Python

POR 已同步根 `uv.lock` 时，可以在本目录用根环境的 Python 执行下列命令。单独使用此目录时，用 Python 3.11 安装实验依赖。

```powershell
uv venv --python 3.11 .venv
uv pip install --python .venv/Scripts/python.exe -r pyproject.toml --group dev
$python = '.venv/Scripts/python.exe'
# 若使用当前 Worktree 的根环境，改为 '../../.venv/Scripts/python.exe'。
$env:PYTHONDONTWRITEBYTECODE = '1'
```

macOS 或 Linux 使用 `.venv/bin/python`。直接依赖版本与 POR 根锁一致。独立安装会重新解析传递依赖，复核 POR 基线时用已同步的根环境。

## SDK 离线实验

```powershell
& $python -m manager
& $python -m pytest -c pyproject.toml -m 'not postgres' -p no:cacheprovider -q
```

演示输出有四条证据 ID。Manager、公域、内域、核验分别发生 4、4、2、2 次模型调用，SDK 汇总为 12 requests。测试另外覆盖无补研、并行、权限、配置和 Hooks、控制失败、取消、结构化输入输出、历史和 Session、RunState、DeepSeek 协议及 compaction。

## PostgreSQL 和进程恢复

使用专用的临时容器。以下命令为测试数据库配置回环监听和免密认证。容器采用自动删除模式，无主机卷。

```powershell
docker run --rm --detach --name por-manager-agent-poc --publish 127.0.0.1::5432 --env POSTGRES_DB=por_manager_agent_poc --env POSTGRES_USER=por_manager_poc --env POSTGRES_HOST_AUTH_METHOD=trust postgres:17.6
docker exec por-manager-agent-poc pg_isready -U por_manager_poc -d por_manager_agent_poc
$port = (docker port por-manager-agent-poc 5432/tcp).Split(':')[-1].Trim()
$env:POR_MANAGER_POC_DATABASE_URL = "postgresql://por_manager_poc@127.0.0.1:${port}/por_manager_agent_poc"
& $python -m pytest -c pyproject.toml -m postgres -p no:cacheprovider -vv
docker stop por-manager-agent-poc
```

等 `pg_isready` 返回 `accepting connections` 后启动测试。有失败时同样可以用最后一条命令清理容器。URL 入口只接受上述专用数据库名、用户和数字回环地址。没有设置 URL 时，pytest 将 PostgreSQL 用例标记为跳过。

每个测试使用独立的业务 schema 和 DBOS journal schema。测试进程通过原生队列启动 Executor，等指定窗口的记录提交后用 `Popen.kill()` 强杀。它另起一个同版本、同 Executor ID 的进程，检查原生状态、Step 列表和实际调用记录。失败时读取 pytest 临时目录内的 `executor.log`。模型和外部采集仍使用离线 fixture。该容器运行真实 PostgreSQL，且每次恢复都启动新 Python 进程。

## 单独比较 SDK 0.26.0

使用另一套 Python 3.11 环境。在独立目录中安装 `pyproject.toml` 里的直接依赖，并将实验环境中的 `openai-agents` pin 单独换成 `0.26.0`。用该解释器从本目录运行 SDK 离线命令。DBOS 故障矩阵以 POR 锁定组合为基准。

## 代码入口

| 文件 | 用途 |
| --- | --- |
| `manager.py` | Manager、三个专业工具、参数与结果模型、共享 Context、固定 Usage、事件投影 |
| `tests/test_sdk_manager.py` | 原生调度、并行与补研、工具权限、预算、失败传播、生命周期和流事件 |
| `tests/test_sdk_context.py` | 历史选择、模型过滤、Session 混写与中断快照恢复 |
| `tests/test_provider_protocol.py` | 真实 SDK 模型适配器处理 DeepSeek 风格 Chat Completions 与 Responses compaction |
| `dbos_worker.py`、`postgres_lab.py` | 方案 A、真实队列和两个阶段 Step、原子调用准入、证据和外部效果夹具 |
| `dbos_variants.py` | 方案 B 的子 Workflow 和方案 C 的 provider、Turnstile、Step 顺序反例 |
| `tests/test_postgres_recovery.py` | 真进程强杀、取消、重启与逐项断言 |
