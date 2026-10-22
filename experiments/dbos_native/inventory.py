"""Reproduce baseline, replacement bundle, copied SDK method and gap budgets."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

BASE = "5ca71c12f15013654a26f619071218a2be151a00"
FILES = [
    "web/run_store.py",
    "web/worker.py",
    "workflow/python_workflow.py",
    "workflow/execution.py",
    "web/runs.py",
    "runtime/sdk_runtime.py",
    "runtime/sdk_context.py",
]
REPLACED = {
    "web/run_store.py",
    "web/worker.py",
    "workflow/python_workflow.py",
    "web/runs.py",
}


def count(source):
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if (
            isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
        ):
            expr = node.body[0]
            if (
                isinstance(expr, ast.Expr)
                and isinstance(expr.value, ast.Constant)
                and isinstance(expr.value.value, str)
            ):
                docs.update(range(expr.lineno, expr.end_lineno + 1))
    lines = source.splitlines()
    return dict(
        physical=len(lines),
        sloc=sum(
            bool(line.strip()) and not line.lstrip().startswith("#") and n not in docs
            for n, line in enumerate(lines, 1)
        ),
    )


def main():
    root = Path(__file__).resolve().parents[2]
    baseline = {
        name: count(
            subprocess.check_output(
                ["git", "show", BASE + ":src/open_deep_research/" + name],
                cwd=root,
                encoding="utf-8",
            )
        )
        for name in FILES
    }
    bundle = {
        p.name: count(p.read_text(encoding="utf-8-sig"))
        for p in Path(__file__).parent.glob("*.py")
    }
    a_files = [
        "env.py",
        "codec.py",
        "domain.py",
        "business.py",
        "engine.py",
        "api.py",
        "executor.py",
    ]
    print(
        json.dumps(
            dict(
                baseline_sha=BASE,
                baseline=baseline,
                old_replacement_sloc=sum(baseline[n]["sloc"] for n in REPLACED),
                preserved_sloc=sum(
                    baseline[n]["sloc"] for n in FILES if n not in REPLACED
                ),
                bundle=bundle,
                route_a_files=a_files,
                route_a_sloc=sum(bundle[n]["sloc"] for n in a_files),
                scope_note="All physical production files remain. Route A excludes optional B/probe source; no fixture discount is taken in its conservative total.",
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
