# opinion-business

独立发行包为 `opinion-business`，Python 导入名为 `opinion_business`，要求 Python 3.12 或更新版本。运行时仅使用标准库，不依赖 DeerFlow、LangGraph 或原项目 Agent。

Phase 0 提供可执行的离线连通性检查，以及七个业务领域的接口。`rag`、`research_graph`、`social_media`、`sentiment`、`risk`、`evidence`、`reports` 的真实业务实现尚未迁入；接口不执行检索、采集、分类或报告生成。

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e .
.venv/Scripts/python.exe -m unittest discover -s tests -v
```

以上命令从本目录执行，Windows 使用 `.venv/Scripts/python.exe`；WSL/Linux 使用 `.venv/bin/python`。也可从仓库根目录运行 `uv run --python 3.12 --with ./opinion_business python -c "from opinion_business import ping; print(ping('phase0'))"`。

```python
from opinion_business import get_status, ping

ping("phase0")
# {'status': 'ok', 'phase': 'phase0', 'version': '0.1.0', 'text': 'phase0'}
get_status()
# domains 中的七个领域均为 interface_only，不能据此宣称业务已迁移。
```

`ping` 接受去除首尾空白后 1–200 个字符的字符串；类型不符抛出 `TypeError`，空字符串或超长字符串抛出 `ValueError`。它不读取配置、环境变量、网络或业务数据。

扩展通过该公开接口调用业务包。后续业务实现应逐个满足领域 Protocol，并在包内添加实现与测试；适配 DeerFlow 的代码归属 `deerflow_extension/`。
