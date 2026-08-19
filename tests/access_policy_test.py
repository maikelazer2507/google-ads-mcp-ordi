# Copyright 2026 Google LLC.

"""Tests for fail-closed customer scoping."""

import os
import unittest
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError

from ads_mcp.access_policy import (
    current_operator,
    filter_accessible_customers,
    normalize_customer_id,
    require_customer_access,
)


class TestAccessPolicy(unittest.TestCase):
    def test_normalizes_hyphenated_id(self):
        self.assertEqual(normalize_customer_id("123-456-7890"), "1234567890")

    def test_rejects_invalid_id(self):
        with self.assertRaisesRegex(ToolError, "exactly 10 digits"):
            normalize_customer_id("123")

    def test_reads_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ToolError, "Read access is disabled"):
                require_customer_access("1234567890")

    def test_read_scope_does_not_fall_back_to_write_scope(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "123-456-7890"},
            clear=True,
        ):
            with self.assertRaisesRegex(ToolError, "Read access is disabled"):
                require_customer_access("1234567890")

    def test_filters_accessible_customers(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_READ_CUSTOMER_IDS": "1234567890"},
            clear=True,
        ):
            self.assertEqual(
                filter_accessible_customers(
                    ["customers/9999999999", "customers/1234567890"]
                ),
                ["1234567890"],
            )

    def test_production_operator_allowlist_is_required(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_PRODUCTION_MODE": "true"},
            clear=True,
        ):
            with self.assertRaisesRegex(ToolError, "OPERATOR_EMAILS"):
                current_operator()

    def test_http_transport_requires_operator_even_if_mode_is_missing(self):
        with patch.dict(
            os.environ,
            {"GOOGLE_ADS_MCP_TRANSPORT": "streamable-http"},
            clear=True,
        ):
            with self.assertRaisesRegex(ToolError, "OPERATOR_EMAILS"):
                current_operator()

    def test_operator_identity_comes_from_oauth_claims(self):
        token = MagicMock()
        token.claims = {"email": "MARIA@example.com", "sub": "subject"}
        token.subject = "subject"
        with (
            patch.dict(
                os.environ,
                {"GOOGLE_ADS_MCP_OPERATOR_EMAILS": "maria@example.com"},
                clear=True,
            ),
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=token,
            ),
        ):
            actor = current_operator()
        self.assertEqual(actor.email, "maria@example.com")

    def test_non_allowlisted_operator_is_rejected(self):
        token = MagicMock()
        token.claims = {"email": "sofia@example.com", "sub": "subject"}
        token.subject = "subject"
        with (
            patch.dict(
                os.environ,
                {"GOOGLE_ADS_MCP_OPERATOR_EMAILS": "maria@example.com"},
                clear=True,
            ),
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=token,
            ),
        ):
            with self.assertRaisesRegex(ToolError, "not authorized"):
                current_operator()


if __name__ == "__main__":
    unittest.main()
