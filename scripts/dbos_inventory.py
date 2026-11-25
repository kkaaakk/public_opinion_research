"""Inventory the native durability boundaries and the Python diff against Phase4.

Two reports, one command:

* ``files`` counts the production SLOC diff against the frozen baseline.
* ``structure`` names every Workflow, decorated Step, dynamic Step and
  Datasource transaction, before and after, so the durability surface can be
  compared by vocabulary instead of by line count.
"""

import ast
import json
import subprocess
from pathlib import Path

BASE = "5ca71c12f15013654a26f619071218a2be151a00"
STEP_CALLS = {"run_step", "run_step_async"}
# Every business transaction goes through workflow/transactions.py, whose single
# entry point carries the transaction name as a literal first argument.
TRANSACTION_CALLS = {"run_transaction"}


def count(source):
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (
            isinstance(body, list)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.update(range(body[0].lineno, body[0].end_lineno + 1))
    lines = source.splitlines()
    return {
        "physical": len(lines),
        "sloc": sum(
            bool(line.strip())
            and not line.lstrip().startswith("#")
            and i not in docstrings
            for i, line in enumerate(lines, 1)
        ),
    }


def _named(call, fallback):
    """Read the literal name of a decorated or dynamically named boundary."""
    for kw in getattr(call, "keywords", ()):
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    if isinstance(call, ast.Call) and call.args:
        options = call.args[0]
        if isinstance(options, ast.Dict):
            for key, value in zip(options.keys, options.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "name"
                    and isinstance(value, ast.Constant)
                ):
                    return str(value.value)
    return fallback


def structure(source):
    """Name every native durability boundary one module declares."""
    tree = ast.parse(source)
    found = {"workflows": set(), "steps": set(), "dynamic_steps": set()}
    found["transactions"] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                target = decorator.func
                if not isinstance(target, ast.Attribute):
                    continue
                if target.attr == "workflow":
                    found["workflows"].add(_named(decorator, node.name))
                elif target.attr == "step":
                    found["steps"].add(_named(decorator, node.name))
                elif target.attr == "transaction":
                    found["transactions"].add(_named(decorator, node.name))
        elif isinstance(node, ast.Call):
            target = node.func
            # A boundary is called either as ``DBOS.run_step_async`` or, inside
            # its own module, as a bare name.
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name in STEP_CALLS:
                found["dynamic_steps"].add(_named(node, f"<{name}>"))
            elif name in TRANSACTION_CALLS:
                literal = bool(node.args) and isinstance(node.args[0], ast.Constant)
                found["transactions"].add(
                    str(node.args[0].value) if literal else f"<{name}>"
                )
    return found


def aggregate(sources):
    total = {
        "workflows": set(),
        "steps": set(),
        "dynamic_steps": set(),
        "transactions": set(),
    }
    for source in sources:
        for key, values in structure(source).items():
            total[key].update(values)
    return {key: sorted(values) for key, values in total.items()} | {
        f"{key}_count": len(values) for key, values in total.items()
    }


def git_sources(ref, paths):
    for path in paths:
        if path.endswith(".py"):
            yield subprocess.check_output(
                ["git", "show", ref + ":" + path]
            ).decode("utf-8-sig")


def main():
    old_paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASE, "src"], text=True
    ).splitlines()
    # The durability vocabulary is compared with the release this branch started
    # from: the SLOC baseline predates the native Workflow layout.
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    head_paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", head, "src"], text=True
    ).splitlines()
    new_paths = [p.as_posix() for p in Path("src").rglob("*.py")]
    changes = []
    for path in sorted(set(old_paths) | set(new_paths)):
        if not path.endswith(".py"):
            continue
        before = (
            subprocess.check_output(["git", "show", BASE + ":" + path]).decode(
                "utf-8-sig"
            )
            if path in old_paths
            else ""
        )
        after = (
            Path(path).read_text(encoding="utf-8-sig") if Path(path).is_file() else ""
        )
        if before.replace("\r\n", "\n") == after.replace("\r\n", "\n"):
            continue
        old = count(before)
        new = count(after)
        changes.append(
            {
                "path": path,
                "before": old,
                "after": new,
                "sloc_delta": new["sloc"] - old["sloc"],
            }
        )
    current = [
        Path(path).read_text(encoding="utf-8-sig")
        for path in new_paths
        if Path(path).is_file()
    ]
    print(
        json.dumps(
            {
                "baseline": BASE,
                "scope": "All changed/new/deleted Python files under src; blanks/comments/docstrings excluded. Tests, deployment and docs reported separately.",
                "files": changes,
                "removed_whole_files_sloc": sum(
                    r["before"]["sloc"] for r in changes if not r["after"]["physical"]
                ),
                "new_whole_files_sloc": sum(
                    r["after"]["sloc"] for r in changes if not r["before"]["physical"]
                ),
                "net_production_sloc": sum(r["sloc_delta"] for r in changes),
                "net_production_physical": sum(
                    r["after"]["physical"] - r["before"]["physical"] for r in changes
                ),
                "structure": {
                    "after_ref": "working tree",
                    "before_ref": head,
                    "before": aggregate(git_sources(head, head_paths)),
                    "after": aggregate(current),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
