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
        "por.cancel",
    }
    assert not any(
        isinstance(node, ast.AsyncFunctionDef) and node.name == "business_step"
        for node in tree.body
    )
    assert not (SOURCE_ROOT / "workflow/business_steps.py").exists()
    assert not (SOURCE_ROOT / "workflow/durability.py").exists()


def test_research_subgraph_contract():
    from open_deep_research.workflow.python_state import ANALYSIS_ROLES, RESEARCH_ROLES

    assert RESEARCH_ROLES == ("public_signal", "internal_knowledge")
    assert ANALYSIS_ROLES == ("risk_assessment", "response_strategy")
    source = (SOURCE_ROOT / "workflow/dbos_workflow.py").read_text(encoding="utf-8")
    assert (
        source.index("packets = await review_round(run_id, state, config, round_)")
        < source.index("research.risk_assessment")
        < source.index("research.response_strategy")
        < source.index("await reporting.generate_final_report(")
    )
    assert source.index(
        "assemble_research_results(state, results, packets)"
    ) < source.index('round_ = state["workflow"]["round"]')
    assert "await intake.plan_research(" in source
    assert "await intake.revise_brief(" in source
    assert "await research.research_review(" in source
    assert "await publish_stage(" in source
    # Review is a bounded model call inside por.research, not its own Workflow.
    assert "def review_round(" in source
    assert "por.review" not in source
    assert "stage_receipts" not in source
    assert not (SOURCE_ROOT / "workflow/run_facts.py").exists()
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
