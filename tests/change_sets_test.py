# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Tests for signed, allowlisted Google Ads change sets."""

import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.change_sets import (
    create_change_set,
    require_allowed_customer,
    verify_change_set,
)


class TestChangeSets(unittest.TestCase):
    """Verifies the approval and tamper-resistance boundary."""

    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "123-456-7890",
            "GOOGLE_ADS_MCP_CHANGESET_SIGNING_KEY": "x" * 32,
            "GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS": "900",
        }

    def test_customer_must_be_allowlisted(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(
                require_allowed_customer("123-456-7890"), "1234567890"
            )
            with self.assertRaisesRegex(ToolError, "not permitted"):
                require_allowed_customer("9999999999")

    def test_empty_allowlist_disables_writes(self):
        environment = dict(self.environment)
        environment.pop("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS")
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ToolError, "Write tools are disabled"):
                require_allowed_customer("1234567890")

    def test_round_trip_requires_exact_approval(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = create_change_set(
                {"action": "test", "customer_id": "1234567890"}
            )
            with self.assertRaisesRegex(ToolError, "Expected exactly"):
                verify_change_set(preview["change_set_token"], "APPROVE")

            payload = verify_change_set(
                preview["change_set_token"], preview["approval_statement"]
            )
            self.assertEqual(payload["change_set_id"], preview["change_set_id"])

    def test_tampering_is_rejected(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = create_change_set(
                {"action": "test", "customer_id": "1234567890"}
            )
            token = preview["change_set_token"]
            tampered = ("A" if token[0] != "A" else "B") + token[1:]
            with self.assertRaisesRegex(ToolError, "signature"):
                verify_change_set(tampered, preview["approval_statement"])

    def test_expired_change_set_is_rejected(self):
        environment = dict(self.environment)
        environment["GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS"] = "60"
        with patch.dict(os.environ, environment, clear=True):
            with patch("ads_mcp.change_sets.time.time", return_value=1_000):
                preview = create_change_set(
                    {"action": "test", "customer_id": "1234567890"}
                )
            with patch("ads_mcp.change_sets.time.time", return_value=1_061):
                with self.assertRaisesRegex(ToolError, "expired"):
                    verify_change_set(
                        preview["change_set_token"],
                        preview["approval_statement"],
                    )


if __name__ == "__main__":
    unittest.main()
