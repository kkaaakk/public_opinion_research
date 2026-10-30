"""Subprocess exit probe; synthetic keys and loopback OTLP only."""

import os
import signal
import sys
import time
from pathlib import Path

from open_deep_research.observability.telemetry import (
    business_span,
    initialize_telemetry,
)

assert os.environ["LANGFUSE_BASE_URL"].startswith("http://127.0.0.1:")
assert initialize_telemetry()
with business_span("exit_probe", run_id="exit-probe"):
    pass
if sys.argv[1] == "unhandled":
    raise RuntimeError("controlled exit probe")
if sys.argv[1] == "wait":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Path(sys.argv[2]).write_text("ready")
    while True:
        time.sleep(0.1)
