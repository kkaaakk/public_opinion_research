"""Native workflow ownership and retired execution boundary contracts."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src/open_deep_research"


def test_main_graph_contract():
    tree = ast.parse(
        (SOURCE_ROOT / "workflow/dbos_workflow.py").read_text(encoding="utf-8")
    )
    workflows = {
        kw.value.value
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        if isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "workflow"
        for kw in decorator.keywords
        if kw.arg == "name"
    }
    assert workflows == {
        "por.run",
        "por.plan",
        "por.research",
        "por.role",
        "por.events",
        "por.finalize",
    }


def test_research_subgraph_contract():
    from open_deep_research.workflow.dbos_workflow import OPERATIONS
    from open_deep_research.workflow.python_state import RESEARCH_ROLES

    assert RESEARCH_ROLES == ("public_signal", "internal_knowledge")
    assert set(OPERATIONS) == {
        "enrich_query_images",
        "clarify_with_user",
        "write_research_brief",
        "plan_report_sections",
        "research_review",
        "section_writer",
        "write_final_sections",
        "compile_final_report",
    }
    source = (SOURCE_ROOT / "workflow/dbos_workflow.py").read_text(encoding="utf-8")
    assert 'for role in ("risk_assessment", "response_strategy"):' in source
    assert source.index("merge_research_patches(state, patches)") < source.index(
        'round_ = state["workflow"]["round"]'
    )
    assert "DBOS.wait_first_async" in source
    assert "DBOS.cancel_workflow_async" in source


def test_graph_and_utils_keep_their_module_boundaries():
    shim = ast.parse((SOURCE_ROOT / "utils.py").read_text(encoding="utf-8"))
    assert all(
        isinstance(node, (ast.Expr, ast.ImportFrom, ast.Assign)) for node in shim.body
    )
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for node in shim.body
    )
    for path in SOURCE_ROOT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    (
                        "langgraph",
                        "open_deep_research.runtime.agent_runtime",
                        "open_deep_research.workflow.graph",
                    )
                ), path
