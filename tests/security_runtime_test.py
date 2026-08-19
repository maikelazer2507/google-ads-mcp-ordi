# Copyright 2026 Google LLC.

"""Tests that transport selection cannot silently disable HTTP security."""

import os
import unittest
from unittest.mock import patch

from ads_mcp import server


class TestSecureRuntime(unittest.TestCase):
    def test_transport_must_be_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicitly configured"):
                server.run_server()

    def test_http_never_starts_with_incomplete_security(self):
        with (
            patch.dict(
                os.environ,
                {"GOOGLE_ADS_MCP_TRANSPORT": "streamable-http"},
                clear=True,
            ),
            patch.object(server.mcp, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                server.run_server()
        run.assert_not_called()

    def test_stdio_requires_explicit_development_mode(self):
        for environment in (
            {"GOOGLE_ADS_MCP_TRANSPORT": "stdio"},
            {
                "GOOGLE_ADS_MCP_TRANSPORT": "stdio",
                "GOOGLE_ADS_MCP_PRODUCTION_MODE": "ture",
                "GOOGLE_ADS_MCP_ENVIRONMENT": "development",
            },
        ):
            with self.subTest(environment=environment):
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch.object(server.mcp, "run") as run,
                ):
                    with self.assertRaisesRegex(RuntimeError, "development"):
                        server.run_server()
                run.assert_not_called()

    def test_explicit_development_stdio_starts_stdio_only(self):
        environment = {
            "GOOGLE_ADS_MCP_TRANSPORT": "stdio",
            "GOOGLE_ADS_MCP_PRODUCTION_MODE": "false",
            "GOOGLE_ADS_MCP_ENVIRONMENT": "development",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(server.mcp, "run") as run,
        ):
            server.run_server()
        run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()
