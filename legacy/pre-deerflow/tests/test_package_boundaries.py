# 检查历史源码包已移除，打包发现仅包含当前有效包。
"""Packaging regression guards for retired source packages."""

from pathlib import Path

import tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# 验证旧源码包目录不存在。
def test_legacy_source_package_is_absent() -> None:
    assert not (PROJECT_ROOT / "src" / "legacy").exists()


# 验证 setuptools 包发现不会包含旧实现。
def test_setuptools_discovery_does_not_include_legacy() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        config = tomllib.load(file)
    include = config["tool"]["setuptools"]["packages"]["find"]["include"]
    assert all(not pattern.startswith("legacy") for pattern in include)
