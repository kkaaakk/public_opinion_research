"""Reproduce the Phase 4 source inventory at a fixed Git revision.

SLOC excludes blank lines, standalone comments and module/class/function
docstrings. Physical lines are also reported. Method ranges never overlap
within a file. Counts are an inventory, NOT an automatic deletion claim.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

BASE = "5ca71c12f15013654a26f619071218a2be151a00"
PHASE3 = "c6c6f54d5fe66972d33d29ccbc5b414c62cdc6ee"
ROOT = Path(__file__).resolve().parents[2]
FILES = [
    "web/run_store.py",
    "web/worker.py",
    "workflow/python_workflow.py",
    "workflow/execution.py",
    "web/runs.py",
    "web/history.py",
    "runtime/sdk_context.py",
    "runtime/sdk_runtime.py",
    "workflow/python_state.py",
]


def source(path, revision=BASE):
    return subprocess.check_output(
        ["git", "show", f"{revision}:{path}"], cwd=ROOT, stderr=subprocess.DEVNULL
    ).decode("utf-8-sig")


def inventory(text):
    tree = ast.parse(text)
    lines = text.splitlines()
    docstrings = set()
    for node in ast.walk(tree):
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
        ):
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstrings.update(range(first.lineno, first.end_lineno + 1))
    sloc = {
        n
        for n, line in enumerate(lines, 1)
        if line.strip() and not line.lstrip().startswith("#") and n not in docstrings
    }
    definitions = []
    for top in tree.body:
        nodes = (
            [
                (top.name + "." + node.name, node)
                for node in top.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if isinstance(top, ast.ClassDef)
            else [(getattr(top, "name", ""), top)]
        )
        for name, node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]])
            end = node.end_lineno
            definitions.append(
                dict(
                    name=name,
                    start=start,
                    end=end,
                    physical=end - start + 1,
                    sloc=sum(start <= n <= end for n in sloc),
                )
            )
    return dict(physical=len(lines), sloc=len(sloc), definitions=definitions)


def main():
    output = dict(baseline=BASE, phase3=PHASE3, files={})
    for name in FILES:
        full = f"src/open_deep_research/{name}"
        item = inventory(source(full))
        try:
            previous = inventory(source(full, PHASE3))
        except subprocess.CalledProcessError:
            previous = dict(physical=0, sloc=0)
        item["phase3"] = {k: previous[k] for k in ("physical", "sloc")}
        output["files"][name] = item
    output["core_total"] = {
        key: sum(output["files"][name][key] for name in FILES[:3])
        for key in ("physical", "sloc")
    }
    output["poc_files"] = {
        p.name: inventory(p.read_text(encoding="utf-8-sig"))
        for p in Path(__file__).parent.glob("*.py")
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
