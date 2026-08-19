# Copyright 2026 Google LLC.

"""Security contract tests for the FastMCP OAuth coordinator."""

import inspect
import unittest

from fastmcp.server.auth.providers.google import GoogleProvider

from ads_mcp.coordinator import parse_allowed_client_redirect_uris


class TestRedirectAllowlist(unittest.TestCase):
    def test_locked_fastmcp_provider_exposes_redirect_allowlist(self):
        parameter = inspect.signature(GoogleProvider).parameters[
            "allowed_client_redirect_uris"
        ]
        self.assertIsNone(parameter.default)

    def test_missing_allowlist_denies_all_redirects(self):
        self.assertEqual(
            parse_allowed_client_redirect_uris(None, secure_mode=True), []
        )

    def test_secure_allowlist_accepts_concrete_https_callbacks(self):
        self.assertEqual(
            parse_allowed_client_redirect_uris(
                "https://chatgpt.example/oauth/callback,"
                "https://client.example/mcp/callback",
                secure_mode=True,
            ),
            [
                "https://chatgpt.example/oauth/callback",
                "https://client.example/mcp/callback",
            ],
        )

    def test_secure_allowlist_rejects_broad_or_unsafe_patterns(self):
        invalid = (
            "http://chatgpt.example/callback",
            "https://*.example.com/callback",
            "https://example.com/*",
            "https://example.com/",
            "https://localhost/callback",
            "javascript:alert(1)",
            "https://user@example.com/callback",
            "https://example.com/callback?next=evil",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid"):
                    parse_allowed_client_redirect_uris(value, secure_mode=True)


if __name__ == "__main__":
    unittest.main()
