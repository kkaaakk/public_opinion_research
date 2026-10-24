"""Run actual tools from both project and child directories to check cache roots."""

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[1]
OLD_CACHES = (".pytest_cache", ".benchmarks", ".ruff_cache", ".mypy_cache")


def _mini_project(tmp_path):
    (tmp_path / "pyproject.toml").write_bytes((ROOT / "pyproject.toml").read_bytes())
    tests = tmp_path / "tests"
    tests.mkdir()
    source = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    hook = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_configure"
    )
    start = min([hook.lineno, *(node.lineno for node in hook.decorator_list)])
    code = "\n".join(source.splitlines()[start - 1 : hook.end_lineno])
    (tests / "conftest.py").write_text(
        "import pytest\n\n" + code + "\n", encoding="utf-8"
    )
    (tests / "test_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_cache_paths(pytestconfig, benchmark):\n"
        "    root = pytestconfig.rootpath\n"
        "    assert pytestconfig.cache._cachedir == root / '.tmp/pytest-cache'\n"
        "    pytestconfig.cache.set('cache_contract/probe', 1)\n"
        "    assert pytestconfig.cache.get('cache_contract/probe', None) == 1\n"
        "    assert pytestconfig._benchmarksession.storage.path == root / '.tmp/benchmarks'\n"
        "    assert benchmark.pedantic(lambda: 1, rounds=1, iterations=1) == 1\n",
        encoding="utf-8",
    )
    (tmp_path / "probe.py").write_text(
        '"""Cache probe."""\nvalue: int = 1\n', encoding="utf-8"
    )
    env = dict(os.environ)
    for name in (
        "PYTEST_ADDOPTS",
        "RUFF_CACHE_DIR",
        "MYPY_CACHE_DIR",
        "MYPY_CONFIG_FILE_DIR",
    ):
        env.pop(name, None)
    env.update(
        PYTHON_DOTENV_DISABLED="1",
        OPENAI_AGENTS_DISABLE_TRACING="1",
        LANGSMITH_TRACING="false",
        MYPYPATH="",
    )
    return tests, env


def _run(cwd, env, *args):
    result = subprocess.run(
        [sys.executable, "-m", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("child_cwd", [False, True])
def test_project_defaults_keep_all_tool_caches_below_tmp(tmp_path, child_cwd):
    tests, env = _mini_project(tmp_path)
    cwd = tests if child_cwd else tmp_path
    _run(
        cwd,
        env,
        "pytest",
        "-q",
        "--benchmark-save=cache-contract",
        str(tests / "test_probe.py"),
    )
    _run(cwd, env, "ruff", "check", "--select", "E,F,I", str(tmp_path / "probe.py"))
    _run(cwd, env, "mypy", str(tmp_path / "probe.py"))
    for name in ("pytest-cache", "benchmarks", "ruff-cache", "mypy-cache"):
        assert (tmp_path / ".tmp" / name).is_dir(), name
    assert list((tmp_path / ".tmp/benchmarks").rglob("*.json"))
    assert not any(
        (parent / name).exists() for parent in (tmp_path, tests) for name in OLD_CACHES
    )


def test_explicit_benchmark_storage_is_preserved(tmp_path):
    tests, env = _mini_project(tmp_path)
    target = tmp_path / "explicit-benchmarks"
    (tests / "test_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_override(pytestconfig):\n"
        "    assert pytestconfig._benchmarksession.storage.path == Path("
        + repr(str(target))
        + ")\n",
        encoding="utf-8",
    )
    _run(
        tests,
        env,
        "pytest",
        "-q",
        "--benchmark-storage=file://" + str(target),
        str(tests / "test_probe.py"),
    )
    assert target.is_dir()
    assert not (tmp_path / ".tmp" / "benchmarks").exists()


def test_cache_storage_does_not_change_test_selection_or_plugins():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    assert options["testpaths"] == ["tests"]
    assert options["addopts"][:3] == ["--strict-markers", "-m", "not integration"]
    assert not any("no:" in value or "skip" in value for value in options["addopts"])
