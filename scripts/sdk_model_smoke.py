"""Run an explicitly enabled paid SDK/provider smoke; offline tests are default."""

from __future__ import annotations

import argparse
import asyncio
import logging

from agents import Agent, ModelSettings, RunConfig, Runner

from open_deep_research.models.sdk import sdk_model

LOGGER = logging.getLogger(__name__)


async def _run(args: argparse.Namespace) -> None:
    from dotenv import load_dotenv

    load_dotenv()
    async with sdk_model(args.model, api=args.api) as model:
        result = await Runner.run(
            Agent(
                name="sdk-provider-smoke",
                model=model,
                model_settings=ModelSettings(max_tokens=128),
            ),
            "Reply with exactly SDK_SMOKE_OK.",
            max_turns=1,
            run_config=RunConfig(
                tracing_disabled=True, trace_include_sensitive_data=False
            ),
        )
        LOGGER.info("%s", result.final_output)
        LOGGER.info("requests=%s", result.context_wrapper.usage.requests)


def main() -> None:
    """Require an explicit opt-in before reading credentials or making requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--enable-real-api",
        action="store_true",
        help="Explicitly allow a paid provider request.",
    )
    parser.add_argument(
        "--model", required=True, help="Existing provider:model reference."
    )
    parser.add_argument("--api", choices=["responses", "chat_completions"])
    args = parser.parse_args()
    if not args.enable_real_api:
        parser.error("Real API smoke is disabled; pass --enable-real-api to opt in.")
    LOGGER.addHandler(logging.StreamHandler())
    LOGGER.setLevel(logging.INFO)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
