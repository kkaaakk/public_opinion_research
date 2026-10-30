"""Official PG aggregate keys and reset windows must not invent differences."""

import copy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "sql_diagnostics",
    Path(__file__).resolve().parents[1]
    / "evaluation/observability/run_sql_diagnostics.py",
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def row(calls, user=1, top=True):
    value = {
        key: 0
        for key in (
            "plans",
            "total_plan_time",
            "total_exec_time",
            "rows",
            "shared_blks_hit",
            "shared_blks_read",
            "shared_blk_read_time",
            "shared_blk_write_time",
            "wal_records",
            "wal_fpi",
            "wal_bytes",
        )
    }
    value.update(
        userid=user,
        dbid=42,
        queryid=7,
        toplevel=top,
        calls=calls,
        fingerprint="safe-hash",
        sql="select ?",
        stats_since="same",
    )
    return value


def snapshot(rows):
    return {
        "oid": 42,
        "statements": rows,
        "database": {"xact_commit": 10},
        "database_stats_reset": "same",
        "statement_info": {"dealloc": 0, "stats_reset": "same"},
        "wal_cluster": {"wal_sync": 1},
        "wal_stats_reset": "same",
    }


def test_statement_delta_uses_complete_official_key():
    before = snapshot([row(10, user=1), row(100, user=2)])
    after = snapshot([row(12, user=1), row(104, user=2)])
    result = driver.server_delta(before, after)
    assert sorted(r["calls"] for r in result["statements"]) == [2, 4]


@pytest.mark.parametrize(
    "change", ["negative", "oid", "reset", "deallocation", "statement_reset"]
)
def test_reset_or_eviction_invalidates_window_instead_of_negative_numbers(change):
    before = snapshot([row(100)])
    after = copy.deepcopy(before)
    after["statements"][0]["calls"] = 104
    if change == "negative":
        after["statements"][0]["calls"] = 4
    elif change == "oid":
        after["oid"] = 43
    elif change == "reset":
        after["database_stats_reset"] = "new"
    elif change == "deallocation":
        after["statement_info"]["dealloc"] = 1
    else:
        after["statements"][0]["stats_since"] = "new"
    result = driver.server_delta(before, after)
    assert result["valid"] is False
    assert not result["statements"]


@pytest.mark.parametrize("change", ["missing", "duplicate", "wal_reset"])
def test_missing_keys_duplicate_endpoint_and_wal_reset_invalidate_window(change):
    before = snapshot([row(10)])
    after = copy.deepcopy(before)
    if change == "missing":
        after["statements"] = []
    elif change == "duplicate":
        after["statements"] *= 2
    else:
        after["wal_stats_reset"] = "new"
    result = driver.server_delta(before, after)
    assert result["valid"] is False
    assert not result["statements"]


def test_planning_without_completed_execution_remains_observable():
    before = snapshot([row(10)])
    after = copy.deepcopy(before)
    after["statements"][0]["plans"] += 1
    after["statements"][0]["total_plan_time"] += 1
    result = driver.server_delta(before, after)
    assert result["valid"]
    assert len(result["statements"]) == 1
    assert result["statements"][0]["calls"] == 0
    assert result["statements"][0]["plans"] == 1
    assert result["statements"][0]["total_plan_time"] == 1
