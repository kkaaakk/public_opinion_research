"""Opt-in live smoke tests for configured model providers."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from open_deep_research.budget import structured_output_chain
from open_deep_research.models import create_chat_model
from open_deep_research.state import ResearchQuestion, ResearchReview, Summary

LOGGER = logging.getLogger(__name__)
DEFAULT_VOLCENGINE_MODEL = "doubao-seed-2-0-mini-260428"


@tool
def echo_code(code: str) -> str:
    """Echo a short code supplied by the model."""
    return code


def _usage(response: object) -> object:
    return getattr(response, "usage_metadata", None) or getattr(
        response, "response_metadata", {}
    ).get("token_usage")


def smoke_volcengine(model_id: str = DEFAULT_VOLCENGINE_MODEL) -> None:
    """Exercise Ark plain and structured-output calls."""
    logical_model = (
        model_id if model_id.startswith("volcengine:") else f"volcengine:{model_id}"
    )
    model = create_chat_model(logical_model, max_tokens=128)
    response = model.invoke([HumanMessage(content="Reply with only: OK")])
    envelope = structured_output_chain(model, Summary, max_attempts=1).invoke(
        [HumanMessage(content="Summarize and quote: The sky is blue.")]
    )
    assert isinstance(envelope["parsed"], Summary)
    LOGGER.info(
        "volcengine model=%s plain=PASS structured=PASS usage=%r structured_usage=%r",
        logical_model,
        _usage(response),
        _usage(envelope["raw"]),
    )


def smoke_deepseek() -> None:
    """Exercise DeepSeek thinking, tool-calling, and structured-output calls."""
    model = create_chat_model("deepseek:deepseek-flash", max_tokens=1024)
    response = model.invoke(
        [HumanMessage(content="Think briefly, then reply with only: OK")]
    )
    tool_response = model.bind_tools([echo_code]).invoke(
        [HumanMessage(content="Call echo_code with code='OK'.")]
    )
    assert getattr(tool_response, "tool_calls", None)
    brief = structured_output_chain(model, ResearchQuestion, max_attempts=1).invoke(
        [HumanMessage(content="Create a brief about electric vehicle safety.")]
    )
    assert isinstance(brief["parsed"], ResearchQuestion)
    review = structured_output_chain(model, ResearchReview, max_attempts=1).invoke(
        [HumanMessage(content="Review this evidence and mark complete: verified.")]
    )
    assert isinstance(review["parsed"], ResearchReview)
    LOGGER.info(
        "deepseek plain=PASS react_tool_calling=PASS brief_structured=PASS "
        "review_structured=PASS usage=%r brief_usage=%r review_usage=%r",
        _usage(response),
        _usage(brief["raw"]),
        _usage(review["raw"]),
    )


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
        help="Volcengine preset Model ID or ep-* endpoint ID.",
    )
    args = parser.parse_args()
    if os.getenv("RUN_LIVE_MODEL_TESTS") != "1":
        raise SystemExit("SKIPPED: set RUN_LIVE_MODEL_TESTS=1 to call real model APIs")
    providers = ("volcengine", "deepseek") if args.all else (args.provider,)
    for provider in providers:
        smoke_volcengine(args.model) if provider == "volcengine" else smoke_deepseek()


if __name__ == "__main__":
    main()
