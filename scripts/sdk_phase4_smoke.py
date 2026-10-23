"""Run the formal native DBOS service/fault contracts with offline inference."""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-local-services", action="store_true")
    args = parser.parse_args()
    if not args.enable_local_services:
        parser.error(
            "Explicit --enable-local-services is required; see docs/architecture/dbos-deployment.md"
        )
    root = Path(__file__).resolve().parents[1]
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "integration",
            "tests/test_dbos_contracts.py",
            "tests/test_dbos_process.py",
            "--disable-socket",
            "--allow-unix-socket",
            "--allow-hosts=127.0.0.1,::1",
        ],
        cwd=root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
