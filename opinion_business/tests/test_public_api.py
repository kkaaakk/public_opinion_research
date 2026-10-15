"""Offline public boundary tests, runnable without pytest or the old runtime."""

import importlib
import unittest

from opinion_business import __version__, get_status, ping


class PublicApiTests(unittest.TestCase):
    def test_ping_normalizes_and_returns_serializable_health(self):
        self.assertEqual(
            ping("  舆情迁移检查  "),
            {"status": "ok", "phase": "phase0", "version": __version__, "text": "舆情迁移检查"},
        )

    def test_ping_accepts_boundary_and_rejects_invalid_requests(self):
        self.assertEqual(len(ping("x" * 200)["text"]), 200)
        for text in ("", " \t\n", "x" * 201):
            with self.subTest(text_length=len(text)), self.assertRaises(ValueError):
                ping(text)
        for text in (None, 1, b"hello", ["hello"]):
            with self.subTest(value_type=type(text).__name__), self.assertRaises(TypeError):
                ping(text)

    def test_status_exposes_only_interface_capabilities(self):
        status = get_status()
        self.assertEqual(status["phase"], "phase0")
        self.assertEqual(status["version"], __version__)
        expected = {"rag", "research_graph", "social_media", "sentiment", "risk", "evidence", "reports"}
        self.assertEqual(set(status["domains"]), expected)
        self.assertEqual(set(status["domains"].values()), {"interface_only"})
        status["domains"]["rag"] = "altered by caller"
        self.assertEqual(get_status()["domains"]["rag"], "interface_only")

    def test_domain_contracts_import_without_optional_infrastructure(self):
        for domain in get_status()["domains"]:
            with self.subTest(domain=domain):
                importlib.import_module(f"opinion_business.{domain}")


if __name__ == "__main__":
    unittest.main()
