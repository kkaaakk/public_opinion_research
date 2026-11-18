# 用 Agents SDK 原生 MCP 客户端装配多服务工具，并隔离单个服务的初始化失败。
"""Multi-server MCP tool loader using the official Agents SDK clients.

Every server is built, connected and queried inside its own try/except block.
A failure in one server (bad DSN, missing runtime, auth error, timeout, …) is
logged as a warning and the remaining servers continue to load.

Discovery uses a short-lived SDK connection.  Each actual tool call opens its
own connection through :mod:`open_deep_research.mcp.tool_wrapper`, so a stale
session cannot be reused after disconnect and the unified ``ToolExecutor``
still owns the durable call Step.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
from agents.mcp import MCPServer, MCPServerStdio, MCPServerStreamableHttp
from langchain_core.runnables import RunnableConfig

from open_deep_research.configuration import Configuration
from open_deep_research.mcp.tool_wrapper import (
    allocate_tool_name,
    build_mcp_tool,
)
from open_deep_research.tools import BusinessTool, ToolEffect

logger = logging.getLogger(__name__)

# Bound a hung MCP request without changing the DBHub query timeout (30s) or
# ordinary document conversions.  Zero would disable the SDK read timeout.
_MCP_SESSION_TIMEOUT_SECONDS = 300.0


# ---------------------------------------------------------------------------
# DSN security
# ---------------------------------------------------------------------------


# 隐藏数据库连接串中的密码，避免日志泄露凭据。
def _mask_dsn(dsn: str) -> str:
    """Replace password in a database DSN with ``***`` for logging."""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", dsn)


# ---------------------------------------------------------------------------
# Node.js version guard (DBHub requires ≥ 22.5.0)
# ---------------------------------------------------------------------------


# 读取 Node.js 版本并提示 DBHub 运行要求；不可用时返回 None。
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
        logger.warning(
            "Node.js not found — DBHub MCP server requires Node.js >= 22.5.0"
        )
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


# 以 Supabase 令牌执行 OAuth 交换，取得 MCP 访问令牌。
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


# 复用有效令牌，必要时通过 Supabase 凭据获取或刷新令牌。
async def fetch_tokens(config: RunnableConfig) -> dict[str, Any] | None:
    """Exchange a trusted Supabase token without an engine-owned token store."""
    supabase_token = config.get("configurable", {}).get("x-supabase-access-token")
    if not supabase_token:
        return None

    mcp_config = config.get("configurable", {}).get("mcp_config")
    if not mcp_config or not mcp_config.get("url"):
        return None

    mcp_tokens = await get_mcp_access_token(supabase_token, mcp_config.get("url"))
    if not mcp_tokens:
        return None

    return mcp_tokens


# ---------------------------------------------------------------------------
# Server specifications
# ---------------------------------------------------------------------------


# 保存一个 MCP 服务的可信本地策略和 SDK 连接参数。
@dataclass(frozen=True)
class MCPServerSpec:
    """Trusted local policy plus the SDK-native connection parameters.

    ``domain`` and ``effect`` are deliberately supplied here, never from MCP
    server annotations.  An external server cannot lower its own protection
    level.
    """

    name: str
    domain: str
    effect: ToolEffect
    transport: Literal["stdio", "streamable_http"]
    params: dict[str, Any]

    # 创建未连接的 SDK MCP 客户端；发现和每次调用各自使用独立实例。
    def create(self) -> MCPServer:
        """Return a fresh SDK server object for one connection."""
        if self.transport == "stdio":
            return MCPServerStdio(
                params=self.params,
                name=self.name,
                cache_tools_list=False,
                client_session_timeout_seconds=_MCP_SESSION_TIMEOUT_SECONDS,
            )
        return MCPServerStreamableHttp(
            params=self.params,
            name=self.name,
            cache_tools_list=False,
            client_session_timeout_seconds=_MCP_SESSION_TIMEOUT_SECONDS,
        )


# ---------------------------------------------------------------------------
# Per-server config builders
# ---------------------------------------------------------------------------


# 把旧版单服务配置转换为 SDK Streamable HTTP 服务；配置不完整时不启用。
async def _build_legacy_server(
    configurable: Configuration, config: RunnableConfig
) -> MCPServerSpec | None:
    """Convert the old single-server ``mcp_config`` into a server spec."""
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
    params: dict[str, Any] = {"url": server_url}
    if headers:
        params["headers"] = headers
    return MCPServerSpec(
        name="legacy_mcp",
        domain="external_mcp",
        effect="protected",
        transport="streamable_http",
        params=params,
    )


# 校验 Node.js 和数据库连接参数后构造 DBHub 服务，失败仅影响该服务。
def _build_dbhub_server(configurable: Configuration) -> MCPServerSpec | None:
    """Build the Bytebase DBHub connection spec.

    All checks (Node.js version, DSN validity, …) live inside this function
    so that a failure here blocks **only** DBHub, not other MCP servers.
    """
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
        return MCPServerSpec(
            name="dbhub",
            domain="database",
            # HTTP mode cannot prove the remote stays read-only.
            effect="protected",
            transport="streamable_http",
            params={"url": f"http://localhost:{port}/mcp"},
        )

    # stdio mode (default)
    args: list[str] = ["-y", "@bytebase/dbhub", "--transport", "stdio"]
    args.extend(["--dsn", dsn])
    # Defence-in-depth safety flags (primary access control is at the DB level)
    args.extend(["--read-only", "--max-rows", "100", "--query-timeout", "30"])

    return MCPServerSpec(
        name="dbhub",
        domain="database",
        # The adapter-owned stdio transport enforces read-only behavior.
        effect="read",
        transport="stdio",
        params={"command": "npx", "args": args},
    )


# 构造通过标准输入输出启动 MarkItDown 文档转换服务的配置。
def _build_markitdown_server(configurable: Configuration) -> MCPServerSpec:
    """Microsoft official ``markitdown-mcp`` server (stdio)."""
    return MCPServerSpec(
        name="markitdown",
        domain="document",
        effect="read",
        transport="stdio",
        params={"command": "uv", "args": ["run", "markitdown-mcp"]},
    )


# 构造飞书/Lark MCP 连接，并应用工具预设白名单。
def _build_feishu_server(configurable: Configuration) -> MCPServerSpec | None:
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
    if (
        configurable.feishu_domain
        and configurable.feishu_domain != "https://open.feishu.cn"
    ):
        args.extend(["--domain", configurable.feishu_domain])

    # OAuth / user-access-token mode
    if configurable.feishu_oauth_enabled:
        args.extend(["--oauth", "--token-mode", "user_access_token"])

    return MCPServerSpec(
        name="feishu",
        domain="feishu",
        effect="protected",
        transport="stdio",
        params={"command": "npx", "args": args},
    )


# ---------------------------------------------------------------------------
# Discovery and main entry point
# ---------------------------------------------------------------------------


# 在一个短生命周期连接中发现服务方工具，并完成 SDK 原生声明转换。
async def _load_server_tools(
    spec: MCPServerSpec,
    existing_tool_names: set[str],
    *,
    allowed_tool_names: set[str] | None = None,
) -> list[BusinessTool]:
    """Discover one server and return trusted ``BusinessTool`` declarations."""
    server = spec.create()
    async with server:
        mcp_tools = await server.list_tools()
        if allowed_tool_names is not None:
            mcp_tools = [
                tool for tool in mcp_tools if tool.name in allowed_tool_names
            ]

        loaded: list[BusinessTool] = []
        taken = set(existing_tool_names)
        for mcp_tool in mcp_tools:
            public_name = allocate_tool_name(spec.name, mcp_tool.name, taken)
            if public_name != mcp_tool.name:
                logger.info(
                    "MCP tool name conflict: '%s' from server '%s' → renaming to '%s'.",
                    mcp_tool.name,
                    spec.name,
                    public_name,
                )
            try:
                loaded.append(
                    build_mcp_tool(
                        mcp_tool,
                        server,
                        server_name=spec.name,
                        server_factory=spec.create,
                        public_name=public_name,
                        domain=spec.domain,
                        effect=spec.effect,
                    )
                )
            except Exception:
                logger.warning(
                    "MCP server '%s': tool '%s' could not be declared — skipping it.",
                    spec.name,
                    mcp_tool.name,
                    exc_info=True,
                )
                continue
            taken.add(public_name)
        return loaded


# 逐个加载已启用的 MCP 服务并收集工具；单服务失败时记录后继续。
async def load_mcp_tools(
    config: RunnableConfig,
    existing_tool_names: set[str],
    *,
    extra_servers: list[MCPServerSpec] | None = None,
) -> list[BusinessTool]:
    """Load tools from all configured MCP servers with fault isolation.

    ``extra_servers`` lets a trusted plugin contribute additional server specs
    through this same discovery and declaration path instead of a second client.
    """
    configurable = Configuration.from_runnable_config(config)
    servers: list[MCPServerSpec] = []

    # 1) Legacy single-server mcp_config → "legacy_mcp"
    legacy = await _build_legacy_server(configurable, config)
    if legacy:
        servers.append(legacy)

    # 2) Bytebase DBHub
    if configurable.dbhub_enabled and configurable.dbhub_dsn:
        try:
            dbhub = _build_dbhub_server(configurable)
            if dbhub:
                servers.append(dbhub)
        except Exception:
            logger.warning(
                "Failed to build DBHub config — DBHub disabled.", exc_info=True
            )

    # 3) Microsoft MarkItDown
    if configurable.markitdown_enabled:
        try:
            servers.append(_build_markitdown_server(configurable))
        except Exception:
            logger.warning(
                "Failed to build MarkItDown config — MarkItDown disabled.",
                exc_info=True,
            )

    # 4) Feishu / Lark
    if configurable.feishu_enabled and configurable.feishu_app_id:
        try:
            feishu = _build_feishu_server(configurable)
            if feishu:
                servers.append(feishu)
        except Exception:
            logger.warning(
                "Failed to build Feishu config — Feishu disabled.", exc_info=True
            )

    # 5) Plugin-declared servers keep their trusted local domain/effect policy.
    servers.extend(extra_servers or [])

    # --- Per-server loading with full fault isolation ---
    all_tools: list[BusinessTool] = []
    for spec in servers:
        allowed: set[str] | None = None
        if (
            spec.name == "legacy_mcp"
            and configurable.mcp_config
            and configurable.mcp_config.tools
        ):
            allowed = set(configurable.mcp_config.tools)
        try:
            loaded = await _load_server_tools(
                spec, existing_tool_names, allowed_tool_names=allowed
            )
        except Exception:
            logger.warning(
                "MCP server '%s' failed to load — other servers will continue.",
                spec.name,
                exc_info=True,
            )
            continue
        all_tools.extend(loaded)
        existing_tool_names.update(tool.name for tool in loaded)
        logger.info(
            "MCP server '%s': loaded %d tools%s",
            spec.name,
            len(loaded),
            f" (tools: {[tool.name for tool in loaded]})" if loaded else "",
        )

    return all_tools
