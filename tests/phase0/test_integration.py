"""Offline checks across installed business, public extension API and official host.

These HTTP tests exercise the real Gateway application and middleware without
starting its database lifespan. The separate Phase 0 live smoke check owns
process startup/readiness; no test here calls a model or a network service.
"""

import asyncio
import hashlib
import json
import sys
import tomllib
from dataclasses import replace
from importlib.metadata import distribution
from pathlib import Path, PureWindowsPath

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
NAMESPACE = "public-opinion.phase0"
ENTRY_POINT = "opinion_deerflow_extension:install"
PUBLIC_SKILLS = {"opinion-research", "risk-assessment", "opinion-report"}


@pytest.fixture
def offline_gateway(tmp_path, monkeypatch):
    """Use only isolated, explicit official config paths and reversible env."""
    config_path = tmp_path / "config.yaml"
    extensions_path = tmp_path / "extensions_config.json"
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    extensions_path.write_text(
        json.dumps(
            {
                "mcpServers": {},
                "skills": {name: {"enabled": True} for name in PUBLIC_SKILLS},
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        yaml.safe_dump(
            {
                "models": [],
                "sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"},
                "tools": [],
                "skills": {"path": str(ROOT / "skills")},
                "memory": {"enabled": False},
                "plugins": [
                    {
                        "name": "opinion",
                        "package": "opinion-deerflow-extension",
                        "use": ENTRY_POINT,
                        "enabled": True,
                        "required": True,
                        "config": {"enabled": True},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for name, value in {
        "DEER_FLOW_PROJECT_ROOT": str(tmp_path),
        "DEER_FLOW_CONFIG_PATH": str(config_path),
        "DEER_FLOW_EXTENSIONS_CONFIG_PATH": str(extensions_path),
        "DEER_FLOW_HOME": str(tmp_path / "runtime"),
        "DEER_FLOW_ENV_FILE": str(env_path),
        "DEER_FLOW_AUTH_DISABLED": "1",
        "DEER_FLOW_ENV": "development",
        "ENVIRONMENT": "development",
        "LANGSMITH_TRACING": "false",
        "LANGCHAIN_TRACING_V2": "false",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)

    from deerflow.config.app_config import get_app_config, reset_app_config
    from deerflow.config.extensions_config import reset_extensions_config
    from deerflow.extensions import get_loaded_extensions, set_loaded_extensions

    previous_extensions = get_loaded_extensions()
    reset_app_config()
    reset_extensions_config()
    try:
        # Import after selecting configuration: this module constructs its
        # default application at import time using the same public boot path.
        from app.gateway.app import create_app

        app = create_app()
        yield app, get_app_config(), config_path
    finally:
        set_loaded_extensions(previous_extensions)
        reset_app_config()
        reset_extensions_config()


def _assembled_plugin_tool(app, config):
    from deerflow.extensions.plugin_tools import plugin_tool_name
    from deerflow.tools.tools import get_available_tools

    tools = get_available_tools(
        include_mcp=False,
        include_upload_tool=False,
        app_config=config,
        extensions=app.state.extensions,
    )
    name = plugin_tool_name(NAMESPACE, "opinion_ping")
    return next(tool for tool in tools if tool.name == name)


def _tool_graph(tool):
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([tool]))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


async def _invoke(graph, tool, payload):
    from langchain_core.messages import AIMessage

    result = await graph.ainvoke(
        {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "phase0-call", "name": tool.name, "args": payload}
                    ],
                )
            ]
        },
        context={"user_id": "phase0-user", "thread_id": "phase0-thread"},
    )
    return result["messages"][-1]


def test_installed_entry_point_loads_in_official_gateway(offline_gateway):
    app, config, _ = offline_gateway
    from opinion_deerflow_extension import install

    entry_points = [
        entry
        for entry in distribution("opinion-deerflow-extension").entry_points
        if entry.group == "deerflow.extensions"
    ]
    assert [(entry.name, entry.value) for entry in entry_points] == [
        ("opinion", ENTRY_POINT)
    ]
    assert entry_points[0].load() is install
    assert install.__deerflow_api__ == "0.2.7"
    assert config.models == []
    assert [
        (source, plugin.namespace) for source, plugin in app.state.extensions.plugins
    ] == [(ENTRY_POINT, NAMESPACE)]
    assert not [
        diagnostic
        for diagnostic in app.state.extension_diagnostics
        if diagnostic.level == "error"
    ]
    assert not any(
        name == "open_deep_research" or name.startswith("open_deep_research.")
        for name in sys.modules
    ), "The DeerFlow host must not load the legacy agent runtime"


def test_real_tool_assembly_and_langgraph_toolnode_execute_business(offline_gateway):
    app, config, _ = offline_gateway
    from deerflow.tools.tool_provenance import get_plugin_source

    tool = _assembled_plugin_tool(app, config)
    assert get_plugin_source(tool) == {
        "namespace": NAMESPACE,
        "declaration": "opinion_ping",
        "installation": ENTRY_POINT,
    }
    result = asyncio.run(_invoke(_tool_graph(tool), tool, {"text": "  舆情迁移验证  "}))
    assert result.status == "success"
    assert json.loads(result.content) == {
        "status": "ok",
        "phase": "phase0",
        "version": "0.1.0",
        "text": "舆情迁移验证",
    }
    assert result.tool_call_id == "phase0-call"


@pytest.mark.parametrize(
    "payload",
    [{"text": " "}, {"text": "ok", "user_id": "spoofed"}, {"text": "x" * 201}],
)
def test_toolnode_rejects_bad_payload_without_business_success(
    offline_gateway, payload
):
    app, config, _ = offline_gateway
    tool = _assembled_plugin_tool(app, config)
    result = asyncio.run(_invoke(_tool_graph(tool), tool, payload))
    assert result.status == "error"
    assert '"status": "ok"' not in result.content


def test_disabled_manifest_excludes_tool_from_official_assembly(offline_gateway):
    app, config, _ = offline_gateway
    from deerflow.extensions.plugin_tools import plugin_tool_name
    from deerflow.tools.tools import get_available_tools

    disabled = replace(
        app.state.extensions,
        plugins=tuple(
            (source, replace(plugin, enabled=False))
            for source, plugin in app.state.extensions.plugins
        ),
    )
    tools = get_available_tools(
        include_mcp=False,
        include_upload_tool=False,
        app_config=config,
        extensions=disabled,
    )
    assert plugin_tool_name(NAMESPACE, "opinion_ping") not in {
        tool.name for tool in tools
    }


def test_official_gateway_discovers_and_executes_backend_status(offline_gateway):
    app, _, _ = offline_gateway
    from fastapi.testclient import TestClient

    # Deliberately avoid `with TestClient`: process startup/lifespan is verified
    # separately, while this checks the unmodified official route and auth chain.
    client = TestClient(app)
    try:
        discovered = client.get("/api/plugins")
        assert discovered.status_code == 200
        descriptor = next(
            plugin for plugin in discovered.json() if plugin["namespace"] == NAMESPACE
        )
        assert descriptor["settings"] == {"enabled": True}
        assert descriptor["backend_actions"] == ["status"]
        response = client.post(
            f"/api/plugins/{NAMESPACE}/actions/status",
            json={},
            headers={"x-deerflow-plugin-viewer": descriptor["viewer_id"]},
        )
        assert response.status_code == 200
        assert response.json()["business"]["version"] == "0.1.0"
        assert set(response.json()["business"]["domains"].values()) == {
            "interface_only"
        }
        assert (
            client.post(
                f"/api/plugins/{NAMESPACE}/actions/status", json={"enabled": False}
            ).status_code
            == 422
        )
        assert (
            client.post(f"/api/plugins/{NAMESPACE}/actions/status", json=[]).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/plugins/{NAMESPACE}/actions/status",
                json={},
                headers={"x-deerflow-plugin-viewer": "stale-viewer"},
            ).status_code
            == 409
        )
    finally:
        client.close()


def test_official_plugin_routes_require_authentication(offline_gateway, monkeypatch):
    app, _, _ = offline_gateway
    from fastapi.testclient import TestClient

    monkeypatch.delenv("DEER_FLOW_AUTH_DISABLED")
    client = TestClient(app)
    try:
        assert client.get("/api/plugins").status_code == 401
    finally:
        client.close()


def test_public_skills_are_recognized_and_enabled_by_official_storage(offline_gateway):
    _, config, _ = offline_gateway
    from deerflow.skills.storage import get_or_new_skill_storage
    from deerflow.skills.types import SkillCategory

    storage = get_or_new_skill_storage(app_config=config)
    discovered = {skill.name: skill for skill in storage.load_skills(enabled_only=True)}
    assert PUBLIC_SKILLS <= discovered.keys()
    for name in PUBLIC_SKILLS:
        assert discovered[name].category == SkillCategory.PUBLIC
        assert (
            discovered[name].skill_file.resolve()
            == (ROOT / "skills" / "public" / name / "SKILL.md").resolve()
        )
        assert discovered[name].description


@pytest.mark.parametrize(
    ("canonical", "snapshot", "package"),
    [
        ("opinion_business", "opinion-business", "opinion_business"),
        (
            "deerflow_extension",
            "opinion-deerflow-extension",
            "opinion_deerflow_extension",
        ),
    ],
)
def test_deployable_runtime_sources_match_canonical_packages(
    canonical, snapshot, package
):
    source = ROOT / canonical
    deployed = BACKEND / "extensions" / "sources" / snapshot
    assert deployed.is_dir(), (
        "Install the Phase 0 packages through the documented official extension workflow"
    )

    def runtime_files(root):
        files = {Path("pyproject.toml"), Path("README.md")}
        files.update(path.relative_to(root) for path in (root / package).rglob("*.py"))
        return {path: (root / path).read_bytes() for path in sorted(files)}

    assert runtime_files(source) == runtime_files(deployed), (
        "Refresh the managed source after editing a canonical package"
    )


def test_backend_lock_uses_reproducible_sources_and_passes_official_manager_audit():
    from deerflow.extensions.manager import _validate_locked_local_sources

    lock_path = BACKEND / "uv.lock"
    with lock_path.open("rb") as stream:
        document = tomllib.load(stream)
    packages = {package["name"]: package for package in document["package"]}
    for name in ("opinion-business", "opinion-deerflow-extension"):
        source = packages[name]["source"]
        relative = source.get("directory", source.get("editable"))
        assert isinstance(relative, str)
        assert (
            not Path(relative).is_absolute()
            and not PureWindowsPath(relative).is_absolute()
        )
        resolved = (BACKEND / relative).resolve()
        assert resolved.is_relative_to(BACKEND / "extensions" / "sources")
        assert resolved.is_dir()
    _validate_locked_local_sources(lock_path, BACKEND)


def test_official_manager_reports_configured_installation(offline_gateway):
    _, _, config_path = offline_gateway
    from deerflow.extensions.manager import ExtensionManager

    (installed,) = ExtensionManager(ROOT, config_path=config_path).list_configured()
    assert (installed.name, installed.distribution, installed.use) == (
        "opinion",
        "opinion-deerflow-extension",
        ENTRY_POINT,
    )
    assert installed.enabled and installed.required


def test_complete_upstream_source_and_reviewed_deltas():
    manifest = json.loads(
        (ROOT / "docs/migration/upstream-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["commit"] == "8d994cc1e7ee63c61283a71a169248beb4ed1c52"
    assert len(manifest["files"]) == 4000
    reviewed = {
        ".gitignore",
        "AGENTS.md",
        "README.md",
        "backend/pyproject.toml",
        "backend/uv.lock",
    }
    for relative, digest in manifest["files"].items():
        path = ROOT / relative
        assert path.is_file(), f"Missing official engineering file: {relative}"
        if relative not in reviewed:
            content = path.read_bytes()
            actual = hashlib.sha256(content).hexdigest()
            if actual != digest and relative in manifest.get("text_lf_sha256", {}):
                actual = hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()
                digest = manifest["text_lf_sha256"][relative]
            assert actual == digest, relative
