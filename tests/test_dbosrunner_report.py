"""Native response validation and chapter retry preserve successful calls."""

from collections import Counter

import pytest
from dbos_support import retry_observed_failure
from sdk_model_support import Reply, install_inference

from open_deep_research.state import Section, Sections

pytestmark = pytest.mark.integration


def test_section_failure_reuses_completed_chapter_and_prior_phase(
    native_case, monkeypatch
):
    case = native_case
    calls = Counter()

    def chapters(request):
        if request.schema_name == "Sections":
            calls["plan"] += 1
            return Sections(
                sections=[
                    Section(
                        name=f"CHAPTER_{chapter}_SENTINEL",
                        description=subject,
                        research=True,
                    )
                    for chapter, subject in (("A", "Evidence"), ("B", "Risk"))
                ]
            )
        if request.schema_name is None:
            for chapter in ("A", "B"):
                if f"CHAPTER_{chapter}_SENTINEL" in request.prompt:
                    calls[chapter] += 1
                    if chapter == "B" and calls[chapter] == 1:
                        return Reply("Partial chapter with real usage", status="length")
                    return f"Chapter {chapter}: supported evidence"

    install_inference(monkeypatch, chapters)
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


@pytest.mark.parametrize(
    "first_reply", [Reply(""), Reply("Partial synthesis", status="length")]
)
def test_incomplete_synthesis_retries_last_response_without_repeating_roles(
    native_case, monkeypatch, first_reply
):
    case = native_case
    calls = []

    def responses(request):
        if (
            request.schema_name is None
            and "Create the complete enterprise" in request.prompt
        ):
            calls.append(1)
            if len(calls) == 1:
                return first_reply
        return None

    install_inference(monkeypatch, responses)
    run = case.create()
    failed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert failed["retry_safe"] and len(calls) == 1
    retry_observed_failure(case, run, "replace-incomplete-response", failed)
    done = case.wait(
        run,
        lambda r: r["status"] == "completed"
        and r["archive_status"] == "saved"
        and r["events_complete"],
    )
    assert len(calls) == 2 and len(case.models) == 4
    assert done["usage"]["model_calls"] == failed["usage"]["model_calls"] + 1
