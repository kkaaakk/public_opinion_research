"""Map provider messages and reported usage into the vendored DSH value shapes."""

from __future__ import annotations

import json
from typing import Any


def text(value: Any) -> str:
    """Render bounded values, keeping truncation and unknown/error identities visible."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, dict) and value.get("truncated"):
        return f"{value.get('preview', '')}\n[truncated; original_size={value.get('original_size', 'unknown')}]"
    if isinstance(value, list):
        return "\n".join(text(v) for v in value)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return text(value[key])
    return json.dumps(value, ensure_ascii=False)


def blocks(content: Any, calls: list[dict] | None = None) -> list[dict]:
    """Retain text/reasoning and actual model-declared calls without inventing blocks."""
    result: list[dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"reasoning", "thinking"}:
                result.append({"kind": "reasoning", "text": text(item.get("reasoning", item.get("thinking", item.get("text"))))})
            elif isinstance(item, str) or isinstance(item, dict) and item.get("type") in {"text", "text_delta"}:
                result.append({"kind": "text", "text": text(item)})
            else:
                result.append({"kind": "other", "block": item})
    elif content:
        result.append({"kind": "text", "text": text(content)})
    for call in calls or []:
        if call.get("executable") is False:
            result.append({"kind": "other", "block": {"type": "structured-output", "name": call["name"],
                                                       "arguments": call["arguments"]}})
            continue
        result.append({"kind": "tool-call", "callId": call["id"], "name": call["name"],
                       "argsRaw": call.get("arguments", text(call.get("args", {})))})
    return result


def usage(value: dict | None) -> dict | None:
    """Convert only provider-reported fields; absent cache/reasoning stays absent."""
    if value is None:
        return None
    result = {}
    for source, target in (("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
        if isinstance(value.get(source), int):
            result[target] = value[source]
    if "inputTokens" in result:
        # LangChain reports total input, including cache. DSH's native usage
        # contract instead treats inputTokens as a disjoint non-cache bucket.
        result["inputIncludesCache"] = True
    for group, source, target in (("input_token_details", "cache_read", "cacheReadTokens"),
                                  ("input_token_details", "cache_creation", "cacheWriteTokens"),
                                  ("output_token_details", "reasoning", "reasoningTokens")):
        detail = value.get(group) or {}
        if isinstance(detail.get(source), int):
            result[target] = detail[source]
    return result


def request_config(header: dict) -> dict:
    """Rename available invocation scalars for DSH's request Inspector."""
    aliases = {"max_tokens": "maxTokens", "reasoning_effort": "reasoningEffort", "top_p": "topP"}
    return {aliases.get(k, k): v for k, v in header.get("config", {}).items()}
