"""Export FastAPI's REST contract for the frontend TypeScript generator."""

import json
import os
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
os.environ["LANGSMITH_TRACING"] = "false"

if __name__ == "__main__":
    from open_deep_research.web.server import app

    target = Path(__file__).resolve().parents[1] / ".tmp" / "por-openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
