# 对配置模型执行探测调用，检查连接和结构化输出能力。
"""Probe model availability and structured-output reliability for Round 2."""
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / ".env", override=True)


# 运行模型探测并输出响应或失败诊断。
async def main() -> None:
    from pydantic import BaseModel

    from open_deep_research.llm.generation import generate_structured, generate_text

    # 定义本次结构化响应探测所需的数据模型。
    class Probe(BaseModel):
        need_clarification: bool
        question: str
        verification: str

    for model_name in ("deepseek:deepseek-v4-flash", "deepseek:deepseek-chat"):
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        print(f"=== {model_name} key={'set' if key else 'MISSING'} base={os.environ.get('DEEPSEEK_API_BASE','default')}")
        try:
            for attempt in range(3):
                try:
                    out, _ = await generate_structured(
                        model_name,
                        "Decide: the user asked a complete question about Tesla safety. "
                        "Return need_clarification=false, question='', verification='proceed'.",
                        Probe,
                    )
                    print(f"  structured attempt {attempt+1}: {'PARSED' if out else 'NONE'}", out)
                except Exception as exc:
                    print(f"  structured attempt {attempt+1}: ERROR {type(exc).__name__}: {str(exc)[:200]}")
            plain, _ = await generate_text(model_name, "Reply with exactly: OK")
            print("  plain:", repr(plain)[:100])
        except Exception as exc:
            print(f"  init/invoke ERROR {type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    asyncio.run(main())
