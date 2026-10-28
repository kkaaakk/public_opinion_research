"""The benchmark must reject external services and distinguish absent metrics."""

import runpy
from pathlib import Path

import pytest

DRIVER = runpy.run_path(
    str(
        Path(__file__).resolve().parents[1]
        / "evaluation/observability/run_benchmark.py"
    )
)


def test_percentiles_do_not_invent_values_for_missing_samples():
    assert DRIVER["distribution"]([]) is None
    assert DRIVER["distribution"]([100])["p99"] == 100
    values = DRIVER["distribution"]([40, 10, 30, 20])
    assert values["p50"] == 25 and values["p95"] == pytest.approx(38.5)
    assert values["min"] == 10 and values["max"] == 40 and values["n"] == 4


def test_benchmark_rejects_external_and_obfuscated_service_addresses():
    for url in (
        "https://cloud.langfuse.com",
        "http://127.0.0.1@external.test",
        "bolt://external.test:7687",
    ):
        with pytest.raises(ValueError, match="isolated loopback"):
            DRIVER["local"](url)
    DRIVER["local"]("postgresql+psycopg://local:fixture@127.0.0.1:15434/por_test")


def test_rss_measurement_is_available_and_not_a_zero_fallback():
    assert DRIVER["rss"]() > 1024 * 1024


def test_summary_uses_raw_quantiles_and_does_not_pass_incomplete_experiments():
    def row(samples):
        return {
            "case": "off",
            "runs": [{"elapsed_ms": n} for n in samples],
            "wall_seconds": 1,
            "cpu_seconds": 0.1,
            "peak_rss_mib": 100,
            "latency_ms": {"run": DRIVER["distribution"](samples)},
            "timing_samples_ms": {},
            "export": {},
            "queue": {},
            "span_count": 0,
            "acknowledged_spans": 0,
            "shutdown_seconds": 0.01,
        }

    result = DRIVER["summarize"]([row([1, 1]), row([1, 100])])
    assert result["cases"]["off"]["run_ms"]["p95"] == pytest.approx(85.15)
    assert result["gates"]["all_configurations"] is False
    assert result["gates"]["repeated_samples"] is False
    assert result["all_gates_passed"] is False
