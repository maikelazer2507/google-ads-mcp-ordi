# Copyright 2026 Google LLC.

"""Tests for opinionated read-only diagnostics."""

import os
import unittest
from datetime import date
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.tools import diagnostics


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        self.environment = {"GOOGLE_ADS_MCP_READ_CUSTOMER_IDS": "1234567890"}

    def test_performance_snapshot_calculates_platform_metrics(self):
        rows = [
            {
                "campaign.id": 1,
                "metrics.impressions": 100,
                "metrics.clicks": 10,
                "metrics.cost_micros": 20_000_000,
                "metrics.conversions": 2,
                "metrics.conversions_value": 80,
            }
        ]
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(diagnostics, "search", return_value=rows),
        ):
            result = diagnostics.campaign_performance_snapshot(
                "1234567890", "2026-08-01", "2026-08-18"
            )
        self.assertEqual(result["totals"]["cost"], 20.0)
        self.assertEqual(result["totals"]["platform_cpa"], 10.0)
        self.assertEqual(result["totals"]["platform_roas"], 4.0)
        self.assertIn("UNVERIFIED", result["verdict"])

    def test_change_history_is_limited_to_30_days(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ToolError, "30 days"):
                diagnostics.change_history_timeline(
                    "1234567890", "2026-06-01", "2026-08-01"
                )

    def test_future_date_is_rejected(self):
        future = date(date.today().year + 1, 1, 1).isoformat()
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ToolError, "future"):
                diagnostics.campaign_performance_snapshot(
                    "1234567890", "2026-08-01", future
                )

    def test_search_terms_are_candidates_only(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                diagnostics,
                "search",
                return_value=[
                    {
                        "search_term_view.search_term": "free dentist",
                        "metrics.cost_micros": 7_500_000,
                        "metrics.conversions": 0,
                    }
                ],
            ),
        ):
            result = diagnostics.search_term_waste_candidates(
                "1234567890", "2026-08-01", "2026-08-18"
            )
        self.assertEqual(
            result["execution_status"], "READ_ONLY_NO_NEGATIVES_ADDED"
        )
        self.assertEqual(
            result["candidates"][0]["review_status"],
            "HUMAN_REVIEW_REQUIRED",
        )

    def test_performance_breakdown_rejects_unknown_dimension(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ToolError, "dimension must be"):
                diagnostics.performance_breakdown(
                    "1234567890",
                    "2026-08-01",
                    "2026-08-18",
                    "patient_name",
                )

    def test_budget_pacing_uses_daily_budget_reference(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                diagnostics,
                "search",
                return_value=[
                    {
                        "campaign_budget.amount_micros": 10_000_000,
                        "campaign_budget.explicitly_shared": False,
                        "metrics.cost_micros": 50_000_000,
                    }
                ],
            ),
        ):
            result = diagnostics.budget_pacing_snapshot(
                "1234567890", "2026-08-01", "2026-08-05"
            )
        campaign = result["campaigns"][0]
        self.assertEqual(campaign["estimated_30_4_day_cap"], 304.0)
        self.assertEqual(campaign["current_budget_pacing_ratio"], 1.0)
        self.assertIn("current daily budget", result["limitation"])

    def test_exception_report_flags_spend_without_conversions(self):
        current = {
            "customer_id": "1234567890",
            "date_range": {"start": "2026-08-01", "end": "2026-08-07"},
            "campaigns": [
                {
                    "campaign.id": 1,
                    "campaign.name": "Search",
                    "cost": 50.0,
                    "metrics.conversions": 0,
                    "platform_cpa": None,
                }
            ],
        }
        previous = {
            "customer_id": "1234567890",
            "date_range": {"start": "2026-07-25", "end": "2026-07-31"},
            "campaigns": [
                {
                    "campaign.id": 1,
                    "campaign.name": "Search",
                    "cost": 25.0,
                    "metrics.conversions": 2,
                    "platform_cpa": 12.5,
                }
            ],
        }
        with patch.object(
            diagnostics,
            "campaign_performance_snapshot",
            side_effect=[current, previous],
        ):
            result = diagnostics.performance_exception_report(
                "1234567890",
                "2026-08-01",
                "2026-08-07",
                "2026-07-25",
                "2026-07-31",
            )
        self.assertIn(
            "SPEND_WITH_ZERO_PLATFORM_CONVERSIONS",
            result["findings"][0]["reason_codes"],
        )
        self.assertEqual(
            result["findings"][0]["decision"],
            "INVESTIGATE_NO_AUTOMATIC_CHANGE",
        )

    def test_exception_report_rejects_unequal_periods(self):
        with patch.dict(os.environ, self.environment, clear=True):
            with self.assertRaisesRegex(ToolError, "same number of days"):
                diagnostics.performance_exception_report(
                    "1234567890",
                    "2026-08-01",
                    "2026-08-07",
                    "2026-07-20",
                    "2026-07-31",
                )

    def test_diagnostics_report_truncation(self):
        rows = [
            {"campaign.id": index, "metrics.cost_micros": 0}
            for index in range(501)
        ]
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(diagnostics, "search", return_value=rows),
        ):
            result = diagnostics.campaign_performance_snapshot(
                "1234567890", "2026-08-01", "2026-08-07"
            )
        self.assertTrue(result["is_truncated"])
        self.assertEqual(len(result["campaigns"]), 500)


class TestDiagnosticSchemas(unittest.IsolatedAsyncioTestCase):
    async def test_all_diagnostics_are_read_only(self):
        tools = await diagnostics.diagnostics_mcp.list_tools()
        self.assertEqual(len(tools), 8)
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
        self.assertTrue(all(tool.annotations.openWorldHint for tool in tools))


if __name__ == "__main__":
    unittest.main()
