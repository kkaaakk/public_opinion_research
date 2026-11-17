"""Retry grants use real native execution, public messages and actual archives."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from dbos_support import (
    has_unconfirmed_bills_or_effects,
    record_scripted_model_attempt,
    retry_observed_failure,
)
from sqlalchemy import text

from open_deep_research.web.run_data import (
    RunConflict,
    digest,
    loads,
    publish_event,
)
from open_deep_research.workflow import dbos_workflow as engine
from open_deep_research.workflow.dbos_config import domain_engine

pytestmark = pytest.mark.integration


def test_terminal_delivery_drains_reordered_committed_messages(
    native_case, monkeypatch
):
    """A terminal envelope can arrive first when independent sends race."""
    from dbos import DBOS

    case = native_case
    original_review, original_send = engine.research.research_review, engine.send_event
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original_review(state, cfg)

    def enqueue_terminal_first(run_id, data):
        with domain_engine().begin() as db:
            publish_event(
                db, run_id, "_terminal", idempotency_key="terminal-first", **data
            )
            publish_event(
                db,
                run_id,
                "status",
                idempotency_key="committed-tail",
                generation=1,
                message="Producer tail",
            )

    async def send(run_id, event_type, **data):
        if event_type == "_terminal" and data["generation"] == 1:
            assert (
                next(frame for frame in data["frames"] if frame["type"] == "usage")[
                    "model_calls"
                ]
                > 0
            )
            await DBOS.run_step_async(None, enqueue_terminal_first, run_id, data)
        else:
            await original_send(run_id, event_type, **data)

    monkeypatch.setattr(engine.research, "research_review", review)
    monkeypatch.setattr(engine, "send_event", send)
    run = case.create()
    failed = saved(case, run, "failed", 0)
    case.client.retry(
        run["run_id"], "alice", "reordered-terminal", failed["state_version"]
    )
    completed = saved(case, run, "completed", 1)
    _, events = assert_consistent(case, run, completed)
    terminal = [e for e in events if e["generation"] == 1]
    tail = next(e for e in terminal if e.get("message") == "Producer tail")
    usage = next(e for e in terminal if e["type"] == "usage")
    history = next(e for e in terminal if e["type"] == "history_saved")
    assert tail["seq"] < usage["seq"] < history["seq"] < events[-1]["seq"]
    assert len(attempts) == 2 and len(case.models) == 4


def saved(case, run, status, generation):
    return case.wait(
        run,
        lambda r: r["status"] == status
        and r["generation"] == generation
        and not r["retry_requested"]
        and r["archive_status"] == "saved",
    )


def command_for(run_id, key):
    with domain_engine().connect() as db:
        row = db.execute(
            text(
                "SELECT version,generation,outcome FROM por_domain.retries WHERE run_id=:id AND key=:k"
            ),
            {"id": run_id, "k": key},
        ).one()
    return {
        "key": key,
        "version": row[0],
        "generation": row[1],
        "outcome": digest(loads(row[2])),
    }


def assert_consistent(case, run, result):
    record = case.history.get(result["history_id"], owner="alice")
    events = case.client.read_events(run["run_id"], "alice", 0, 10000)
    assert record["status"] == result["status"]
    assert record["run_state_version"] == result["generation"] + 1
    assert record["usage"] == {
        k: result["usage"].get(k, 0)
        for k in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "usage_complete",
        )
        if k in result["usage"]
    }
    assert record["report"] == result["report"]
    assert result["events_complete"]
    assert events[-1]["type"] == "done"
    assert events[-1]["generation"] == result["generation"]
    assert events[-1]["status"] == result["status"]
    assert events[-1]["archive_status"] == "saved"
    usage = next(e for e in reversed(events) if e["type"] == "usage")
    assert usage["generation"] == result["generation"]
    for key in ("model_calls", "input_tokens", "output_tokens", "total_tokens"):
        assert usage[key] == result["usage"].get(key, 0)
    return record, events


def test_old_retry_key_never_grants_a_later_failure_and_new_key_can_retry(
    native_case, monkeypatch
):
    """Promote the final-review reproducer, including in-flight and success ACKs."""
    case = native_case
    original = engine.research.research_review
    entered = {i: threading.Event() for i in (2, 3)}
    release = {i: threading.Event() for i in (2, 3)}
    attempts = []

    async def review(state, cfg):
        attempt = len(attempts) + 1
        attempts.append(attempt)
        if attempt > 1:
            entered[attempt].set()
            while not release[attempt].is_set():
                await asyncio.sleep(0.02)
        if attempt <= 2:
            await record_scripted_model_attempt()
            raise RuntimeError("Synthetic settled safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    first = saved(case, run, "failed", 0)
    first_record, _ = assert_consistent(case, run, first)
    key = "original-review-retry"
    try:
        case.client.retry(run["run_id"], "alice", key, first["state_version"])
        assert entered[2].wait(15)
        assert case.client.get(run["run_id"], "alice")["retry_requested"]
        for _ in range(3):
            current = case.client.retry(
                run["run_id"], "alice", key, first["state_version"]
            )
            assert current["generation"] == 1
            assert current["archive_status"] != "saved"
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"],
                "alice",
                "cannot-grant-unsettled",
                current["state_version"],
            )
        release[2].set()
        second = saved(case, run, "failed", 1)
        second_record, _ = assert_consistent(case, run, second)
        assert second["usage"]["model_calls"] == first["usage"]["model_calls"] + 1
        with pytest.raises(RunConflict):
            case.client.retry(run["run_id"], "alice", key, second["state_version"])
        native_version = case.client.native(run["run_id"]).updated_at
        for _ in range(3):
            assert (
                case.client.retry(run["run_id"], "alice", key, first["state_version"])[
                    "status"
                ]
                == "failed"
            )
        assert case.client.native(run["run_id"]).updated_at == native_version
        assert attempts == [1, 2]
        assert len(case.models) == 2
        assert case.calls == {"public_signal": 1, "internal_knowledge": 1}

        stale = command_for(run["run_id"], key)
        for i, cmd in enumerate(
            (stale, dict(stale, generation=2), dict(stale, key="forged"))
        ):
            case.client.client.send(
                run["run_id"] + ":finalize",
                cmd,
                topic="retry",
                idempotency_key="stale-" + str(i),
            )
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"], "alice", "new-key-old-view", first["state_version"]
            )
        case.client.retry(
            run["run_id"], "alice", "second-grant", second["state_version"]
        )
        assert entered[3].wait(15)
        current = case.client.get(run["run_id"], "alice")
        assert current["generation"] == 2 and current["archive_status"] != "saved"
        assert current["history_id"] is None
        for k, v in (
            (key, first["state_version"]),
            ("second-grant", second["state_version"]),
        ):
            case.client.retry(run["run_id"], "alice", k, v)
        case.wait(
            run,
            lambda r: any(
                e["type"] == "retry_started" and e["generation"] == 2
                for e in case.client.read_events(run["run_id"], "alice", 0, 10000)
            ),
        )
        with domain_engine().begin() as db:
            for event in ("run_status", "done", "history_saved"):
                publish_event(
                    db,
                    run["run_id"],
                    event,
                    generation=1,
                    status="failed",
                    id="late-old-archive",
                )
        release[3].set()
        completed = saved(case, run, "completed", 2)
        record, events = assert_consistent(case, run, completed)
        assert completed["usage"]["model_calls"] == 28
        assert attempts == [1, 2, 3]
        assert len(case.models) == 4
        assert case.calls == {"public_signal": 1, "internal_knowledge": 1}
        assert not any(e.get("id") == "late-old-archive" for e in events)
        assert [e["generation"] for e in events if e["type"] == "retry_started"] == [
            1,
            2,
        ]
        from open_deep_research.web.history import HistoryRecord

        case.history.save([HistoryRecord.model_validate(first_record)])
        case.history.save([HistoryRecord.model_validate(second_record)])
        assert case.history.get(completed["history_id"], owner="alice") == record
        native_version = case.client.native(run["run_id"]).updated_at
        for k, v in (
            (key, first["state_version"]),
            ("second-grant", second["state_version"]),
        ):
            assert (
                case.client.retry(run["run_id"], "alice", k, v)["status"] == "completed"
            )
        assert case.client.native(run["run_id"]).updated_at == native_version
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"],
                "alice",
                "completed-new-grant",
                completed["state_version"],
            )
        assert attempts == [1, 2, 3]
    finally:
        for event in release.values():
            event.set()


def test_concurrent_distinct_keys_authorize_one_native_failure(
    native_case, monkeypatch
):
    case = native_case
    original = engine.research.research_review
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    failed = saved(case, run, "failed", 0)
    barrier = threading.Barrier(2)

    def submit(key):
        barrier.wait(timeout=10)
        try:
            case.client.retry(run["run_id"], "alice", key, failed["state_version"])
            return "accepted"
        except RunConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["first", "second"]))
    assert sorted(results) == ["accepted", "conflict"]
    completed = saved(case, run, "completed", 1)
    assert_consistent(case, run, completed)
    assert len(attempts) == 2
    assert len(case.models) == 4
    with domain_engine().connect() as db:
        assert (
            db.execute(
                text("SELECT count(*) FROM por_domain.retries WHERE run_id=:id"),
                {"id": run["run_id"]},
            ).scalar_one()
            == 1
        )


@pytest.mark.parametrize("outcome", ["cancelled", "unknown"])
def test_old_key_cannot_bypass_cancel_or_unknown_effect(
    native_case, monkeypatch, outcome
):
    case = native_case
    original = engine.research.research_review
    attempts = []
    entered = threading.Event()

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        if outcome == "cancelled":
            entered.set()
            await asyncio.Event().wait()
        return await original(state, cfg)

    async def uncertain_external_write(state, cfg, report, usage):
        from dbos import DBOS

        from open_deep_research.workflow.execution import (
            finish_operation,
            stage_context,
            start_operation,
            unconfirmed,
        )

        run_id = stage_context.get().run_id
        await asyncio.to_thread(
            start_operation,
            run_id,
            DBOS.workflow_id,
            DBOS.executor_id,
            "opaque-before-ack",
            "external_tool",
            "fingerprint",
        )
        with domain_engine().begin() as db:
            db.execute(text("CREATE TABLE external_retry_effects(id text PRIMARY KEY)"))
            db.execute(text("INSERT INTO external_retry_effects VALUES ('one')"))
        await asyncio.to_thread(
            finish_operation, run_id, "opaque-before-ack", "unknown"
        )
        raise unconfirmed("Actual committed external effect, acknowledgement unknown")

    monkeypatch.setattr(engine.research, "research_review", review)
    if outcome == "unknown":
        case.cfg["configurable"].update(
        )
        monkeypatch.setattr(
            engine.reporting, "persist_report_memory", uncertain_external_write
        )
    run = case.create()
    failed = saved(case, run, "failed", 0)
    failed = retry_observed_failure(case, run, "only-grant", failed)
    if outcome == "cancelled":
        assert entered.wait(15)
        assert case.client.get(run["run_id"], "alice")["retry_requested"]
        case.client.retry(run["run_id"], "alice", "only-grant", failed["state_version"])
        case.client.cancel(run["run_id"], "alice")
    expected = "cancelled" if outcome == "cancelled" else "interrupted"
    result = saved(case, run, expected, 1)
    native_version = case.client.native(run["run_id"]).updated_at
    for _ in range(3):
        current = case.client.retry(
            run["run_id"], "alice", "only-grant", failed["state_version"]
        )
        assert current["status"] == expected
        assert current["usage"] == result["usage"]
    assert case.client.native(run["run_id"]).updated_at == native_version
    assert len(attempts) == 2
    assert not result["retry_safe"]
    with pytest.raises(RunConflict):
        case.client.retry(run["run_id"], "alice", "unsafe-new", result["state_version"])
    if outcome == "unknown":
        assert result["settlement"] == "unresolved"
        assert has_unconfirmed_bills_or_effects(run["run_id"])
        with domain_engine().connect() as db:
            assert (
                db.execute(
                    text("SELECT count(*) FROM external_retry_effects")
                ).scalar_one()
                == 1
            )
    else:
        assert_consistent(case, run, result)


@pytest.mark.parametrize(
    "scenario", ["retry-before", "retry-between", "retry-after", "retry-error"]
)
def test_retry_command_recovers_process_death_without_reauthorizing(
    pg_database, tmp_path, scenario
):
    from uuid import uuid4

    from dbos_support import NEO4J, NEO4J_PASSWORD
    from neo4j import GraphDatabase
    from test_dbos_process import configuration, effect_count, kill_owned, start, wait
    from test_python_workflow import initial

    from open_deep_research.web.durable import RunClient
    from open_deep_research.web.history import Neo4jHistoryStore

    client = RunClient()
    executor = "retry-fault-" + uuid4().hex
    process = start(tmp_path, executor, scenario)
    restart = None
    store = Neo4jHistoryStore(
        GraphDatabase.driver(NEO4J, auth=("neo4j", NEO4J_PASSWORD))
    )
    try:
        wait(lambda: (tmp_path / "executor-ready").exists(), process, tmp_path)
        run = client.create(
            "alice",
            "fault",
            {"topic": "brand recall"},
            configuration(tmp_path),
            initial(),
        )[0]
        wait(
            lambda: client.get(run["run_id"], "alice")["archive_status"] == "saved",
            process,
            tmp_path,
        )
        first = client.get(run["run_id"], "alice")
        assert first["status"] == "failed" and first["retry_safe"]
        assert effect_count(tmp_path, "reviews") == 1
        root_failure = client.native(run["run_id"]).completed_at
        research_id = run["run_id"] + ":research"
        research_failure = client.native(research_id).completed_at
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        wait(lambda: (tmp_path / scenario).exists(), process, tmp_path)
        # The command is durable and consumed, but its native application has
        # not yet acknowledged as applied; a transient result may be recorded.
        assert command_for(run["run_id"], "original")["generation"] == 1
        steps = client.client.list_workflow_steps(run["run_id"] + ":finalize")
        if scenario == "retry-error":
            assert any(
                s["function_name"].endswith("retry_execution")
                and s["output"] == {"retryable": True}
                for s in steps
            )
        else:
            assert not any(
                s["function_name"].endswith("retry_execution")
                and (s["output"] or {}).get("last")
                for s in steps
            )
        if scenario in {"retry-after", "retry-error"}:
            wait(
                lambda: effect_count(tmp_path, "reviews") == 2
                and client.native(run["run_id"]).status == "ERROR",
                process,
                tmp_path,
            )
        if scenario in {"retry-before", "retry-between"}:
            # The Finalizer waits for the rewound leaf before rewinding its parent.
            assert client.native(run["run_id"]).completed_at == root_failure
            changed = client.native(research_id).completed_at != research_failure
            assert changed == (scenario == "retry-between")
        assert effect_count(tmp_path, "reviews") == 2
        kill_owned(process)
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        assert effect_count(tmp_path, "reviews") == 2
        (tmp_path / (scenario + ".release")).write_text("continue")
        (tmp_path / "executor-ready").unlink()
        restart = start(tmp_path, executor, scenario)
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["generation"] == 1
            and r["status"] == "failed"
            and not r["retry_requested"]
            and r["archive_status"] == "saved",
            restart,
            tmp_path,
        )
        second = client.get(run["run_id"], "alice")
        assert effect_count(tmp_path, "reviews") == 2
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
        assert second["usage"]["model_calls"] == first["usage"]["model_calls"] + 1
        receipt = client.client.get_event(
            run["run_id"] + ":finalize", "retry_applied", timeout_seconds=0
        )
        assert receipt["generation"] == 1
        native_version = client.native(run["run_id"]).updated_at
        client.retry(run["run_id"], "alice", "original", first["state_version"])
        assert client.native(run["run_id"]).updated_at == native_version
        assert store.get(second["history_id"], owner="alice")["status"] == "failed"
        client.retry(run["run_id"], "alice", "next-generation", second["state_version"])
        wait(
            lambda: (r := client.get(run["run_id"], "alice"))["generation"] == 2
            and r["status"] == "completed"
            and r["archive_status"] == "saved",
            restart,
            tmp_path,
        )
        final = client.get(run["run_id"], "alice")
        assert effect_count(tmp_path, "reviews") == 3
        assert effect_count(tmp_path) == effect_count(tmp_path, "effects") == 2
        assert final["usage"]["model_calls"] == 28
        archive = store.get(final["history_id"], owner="alice")
        assert archive["status"] == "completed"
        assert archive["report"] == final["report"]
        assert archive["usage"]["model_calls"] == 28
        assert archive["run_state_version"] == 3
        events = client.read_events(run["run_id"], "alice", 0, 10000)
        assert [e["generation"] for e in events if e["type"] == "retry_started"] == [
            1,
            2,
        ]
        assert events[-1]["type"] == "done" and events[-1]["generation"] == 2
        assert (
            events[-1]["status"] == "completed"
            and events[-1]["archive_status"] == "saved"
        )
    finally:
        for child in (process, restart):
            if child:
                kill_owned(child)
        store.close()


def test_terminal_receipt_advancement_preserves_retry_cas(native_case, monkeypatch):
    case = native_case
    original = engine.research.research_review
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("Safe read failure")
        return await original(state, cfg)

    monkeypatch.setattr(engine.research, "research_review", review)
    run = case.create()
    observed = case.wait(
        run,
        lambda r: r["status"] == "failed"
        and r["archive_status"] == "saved"
        and r["events_complete"]
        and r["retry_safe"],
    )
    # A late duplicate usage delivery advances the real relay cursor while the
    # observed failure and its execution authority stay unchanged.
    case.client.client.send(
        run["run_id"] + ":events",
        {"type": "usage", "generation": 0, "usage": observed["usage"]},
        topic="events",
        idempotency_key="late-terminal-usage",
    )
    current = case.wait(run, lambda r: r["state_version"] > observed["state_version"])
    assert current["generation"] == observed["generation"] == 0
    with pytest.raises(RunConflict):
        case.client.retry(run["run_id"], "alice", "fresh", observed["state_version"])
    retry_observed_failure(case, run, "fresh", observed)
    final = saved(case, run, "completed", 1)
    assert final["generation"] == 1
    assert len(attempts) == 2 and len(case.models) == 4
    assert case.calls == {"public_signal": 1, "internal_knowledge": 1}


def test_stale_retry_message_cannot_abandon_pending_archive(native_case, monkeypatch):
    case = native_case
    original_review = engine.research.research_review
    original_save = case.history.save
    unavailable = threading.Event()
    unavailable.set()
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) <= 2:
            await record_scripted_model_attempt()
            raise RuntimeError("Safe read failure")
        return await original_review(state, cfg)

    def save(records):
        result = original_save(records)  # Real Neo4j commit before lost ACK.
        if records[0].run_state_version == 2 and unavailable.is_set():
            from open_deep_research.web.history import HistoryUnavailable

            raise HistoryUnavailable("Committed archive ACK unavailable")
        return result

    monkeypatch.setattr(engine.research, "research_review", review)
    monkeypatch.setattr(case.history, "save", save)
    run = case.create()
    first = saved(case, run, "failed", 0)
    retry_observed_failure(case, run, "original", first)
    case.wait(
        run,
        lambda r: r["generation"] == 1
        and any(
            e["type"] == "archive_pending" and e["generation"] == 1
            for e in case.client.read_events(run["run_id"], "alice", 0, 10000)
        ),
    )
    assert case.client.get(run["run_id"], "alice")["archive_status"] == "pending"
    unavailable.clear()
    case.client.client.send(
        run["run_id"] + ":finalize",
        command_for(run["run_id"], "original"),
        topic="retry",
        idempotency_key="duplicate-during-archive",
    )
    second = saved(case, run, "failed", 1)
    assert_consistent(case, run, second)
    assert len(attempts) == 2
    retry_observed_failure(case, run, "next", second)
    final = saved(case, run, "completed", 2)
    assert_consistent(case, run, final)
    assert len(attempts) == 3
    assert final["usage"]["model_calls"] == 28


def test_new_retry_requires_delivery_of_the_current_failed_generation(
    native_case, monkeypatch
):
    case = native_case
    original_review = engine.research.research_review
    original_event = engine.public_event
    entered, release = threading.Event(), threading.Event()
    attempts = []

    async def review(state, cfg):
        attempts.append(1)
        if len(attempts) <= 2:
            raise RuntimeError("Safe read failure")
        return await original_review(state, cfg)

    async def delayed_event(event):
        # Delay real terminal stream delivery, never fake a native status.
        if event["type"] == "done" and event.get("generation") == 1:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
        return await original_event(event)

    monkeypatch.setattr(engine.research, "research_review", review)
    monkeypatch.setattr(engine, "public_event", delayed_event)
    run = case.create()
    first = saved(case, run, "failed", 0)
    case.client.retry(run["run_id"], "alice", "first", first["state_version"])
    try:
        assert entered.wait(15)
        second = case.client.get(run["run_id"], "alice")
        assert second["status"] == "failed" and second["generation"] == 1
        assert not second["events_complete"]
        with pytest.raises(RunConflict):
            case.client.retry(
                run["run_id"], "alice", "not-yet-observed", second["state_version"]
            )
        case.client.retry(run["run_id"], "alice", "first", first["state_version"])
        assert len(attempts) == 2
    finally:
        release.set()
    delivered = saved(case, run, "failed", 1)
    assert delivered["state_version"] > second["state_version"]
    case.client.retry(run["run_id"], "alice", "second", delivered["state_version"])
    completed = saved(case, run, "completed", 2)
    assert_consistent(case, run, completed)
    assert len(attempts) == 3
