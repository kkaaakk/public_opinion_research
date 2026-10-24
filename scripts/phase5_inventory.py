"""Reproduce the Phase 5 file dependency audit and complete production SLOC diff."""

import argparse
import ast
import json
import re
import subprocess
from pathlib import Path

import tomllib
from dbos_inventory import count

BASE = "a8574f61d0adba4b07ec860d5c6d7de0a0041e76"
RETIRED = {
    "src/open_deep_research/runtime/agent_runtime.py",
    "src/open_deep_research/workflow/graph.py",
    "src/security/auth.py",
    "langgraph.json",
}
PATTERN = re.compile(
    r"langgraph|StateGraph|AgentRuntime|agent_runtime|business_agent_runtime|"
    r"workflow_backend|deep_researcher|Callback|langsmith|langchain",
    re.I,
)


def git(*args):
    """Read immutable Git evidence without importing application code."""
    return subprocess.check_output(["git", *args]).decode("utf-8-sig")


def inventory(base):
    """Classify references, retain import edges and measure every src Python diff."""
    paths = git("ls-tree", "-r", "--name-only", base).splitlines()
    references = []
    imports = {}
    changed = set(git("diff", "--name-only", base).splitlines())
    for name in paths:
        if not name.endswith(
            (
                ".py",
                ".toml",
                ".json",
                ".yaml",
                ".yml",
                ".md",
                ".js",
                ".cjs",
                ".ipynb",
                ".example",
                ".lock",
                ".jsonl",
                ".svg",
                ".graphml",
                ".cfg",
                ".txt",
                ".ps1",
                ".sh",
                ".bat",
            )
        ):
            continue
        source = git("show", base + ":" + name)
        if not PATTERN.search(source):
            continue
        references.append(
            {
                "path": name,
                "symbols": sorted(set(PATTERN.findall(source))),
                "disposition": "1_delete_engine_only"
                if name in RETIRED
                else "4_historical_reference"
                if name.endswith(".ipynb")
                or name.startswith(
                    (
                        "docs/audits/",
                        "docs/project-learning/",
                        "docs/interview-architecture/",
                        "evaluation/",
                        "tests/expt_results/",
                    )
                )
                or name == "CLAUDE.md"
                else "2_migrated_consumer"
                if name in changed
                else "3_retained_business_or_contract",
            }
        )
        if name.endswith(".py"):
            tree = ast.parse(source)
            imports[name] = sorted(
                {
                    module
                    for node in ast.walk(tree)
                    for module in (
                        [node.module or ""]
                        if isinstance(node, ast.ImportFrom)
                        else [alias.name for alias in node.names]
                        if isinstance(node, ast.Import)
                        else []
                    )
                }
            )
    current = {p.as_posix() for p in Path("src").rglob("*.py")}
    changes = []
    for name in sorted(
        {p for p in paths if p.startswith("src/") and p.endswith(".py")} | current
    ):
        old = git("show", base + ":" + name) if name in paths else ""
        new = Path(name).read_text(encoding="utf-8-sig") if Path(name).is_file() else ""
        if old.replace("\r\n", "\n") != new.replace("\r\n", "\n"):
            before, after = count(old), count(new)
            changes.append(
                dict(
                    path=name,
                    before=before,
                    after=after,
                    sloc_delta=after["sloc"] - before["sloc"],
                )
            )
    consumers = {}
    for path, modules in imports.items():
        for module in modules:
            consumers.setdefault(module, []).append(path)
    old_lock = tomllib.loads(git("show", base + ":uv.lock"))["package"]
    new_lock = tomllib.loads(Path("uv.lock").read_text(encoding="utf-8"))["package"]
    old_names = {p["name"] for p in old_lock}
    new_names = {p["name"] for p in new_lock}
    return {
        "baseline": base,
        "historical_note": "historical note: removed integrations can appear in immutable baseline import edges and negative regression guard filenames; these references do not describe current runtime integrations.",
        "scope": "All tracked textual references, Python import edges; all src Python SLOC excluding blanks/comments/docstrings",
        "references_before": references,
        "imports_before": imports,
        "import_consumers_before": consumers,
        "dependency_retirement": {
            "before_count": len(old_lock),
            "after_count": len(new_lock),
            "removed": sorted(old_names - new_names),
            "remaining_langgraph": sorted(
                name for name in new_names if name.startswith("langgraph")
            ),
        },
        "historical_data_readers_retained": [
            "src/open_deep_research/web/history.py",
            "src/open_deep_research/observability/langsmith.py",
        ],
        "production_changes": changes,
        "net_production_sloc": sum(row["sloc_delta"] for row in changes),
        "net_production_physical": sum(
            row["after"]["physical"] - row["before"]["physical"] for row in changes
        ),
    }


def main():
    """Write the reproducible inventory and print only aggregate metrics."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = inventory(args.base)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(  # noqa: T201
        json.dumps(
            {
                k: result[k]
                for k in ("baseline", "net_production_sloc", "net_production_physical")
            }
        )
    )


if __name__ == "__main__":
    main()
