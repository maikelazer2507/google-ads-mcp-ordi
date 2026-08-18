# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Tests for guarded Google Ads write tools."""

import os
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import safe_mutations


class TestSafeMutations(unittest.TestCase):
    """Verifies preview/apply separation and spend guardrails."""

    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "1234567890",
            "GOOGLE_ADS_MCP_CHANGESET_SIGNING_KEY": "x" * 32,
            "GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS": "example.com",
        }
        self.campaign = {
            "id": "11",
            "name": "Search",
            "resource_name": "customers/1234567890/campaigns/11",
            "status": "ENABLED",
            "campaign_budget": "customers/1234567890/campaignBudgets/22",
        }

    def test_campaign_preview_validates_without_applying(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    return_value=self.campaign,
                ),
                patch.object(
                    safe_mutations, "_validate_campaign_status"
                ) as validate,
                patch.object(
                    safe_mutations, "_mutate_campaign_status"
                ) as mutate,
            ):
                preview = safe_mutations.preview_campaign_status_change(
                    "1234567890",
                    "11",
                    "paused",
                    "control spend",
                    "less spend",
                )

        self.assertEqual(
            preview["execution_status"], "PREVIEW_ONLY_NOT_APPLIED"
        )
        self.assertEqual(preview["proposed"], {"status": "PAUSED"})
        validate.assert_called_once()
        mutate.assert_not_called()

    def test_apply_rechecks_and_verifies_campaign_state(self):
        after = {**self.campaign, "status": "PAUSED"}
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    return_value=self.campaign,
                ),
                patch.object(safe_mutations, "_validate_campaign_status"),
            ):
                preview = safe_mutations.preview_campaign_status_change(
                    "1234567890",
                    "11",
                    "PAUSED",
                    "control spend",
                    "less spend",
                )

            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    side_effect=[self.campaign, after],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_campaign_status",
                    return_value=self.campaign["resource_name"],
                ) as mutate,
            ):
                result = safe_mutations.apply_campaign_status_change(
                    preview["change_set_token"], preview["approval_statement"]
                )

        self.assertEqual(result["execution_status"], "APPLIED_AND_VERIFIED")
        self.assertEqual(result["after"], {"status": "PAUSED"})
        mutate.assert_called_once()

    def test_apply_aborts_on_drift(self):
        drifted = {**self.campaign, "status": "PAUSED"}
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    return_value=self.campaign,
                ),
                patch.object(safe_mutations, "_validate_campaign_status"),
            ):
                preview = safe_mutations.preview_campaign_status_change(
                    "1234567890",
                    "11",
                    "PAUSED",
                    "control spend",
                    "less spend",
                )

            with (
                patch.object(
                    safe_mutations, "_campaign_state", return_value=drifted
                ),
                patch.object(
                    safe_mutations, "_mutate_campaign_status"
                ) as mutate,
            ):
                with self.assertRaisesRegex(ToolError, "state changed"):
                    safe_mutations.apply_campaign_status_change(
                        preview["change_set_token"],
                        preview["approval_statement"],
                    )
                mutate.assert_not_called()

    def test_shared_budget_is_blocked(self):
        budget = {
            "id": "22",
            "name": "Shared",
            "resource_name": "customers/1234567890/campaignBudgets/22",
            "amount_micros": 10_000_000,
            "amount": 10.0,
            "explicitly_shared": True,
            "reference_count": 2,
        }
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "Shared budgets"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 9.0, "test", "test"
                )

    def test_budget_increase_above_ten_percent_is_blocked(self):
        budget = {
            "id": "22",
            "name": "Daily",
            "resource_name": "customers/1234567890/campaignBudgets/22",
            "amount_micros": 10_000_000,
            "amount": 10.0,
            "explicitly_shared": False,
            "reference_count": 1,
        }
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "above 10 percent"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 11.01, "test", "test"
                )

    def test_apply_budget_aborts_if_budget_became_shared(self):
        current = {
            "id": "22",
            "name": "Daily",
            "resource_name": "customers/1234567890/campaignBudgets/22",
            "amount_micros": 10_000_000,
            "amount": 10.0,
            "explicitly_shared": False,
            "reference_count": 1,
        }
        shared = {**current, "explicitly_shared": True, "reference_count": 2}
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_budget_state", return_value=current
                ),
                patch.object(safe_mutations, "_validate_budget"),
            ):
                preview = safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 9.0, "control spend", "less spend"
                )

            with (
                patch.object(
                    safe_mutations, "_budget_state", return_value=shared
                ),
                patch.object(safe_mutations, "_mutate_budget") as mutate,
            ):
                with self.assertRaisesRegex(ToolError, "now shared"):
                    safe_mutations.apply_campaign_budget_change(
                        preview["change_set_token"],
                        preview["approval_statement"],
                    )
                mutate.assert_not_called()

    def test_final_url_requires_https_and_allowlisted_host(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(
                safe_mutations._validate_final_url("https://example.com/book"),
                "https://example.com/book",
            )
            with self.assertRaisesRegex(ToolError, "HTTPS"):
                safe_mutations._validate_final_url("http://example.com/book")
            with self.assertRaisesRegex(ToolError, "not allowlisted"):
                safe_mutations._validate_final_url("https://evil.example/book")

    def test_duplicate_negative_keyword_is_blocked(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                safe_mutations, "_negative_keyword_exists", return_value=True
            ),
        ):
            with self.assertRaisesRegex(ToolError, "already exists"):
                safe_mutations.preview_add_negative_keyword(
                    "1234567890",
                    "33",
                    "free dentist",
                    "EXACT",
                    "waste",
                    "less waste",
                )


class TestSafeMutationSchemas(unittest.IsolatedAsyncioTestCase):
    """Verifies write-tool discovery and annotations."""

    async def test_namespace_exposes_paired_tools(self):
        tools = await safe_mutations.changes_mcp.list_tools()
        by_name = {tool.name: tool for tool in tools}
        self.assertEqual(len(by_name), 10)
        self.assertIn("preview_campaign_budget_change", by_name)
        self.assertIn("apply_campaign_budget_change", by_name)
        self.assertTrue(
            by_name["preview_campaign_budget_change"].annotations.readOnlyHint
        )
        apply_annotations = by_name["apply_campaign_budget_change"].annotations
        self.assertFalse(apply_annotations.readOnlyHint)
        self.assertFalse(apply_annotations.idempotentHint)


if __name__ == "__main__":
    unittest.main()
