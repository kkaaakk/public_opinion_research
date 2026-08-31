"""Multi-server MCP tool loader with fault isolation.

Extracted from ``open_deep_research.utils`` so that MCP concerns live in a
dedicated module.  Supports:

* The **legacy** single-server ``mcp_config`` path (HTTP / streamable-http),
  converted into the new multi-server registry for backward compatibility.
* **Bytebase DBHub** — database query tools via stdio (or HTTP).
* **Microsoft MarkItDown** — file-to-markdown conversion via stdio.
* **Feishu / Lark Official MCP** — Feishu/Lark API tools via stdio.

Every server is independently loaded: a failure in one server (bad DSN,
missing runtime, auth error, …) is logged as a warning and the remaining
servers continue to load.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool, ToolException
from langgraph.config import get_store

from open_deep_research.configuration import Configuration
from open_deep_research.mcp.domain_filter import tag_tools_with_domain
from open_deep_research.mcp.tool_wrapper import wrap_mcp_tools

# MCP adapter imports — gracefully degrade when incompatible versions are installed.
# langchain_mcp_adapters may require a newer mcp SDK than currently installed.
MCP_AVAILABLE = False
MultiServerMCPClient = None  # type: ignore[assignment,misc]
McpError = None  # type: ignore[assignment,misc]
try:
    from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: F811

    from mcp import McpError  # noqa: F811
    MCP_AVAILABLE = True
except (ImportError, AttributeError) as _mcp_import_err:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "MCP adapters unavailable (%s). MCP tools will be skipped.", _mcp_import_err
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DSN security
# ---------------------------------------------------------------------------


def _mask_dsn(dsn: str) -> str:
    """Replace password in a database DSN with ``***`` for logging."""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)


# ---------------------------------------------------------------------------
# Node.js version guard (DBHub requires ≥ 22.5.0)
# ---------------------------------------------------------------------------


def _check_node_version() -> str | None:
    """Return the installed Node.js version string, or ``None`` if unavailable.

    Also emits a warning when the version is too old for DBHub (< 22.5.0).
    The caller should treat a ``None`` return (or a warning) as
    "DBHub cannot start".
    """
    try:
        result = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        logger.warning("Node.js not found — DBHub MCP server requires Node.js >= 22.5.0")
        return None

    version_str = result.stdout.strip().lstrip("v")
    if not version_str:
        logger.warning("Could not detect Node.js version — DBHub requires >= 22.5.0")
        return None

    try:
        parts = version_str.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        logger.warning(
            "Could not parse Node.js version '%s' — DBHub requires >= 22.5.0",
            version_str,
        )
        return None

    if major < 22 or (major == 22 and minor < 5):
        logger.warning(
            "DBHub requires Node.js >= 22.5.0 (found %s). DBHub will be disabled.",
            version_str,
        )
    return version_str


# ---------------------------------------------------------------------------
# Auth helpers (preserved from the legacy single-server MCP flow)
# ---------------------------------------------------------------------------


async def get_mcp_access_token(
    supabase_token: str,
    base_mcp_url: str,
) -> dict[str, Any] | None:
    """Exchange a Supabase token for an MCP access token (OAuth token exchange)."""
    try:
        form_data = {
            "client_id": "mcp_default",
            "subject_token": supabase_token,
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "resource": base_mcp_url.rstrip("/") + "/mcp",
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        }
        async with aiohttp.ClientSession() as session:
            token_url = base_mcp_url.rstrip("/") + "/oauth/token"
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            async with session.post(
                token_url, headers=headers, data=form_data
            ) as response:
                if response.status == 200:
                    return await response.json()
                response_text = await response.text()
                logger.error("Token exchange failed: %s", response_text)
    except Exception as exc:
        logger.error("Error during token exchange: %s", exc)
    return None


async def get_tokens(config: RunnableConfig) -> dict[str, Any] | None:
    """Retrieve stored MCP tokens, validating expiration."""
    store = get_store()
    thread_id = config.get("configurable", {}).get("thread_id")
    if not thread_id:
        return None
    user_id = config.get("metadata", {}).get("owner")
    if not user_id:
        return None

    tokens = await store.aget((user_id, "tokens"), "data")
    if not tokens:
        return None

    expires_in = tokens.value.get("expires_in")
    created_at = tokens.created_at
    current_time = datetime.now(timezone.utc)
    expiration_time = created_at + timedelta(seconds=expires_in)
    if current_time > expiration_time:
        await store.adelete((user_id, "tokens"), "data")
        return None
    return tokens.value


async def set_tokens(config: RunnableConfig, tokens: dict[str, Any]) -> None:
    """Persist MCP tokens into the configuration store."""
    store = get_store()
    thread_id = config.get("configurable", {}).get("thread_id")
    if not thread_id:
        return
    user_id = config.get("metadata", {}).get("owner")
    if not user_id:
        return
    await store.aput((user_id, "tokens"), "data", tokens)


async def fetch_tokens(config: RunnableConfig) -> dict[str, Any] | None:
    """Fetch / refresh MCP tokens, exchanging a Supabase token if needed."""
    current_tokens = await get_tokens(config)
    if current_tokens:
        return current_tokens

    supabase_token = config.get("configurable", {}).get("x-supabase-access-token")
    if not supabase_token:
        return None

    mcp_config = config.get("configurable", {}).get("mcp_config")
    if not mcp_config or not mcp_config.get("url"):
        return None

    mcp_tokens = await get_mcp_access_token(supabase_token, mcp_config.get("url"))
    if not mcp_tokens:
        return None

    await set_tokens(config, mcp_tokens)
    return mcp_tokens


def wrap_mcp_authenticate_tool(tool: StructuredTool) -> StructuredTool:
    """Wrap an MCP tool so that ``-32003`` errors surface as user-friendly messages.

    Preserved from the legacy single-server code path.
    """
    original_coroutine = tool.coroutine

    async def _authentication_wrapper(**kwargs: Any) -> Any:
        def _find_mcp_error(exc: BaseException) -> McpError | None:
            if isinstance(exc, McpError):
                return exc
            if hasattr(exc, "exceptions"):
                for sub in exc.exceptions:
                    if found := _find_mcp_error(sub):
                        return found
            return None

        try:
            return await original_coroutine(**kwargs)
        except BaseException as original_error:
            mcp_error = _find_mcp_error(original_error)
            if mcp_error is None:
                raise
            error_code = getattr(mcp_error.error, "code", None)
            error_data = getattr(mcp_error.error, "data", None) or {}
            if error_code == -32003:
                message_payload = error_data.get("message", {})
                msg = "Required interaction"
                if isinstance(message_payload, dict):
                    msg = message_payload.get("text") or msg
                if url := error_data.get("url"):
                    msg = f"{msg} {url}"
                raise ToolException(msg) from original_error
            raise

    tool.coroutine = _authentication_wrapper
    return tool


# ---------------------------------------------------------------------------
# Domain tagging — maps each MCP server to a tool domain category
# ---------------------------------------------------------------------------

_SERVER_DOMAIN_MAP: dict[str, str] = {
    "dbhub": "database",
    "markitdown": "document",
    "feishu": "feishu",
    "legacy_mcp": "external_mcp",
    "social_media": "social_media",
}


def _tag_tools_with_domain(tools: list[BaseTool], server_name: str) -> list[BaseTool]:
    """Tag every MCP tool with its server's registered domain."""
    domain = _SERVER_DOMAIN_MAP.get(server_name, "external_mcp")
    return tag_tools_with_domain(tools, domain)


# ---------------------------------------------------------------------------
# Per-server config builders
# ---------------------------------------------------------------------------


async def _build_legacy_config(
    configurable: Configuration, config: RunnableConfig
) -> dict[str, Any] | None:
    """Convert the old single-server ``mcp_config`` into the new multi-server format.

    Returns ``None`` when the legacy config is absent or incomplete.
    """
    if not (configurable.mcp_config and configurable.mcp_config.url):
        return None
    if not configurable.mcp_config.tools:
        logger.debug("Legacy mcp_config present but tools list is empty — skipping.")
        return None

    headers = None
    if configurable.mcp_config.auth_required:
        tokens = await fetch_tokens(config)
        if tokens:
            headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        else:
            logger.warning(
                "Legacy MCP server requires authentication but no token "
                "is available — skipping."
            )
            return None

    server_url = configurable.mcp_config.url.rstrip("/") + "/mcp"
    logger.info("Legacy MCP server configured at %s", server_url)
    return {
        "url": server_url,
        "headers": headers,
        "transport": "streamable_http",
    }


def _build_dbhub_config(configurable: Configuration) -> dict[str, Any] | None:
    """Build the Bytebase DBHub connection config.

    All checks (Node.js version, DSN validity, …) live inside this function
    so that a failure here blocks **only** DBHub, not other MCP servers.
    """
    # --- Node.js version guard (inside DBHub scope!) ---
    node_version = _check_node_version()
    if node_version is None:
        return None  # warning already logged

    dsn = (configurable.dbhub_dsn or "").strip()
    if not dsn:
        logger.warning("DBHub enabled but DSN is empty — DBHub will be disabled.")
        return None

    logger.info("DBHub: configuring with masked DSN (%s)", _mask_dsn(dsn))

    transport = (configurable.dbhub_transport or "stdio").strip().lower()

    if transport == "http":
        port = configurable.dbhub_http_port or 8080
        return {
            "url": f"http://localhost:{port}/mcp",
            "transport": "streamable_http",
        }

    # stdio mode (default)
    args: list[str] = ["-y", "@bytebase/dbhub", "--transport", "stdio"]
    args.extend(["--dsn", dsn])
    # Defence-in-depth safety flags (primary access control is at the DB level)
    args.extend(["--read-only", "--max-rows", "100", "--query-timeout", "30"])

    return {
        "command": "npx",
        "args": args,
        "transport": "stdio",
    }


def _build_markitdown_config(configurable: Configuration) -> dict[str, Any]:
    """Microsoft official ``markitdown-mcp`` server (stdio)."""
    return {
        "command": "uv",
        "args": ["run", "markitdown-mcp"],
        "transport": "stdio",
    }


def _build_feishu_config(configurable: Configuration) -> dict[str, Any] | None:
    """Feishu / Lark official MCP with tool-preset whitelist."""
    app_id = (configurable.feishu_app_id or "").strip()
    app_secret = (configurable.feishu_app_secret or "").strip()
    if not app_id or not app_secret:
        logger.warning("Feishu MCP enabled but app_id / app_secret missing.")
        return None

    preset = (configurable.feishu_mcp_preset or "preset.light").strip()
    args: list[str] = [
        "-y",
        "@larksuiteoapi/lark-mcp",
        "mcp",
        "-a",
        app_id,
        "-s",
        app_secret,
        "-t",
        preset,
    ]

    # Lark international domain
    if configurable.feishu_domain and configurable.feishu_domain != "https://open.feishu.cn":
        args.extend(["--domain", configurable.feishu_domain])

    # OAuth / user-access-token mode
    if configurable.feishu_oauth_enabled:
        args.extend(["--oauth", "--token-mode", "user_access_token"])

    return {
        "command": "npx",
        "args": args,
        "transport": "stdio",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def load_mcp_tools(
    config: RunnableConfig,
    existing_tool_names: set[str],
) -> list[BaseTool]:
    """Load tools from all configured MCP servers with fault isolation.

    Each server is built, connected, and queried inside its own try/except
    block.  A failure in one server (bad DSN, missing runtime, auth error,
    network timeout, …) is logged as a warning — the remaining servers
    continue to load normally.
    """
    if not MCP_AVAILABLE:
        logger.debug("MCP adapters not installed; skipping MCP tool loading.")
        return []

    configurable = Configuration.from_runnable_config(config)
    servers: dict[str, dict[str, Any]] = {}

    # 1) Legacy single-server mcp_config → "legacy_mcp"
    legacy = await _build_legacy_config(configurable, config)
    if legacy:
        servers["legacy_mcp"] = legacy

    # 2) Bytebase DBHub
    if configurable.dbhub_enabled and configurable.dbhub_dsn:
        try:
            dbhub_cfg = _build_dbhub_config(configurable)
            if dbhub_cfg:
                servers["dbhub"] = dbhub_cfg
        except Exception:
            logger.warning("Failed to build DBHub config — DBHub disabled.", exc_info=True)

    # 3) Microsoft MarkItDown
    if configurable.markitdown_enabled:
        try:
            servers["markitdown"] = _build_markitdown_config(configurable)
        except Exception:
            logger.warning(
                "Failed to build MarkItDown config — MarkItDown disabled.",
                exc_info=True,
            )

    # 4) Feishu / Lark
    if configurable.feishu_enabled and configurable.feishu_app_id:
        try:
            feishu_cfg = _build_feishu_config(configurable)
            if feishu_cfg:
                servers["feishu"] = feishu_cfg
        except Exception:
            logger.warning(
                "Failed to build Feishu config — Feishu disabled.", exc_info=True
            )

    if not servers:
        return []

    # --- Per-server loading with full fault isolation ---
    all_tools: list[BaseTool] = []

    for server_name, server_conn in servers.items():
        try:
            client = MultiServerMCPClient({server_name: server_conn})
            tools = await client.get_tools()

            # Legacy tool-name filtering (only for the old mcp_config path)
            if (
                server_name == "legacy_mcp"
                and configurable.mcp_config
                and configurable.mcp_config.tools
            ):
                allowed = set(configurable.mcp_config.tools)
                tools = [t for t in tools if t.name in allowed]

            # Apply auth-error wrapper to legacy tools
            if server_name == "legacy_mcp" and configurable.mcp_config:
                tools = [wrap_mcp_authenticate_tool(t) for t in tools]

            # Wrap with server namespace — resolves conflicts explicitly
            wrapped = wrap_mcp_tools(server_name, tools, existing_tool_names)
            # Tag each tool with its domain category for scene-based filtering
            wrapped = _tag_tools_with_domain(wrapped, server_name)
            all_tools.extend(wrapped)
            existing_tool_names.update(t.name for t in wrapped)

            logger.info(
                "MCP server '%s': loaded %d tools%s",
                server_name,
                len(wrapped),
                f" (tools: {[t.name for t in wrapped]})" if wrapped else "",
            )
        except Exception:
            logger.warning(
                "MCP server '%s' failed to load — other servers will continue.",
                server_name,
                exc_info=True,
            )
            continue

    return all_tools
