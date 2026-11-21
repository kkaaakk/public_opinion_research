# 在显式启用后对真实模型供应商执行文本、工具和结构化输出冒烟检查。
"""Opt-in live smoke tests for the configured model providers."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from agents import ModelSettings, ModelTracing, function_tool
from dotenv import load_dotenv
from openai.types.responses import ResponseFunctionToolCall

from open_deep_research.llm.generation import generate_structured, generate_text
from open_deep_research.models.sdk import sdk_model
from open_deep_research.state import ResearchQuestion, ResearchReview, Summary

LOGGER = logging.getLogger(__name__)
DEFAULT_VOLCENGINE_MODEL = "doubao-seed-2-0-mini-260428"


# 回显模型传入的短代码，供工具调用链路验证。
@function_tool
def echo_code(code: str) -> str:
    """Echo a short code supplied by the model."""
    return code


async def smoke_volcengine(model_id: str = DEFAULT_VOLCENGINE_MODEL) -> None:
    """Exercise Ark plain text and structured generation."""
    ref = model_id if model_id.startswith("volcengine:") else f"volcengine:{model_id}"
    _, usage = await generate_text(ref, "Reply with only: OK", max_tokens=128)
    summary, structured_usage = await generate_structured(
        ref, "Summarize and include one key excerpt: The sky is blue.", Summary, max_tokens=128,
    )
    assert summary.summary and summary.key_excerpts
    LOGGER.info("Ark model=%s plain=%r structured=%r", ref, usage, structured_usage)


async def smoke_deepseek() -> None:
    """Exercise DeepSeek plain, tool request and validated business schemas."""
    ref = "deepseek:deepseek-flash"
    _, usage = await generate_text(ref, "Think briefly, then reply with only: OK", max_tokens=1024)
    async with sdk_model(ref) as model:
        response = await model.get_response(
            system_instructions=None, input="Call echo_code with code='OK'.",
            model_settings=ModelSettings(max_tokens=1024), tools=[echo_code],
            output_schema=None, handoffs=[], tracing=ModelTracing.DISABLED,
        )
        assert any(isinstance(item, ResponseFunctionToolCall) for item in response.output)
    _, brief_usage = await generate_structured(
        ref, "Create a brief about electric vehicle safety.", ResearchQuestion, max_tokens=1024,
    )
    _, review_usage = await generate_structured(
        ref, "Review this evidence and mark complete: Verified sales increased.", ResearchReview,
        max_tokens=1024,
    )
    LOGGER.info(
        "DeepSeek plain=%r tool=PASS brief=%r review=%r",
        usage, brief_usage, review_usage,
    )


# 仅运行用户显式启用的真实供应商检查。
def main() -> None:
    """Run explicitly enabled live provider smoke tests."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--provider", choices=("volcengine", "deepseek"))
    group.add_argument("--all", action="store_true")
    parser.add_argument(
        "--model",
        default=DEFAULT_VOLCENGINE_MODEL,
        help=(
            "Volcengine preset Model ID or ep-* endpoint ID. The optional "
            "volcengine: prefix is accepted."
        ),
    )
    args = parser.parse_args()
    if os.getenv("RUN_LIVE_MODEL_TESTS") != "1":
        raise SystemExit("SKIPPED: set RUN_LIVE_MODEL_TESTS=1 to call real model APIs")
    providers = ("volcengine", "deepseek") if args.all else (args.provider,)
    for provider in providers:
        asyncio.run(smoke_volcengine(args.model) if provider == "volcengine" else smoke_deepseek())


if __name__ == "__main__":
    main()
