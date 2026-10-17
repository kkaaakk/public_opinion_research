"""Exercise the offline repository inventory without application imports."""

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_repository.py"
SPEC = importlib.util.spec_from_file_location("audit_repository", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_working_snapshot_does_not_walk_or_include_untracked_files(
    tmp_path, monkeypatch
):
    (tmp_path / "tracked.py").write_bytes(b"value = 1\n")
    (tmp_path / "local.env").write_text("secret=private\n", encoding="utf-8")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "cache.py").write_text("cached = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        audit, "git", lambda *_args, **_kwargs: b"tracked.py\0deleted.py\0"
    )
    assert audit.snapshot(tmp_path, None) == {"tracked.py": b"value = 1\n"}


def test_commit_snapshot_reads_git_blobs_instead_of_current_files(
    tmp_path, monkeypatch
):
    (tmp_path / "tracked.py").write_text("changed = 2\n", encoding="utf-8")
    original = b"original = 1\n"
    object_id = b"1" * 40

    def fake_git(_root, *args, input_data=None):
        if args[0] == "ls-tree":
            return b"100644 blob " + object_id + b"\ttracked.py\0"
        assert args == ("cat-file", "--batch")
        assert input_data == object_id + b"\n"
        return (
            object_id
            + b" blob "
            + str(len(original)).encode()
            + b"\n"
            + original
            + b"\n"
        )

    monkeypatch.setattr(audit, "git", fake_git)
    assert audit.snapshot(tmp_path, "backup") == {"tracked.py": original}


def test_inventory_reports_explicit_line_scopes_and_binary_files():
    report = audit.analyze(
        {
            "src/pkg/main.py": b"# comment\n\nvalue = 1\n",
            "tests/test_main.py": b"assert True\n",
            "scripts/check.py": b"pass\n",
            "docs/example.png": b"\x00binary",
        }
    )
    assert report["summary"]["files"] == 4
    assert report["summary"]["python_files"] == 3
    assert report["summary"]["python_lines_by_scope"] == {
        "production": 3,
        "test": 1,
        "support": 1,
    }
    assert (
        report["summary"]["python_nonblank_noncomment_lines_by_scope"]["production"]
        == 1
    )
    assert report["directories"]["src/pkg"]["python_lines"] == 3


def test_dynamic_entrypoint_and_compatibility_exports_remain_review_candidates():
    report = audit.analyze(
        {
            "src/pkg/entry.py": b'"""Compatibility exports for a public entry point."""\nfrom pkg.core import run\n',
            "src/pkg/core.py": b"def run():\n    pass\n",
            "langgraph.json": b'{"graphs": {"agent": "pkg.entry:run"}}',
        }
    )
    assert report["compatibility_exports"] == [
        {"path": "src/pkg/entry.py", "static_importers": []}
    ]
    assert report["no_static_importer_candidates"] == [
        {
            "path": "src/pkg/entry.py",
            "text_references": ["langgraph.json"],
            "status": "review_only_dynamic_imports_and_entry_points_require_manual_check",
        }
    ]


def test_relative_imports_and_type_only_cycles_are_disclosed():
    report = audit.analyze(
        {
            "src/pkg/a.py": b"from .b import item\n",
            "src/pkg/b.py": b"from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .a import item\n",
        }
    )
    assert report["static_import_cycles"] == [["src/pkg/a.py", "src/pkg/b.py"]]


def test_artifact_heuristics_do_not_classify_golden_inputs_as_disposable():
    assert audit.artifact_category("tests/rag_eval_cases.jsonl") is None
    assert audit.artifact_category("evaluation/plan_execute_ab/cases.jsonl") is None
    assert audit.artifact_category("evaluation/cost_profile/single_case.json") is None
    assert (
        audit.artifact_category("tests/expt_results/baseline.jsonl")
        == "historical_evaluation_baseline"
    )
    assert (
        audit.artifact_category("docs/project-learning/guide.pdf")
        == "learning_or_interview_asset"
    )


def test_normalization_preserves_binary_payloads_and_ignores_checkout_newlines():
    assert audit.normalized_content(b"value = 1\r\n") == b"value = 1\n"
    assert audit.normalized_content(b"\x00\r\n") == b"\x00\r\n"
