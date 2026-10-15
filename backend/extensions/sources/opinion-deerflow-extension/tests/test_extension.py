"""Offline extension-contract tests, without the DeerFlow host or an LLM."""

import ast
import inspect
import unittest
from importlib.metadata import distribution
from pathlib import Path
from types import MappingProxyType

from deerflow_extension_api import ActionContext, ExtensionPrincipal, PluginContribution, ToolContext

from opinion_deerflow_extension import install


class RecordingRegistry:
    def __init__(self, accepted=True):
        self.accepted = accepted
        self.plugins = []

    def plugin(self, contribution):
        if self.accepted:
            self.plugins.append(contribution)
        return self.accepted


def contribution(enabled=True):
    registry = RecordingRegistry()
    install(registry, {"enabled": enabled})
    return registry.plugins[0]


class RegistrationTests(unittest.TestCase):
    def test_installed_entry_point_loads_declared_public_contract(self):
        entry_points = [
            item
            for item in distribution("opinion-deerflow-extension").entry_points
            if item.group == "deerflow.extensions"
        ]
        self.assertEqual(
            [(item.name, item.value) for item in entry_points],
            [("opinion", "opinion_deerflow_extension:install")],
        )
        loaded_install = entry_points[0].load()
        self.assertIs(loaded_install, install)
        self.assertEqual(loaded_install.__deerflow_api__, "0.2.7")
        self.assertEqual(loaded_install.__deerflow_name__, "opinion")

    def test_registers_only_a_tool_and_status_action(self):
        plugin = contribution()
        self.assertIsInstance(plugin, PluginContribution)
        self.assertEqual(plugin.namespace, "public-opinion.phase0")
        self.assertTrue(plugin.enabled)
        self.assertIsNone(plugin.frontend)
        self.assertEqual([tool.name for tool in plugin.tools], ["opinion_ping"])
        self.assertEqual([action.name for action in plugin.backend], ["status"])
        self.assertTrue(inspect.iscoroutinefunction(plugin.tools[0].handler))
        self.assertTrue(inspect.iscoroutinefunction(plugin.backend[0].handler))
        schema = plugin.tools[0].input_schema
        self.assertEqual(schema["required"], ["text"])
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(plugin.tools[0].group, "extensions")

    def test_disabled_by_default(self):
        registry = RecordingRegistry()
        install(registry, {})
        self.assertIs(registry.plugins[0].enabled, False)
        self.assertIs(contribution(False).enabled, False)

    def test_invalid_config_is_rejected_before_registration(self):
        for config in ({"enabled": "true"}, {"enabled": 1}, {"api_key": "never-used"}):
            with self.subTest(config=config):
                registry = RecordingRegistry()
                with self.assertRaises(ValueError):
                    install(registry, config)
                self.assertEqual(registry.plugins, [])

    def test_host_without_full_stack_support_fails_explicitly(self):
        with self.assertRaisesRegex(RuntimeError, "PluginContribution"):
            install(RecordingRegistry(accepted=False), {"enabled": True})

    def test_runtime_package_does_not_import_private_host_modules(self):
        import opinion_deerflow_extension

        package = Path(opinion_deerflow_extension.__file__).parent
        for source in package.rglob("*.py"):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = (
                    [alias.name for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                for module in modules:
                    self.assertNotIn(module.split(".")[0], {"deerflow", "app"}, str(source))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = contribution()
        principal = ExtensionPrincipal("offline-user")
        settings = MappingProxyType({"enabled": True})
        self.tool_context = ToolContext(principal, settings, "offline-thread")
        self.action_context = ActionContext(principal, settings)

    async def test_ping_executes_the_business_package(self):
        result = await self.plugin.tools[0].handler(MappingProxyType({"text": "  阶段零  "}), self.tool_context)
        self.assertEqual(result, {"status": "ok", "phase": "phase0", "version": "0.1.0", "text": "阶段零"})

    async def test_tool_rejects_invalid_inputs(self):
        for payload in ({}, {"text": "ok", "user_id": "spoofed"}, {"text": " "}, {"text": "x" * 201}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                await self.plugin.tools[0].handler(payload, self.tool_context)
        with self.assertRaises(TypeError):
            await self.plugin.tools[0].handler({"text": 12}, self.tool_context)

    async def test_status_describes_only_actual_capabilities(self):
        result = await self.plugin.backend[0].handler({}, self.action_context)
        self.assertEqual(result["namespace"], self.plugin.namespace)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["extension_version"], "0.1.0")
        self.assertEqual(result["business"]["version"], "0.1.0")
        self.assertEqual(set(result["business"]["domains"].values()), {"interface_only"})

    async def test_status_takes_no_user_parameters(self):
        with self.assertRaises(ValueError):
            await self.plugin.backend[0].handler({"enabled": False}, self.action_context)


if __name__ == "__main__":
    unittest.main()
