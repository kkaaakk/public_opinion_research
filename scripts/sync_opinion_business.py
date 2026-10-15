"""Copy the business distribution into the stock backend Docker build context.

This is packaging preparation, not an extension installer or a service launcher.
The operator still uses uv and DeerFlow's official extension manager.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def sync(root: Path) -> Path:
    source = root / "opinion_business"
    destination = root / "backend/extensions/sources/opinion-business"
    files = [
        source / "pyproject.toml",
        source / "README.md",
        *sorted((source / "opinion_business").rglob("*.py")),
    ]
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected a regular business package file: {path}")
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    expected = {path.relative_to(source) for path in files}
    # Refuse stale deployable Python modules instead of silently shipping removed code.
    stale = {
        path.relative_to(destination) for path in destination.rglob("*.py")
    } - expected
    if stale:
        raise ValueError(
            f"Stale deployment modules require review: {sorted(map(str, stale))}"
        )
    return destination


if __name__ == "__main__":
    print(sync(Path(__file__).resolve().parents[1]))
