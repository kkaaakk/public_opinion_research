"""Submit and control research through the same durable Run API as the Web UI."""

from __future__ import annotations

import argparse
import json
import os

import httpx


def main(argv=None):
    """Send one authenticated command; execution remains owned by DBOS."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url", default=os.environ.get("RESEARCH_API_URL", "http://127.0.0.1:8000")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("topic")
    create.add_argument(
        "--key", required=True, help="Reuse this key only for the same request"
    )
    create.add_argument("--model", default="deepseek:deepseek-flash")
    create.add_argument("--mode", choices=["fast", "normal", "deep"], default="normal")
    create.add_argument("--rag", action="store_true")
    create.add_argument("--approval", action="store_true")
    create.add_argument("--no-clarification", action="store_true")
    for name in ("get", "events", "cancel", "approve", "retry", "clarify"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id")
        if name == "events":
            sub.add_argument("--after", type=int, default=0)
        elif name == "approve":
            sub.add_argument("--plan-version", required=True)
            sub.add_argument("--decision", choices=["approve", "reject"], required=True)
        elif name == "clarify":
            sub.add_argument("--version", required=True)
            sub.add_argument(
                "--answers", default="{}", help="JSON object keyed by question ID"
            )
            sub.add_argument("--skip", action="store_true")
        elif name == "retry":
            sub.add_argument("--state-version", type=int, required=True)
            sub.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    token = (
        os.environ.get("PUBLIC_OPINION_API_TOKEN")
        or os.environ.get("WEB_API_TOKEN")
        or ""
    ).strip()
    if not token:
        parser.error("PUBLIC_OPINION_API_TOKEN is required")
    headers = {"Authorization": "Bearer " + token}
    method, path, body = "GET", "/api/runs", None
    if args.command == "create":
        method = "POST"
        headers["Idempotency-Key"] = args.key
        body = dict(
            topic=args.topic,
            model=args.model,
            mode=args.mode,
            rag_enabled=args.rag,
            require_approval=args.approval,
            allow_clarification=not args.no_clarification,
        )
    else:
        path += "/" + args.run_id
        if args.command == "events":
            path += "/events?after=" + str(args.after)
        elif args.command != "get":
            method = "POST"
            path += (
                "/approval"
                if args.command == "approve"
                else "/clarification"
                if args.command == "clarify"
                else "/" + args.command
            )
            if args.command == "approve":
                body = dict(plan_version=args.plan_version, decision=args.decision)
            elif args.command == "clarify":
                body = dict(
                    version=args.version,
                    answers=json.loads(args.answers),
                    skip=args.skip,
                )
            elif args.command == "retry":
                headers["Idempotency-Key"] = args.key
                body = dict(state_version=args.state_version)
    try:
        with httpx.Client(base_url=args.url, headers=headers, timeout=30) as client:
            if args.command == "events":
                with client.stream(method, path, timeout=None) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        print(line, flush=True)  # noqa: T201
            else:
                response = client.request(method, path, json=body)
                response.raise_for_status()
                print(json.dumps(response.json(), ensure_ascii=False, indent=2))  # noqa: T201
    except httpx.HTTPError:
        # Do not print URLs, response bodies or credentials. A lost create ACK
        # is resolved by repeating the same key and body, never a new delivery.
        print(  # noqa: T201
            "Run API request failed. Reconcile the Run or repeat the same request key."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
