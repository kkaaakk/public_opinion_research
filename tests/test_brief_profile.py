"""Reproducible native fixture measurement; inference is synthetic, services are real."""

import json
import os
import statistics
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def test_native_fixture_profile(native_case):
    c = native_case
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        run = c.create()
        done = c.wait(
            run, lambda r: r["archive_status"] == "saved" and r["events_complete"]
        )
        assert done["status"] == "completed"
        samples.append(
            dict(
                seconds=time.perf_counter() - start,
                usage=done["usage"],
                workflow_version=done["workflow_version"],
            )
        )
    times = [v["seconds"] for v in samples]
    result = dict(
        samples=samples,
        n=len(samples),
        p50=statistics.median(times),
        p95=statistics.quantiles(times, n=20, method="inclusive")[18],
        inference="controlled SDK fixture; not provider quality or production latency",
    )
    if os.environ.get("BRIEF_PROFILE_OUTPUT"):
        Path(os.environ["BRIEF_PROFILE_OUTPUT"]).write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
