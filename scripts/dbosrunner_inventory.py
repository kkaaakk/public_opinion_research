"""Reproduce the Workflow/Step/model-call inventory without loading services."""

import argparse
import ast
import json
from pathlib import Path


def inventory(root):
    workflows, steps, calls = [], [], []
    for file in sorted((root / "src/open_deep_research").rglob("*.py")):
        tree = ast.parse(file.read_text(encoding="utf-8-sig"))
        path = file.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Call) and ast.unparse(
                        decorator.func
                    ) in {"DBOS.workflow", "DBOS.step"}:
                        name = next(
                            (
                                ast.literal_eval(k.value)
                                for k in decorator.keywords
                                if k.arg == "name"
                            ),
                            node.name,
                        )
                        (
                            workflows
                            if ast.unparse(decorator.func) == "DBOS.workflow"
                            else steps
                        ).append(
                            dict(
                                name=name,
                                function=node.name,
                                file=path,
                                line=node.lineno,
                            )
                        )
            if isinstance(node, ast.Call):
                target = ast.unparse(node.func)
                if target.endswith(
                    (
                        "ainvoke_model_with_budget",
                        "invoke_model_with_budget",
                        "Runner.run",
                        "runner.run",
                        ".get_response",
                        "checkpoint",
                        "checkpoint_sync",
                    )
                ):
                    calls.append(
                        dict(
                            target=target,
                            file=path,
                            line=node.lineno,
                            boundary=ast.literal_eval(node.args[0])
                            if target.endswith(("checkpoint", "checkpoint_sync"))
                            and node.args
                            and isinstance(node.args[0], ast.Constant)
                            else None,
                        )
                    )
    source_files = list((root / "src/open_deep_research").rglob("*.py"))
    source_lines = sum(
        sum(
            bool(line.strip()) and not line.lstrip().startswith("#")
            for line in file.read_text(encoding="utf-8-sig").splitlines()
        )
        for file in source_files
    )
    return dict(
        workflows=workflows,
        steps=steps,
        call_boundaries=calls,
        source_stats=dict(
            python_files=len(source_files), nonblank_noncomment_lines=source_lines
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--baseline", type=Path, help="Optional exported main checkout")
    args = parser.parse_args()
    data = inventory(Path(__file__).resolve().parents[1])
    if args.baseline:
        data["baseline"] = inventory(args.baseline)
    result = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
    else:
        print(result)
