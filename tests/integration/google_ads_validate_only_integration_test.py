# Copyright 2026 Google LLC.

"""Live Google Ads test-account validation without applying mutations."""

from __future__ import annotations

import os
import unittest
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ads_mcp.change_set_store import InMemoryChangeSetStore
from ads_mcp.change_sets import (
    reset_change_set_store,
    set_change_set_store_for_testing,
)


@unittest.skipUnless(
    os.environ.get("GOOGLE_ADS_TEST_CUSTOMER_ID"),
    "Google Ads test-account credentials are not configured",
)
class GoogleAdsValidateOnlyIntegrationTest(unittest.TestCase):
    """Discover fixtures and exercise every preview against a test account."""

    @classmethod
    def setUpClass(cls):
        cls.customer_id = os.environ["GOOGLE_ADS_TEST_CUSTOMER_ID"].replace(
            "-", ""
        )
        os.environ.update(
            {
                "GOOGLE_ADS_MCP_READ_CUSTOMER_IDS": cls.customer_id,
                "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": cls.customer_id,
                "GOOGLE_ADS_MCP_ENVIRONMENT": "integration",
                "GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE": "firestore",
                "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
            }
        )

    def setUp(self):
        set_change_set_store_for_testing(InMemoryChangeSetStore())

    def tearDown(self):
        reset_change_set_store()

    def _rows(self, fields, resource, conditions):
        from ads_mcp.tools.search import search

        return search(
            self.customer_id,
            fields,
            resource,
            conditions=conditions,
            limit=50,
        )

    def test_all_guarded_previews_use_live_validate_only(self):
        from ads_mcp.tools import safe_mutations

        campaigns = self._rows(
            ["campaign.id", "campaign.status"],
            "campaign",
            ["campaign.status = 'ENABLED'"],
        )
        self.assertTrue(campaigns, "Test account needs an enabled campaign")
        campaign = campaigns[0]
        campaign_preview = safe_mutations.preview_campaign_status_change(
            self.customer_id,
            str(campaign["campaign.id"]),
            "PAUSED",
            "test-account contract validation",
            "no live change; validate_only",
        )

        ad_groups = self._rows(
            ["ad_group.id", "ad_group.status"],
            "ad_group",
            ["ad_group.status = 'ENABLED'"],
        )
        self.assertTrue(ad_groups, "Test account needs an enabled ad group")
        ad_group = ad_groups[0]
        self.assertEqual(ad_group["ad_group.status"], "ENABLED")
        ad_group_preview = safe_mutations.preview_ad_group_status_change(
            self.customer_id,
            str(ad_group["ad_group.id"]),
            "PAUSED",
            "test-account contract validation",
            "no live change; validate_only",
        )

        budgets = self._rows(
            [
                "campaign_budget.id",
                "campaign_budget.amount_micros",
                "campaign_budget.explicitly_shared",
                "campaign_budget.period",
                "campaign_budget.reference_count",
                "campaign_budget.status",
            ],
            "campaign_budget",
            [
                "campaign_budget.status = 'ENABLED'",
                "campaign_budget.explicitly_shared = FALSE",
                "campaign_budget.period = 'DAILY'",
                "campaign_budget.reference_count = 1",
                "campaign_budget.amount_micros >= 1000000",
                "campaign_budget.status = 'ENABLED'",
            ],
        )
        self.assertTrue(budgets, "Test account needs one unshared daily budget")
        budget = budgets[0]
        new_daily_amount = round(
            int(budget["campaign_budget.amount_micros"]) / 1_000_000 * 0.9,
            2,
        )
        budget_preview = safe_mutations.preview_campaign_budget_change(
            self.customer_id,
            str(budget["campaign_budget.id"]),
            new_daily_amount,
            "test-account contract validation",
            "no live change; validate_only",
        )

        keywords = self._rows(
            [
                "ad_group.id",
                "ad_group_criterion.criterion_id",
                "ad_group_criterion.status",
                "ad_group_criterion.type",
                "ad_group_criterion.negative",
            ],
            "keyword_view",
            [
                "ad_group_criterion.type = 'KEYWORD'",
                "ad_group_criterion.negative = FALSE",
                "ad_group_criterion.status = 'ENABLED'",
            ],
        )
        self.assertTrue(keywords, "Test account needs one keyword criterion")
        keyword = keywords[0]
        keyword_preview = safe_mutations.preview_keyword_status_change(
            self.customer_id,
            str(keyword["ad_group.id"]),
            str(keyword["ad_group_criterion.criterion_id"]),
            "PAUSED",
            "test-account contract validation",
            "no live change; validate_only",
        )
        negative_preview = safe_mutations.preview_add_negative_keyword(
            self.customer_id,
            str(keyword["ad_group.id"]),
            "mcp integration validation phrase",
            "EXACT",
            "test-account contract validation",
            "no live change; validate_only",
        )

        negative_keywords = self._rows(
            [
                "ad_group.id",
                "ad_group_criterion.criterion_id",
                "ad_group_criterion.type",
                "ad_group_criterion.negative",
            ],
            "keyword_view",
            [
                "ad_group_criterion.type = 'KEYWORD'",
                "ad_group_criterion.negative = TRUE",
                "ad_group_criterion.status != 'REMOVED'",
            ],
        )
        self.assertTrue(
            negative_keywords,
            "Test account needs one negative ad-group keyword",
        )
        negative_keyword = negative_keywords[0]
        remove_negative_preview = (
            safe_mutations.preview_remove_negative_keyword(
                self.customer_id,
                str(negative_keyword["ad_group.id"]),
                str(negative_keyword["ad_group_criterion.criterion_id"]),
                "test-account contract validation",
                "no live change; validate_only",
            )
        )

        ads = self._rows(
            [
                "ad_group.id",
                "ad_group_ad.ad.id",
                "ad_group_ad.ad.final_urls",
                "ad_group_ad.status",
                "ad_group_ad.ad.type",
            ],
            "ad_group_ad",
            [
                "ad_group_ad.status IN ('ENABLED','PAUSED')",
                "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
            ],
        )
        ads = [row for row in ads if row["ad_group_ad.ad.final_urls"]]
        self.assertTrue(ads, "Test account needs an ad with a final URL")
        ad = ads[0]
        ad_status_preview = safe_mutations.preview_ad_group_ad_status_change(
            self.customer_id,
            str(ad["ad_group.id"]),
            str(ad["ad_group_ad.ad.id"]),
            "PAUSED" if ad["ad_group_ad.status"] == "ENABLED" else "ENABLED",
            "test-account contract validation",
            "no live change; validate_only",
        )
        current_url = ad["ad_group_ad.ad.final_urls"][0]
        parts = urlsplit(current_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["mcp_validate_only"] = "1"
        proposed_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                "",
            )
        )
        os.environ["GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS"] = (
            parts.hostname or ""
        )
        url_preview = safe_mutations.preview_ad_final_url_change(
            self.customer_id,
            str(ad["ad_group.id"]),
            str(ad["ad_group_ad.ad.id"]),
            proposed_url,
            "test-account contract validation",
            "no live change; validate_only",
        )

        for preview in (
            campaign_preview,
            ad_group_preview,
            budget_preview,
            keyword_preview,
            negative_preview,
            remove_negative_preview,
            ad_status_preview,
            url_preview,
        ):
            self.assertEqual(
                preview["execution_status"], "PREVIEW_ONLY_NOT_APPLIED"
            )
            self.assertEqual(preview["status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
