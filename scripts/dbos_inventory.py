"""Count the complete production Python diff against the frozen Phase4 baseline."""

import ast
import json
import subprocess
from pathlib import Path

BASE = "5ca71c12f15013654a26f619071218a2be151a00"


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


def main():
    old_paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", BASE, "src"], text=True
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
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
