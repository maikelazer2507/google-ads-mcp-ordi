# Copyright 2026 Google LLC.

"""Tests for production readiness checks."""

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from ads_mcp.http_routes import _production_configuration_errors, readyz


def complete_environment() -> dict[str, str]:
    return {
        "GOOGLE_ADS_MCP_PRODUCTION_MODE": "true",
        "GOOGLE_ADS_MCP_TRANSPORT": "streamable-http",
        "GOOGLE_ADS_MCP_ENVIRONMENT": "production",
        "GOOGLE_ADS_DEVELOPER_TOKEN": "secret-reference",
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID": "client",
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": "oauth-secret-reference-32-characters",
        "GOOGLE_ADS_MCP_BASE_URL": "https://example.run.app",
        "GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS": (
            "https://chatgpt.example/oauth/callback"
        ),
        "GOOGLE_ADS_MCP_JWT_SIGNING_KEY": "j" * 32,
        "GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY": "e" * 32,
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "9999999999",
        "GOOGLE_ADS_MCP_READ_CUSTOMER_IDS": "1234567890",
        "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "1234567890",
        "GOOGLE_ADS_MCP_OPERATOR_EMAILS": (
            "maria@example.com,owner@example.com"
        ),
        "GOOGLE_ADS_MCP_APPROVER_EMAILS": "owner@example.com",
        "GOOGLE_ADS_MCP_APPROVAL_BASE_URL": "https://example.run.app",
        "GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID": "web-client",
        "GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY": "s" * 32,
        "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
        "GOOGLE_ADS_MCP_STORAGE_TYPE": "firestore",
        "GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE": "firestore",
        "GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT": "example-project",
        "GOOGLE_PROJECT_ID": "example-project",
        "GOOGLE_ADS_MCP_ALLOW_UNSCOPED_READS": "false",
        "GOOGLE_ADS_MCP_ALLOW_SENSITIVE_READS": "false",
        "GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION": "false",
    }


class TestProductionReadiness(unittest.TestCase):
    def test_missing_mode_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            errors = _production_configuration_errors()
        self.assertIn("production-mode", errors)
        self.assertIn("transport", errors)

    def test_production_mode_fails_closed(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_PRODUCTION_MODE": "true"},
            clear=True,
        ):
            errors = _production_configuration_errors()
        self.assertTrue(errors)
        self.assertIn("required-credentials", errors)

    def test_complete_production_configuration_is_ready(self):
        with patch.dict(os.environ, complete_environment(), clear=True):
            self.assertEqual(_production_configuration_errors(), [])

    def test_redirect_allowlist_is_explicit_and_https(self):
        for value in (
            "",
            "http://chatgpt.example/callback",
            "https://*.example/cb",
        ):
            with self.subTest(value=value):
                environment = complete_environment()
                environment["GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS"] = (
                    value
                )
                with patch.dict(os.environ, environment, clear=True):
                    self.assertIn(
                        "redirect-allowlist",
                        _production_configuration_errors(),
                    )

    def test_weak_or_ambiguous_storage_configuration_fails(self):
        environment = complete_environment()
        environment["GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION"] = "true"
        environment["GOOGLE_ADS_MCP_CHANGESET_STORE"] = "memory"
        environment["GOOGLE_ADS_MCP_JWT_SIGNING_KEY"] = "short"
        with patch.dict(os.environ, environment, clear=True):
            errors = _production_configuration_errors()
        self.assertIn("storage-encryption", errors)
        self.assertIn("storage-alias-conflict", errors)
        self.assertIn("key-strength", errors)

    def test_readiness_response_does_not_disclose_configuration_details(self):
        with patch.dict(os.environ, {}, clear=True):
            response = asyncio.run(readyz(None))  # type: ignore[arg-type]
        payload = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload, {"status": "not_ready"})


if __name__ == "__main__":
    unittest.main()
