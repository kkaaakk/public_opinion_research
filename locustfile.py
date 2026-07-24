"""Locust load-testing script for Public Opinion Research web server.

Exercises the public_opinion_risk scenario with all three modes (fast / normal / deep),
and multiple model / search_api combinations.

Launch the server first:
    uv run python -m open_deep_research.web.server

Run interactively:
    locust -f locustfile.py --host=http://localhost:8000

Run headless (safe baseline — only health/index, no LLM cost):
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 50 -r 10 -t 120s

Run headless with research (CAUTION — costs money):
    locust -f locustfile.py --host=http://localhost:8000 --headless -u 2 -r 1 -t 300s

Environment variables:
    SKIP_RESEARCH_LOAD    – "1" → only health/index (default 0)
    RESEARCH_MODE         – "fast" / "normal" / "deep" / "all" (default "fast")
    RESEARCH_WEIGHT       – task weight for research vs health (default 1)
    RESEARCH_TIMEOUT      – per-request timeout in seconds (default 600)
"""

from __future__ import annotations

import json
import os
import random
import time
from enum import Enum

from locust import HttpUser, between, events, task


# ═══════════════════════════════════════════════════════════════════════════
# Environment-configurable knobs
# ═══════════════════════════════════════════════════════════════════════════

SKIP_RESEARCH = os.environ.get("SKIP_RESEARCH_LOAD", "0") == "1"
RESEARCH_MODE = os.environ.get("RESEARCH_MODE", "fast")   # fast | normal | deep | all
RESEARCH_WEIGHT = int(os.environ.get("RESEARCH_WEIGHT", "1"))
RESEARCH_TIMEOUT = int(os.environ.get("RESEARCH_TIMEOUT", "600"))

# ═══════════════════════════════════════════════════════════════════════════
# Scenario definitions — maps to the two business_scenario values in the app
# ═══════════════════════════════════════════════════════════════════════════

# ── public_opinion_risk topics (brand / enterprise risk) ───────────────
PUBLIC_OPINION_TOPICS = [
    "Analyze public sentiment around Tesla's recent product launches",
    "Assess brand reputation risks for Apple in the EU regulatory environment",
    "Evaluate social media reaction to Microsoft's latest AI announcements",
    "Public opinion analysis of OpenAI's safety practices and governance",
    "Brand risk assessment for Nike after recent marketing campaigns",
]

# ── Organization contexts for public_opinion_risk ──────────────────────
ORG_CONTEXTS = [
    "A Fortune 500 technology company with global consumer brand presence. "
    "Key stakeholders: C-suite, investors, regulators in US/EU/China. "
    "Product lines: cloud services, AI platforms, consumer devices.",

    "A multinational financial services firm with retail and institutional clients. "
    "Regulated by SEC, FCA, and EU authorities. "
    "Recent focus on AI-driven trading and digital banking expansion.",

    "A pharmaceutical company with vaccine and oncology drug portfolios. "
    "FDA/EMA regulated. Public scrutiny around drug pricing and clinical trial transparency. "
    "Operations in 40+ countries.",
]

# ═══════════════════════════════════════════════════════════════════════════
# Model / search-api pools — rotated per-request to spread provider load
# ═══════════════════════════════════════════════════════════════════════════

MODELS = [
    "deepseek:deepseek-chat",
    "openai:gpt-4o-mini",
    "anthropic:claude-sonnet-4-6",
]

SEARCH_APIS = ["tavily", "openai", "anthropic"]


# ═══════════════════════════════════════════════════════════════════════════
# Research profile — a complete parameter set for one request
# ═══════════════════════════════════════════════════════════════════════════

class Scenario(str, Enum):
    PUBLIC_OPINION = "public_opinion_risk"


def _build_profile(scenario: Scenario, mode: str) -> dict:
    """Build a realistic request payload for public_opinion_risk scenario."""
    topic = random.choice(PUBLIC_OPINION_TOPICS)
    org_context = random.choice(ORG_CONTEXTS)

    return {
        "topic": topic,
        "model": random.choice(MODELS),
        "search_api": random.choice(SEARCH_APIS),
        "mode": mode,
        "scenario": scenario.value,
        "org_context": org_context,
        "rag_enabled": False,  # requires local infra — opt-in via env if needed
    }


# ═══════════════════════════════════════════════════════════════════════════
# SSE event consumer — validates the research endpoint's streaming contract
# ═══════════════════════════════════════════════════════════════════════════

class SSEValidator:
    """Parses SSE stream and records which nodes / events were observed."""

    def __init__(self):
        self.nodes_seen: set[str] = set()
        self.events: list[dict] = []
        self.report_content: str | None = None
        self.usage: dict | None = None
        self.error: str | None = None
        self.start_time: float | None = None
        self.first_status_time: float | None = None
        self.report_time: float | None = None

    def feed_line(self, raw_line: str):
        line = raw_line.strip()
        if not line:
            return
        if line.startswith("data:"):
            payload = line[5:].strip()
        elif line.startswith("data: "):
            payload = line[6:].strip()
        else:
            return

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return

        self.events.append(event)
        etype = event.get("type")

        if etype == "status":
            self.nodes_seen.add("status")
        elif etype == "stream":
            node = event.get("node", "")
            if node:
                self.nodes_seen.add(node)
        elif etype == "report":
            self.report_content = event.get("content", "")
            self.report_time = time.time()
        elif etype == "usage":
            self.usage = event
        elif etype == "error":
            self.error = event.get("message", "unknown")
        elif etype == "done":
            pass

    def is_valid(self, scenario: Scenario) -> tuple[bool, str]:
        """Return (passed, reason)."""
        if self.error:
            return False, f"research error: {self.error}"

        if not self.report_content:
            return False, "no final report received"

        # Check that key nodes ran
        expected = {"write_research_brief", "final_report_generation"}

        missing = expected - self.nodes_seen
        if missing:
            return False, f"missing expected nodes: {missing}"

        if not self.usage:
            return False, "no budget/usage info returned"

        return True, "ok"


# ═══════════════════════════════════════════════════════════════════════════
# Locust task sets
# ═══════════════════════════════════════════════════════════════════════════

class SmokeChecks:
    """Lightweight baseline — verify the server process is alive and routing."""

    @task(3)
    def health(self):
        with self.client.get("/api/health", catch_response=True, name="GET /api/health") as r:
            if r.status_code == 200 and r.json().get("status") == "ok":
                r.success()
            else:
                r.failure(f"status={r.status_code} body={r.text[:100]}")

    @task(2)
    def index(self):
        with self.client.get("/", catch_response=True, name="GET /") as r:
            if r.status_code == 200 and len(r.text) > 0:
                r.success()
            else:
                r.failure(f"status={r.status_code} len={len(r.text)}")


class ResearchPublicOpinion:
    """Run the public_opinion_risk scenario — enterprise brand monitoring."""

    @task(2)
    def public_opinion_fast(self):
        self._run(mode="fast", name="POST /api/research [public_opinion_risk / fast]")

    @task(1)
    def public_opinion_normal(self):
        self._run(mode="normal", name="POST /api/research [public_opinion_risk / normal]")

    def _run(self, mode: str, name: str):
        payload = _build_profile(Scenario.PUBLIC_OPINION, mode)
        validator = SSEValidator()

        with self.client.post(
            "/api/research",
            json=payload,
            catch_response=True,
            stream=True,
            timeout=RESEARCH_TIMEOUT,
            name=name,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}: {resp.text[:200]}")
                return

            try:
                for line in resp.iter_lines(decode_unicode=True):
                    validator.feed_line(line)
            except Exception as exc:
                resp.failure(f"SSE read error: {exc}")
                return

            ok, reason = validator.is_valid(Scenario.PUBLIC_OPINION)
            if ok:
                resp.success()
            else:
                resp.failure(reason)


# ═══════════════════════════════════════════════════════════════════════════
# User classes — assembled from the task-set building blocks above
# ═══════════════════════════════════════════════════════════════════════════

class ResearchUser(HttpUser):
    """Simulates real users: health checks + public-opinion research.

    Task weighting (default):
        ~75%  smoke checks (health + index)
        ~25%  public_opinion_risk (fast > normal)
    """
    wait_time = between(5, 15)

    tasks = {
        SmokeChecks: 6,
        ResearchPublicOpinion: RESEARCH_WEIGHT,
    }


class ReadOnlyUser(HttpUser):
    """Safe baseline — only health / index, zero LLM cost."""
    wait_time = between(2, 5)
    tasks = [SmokeChecks]


# ═══════════════════════════════════════════════════════════════════════════
# Global user-class selection
# ═══════════════════════════════════════════════════════════════════════════

if SKIP_RESEARCH:
    user_classes = [ReadOnlyUser]
else:
    user_classes = [ResearchUser]


# ═══════════════════════════════════════════════════════════════════════════
# Custom event hooks — emit metrics for downstream analysis
# ═══════════════════════════════════════════════════════════════════════════

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print(f"\n[locust] SKIP_RESEARCH_LOAD = {SKIP_RESEARCH}")
    print(f"[locust] RESEARCH_MODE       = {RESEARCH_MODE}")
    print(f"[locust] RESEARCH_WEIGHT     = {RESEARCH_WEIGHT}")
    print(f"[locust] RESEARCH_TIMEOUT    = {RESEARCH_TIMEOUT}s")
    if not SKIP_RESEARCH:
        print("[locust] ⚠  Research endpoints WILL call real LLMs — costs money!\n")


@events.request.add_listener
def log_research_failure(request_type, name, response_time, response_length,
                         exception, **kwargs):
    if exception and "research" in name.lower():
        print(f"[locust] FAIL  [{name}]  {exception}")
