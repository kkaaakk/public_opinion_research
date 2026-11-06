"""Native response validation and chapter retry preserve successful calls."""

from collections import Counter

import pytest
from dbos_support import retry_observed_failure
from langchain_core.runnables import RunnableLambda

from open_deep_research.state import Section, Sections
from open_deep_research.workflow import shared

pytestmark = pytest.mark.integration


def test_section_failure_reuses_completed_chapter_and_prior_phase(
    native_case, monkeypatch
):
    case = native_case
    service = shared.configurable_model
    calls = Counter()

    class Chapters:
        def with_config(self, config):
            return self

        def with_structured_output(self, schema, include_raw=False):
            raw = service.with_structured_output(schema, include_raw)

            async def respond(messages, config=None):
                result = await raw.ainvoke(messages, config)
                if schema is Sections:
                    calls["plan"] += 1
                    result["parsed"] = Sections(
                        sections=[
                            Section(
                                name="CHAPTER_A_SENTINEL",
                                description="Evidence",
                                research=True,
                            ),
                            Section(
                                name="CHAPTER_B_SENTINEL",
                                description="Risk",
                                research=True,
                            ),
                        ]
                    )
                return result

            return RunnableLambda(respond)

        async def ainvoke(self, messages, config=None):
            result = await service.ainvoke(messages, config)
            text = str(messages)
            for chapter in ("A", "B"):
                if f"CHAPTER_{chapter}_SENTINEL" in text:
                    calls[chapter] += 1
                    if chapter == "B" and calls[chapter] == 1:
                        raise RuntimeError(
                            "Controlled chapter failure after real usage"
                        )
                    return result.model_copy(
                        update={"content": f"Chapter {chapter}: supported evidence"}
                    )
            return result

    monkeypatch.setattr(shared, "configurable_model", Chapters())
    case.cfg["configurable"]["report_mode"] = "sections"
    run = case.create()
    failed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert failed["retry_safe"]
    assert calls == {"plan": 1, "A": 1, "B": 1}
    before = failed["usage"]["model_calls"]
    retry_observed_failure(case, run, "chapter-only", failed)
    done = case.wait(
        run,
        lambda r: r["status"] == "completed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert calls == {"plan": 1, "A": 1, "B": 2}
    assert done["usage"]["model_calls"] == before + 1
    assert len(case.models) == 4
    assert "Chapter A:" in done["report"] and "Chapter B:" in done["report"]


def test_incomplete_synthesis_retries_last_response_without_repeating_roles(
    native_case, monkeypatch
):
    case = native_case
    service = shared.configurable_model
    calls = []

    class Responses:
        def with_config(self, config):
            return self

        def with_structured_output(self, *args, **kwargs):
            return service.with_structured_output(*args, **kwargs)

        async def ainvoke(self, messages, config=None):
            result = await service.ainvoke(messages, config)
            if "Role:" not in str(messages[0].content):
                calls.append(1)
                if len(calls) == 1:
                    return result.model_copy(update={"content": ""})
            return result

    monkeypatch.setattr(shared, "configurable_model", Responses())
    run = case.create()
    failed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert failed["retry_safe"] and len(calls) == 1
    retry_observed_failure(case, run, "replace-empty-response", failed)
    done = case.wait(
        run,
        lambda r: r["status"] == "completed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert len(calls) == 2 and len(case.models) == 4
    assert done["usage"]["model_calls"] == failed["usage"]["model_calls"] + 1
