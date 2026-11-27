"""Terminal-first research entry point.

``por run`` and ``por smoke`` call the same durable ``RunClient`` as the Web API
and then let DBOS own execution. The CLI never proxies through FastAPI and never
accepts model, RAG, mode or feature-switch overrides.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.config_file import (
    DEFAULT_CONFIG_FILE,
    SMOKE_CONFIG_FILE,
    ConfigFileError,
    config_summary,
    frozen_config,
    request_payload,
)
from open_deep_research.configuration import Configuration
from open_deep_research.env import load_environment
from open_deep_research.preflight import ERROR, PreflightReport, run_preflight
from open_deep_research.web.run_data import RunConflict, RunNotFound

OWNER = "operator"


def main(argv=None) -> int:
    """Run one CLI command and return a process exit code."""
    load_environment()
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except RunNotFound:
        _error("Run not found.")
        return 1
    except RunConflict as exc:
        _error(str(exc))
        return 1
    except (ConfigFileError, ValueError) as exc:
        _error(str(exc))
        return 2
    except SQLAlchemyError:
        _error("Durable Run storage is unavailable.")
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="por", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="Check the formal run environment.")
    doctor.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_FILE),
        help="Configuration file to inspect (default: config.yaml).",
    )

    for name, description in (
        ("run", "Submit a research Run using config.yaml."),
        ("smoke", "Submit a low-cost Run using config.smoke.yaml."),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("topic")
        command.add_argument(
            "--key",
            help="Idempotency-Key; reuse it only for the same request.",
        )
        command.add_argument("--session", help="Existing 32-character research session id.")
        command.add_argument("--org-context", default=None)

    for name, help_text in (
        ("status", "Show Run status and pending interaction."),
        ("inspect", "Show failure, model and tool execution details."),
        ("approve", "Approve, reject or edit the Research Brief."),
        ("clarify", "Answer or skip the pending clarification."),
        ("report", "Print or save the final Markdown report."),
        ("retry", "Retry a verified safe failure."),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("run_id")
        command.add_argument("--json", action="store_true", help="Print raw JSON.")

    approve = commands.choices["approve"]
    approve.add_argument(
        "--decision",
        choices=["approve", "reject", "edit", "revise"],
        default="approve",
    )
    approve.add_argument("--version", help="Expected Brief version (default: current).")
    approve.add_argument("--brief-file", help="Markdown file for --decision edit.")
    approve.add_argument("--feedback", help="Revision feedback for --decision revise.")
    approve.add_argument("--key", help="Idempotency-Key for --decision revise.")

    clarify = commands.choices["clarify"]
    clarify.add_argument("--version", help="Expected clarification version (default: current).")
    clarify.add_argument("--answers", help="JSON object keyed by question id.")
    clarify.add_argument(
        "--answer",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="Repeatable single answer; use --answers for multiple-choice lists.",
    )
    clarify.add_argument("--skip", action="store_true")

    report = commands.choices["report"]
    report.add_argument("--output", help="Save the Markdown report to this path.")

    retry = commands.choices["retry"]
    retry.add_argument("--key", help="Idempotency-Key; default is a fresh uuid.")
    return parser


def _dispatch(args) -> int:
    if args.command == "doctor":
        return _doctor(Path(args.config))
    if args.command in {"run", "smoke"}:
        return _submit(args)
    if args.command == "status":
        return _status(args)
    if args.command == "inspect":
        return _inspect(args)
    if args.command == "approve":
        return _approve(args)
    if args.command == "clarify":
        return _clarify(args)
    if args.command == "report":
        return _report(args)
    if args.command == "retry":
        return _retry(args)
    raise ValueError(f"Unsupported command {args.command!r}.")


def _client():
    from open_deep_research.web.durable import get_run_client

    return get_run_client()


def _doctor(config_path: Path) -> int:
    report = run_preflight(config_path)
    _print_report(report)
    return 0 if report.level != ERROR else 2


def _submit(args) -> int:
    config_path = SMOKE_CONFIG_FILE if args.command == "smoke" else DEFAULT_CONFIG_FILE
    if args.session and not re.fullmatch(r"[a-f0-9]{32}", str(args.session)):
        raise ValueError("--session must be a 32-character lowercase hex id.")
    report = run_preflight(config_path)
    _print_report(report)
    if report.level == ERROR or report.configuration is None:
        _error("Preflight reported ERROR; the Run was not submitted.")
        return 2
    configuration = report.configuration
    key = args.key or uuid4().hex
    client = _client()
    run, created = client.create(
        OWNER,
        key,
        request_payload(
            args.topic,
            configuration,
            org_context=args.org_context,
        ),
        frozen_config(configuration),
        {"messages": [_human_message(args.topic)]},
        session_id=args.session,
        config_source=str(config_path),
    )
    print(f"Run: {run['run_id']}")
    print(f"Status: {run['status']}")
    print(f"Idempotency-Key: {key}")
    print(f"Configuration: {config_path.name} ({'created' if created else 'existing'})")
    print(f"Next: por status {run['run_id']}")
    if configuration.allow_plan_feedback:
        print(f"Approval required: por approve {run['run_id']}")
    if configuration.allow_clarification:
        print(f"Clarification possible: por clarify {run['run_id']}")
    return 0


def _status(args) -> int:
    run = _client().get(args.run_id, OWNER)
    if args.json:
        print(json.dumps(run, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"Run: {run['run_id']}")
    print(f"Status: {run['status']} ({run['native_status']}, settlement={run['settlement']})")
    print(f"Phase: {run.get('phase') or '-'}")
    print(f"Current step: {run.get('current_step') or '-'}")
    print(f"Completed steps: {', '.join(run.get('completed_steps') or []) or '-'}")
    print(f"Report: {'ready' if run.get('report') else 'pending'}")
    plan = run.get("plan")
    if plan:
        print(
            f"Brief: version={plan['version']} revision={plan.get('revision')} "
            f"locked={plan.get('locked')}"
        )
    clarification = run.get("clarification")
    if clarification:
        print(f"Clarification version: {clarification['version']}")
        for question in clarification.get("questions", []):
            print(f"  [{question['id']}] {question['text']}")
    if run.get("error"):
        error = run["error"]
        print(
            f"Error: {error.get('kind')} {error.get('error_type', '')} "
            f"step={error.get('step', '')}"
        )
    print(f"State version: {run['state_version']} retry_safe={run['retry_safe']}")
    return 0


def _observation_counts(events):
    """Count model and tool observations using the Harness tool-status contract."""
    tool_calls: dict[str, int] = {}
    tool_failures: dict[str, int] = {}
    model_calls = 0
    failed_events = []
    for event in events:
        if event.get("type") != "sdk_event":
            continue
        kind = event.get("event")
        if kind == "model_completed":
            model_calls += 1
        elif kind == "tool_result":
            name = str(event.get("name") or "tool")
            tool_calls[name] = tool_calls.get(name, 0) + 1
            # The Harness records ``succeeded`` for a completed tool. ``returned``
            # is the separate external-effect receipt state, not the tool status.
            if event.get("status") != "succeeded":
                tool_failures[name] = tool_failures.get(name, 0) + 1
        if event.get("status") == "failed":
            failed_events.append(
                {
                    "event": kind,
                    "stage": event.get("stage"),
                    "error_type": event.get("error_type"),
                }
            )
    return model_calls, tool_calls, tool_failures, failed_events


def _inspect(args) -> int:
    from open_deep_research.web.observations import execution_view
    from open_deep_research.web.run_data import read_request

    client = _client()
    run = client.get(args.run_id, OWNER)
    request = read_request(args.run_id, OWNER)
    configuration = Configuration.from_runnable_config(request["data"]["config"])
    execution = execution_view(client, args.run_id, OWNER, "", 0, 100)
    events = client.read_events(args.run_id, OWNER, 0, 500)
    model_calls, tool_calls, tool_failures, failed_events = _observation_counts(events)
    failed_workflows = [
        {
            "workflow_id": item["workflow_id"],
            "name": item["name"],
            "status": item["status"],
        }
        for item in execution["workflows"]
        if item.get("status") == "ERROR"
    ]
    view = {
        "run_id": run["run_id"],
        "status": run["status"],
        "phase": run.get("phase"),
        "current_step": run.get("current_step"),
        "error": run.get("error"),
        "retry_safe": run["retry_safe"],
        "state_version": run["state_version"],
        "models": config_summary(configuration),
        "model_calls_observed": model_calls,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "failed_events": failed_events,
        "failed_workflows": failed_workflows,
        "workflows": execution["workflows"],
        "configuration_source": run.get("configuration_source"),
    }
    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2, default=str))
        return 0
    print(f"Run: {view['run_id']} status={view['status']} phase={view['phase']}")
    print(f"Current step: {view['current_step'] or '-'}")
    error = view["error"] or {}
    if error:
        print(
            f"Failure: kind={error.get('kind')} type={error.get('error_type')} "
            f"step={error.get('step')} retry_safe={error.get('retry_safe')}"
        )
    print(f"Configuration: {view['configuration_source'] or '-'}")
    print("Models:")
    for name, value in view["models"].items():
        if name.endswith("_model"):
            print(f"  {name}: {value}")
    # Two different authorities: the ledger owns what the provider was asked,
    # the Stream observations only carry SDK Agent turns and tool results.
    usage = run.get("usage") or {}
    print(
        "Model attempts in ledger: "
        f"{usage.get('model_calls', 0)} "
        f"(tokens={usage.get('total_tokens', 0)}, "
        f"complete={usage.get('usage_complete')})"
    )
    print(
        "Ledger tool calls: "
        f"{usage.get('tool_calls', 0)} (search={usage.get('search_calls', 0)})"
    )
    print(f"Model calls observed: {model_calls}")
    if tool_calls:
        print("Tool calls:")
        for name, count in sorted(tool_calls.items()):
            failures = tool_failures.get(name, 0)
            print(f"  {name}: {count} (failed={failures})")
    else:
        print("Tool calls: none")
    if failed_events:
        print("Failed observations:")
        for item in failed_events:
            print(f"  {item}")
    if failed_workflows:
        print("Failed workflows:")
        for item in failed_workflows:
            print(f"  {item}")
    print(f"Retry: state_version={view['state_version']} safe={view['retry_safe']}")
    return 0


def _approve(args) -> int:
    client = _client()
    run = client.get(args.run_id, OWNER)
    if run["status"] != "awaiting_approval":
        raise RunConflict("Run is not awaiting Brief approval.")
    plan = run.get("plan") or {}
    version = args.version or plan.get("version")
    if not version:
        raise RunConflict("Approval version is unavailable.")
    decision = args.decision
    if decision == "edit":
        if not args.brief_file:
            raise ValueError("--brief-file is required for --decision edit.")
        brief_md = Path(args.brief_file).read_text(encoding="utf-8")
        result = client.approve(args.run_id, OWNER, version, "edit", brief_md)
        print(f"Brief saved as revision {result['plan']['revision']}.")
        version = result["plan"]["version"]
        print(f"Approve it with: por approve {args.run_id} --version {version}")
        return 0
    if decision == "revise":
        if not args.feedback:
            raise ValueError("--feedback is required for --decision revise.")
        key = args.key or uuid4().hex
        result = client.approve(
            args.run_id,
            OWNER,
            version,
            "revise",
            feedback=args.feedback,
            key=key,
        )
    else:
        result = client.approve(args.run_id, OWNER, version, decision)
    print(f"Decision {decision} accepted; status={result['status']}.")
    return 0


def _clarify(args) -> int:
    client = _client()
    run = client.get(args.run_id, OWNER)
    if run["status"] != "awaiting_clarification":
        raise RunConflict("Run is not awaiting clarification.")
    pending = run.get("clarification") or {}
    version = args.version or pending.get("version")
    if not version:
        raise RunConflict("Clarification version is unavailable.")
    answers = _clarification_answers(args, pending)
    result = client.clarify(
        args.run_id,
        OWNER,
        version,
        answers,
        args.skip,
    )
    print(f"Clarification submitted; status={result['status']}.")
    return 0


def _clarification_answers(args, pending: dict[str, Any]) -> dict[str, Any]:
    if args.skip:
        if args.answers or args.answer:
            raise ValueError("--skip cannot be combined with answers.")
        return {}
    if args.answers:
        parsed = json.loads(args.answers)
        if not isinstance(parsed, dict):
            raise ValueError("--answers must be a JSON object keyed by question id.")
        return parsed
    answers: dict[str, Any] = {}
    for item in args.answer:
        key, separator, value = item.partition("=")
        if not separator or not key.strip() or not value.strip():
            raise ValueError("--answer values must use ID=VALUE.")
        answers[key.strip()] = value.strip()
    if answers:
        return answers
    if sys.stdin.isatty():
        return _prompt_answers(pending)
    raise ValueError(
        "Provide --answers JSON, repeatable --answer ID=VALUE, or --skip."
    )


def _prompt_answers(pending: dict[str, Any]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for question in pending.get("questions", []):
        print(question["text"])
        options = question.get("options") or []
        for index, option in enumerate(options, start=1):
            print(f"  {index}. {option}")
        raw = input("Answer (blank to leave unanswered): ").strip()
        if not raw:
            continue
        if question.get("kind") == "multiple":
            answers[question["id"]] = [
                options[int(part) - 1] if part.isdigit() and 0 < int(part) <= len(options) else part
                for part in raw.split(",")
                if part.strip()
            ]
        elif question.get("kind") == "single" and raw.isdigit() and 0 < int(raw) <= len(options):
            answers[question["id"]] = options[int(raw) - 1]
        else:
            answers[question["id"]] = raw
    if not answers:
        raise ValueError("No answers were provided; pass --skip to continue without them.")
    return answers


def _report(args) -> int:
    run = _client().get(args.run_id, OWNER)
    text = run.get("report") or ""
    if not text:
        _error(
            f"Report is not ready (status={run['status']}, "
            f"step={run.get('current_step') or '-'})."
        )
        return 1
    if args.output:
        output = Path(args.output)
        output.write_text(text, encoding="utf-8")
        print(f"Report saved: {output.resolve()}")
    else:
        print(text)
    return 0


def _retry(args) -> int:
    client = _client()
    run = client.get(args.run_id, OWNER)
    if not run["retry_safe"]:
        raise RunConflict(run.get("retry_reason") or "Run is not ready to resume.")
    key = args.key or uuid4().hex
    result = client.retry(args.run_id, OWNER, key, run["state_version"])
    print(f"Retry accepted: status={result['status']} key={key}")
    return 0


def _print_report(report: PreflightReport) -> None:
    for check in report.checks:
        print(f"[{check.level}] {check.name}: {check.detail}")
    settings = config_summary(report.configuration) if report.configuration else {}
    if settings:
        print("Effective configuration:")
        for name in (
            "research_model",
            "compression_model",
            "final_report_model",
            "summarization_model",
            "max_research_rounds",
            "max_react_tool_calls",
            "search_api",
            "retrieval_mode",
            "rag_enabled",
            "enabled_business_agents",
            "enabled_plugins",
            "allow_plan_feedback",
            "allow_clarification",
            "budget_enabled",
            "max_model_calls",
            "tracing_enabled",
            "tracing_scope",
            "workflow_step_timeout_seconds",
        ):
            print(f"  {name}: {settings[name]}")
    print(
        f"Result: {report.level} "
        f"({len(report.errors)} error(s), {len(report.warnings)} warning(s))"
    )


def _human_message(topic: str):
    from langchain_core.messages import HumanMessage

    return HumanMessage(content=topic)


def _error(message: str) -> None:
    print(f"Error: {message}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
