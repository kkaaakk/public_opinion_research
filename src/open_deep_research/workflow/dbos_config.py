"""Central DBOS execution settings; operator secrets never enter workflow input."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

APP = "public-opinion-research"
VERSION = "dbos-phase5-studio-v1"
SDK_VERSION = "0.24.0"
CONTROL = "por-control"
BUSINESS = "por-business"
ARCHIVE = "por-archive"
RECOVERY = "por-recovery"
FACT_WRITE_GRACE_SECONDS = 15
SETTLEMENT_GRACE_SECONDS = 60


@dataclass(frozen=True)
class ExecutionSettings:
    url: str
    max_active: int = 4
    owner_active: int = 2
    queue_capacity: int = 1000
    approval_seconds: float = 86400
    session_path: str | None = None
    run_seconds: float = 3600

    def __post_init__(self):
        if not self.url or make_url(self.url).drivername != "postgresql+psycopg":
            raise ValueError("DBOS_DATABASE_URL must use postgresql+psycopg")
        if (
            min(
                self.max_active,
                self.owner_active,
                self.queue_capacity,
                self.approval_seconds,
                self.run_seconds,
            )
            <= 0
        ):
            raise ValueError("Execution limits must be positive")

    @classmethod
    def environment(cls):
        return cls(
            os.environ.get("DBOS_DATABASE_URL", ""),
            int(os.environ.get("RUN_MAX_ACTIVE", "4")),
            int(os.environ.get("RUN_OWNER_ACTIVE", "2")),
            int(os.environ.get("RUN_QUEUE_CAPACITY", "1000")),
            float(os.environ.get("RUN_APPROVAL_SECONDS", "86400")),
            os.environ.get("RUN_SDK_SESSION_PATH") or None,
            float(os.environ.get("RUN_TIMEOUT_SECONDS", "3600")),
        )

    def engine(self):
        return create_engine(
            self.url,
            pool_pre_ping=True,
            pool_timeout=3,
            connect_args={
                "connect_timeout": 3,
                "options": "-c lock_timeout=5000 -c statement_timeout=10000",
            },
        )


@lru_cache(maxsize=1)
def settings():
    return ExecutionSettings.environment()
