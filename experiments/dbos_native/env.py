"""Keep all native POC inference offline and all writes in disposable services."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
os.environ.update(
    PYTHON_DOTENV_DISABLED="1",
    OPENAI_AGENTS_DISABLE_TRACING="1",
    LANGSMITH_TRACING="false",
    LANGCHAIN_TRACING_V2="false",
    RAG_MEMORY_WRITE_ENABLED="false",
)
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *a, **kw: False
_connect = socket.socket.connect


def local_connect(self, address):
    if isinstance(address, tuple) and address[0] not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise RuntimeError("Native POC forbids non-loopback network connections")
    return _connect(self, address)


socket.socket.connect = local_connect

DB_URL = os.environ.get(
    "NATIVE_DB_URL",
    "postgresql+psycopg://postgres:native-poc-password@127.0.0.1:15433/native_poc",
)
APP_NAME = "por-dbos-native"
VERSION = "phase4-native-v1"


_target = urlparse(DB_URL)
if (
    _target.hostname != "127.0.0.1"
    or _target.port != 15433
    or _target.path != "/native_poc"
):
    raise ValueError(
        "Native POC accepts only its dedicated loopback PostgreSQL database"
    )
