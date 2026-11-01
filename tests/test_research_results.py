"""Research-result contracts: deterministic joins, isolation and legacy reads."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from open_deep_research.state import ResearchRoleResult, ResearchTask
from open_deep_research.workflow.python_state import (
    assemble_research_results,
    read_research_result,
)

ROLES = ("public_signal", "internal_knowledge")


def task(role, name="gap"):
    return ResearchTask(
        task_id=name,
        target_role=role,
        objective=name,
        evidence_needed="dated evidence",
        reason="affects risk",
    )


def initial():
    return {
        "workflow": {
            "round": 1,
            "brief": "brand",
            "completed_tasks": [],
            "pending_tasks": [],
        },
        "agents": {
            r: {
                "report": "old-" + r,
                "memory": [{"content": "private-" + r}],
                "rolling_summary": "old-summary-" + r,
            }
            for r in ROLES
        },
        "research": {
            "run_id": "run",
            "working_contexts": {},
            "working_context_scopes": {},
        },
        "runtime": {
            "budget": {"model_calls": 7, "input_tokens": 70},
            "metrics": {"graph_nodes_written": 3},
        },
        "messages": [],
        "report": {"final": "untouched"},
    }


def result(role, round_=1, **updates):
    return ResearchRoleResult(
        role=role,
        round=round_,
        report="new-" + role,
        memory=[{"content": "new-memory-" + role}],
        rolling_summary="summary-" + role,
        budget_delta={"model_calls": 2, "input_tokens": 11, "tool_calls": 1},
        metrics_delta={
            "graph_nodes_written": 2,
            "quality": {"graph_write_latency": "exact"},
        },
        **updates,
    )


def packets(state, roles=ROLES, round_=1):
    return {
        r: {
            **state,
            "workflow": {
                **state["workflow"],
                "round": round_,
                "pending_tasks": [task(r)] if round_ > 1 else [],
            },
        }
        for r in roles
    }


def test_join_is_order_independent_and_counts_only_deltas():
    left, right = initial(), initial()
    values = {r: result(r) for r in ROLES}
    assemble_research_results(left, values, packets(left))
    assemble_research_results(
        right, dict(reversed(list(values.items()))), packets(right)
    )
    assert left == right
    assert left["runtime"]["budget"]["model_calls"] == 11
    assert left["runtime"]["budget"]["input_tokens"] == 92
    assert left["runtime"]["budget"]["tool_calls"] == 2
    assert left["runtime"]["metrics"]["graph_nodes_written"] == 7
    assert left["runtime"]["metrics"]["quality"]["graph_write_latency"] == "exact"
    for r in ROLES:
        assert left["agents"][r]["memory"] == [
            {"content": "private-" + r},
            {"content": "new-memory-" + r},
        ]
        assert left["agents"][r]["rolling_summary"] == "summary-" + r
        assert (
            left["agents"][r]["report"]
            == "old-" + r + "\n\n--- Additional role research report ---\nnew-" + r
        )
    values[ROLES[0]].memory[0]["content"] = "mutated"
    assert left["agents"][ROLES[0]]["memory"][-1]["content"] != "mutated"


@pytest.mark.parametrize("disabled", [False, True])
def test_one_role_unassigned_or_explicitly_skipped(disabled):
    state = initial()
    saved = deepcopy(state["agents"][ROLES[1]])
    values = {ROLES[0]: result(ROLES[0])}
    if disabled:
        values[ROLES[1]] = None
    assemble_research_results(state, values, packets(state, tuple(values)))
    assert state["agents"][ROLES[1]] == saved
    assert state["runtime"]["budget"]["model_calls"] == 9


@pytest.mark.parametrize("bad", ["role", "round", "missing", "extra", "run", "mutated"])
def test_invalid_join_is_atomic(bad):
    state = initial()
    saved = deepcopy(state)
    values = {r: result(r) for r in ROLES}
    if bad == "role":
        values[ROLES[1]] = result(ROLES[0])
    elif bad == "round":
        values[ROLES[1]] = result(ROLES[1], 2)
    elif bad == "missing":
        values.pop(ROLES[1])
    elif bad == "extra":
        values["risk_assessment"] = result(ROLES[1])
    elif bad == "run":
        values[ROLES[1]] = result(
            ROLES[1],
            working_context={},
            working_context_scope={
                "run_id": "other",
                "role": ROLES[1],
                "research_round": 1,
                "task_id": "t",
            },
        )
    else:
        values[ROLES[1]].memory.append({"content": object()})
    with pytest.raises((ValueError, ValidationError)):
        assemble_research_results(state, values, packets(state))
    assert state == saved


@pytest.mark.parametrize("replace", [False, True])
def test_followups_update_only_assigned_role_and_deduplicate_tasks(replace):
    state = initial()
    other = deepcopy(state["agents"][ROLES[1]])
    for round_ in (2, 3):
        value = result(ROLES[0], round_)
        value = value.model_copy(update={"replace_report": replace})
        assemble_research_results(
            state, {ROLES[0]: value}, packets(state, (ROLES[0],), round_)
        )
    assert state["workflow"]["round"] == 3
    assert state["workflow"]["completed_tasks"] == [task(ROLES[0])]
    assert state["agents"][ROLES[1]] == other
    assert len(state["agents"][ROLES[0]]["memory"]) == 3
    expected = "new-" + ROLES[0]
    if not replace:
        expected = (
            "old-"
            + ROLES[0]
            + ("\n\n--- Additional role research report ---\n" + expected) * 2
        )
    assert state["agents"][ROLES[0]]["report"] == expected


def test_graph_context_scope_and_json_roundtrip_preserve_evidence_references():
    state = initial()
    values = {
        r: result(
            r,
            working_context={"active_evidence_ids": ["E-" + r]},
            working_context_scope={
                "run_id": "run",
                "role": r,
                "research_round": 1,
                "task_id": "T-" + r,
            },
        )
        for r in ROLES
    }
    values = {
        r: ResearchRoleResult.model_validate_json(v.model_dump_json())
        for r, v in values.items()
    }
    assemble_research_results(state, values, packets(state))
    for r in ROLES:
        assert state["research"]["working_contexts"][r]["active_evidence_ids"] == [
            "E-" + r
        ]
        assert state["research"]["working_context_scopes"][r]["task_id"] == "T-" + r
    assert state["research"]["run_id"] == "run"
    assert state["report"] == {"final": "untouched"}


def legacy(role, round_=1):
    return {
        "patch": {
            "agents": {
                role: {
                    "report": {"type": "override", "value": "old-wire"},
                    "memory": [{"content": "old-memory"}],
                    "rolling_summary": "old-summary",
                }
            },
            "workflow": {
                "completed_tasks": [task(role)] if round_ > 1 else [],
                **({"round": round_} if round_ > 1 else {}),
            },
            "runtime": {"budget": {"model_calls": 4}},
        },
        "delivery_ids": ["already-delivered"],
    }


@pytest.mark.parametrize("round_", [1, 2])
def test_old_patch_and_new_result_share_one_join_contract(round_):
    state = initial()
    packet = packets(state, (ROLES[0],), round_)[ROLES[0]]
    old = legacy(ROLES[0], round_)
    saved = deepcopy(old)
    value = read_research_result(old, ROLES[0], packet)
    new = read_research_result(
        {"research_result": value.model_dump(mode="json")}, ROLES[0], packet
    )
    assert value == new
    assemble_research_results(state, {ROLES[0]: new}, {ROLES[0]: packet})
    assert state["runtime"]["budget"]["model_calls"] == 11
    assert state["agents"][ROLES[0]]["report"] == "old-wire"
    assert old == saved


@pytest.mark.parametrize(
    "bad", ["domain", "private", "progress", "tasks", "scope", "unknown_role_field"]
)
def test_unsupported_legacy_data_is_rejected_without_loss(bad):
    state = initial()
    value = legacy(ROLES[0])
    patch = value["patch"]
    if bad == "domain":
        patch["report"] = {"final": "cannot discard"}
    elif bad == "private":
        patch["agents"][ROLES[1]] = {"report": "cannot discard"}
    elif bad == "progress":
        patch["workflow"]["pending_tasks"] = []
    elif bad == "tasks":
        patch["workflow"]["completed_tasks"] = [task(ROLES[1])]
    elif bad == "scope":
        patch["research"] = {
            "run_id": "other",
            "working_contexts": {ROLES[0]: {}},
            "working_context_scopes": {
                ROLES[0]: {
                    "run_id": "other",
                    "role": ROLES[0],
                    "research_round": 1,
                    "task_id": "t",
                }
            },
        }
    else:
        patch["agents"][ROLES[0]]["future_field"] = "cannot discard"
    with pytest.raises(ValueError):
        value = read_research_result(value, ROLES[0], state)
        assemble_research_results(state, {ROLES[0]: value}, {ROLES[0]: state})


def test_only_explicit_disabled_observation_can_omit_result():
    assert (
        read_research_result(
            {"patch": {}, "observation": {"skipped": True, "reason": "role_disabled"}},
            ROLES[0],
            initial(),
        )
        is None
    )
    with pytest.raises(ValueError):
        read_research_result({"patch": {}}, ROLES[0], initial())


@pytest.mark.parametrize(
    "update",
    [
        {"role": "risk_assessment"},
        {"round": 0},
        {"round": True},
        {"report": {}},
        {"workflow": {}},
        {"working_context": {}},
        {
            "working_context": {},
            "working_context_scope": {
                "run_id": "run",
                "role": ROLES[1],
                "research_round": 1,
                "task_id": "t",
            },
        },
    ],
)
def test_result_schema_rejects_invalid_structure(update):
    value = result(ROLES[0]).model_dump(mode="json")
    value.update(update)
    with pytest.raises(ValidationError):
        ResearchRoleResult.model_validate(value)


def test_budget_and_metrics_are_additive_even_if_payload_contains_override_words():
    state = initial()
    value = result(ROLES[0]).model_copy(
        update={
            "budget_delta": {"model_calls": 2, "type": "override", "value": {}},
            "metrics_delta": {
                "graph_nodes_written": 2,
                "type": "override",
                "value": {},
            },
        }
    )
    assemble_research_results(state, {ROLES[0]: value}, packets(state, (ROLES[0],)))
    assert state["runtime"]["budget"]["model_calls"] == 9
    assert state["runtime"]["metrics"]["graph_nodes_written"] == 5


def test_disabled_artifact_preserves_historical_zero_usage_normalization():
    from open_deep_research.budget import empty_budget_usage

    artifact = {
        "patch": {"runtime": {"budget": empty_budget_usage()}},
        "observation": {"skipped": True, "reason": "role_disabled"},
    }
    assert read_research_result(artifact, ROLES[0], initial()) is None
    artifact["patch"]["runtime"]["budget"]["model_calls"] = 1
    with pytest.raises(ValueError, match="Skipped"):
        read_research_result(artifact, ROLES[0], initial())


@pytest.mark.parametrize("field", ["working_context", "working_context_scope"])
def test_unknown_graph_payload_fields_are_never_silently_discarded(field):
    value = result(
        ROLES[0],
        working_context={},
        working_context_scope={
            "run_id": "run",
            "role": ROLES[0],
            "research_round": 1,
            "task_id": "t",
        },
    ).model_dump(mode="json")
    value[field]["unknown_saved_data"] = "preserve or reject"
    with pytest.raises(ValidationError):
        ResearchRoleResult.model_validate(value)
