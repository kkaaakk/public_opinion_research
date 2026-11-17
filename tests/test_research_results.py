"""Research-result contracts: deterministic joins, isolation and strict reads."""

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
        "agents": {r: {"report": "old-" + r} for r in ROLES},
        "runtime": {"budget": {"model_calls": 7, "input_tokens": 70}},
        "messages": [],
        "report": {"final": "untouched"},
    }


def result(role, round_=1, **updates):
    return ResearchRoleResult(
        role=role,
        round=round_,
        report="new-" + role,
        budget_delta={"model_calls": 2, "input_tokens": 11, "tool_calls": 1},
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
    for r in ROLES:
        assert (
            left["agents"][r]["report"]
            == "old-" + r + "\n\n--- Additional role research report ---\nnew-" + r
        )
    values[ROLES[0]].report = "mutated"
    assert "mutated" not in left["agents"][ROLES[0]]["report"]


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


@pytest.mark.parametrize("bad", ["role", "round", "missing", "extra", "mutated"])
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
    else:
        values[ROLES[1]].budget_delta = {"bad": object()}
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
    expected = "new-" + ROLES[0]
    if not replace:
        expected = (
            "old-"
            + ROLES[0]
            + ("\n\n--- Additional role research report ---\n" + expected) * 2
        )
    assert state["agents"][ROLES[0]]["report"] == expected


def test_budget_is_additive_even_if_payload_contains_override_words():
    state = initial()
    value = result(ROLES[0]).model_copy(
        update={
            "budget_delta": {"model_calls": 2, "type": "override", "value": {}},
        }
    )
    assemble_research_results(state, {ROLES[0]: value}, packets(state, (ROLES[0],)))
    assert state["runtime"]["budget"]["model_calls"] == 9


def test_only_current_artifact_or_explicit_disabled_receipt_is_accepted():
    state = initial()
    packet = packets(state, (ROLES[0],))[ROLES[0]]
    value = read_research_result(
        {"research_result": result(ROLES[0]).model_dump(mode="json")},
        ROLES[0],
        packet,
    )
    assert value == result(ROLES[0])
    assert (
        read_research_result(
            {"patch": {}, "observation": {"skipped": True, "reason": "role_disabled"}},
            ROLES[0],
            packet,
        )
        is None
    )
    with pytest.raises(ValueError, match="Missing research result"):
        read_research_result({"patch": {}}, ROLES[0], packet)


@pytest.mark.parametrize(
    "patch",
    [
        {"agents": {ROLES[0]: {"report": "old-wire"}}},
        {"runtime": {"budget": {"model_calls": 1}}},
        {"research": {"run_id": "old"}},
        {"agents": {ROLES[0]: {"report": "x", "memory": []}}},
    ],
)
def test_legacy_patches_are_not_replayed(patch):
    state = initial()
    with pytest.raises(ValueError):
        read_research_result({"patch": patch}, ROLES[0], state)


@pytest.mark.parametrize(
    "update",
    [
        {"role": "risk_assessment"},
        {"round": 0},
        {"round": True},
        {"report": {}},
        {"memory": []},
        {"rolling_summary": "old"},
        {"working_context": {}},
        {"metrics_delta": {"graph_nodes_written": 1}},
    ],
)
def test_result_schema_rejects_invalid_or_removed_fields(update):
    value = result(ROLES[0]).model_dump(mode="json")
    value.update(update)
    with pytest.raises(ValidationError):
        ResearchRoleResult.model_validate(value)
