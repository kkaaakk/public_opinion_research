"""Measure the complete production diff and research merge boundaries."""

import argparse
import ast
import json
import subprocess
from pathlib import Path

from dbos_inventory import count

BASE = "31e8ad8e865817c9aa2a610591d90b5d24748562"
SYMBOLS = {
    "src/open_deep_research/workflow/python_state.py": (
        "merge_research_patches",
        "read_research_result",
        "assemble_research_results",
    ),
    "src/open_deep_research/state.py": ("ResearchRoleResult",),
    "src/open_deep_research/runtime/business_agent.py": (
        "_build_state_patch",
        "run_business_agent",
    ),
}


def git(*args):
    return subprocess.check_output(["git", *args]).decode("utf-8-sig")


def inventory(base):
    paths = set(git("ls-tree", "-r", "--name-only", base, "src").splitlines())
    paths.update(p.as_posix() for p in Path("src").rglob("*.py"))
    files, symbols = [], []
    for path in sorted(p for p in paths if p.endswith(".py")):
        old = git("show", base + ":" + path)
        new = Path(path).read_text(encoding="utf-8-sig") if Path(path).exists() else ""
        if old.replace("\r\n", "\n") != new.replace("\r\n", "\n"):
            before, after = count(old), count(new)
            files.append(
                {
                    "path": path,
                    "before": before,
                    "after": after,
                    "sloc_delta": after["sloc"] - before["sloc"],
                }
            )
        if path not in SYMBOLS:
            continue
        for version, source in (("before", old), ("after", new)):
            for node in ast.parse(source).body:
                if getattr(node, "name", None) in SYMBOLS[path]:
                    symbols.append(
                        {
                            "version": version,
                            "path": path,
                            "symbol": node.name,
                            **count(ast.get_source_segment(source, node)),
                        }
                    )
    return {
        "baseline": base,
        "scope": "All src Python; excludes blanks, comments and docstrings. Tests/scripts/docs are separate.",
        "files": files,
        "symbols": symbols,
        "net_production_sloc": sum(f["sloc_delta"] for f in files),
        "net_production_physical": sum(
            f["after"]["physical"] - f["before"]["physical"] for f in files
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE)
    print(json.dumps(inventory(parser.parse_args().base), ensure_ascii=False, indent=2))
