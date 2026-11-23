"""History archive merge contracts: a delayed save never rewrites a completed Run."""

from open_deep_research.web.history import HistoryRecord, _merge_record


def _record(**fields):
    base = dict(
        id="history-1",
        run_id="run-1",
        topic="fixture topic",
        started_at="2026-11-23T00:00:00Z",
        completed_at="2026-11-23T00:01:00Z",
        owner="alice",
        status="failed",
        report="",
        run_state_version=1,
        failed_stage="research_review",
        error_message="Research requires review.",
    )
    return HistoryRecord(**{**base, **fields})


def test_completed_record_keeps_no_failed_stage_after_a_stale_save():
    completed = _record(
        status="completed",
        report="final report",
        run_state_version=2,
        failed_stage="",
        error_message="",
    )
    stale = _record()

    merged = _merge_record(completed, stale)

    assert merged.status == "completed"
    assert merged.report == "final report"
    assert merged.run_state_version == 2
    assert merged.failed_stage == ""


def test_failed_record_still_inherits_the_stage_that_explains_it():
    merged = _merge_record(_record(), _record(failed_stage=""))

    assert merged.status == "failed"
    assert merged.failed_stage == "research_review"
