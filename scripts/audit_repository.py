"""Inventory tracked repository files without importing application code.

Run after staging new files so the working-tree inventory includes them::

    python scripts/audit_repository.py --output docs/audits/pre-openai-sdk-inventory.json

Untracked and ignored local files are deliberately outside this inventory.
Static import findings are review candidates, never proof that code is unused.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_COMMIT = "41cbc134f748750ffbfc7c7dde65c7552ace6296"


def git(root: Path, *args: str, input_data: bytes | None = None) -> bytes:
    """Run a read-only Git command in the selected repository."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        input=input_data,
        check=True,
        capture_output=True,
    ).stdout


def snapshot(root: Path, ref: str | None) -> dict[str, bytes]:
    """Read a commit tree or tracked working files, never walking directories."""
    if ref is None:
        paths = git(root, "ls-files", "--cached", "-z").decode("utf-8").split("\0")
        result = {}
        for name in sorted(set(paths) - {""}):
            path = root / name
            if path.is_symlink():
                result[name] = path.readlink().as_posix().encode("utf-8")
            elif path.is_file():
                result[name] = path.read_bytes()
        return result
    entries = []
    for entry in git(root, "ls-tree", "-rz", "--full-tree", ref).split(b"\0"):
        if entry:
            meta, name = entry.split(b"\t", 1)
            _, kind, object_id = meta.split()
            if kind == b"blob":
                entries.append((name.decode("utf-8"), object_id))
    batch = b"".join(object_id + b"\n" for _, object_id in entries)
    stream = io.BytesIO(git(root, "cat-file", "--batch", input_data=batch))
    result = {}
    for name, _ in entries:
        _, kind, size = stream.readline().split()
        if kind != b"blob":
            raise ValueError(f"Expected blob for {name}")
        result[name] = stream.read(int(size))
        if stream.read(1) != b"\n":
            raise ValueError(f"Invalid Git batch framing for {name}")
    return result


def text_content(content: bytes) -> str | None:
    """Decode UTF-8 text while excluding binary assets."""
    if b"\0" in content:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def normalized_content(content: bytes) -> bytes:
    """Ignore checkout CRLF conversions when reporting changed text files."""
    return (
        content.replace(b"\r\n", b"\n")
        if text_content(content) is not None
        else content
    )


def module_name(path: str) -> str:
    """Map a tracked Python path to its import name."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts[0] == "src":
        parts.pop(0)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_from(tree: ast.AST, module: str, package: bool) -> set[str]:
    """Collect absolute and relative imports conservatively."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = module.split(".") if package else module.split(".")[:-1]
                parent = parent[: len(parent) - node.level + 1]
                base = ".".join([*parent, *([base] if base else [])])
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def artifact_category(path: str) -> str | None:
    """Identify historical/generated material by explicit repository locations."""
    name = PurePosixPath(path)
    if "/archive/" in f"/{path}":
        return "archived_material"
    if path.startswith("evaluation/") and name.suffix in {".json", ".txt"}:
        if name.name not in {"rubric.json", "single_case.json"}:
            return "historical_evaluation_output"
    if path.startswith("tests/expt_results/"):
        return "historical_evaluation_baseline"
    if (
        path.startswith(".trae/specs/")
        or name.name.startswith("ARCHITECTURE_OPTIMIZATION_LOG")
        or path == "CHANGELOG_general_mode_removal.md"
    ):
        return "historical_design_record"
    if path.startswith(("docs/project-learning/", "docs/interview-architecture/")):
        if (
            name.suffix in {".pptx", ".pdf", ".svg", ".graphml"}
            or name.name == "code-graph-analysis.json"
        ):
            return "learning_or_interview_asset"
    if path == "trajectory-live.png":
        return "historical_observability_screenshot"
    return None


def import_cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Find strongly connected components in the local static import graph."""
    indexes, lows, stack, active, components = {}, {}, [], set(), []

    def visit(node: str) -> None:
        indexes[node] = lows[node] = len(indexes)
        stack.append(node)
        active.add(node)
        for child in sorted(edges.get(node, set())):
            if child not in indexes:
                visit(child)
                lows[node] = min(lows[node], lows[child])
            elif child in active:
                lows[node] = min(lows[node], indexes[child])
        if lows[node] == indexes[node]:
            component = []
            while True:
                child = stack.pop()
                active.remove(child)
                component.append(child)
                if child == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(edges):
        if node not in indexes:
            visit(node)
    return sorted(components)


def analyze(files: dict[str, bytes]) -> dict[str, Any]:
    """Calculate counts, file responsibilities, and conservative audit candidates."""
    records, trees, imports = [], {}, {}
    texts = {path: text_content(content) for path, content in files.items()}
    modules = {module_name(path): path for path in files if path.endswith(".py")}
    for path, content in sorted(files.items()):
        text = texts[path]
        python = path.endswith(".py")
        scope = (
            "production"
            if path.startswith("src/")
            else "test"
            if path.startswith("tests/")
            else "support"
        )
        record = {
            "path": path,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "lf_normalized_sha256": hashlib.sha256(
                normalized_content(content)
            ).hexdigest(),
            "text_lines": len(text.splitlines()) if text is not None else None,
            "python": python,
            "scope": scope,
            "artifact_category": artifact_category(path),
        }
        if python and text is not None:
            record["nonblank_noncomment_lines"] = sum(
                bool(line.strip()) and not line.lstrip().startswith("#")
                for line in text.splitlines()
            )
            try:
                tree = ast.parse(text, filename=path)
                trees[path] = tree
                imports[path] = imports_from(
                    tree, module_name(path), path.endswith("/__init__.py")
                )
                record["functions"] = sum(
                    isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                    for node in ast.walk(tree)
                )
                record["classes"] = sum(
                    isinstance(node, ast.ClassDef) for node in ast.walk(tree)
                )
            except SyntaxError as error:
                record["parse_error"] = f"{error.msg}:{error.lineno}"
            record["compatibility_export"] = (
                "compatibility export" in (ast.get_docstring(trees[path]) or "").lower()
                if path in trees
                else False
            )
        records.append(record)
    directories: dict[str, Counter] = defaultdict(Counter)
    for record in records:
        parts = PurePosixPath(record["path"]).parts[:-1]
        for directory in [
            ".",
            *("/".join(parts[:i]) for i in range(1, len(parts) + 1)),
        ]:
            stats = directories[directory]
            stats.update(
                {
                    "files": 1,
                    "bytes": record["bytes"],
                    "text_lines": record["text_lines"] or 0,
                }
            )
            if record["python"]:
                stats.update(
                    {"python_files": 1, "python_lines": record["text_lines"] or 0}
                )
    edges: dict[str, set[str]] = {path: set() for path in imports}
    consumers: dict[str, set[str]] = defaultdict(set)
    runtime_imports = []
    for path, names in sorted(imports.items()):
        for name in sorted(names):
            if name in modules and modules[name] != path:
                edges[path].add(modules[name])
                consumers[modules[name]].add(path)
        runtime_names = sorted(
            name
            for name in names
            if name.split(".")[0] == "langgraph"
            or name.split(".")[0].startswith("langchain")
        )
        if runtime_names:
            runtime_imports.append({"path": path, "imports": runtime_names})
    candidates, compatibility = [], []
    for record in records:
        path = record["path"]
        if not record["python"]:
            continue
        if record.get("compatibility_export"):
            compatibility.append(
                {"path": path, "static_importers": sorted(consumers[path])}
            )
        if (
            path.startswith("src/")
            and not path.endswith("/__init__.py")
            and not consumers[path]
        ):
            tokens = [module_name(path), path, path.removeprefix("src/")]
            mentions = [
                other
                for other, text in texts.items()
                if other != path
                and text is not None
                and any(token in text for token in tokens)
            ]
            candidates.append(
                {
                    "path": path,
                    "text_references": sorted(mentions),
                    "status": "review_only_dynamic_imports_and_entry_points_require_manual_check",
                }
            )
    python_records = [record for record in records if record["python"]]
    artifacts = [record for record in records if record["artifact_category"]]
    return {
        "summary": {
            "files": len(records),
            "bytes": sum(record["bytes"] for record in records),
            "python_files": len(python_records),
            "python_physical_lines": sum(
                record["text_lines"] or 0 for record in python_records
            ),
            "python_lines_by_scope": {
                scope: sum(
                    record["text_lines"] or 0
                    for record in python_records
                    if record["scope"] == scope
                )
                for scope in ("production", "test", "support")
            },
            "python_nonblank_noncomment_lines_by_scope": {
                scope: sum(
                    record["nonblank_noncomment_lines"]
                    for record in python_records
                    if record["scope"] == scope
                )
                for scope in ("production", "test", "support")
            },
            "historical_or_generated_files": len(artifacts),
            "historical_or_generated_by_category": dict(
                sorted(
                    Counter(record["artifact_category"] for record in artifacts).items()
                )
            ),
            "compatibility_export_files": len(compatibility),
        },
        "directories": {
            name: dict(value) for name, value in sorted(directories.items())
        },
        "python_over_lines": {
            str(limit): [
                {"path": record["path"], "lines": record["text_lines"]}
                for record in sorted(
                    python_records,
                    key=lambda row: (-(row["text_lines"] or 0), row["path"]),
                )
                if (record["text_lines"] or 0) > limit
            ]
            for limit in (400, 600, 1000)
        },
        "compatibility_exports": compatibility,
        "no_static_importer_candidates": candidates,
        "langgraph_langchain_imports": runtime_imports,
        "static_import_cycles": import_cycles(edges),
        "files": records,
    }


def main() -> None:
    """Write a deterministic before/after JSON inventory and compact summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-ref", default=BACKUP_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode().strip())
    base = git(root, "rev-parse", f"{args.base_ref}^{{commit}}").decode().strip()
    before_files, after_files = snapshot(root, base), snapshot(root, None)
    before, after = analyze(before_files), analyze(after_files)
    inventory = {
        "schema_version": 1,
        "base_commit": base,
        "method": {
            "historical_note": "historical note: removed Agent Observer integration is retained only in historical evaluation scripts and backup records; its regression-guard filename in this inventory does not describe an active runtime integration.",
            "universe": "Git commit blobs before; git ls-files --cached paths with current file contents after. Stage new files before the final run.",
            "exclusions": "Untracked, ignored, absent working files, and Git submodule trees; no directory traversal and no application imports.",
            "line_counts": "Physical UTF-8 Python lines including comments/blanks/docstrings; a separate nonblank/noncomment count still includes docstrings.",
            "scopes": "production=src/**; test=tests/** (including evaluation drivers); support=all other Python files.",
            "artifact_counts": "Explicit location/extension heuristics classify archived records, evaluation outputs/baselines, and learning assets; classifications do not imply safe deletion.",
            "static_analysis": "AST imports overestimate guarded/type-only cycles and underestimate dynamic tool/entry-point usage. Candidates cannot justify deletion alone.",
            "self_reference": "The output file, if already staged, is counted at its pre-write content size; omit its own bytes/hash when comparing repeated runs.",
            "line_endings": "Byte counts/raw hashes preserve commit or working-file bytes; LF-normalized hashes and modified lists ignore text CRLF checkout conversion.",
        },
        "before": before,
        "after": after,
        "changes": {
            "added": sorted(after_files.keys() - before_files.keys()),
            "removed": sorted(before_files.keys() - after_files.keys()),
            "modified": sorted(
                path
                for path in before_files.keys() & after_files.keys()
                if normalized_content(before_files[path])
                != normalized_content(after_files[path])
            ),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(  # noqa: T201
        json.dumps(
            {"before": before["summary"], "after": after["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
