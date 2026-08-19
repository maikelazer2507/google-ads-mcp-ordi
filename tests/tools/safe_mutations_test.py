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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.change_set_store import InMemoryChangeSetStore
from ads_mcp.change_sets import (
    Principal,
    approve_change_set,
    create_change_set,
    get_change_set_for_review,
    list_change_set_audit_events,
    reset_change_set_store,
    set_change_set_store_for_testing,
)
from ads_mcp.tools import safe_mutations


class TestSafeMutations(unittest.TestCase):
    """Verifies preview/apply separation and spend guardrails."""

    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "1234567890",
            "GOOGLE_ADS_MCP_ENVIRONMENT": "test",
            "GOOGLE_ADS_MCP_APPROVER_EMAILS": "owner@example.com",
            "GOOGLE_ADS_MCP_APPROVER_ROLES": "owner",
            "GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS": "example.com",
            "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
        }
        set_change_set_store_for_testing(InMemoryChangeSetStore())
        self.campaign = {
            "id": "11",
            "name": "Search",
            "resource_name": "customers/1234567890/campaigns/11",
            "status": "ENABLED",
            "campaign_budget": "customers/1234567890/campaignBudgets/22",
            "budget_amount_micros": 10_000_000,
            "budget_total_amount_micros": 0,
            "budget_period": "DAILY",
            "budget_type": "STANDARD",
            "budget_status": "ENABLED",
            "budget_daily_amount": 10.0,
            "budget_estimated_monthly_cap": 304.0,
            "budget_explicitly_shared": False,
            "budget_reference_count": 1,
            "currency_code": "EUR",
            "time_zone": "Europe/Vienna",
            "advertising_channel_type": "SEARCH",
            "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "start_date": "20260101",
            "end_date": "20301231",
            "network_settings": {
                "target_google_search": True,
                "target_search_network": True,
                "target_content_network": False,
                "target_partner_search_network": False,
            },
            "dependent_resources": {
                "campaign_criteria": {"count": 3, "sha256": "criteria"},
                "ad_group_criteria": {"count": 12, "sha256": "keywords"},
                "ads": {"count": 2, "sha256": "ads"},
                "conversion_goals": {"count": 4, "sha256": "goals"},
            },
        }

    def tearDown(self):
        reset_change_set_store()

    def _approve(self, preview):
        approve_change_set(
            preview["change_set_id"],
            expected_payload_hash=preview["payload_hash"],
            expected_customer_id=preview["customer_id"],
            expected_environment=preview["environment"],
            approver=Principal(
                subject="owner-subject",
                email="owner@example.com",
                role="owner",
                authentication_method="google_oidc",
            ),
        )
        return preview["change_set_token"]

    def _budget(self, **overrides):
        budget = {
            "id": "22",
            "name": "Daily",
            "resource_name": "customers/1234567890/campaignBudgets/22",
            "status": "ENABLED",
            "amount_micros": 10_000_000,
            "amount": 10.0,
            "total_amount_micros": 0,
            "period": "DAILY",
            "budget_type": "STANDARD",
            "explicitly_shared": False,
            "reference_count": 1,
            "currency_code": "EUR",
            "time_zone": "Europe/Vienna",
            "campaigns": [
                {
                    "id": "11",
                    "name": "Search",
                    "resource_name": "customers/1234567890/campaigns/11",
                    "status": "ENABLED",
                }
            ],
            "estimated_monthly_cap": 304.0,
        }
        budget.update(overrides)
        return budget

    def _parent_bid_context(self):
        return {
            "campaign_resource_name": "customers/1234567890/campaigns/11",
            "campaign_status": "ENABLED",
            "campaign_bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
            "ad_group_resource_name": "customers/1234567890/adGroups/33",
            "ad_group_status": "ENABLED",
            "ad_group_type": "SEARCH_STANDARD",
            "ad_group_cpc_bid_micros": 1_000_000,
            "ad_group_cpm_bid_micros": 0,
            "ad_group_cpv_bid_micros": 0,
            "ad_group_percent_cpc_bid_micros": 0,
            "ad_group_target_cpa_micros": 0,
            "ad_group_target_cpm_micros": 0,
            "ad_group_target_cpv_micros": 0,
            "ad_group_target_roas": 0.0,
            "ad_group_effective_cpc_bid_micros": 1_000_000,
            "ad_group_effective_target_cpa_micros": 0,
            "ad_group_effective_target_cpa_source": "UNSPECIFIED",
            "ad_group_effective_target_roas": 0.0,
            "ad_group_effective_target_roas_source": "UNSPECIFIED",
            "ad_group_optimized_targeting_enabled": False,
        }

    def _keyword(self, **overrides):
        keyword = {
            "campaign_id": "11",
            "campaign_name": "Search",
            **self._parent_bid_context(),
            "ad_group_id": "33",
            "ad_group_name": "Implants",
            "criterion_id": "44",
            "resource_name": ("customers/1234567890/adGroupCriteria/33~44"),
            "status": "ENABLED",
            "criterion_type": "KEYWORD",
            "negative": False,
            "bid_modifier": 0.0,
            "cpc_bid_micros": 1_000_000,
            "cpm_bid_micros": 0,
            "cpv_bid_micros": 0,
            "percent_cpc_bid_micros": 0,
            "effective_cpc_bid_micros": 1_000_000,
            "effective_cpc_bid_source": "AD_GROUP_CRITERION",
            "effective_cpm_bid_micros": 0,
            "effective_cpm_bid_source": "UNSPECIFIED",
            "effective_cpv_bid_micros": 0,
            "effective_cpv_bid_source": "UNSPECIFIED",
            "text": "implant dentist",
            "match_type": "PHRASE",
        }
        keyword.update(overrides)
        return keyword

    def _ad(self, **overrides):
        ad = {
            "campaign_id": "11",
            "campaign_name": "Search",
            "campaign_resource_name": "customers/1234567890/campaigns/11",
            "campaign_status": "ENABLED",
            "ad_group_id": "33",
            "ad_group_name": "Implants",
            "ad_group_resource_name": "customers/1234567890/adGroups/33",
            "ad_group_status": "ENABLED",
            "ad_id": "55",
            "ad_name": "Implant RSA",
            "ad_type": "RESPONSIVE_SEARCH_AD",
            "resource_name": "customers/1234567890/adGroupAds/33~55",
            "ad_resource_name": "customers/1234567890/ads/55",
            "status": "ENABLED",
            "final_urls": ["https://example.com/old"],
            "final_mobile_urls": [],
            "tracking_url_template": "",
            "final_url_suffix": "",
            "responsive_search_ad": {
                "headlines": [{"text": "Implants"}],
                "descriptions": [{"text": "Book a consultation"}],
                "path1": "implants",
                "path2": "",
            },
        }
        ad.update(overrides)
        if "active_usages" not in overrides:
            ad["active_usages"] = [
                {
                    "campaign_id": ad["campaign_id"],
                    "campaign_name": ad["campaign_name"],
                    "campaign_resource_name": ad["campaign_resource_name"],
                    "campaign_status": ad["campaign_status"],
                    "ad_group_id": ad["ad_group_id"],
                    "ad_group_name": ad["ad_group_name"],
                    "ad_group_resource_name": ad["ad_group_resource_name"],
                    "ad_group_status": ad["ad_group_status"],
                    "resource_name": ad["resource_name"],
                    "status": ad["status"],
                    "ad_id": ad["ad_id"],
                    "ad_name": ad["ad_name"],
                    "ad_type": ad["ad_type"],
                    "final_urls": ad["final_urls"],
                    "final_mobile_urls": ad["final_mobile_urls"],
                    "tracking_url_template": ad["tracking_url_template"],
                    "final_url_suffix": ad["final_url_suffix"],
                    "responsive_search_ad": ad["responsive_search_ad"],
                }
            ]
        ad["active_usages_fingerprint"] = safe_mutations._state_fingerprint(
            ad["active_usages"]
        )
        return ad

    def _ad_group(self, **overrides):
        ad_group = {
            "campaign_id": "11",
            "campaign_name": "Search",
            **self._parent_bid_context(),
            "ad_group_id": "33",
            "ad_group_name": "Implants",
            "dependent_resources": {
                "criteria": {"count": 4, "sha256": "criteria"},
                "ads": {"count": 2, "sha256": "ads"},
            },
        }
        ad_group.update(overrides)
        return ad_group

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
        self.assertIn("Manual reactivation only", preview["rollback"])
        validate.assert_called_once()
        mutate.assert_not_called()

    def test_campaign_preview_rejects_enabling_in_release_one(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_campaign_state") as campaign_state,
            patch.object(
                safe_mutations, "_validate_campaign_status"
            ) as validate,
        ):
            with self.assertRaisesRegex(
                ToolError,
                "manual until full delivery-context test-account validation",
            ):
                safe_mutations.preview_campaign_status_change(
                    "1234567890",
                    "11",
                    "ENABLED",
                    "reactivate",
                    "resume delivery",
                )

        campaign_state.assert_not_called()
        validate.assert_not_called()

    def test_campaign_apply_rejects_legacy_enable_before_mutate(self):
        details = {
            "action": "campaign_status",
            "customer_id": "1234567890",
            "object": {
                "id": "11",
                "resource_name": self.campaign["resource_name"],
            },
            "current": safe_mutations._campaign_drift_snapshot(
                {**self.campaign, "status": "PAUSED"}
            ),
            "proposed": {"status": "ENABLED"},
            "rollback": "legacy value",
        }
        with patch.dict(os.environ, self.environment, clear=True):
            legacy = create_change_set(details)
            token = self._approve(legacy)
            with (
                patch.object(
                    safe_mutations, "_campaign_state"
                ) as campaign_state,
                patch.object(
                    safe_mutations, "_validate_campaign_status"
                ) as validate,
                patch.object(
                    safe_mutations, "_mutate_campaign_status"
                ) as mutate,
            ):
                with self.assertRaisesRegex(
                    ToolError,
                    "manual until full delivery-context test-account validation",
                ):
                    safe_mutations.apply_campaign_status_change(token)

        campaign_state.assert_not_called()
        validate.assert_not_called()
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
            token = self._approve(preview)

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
                patch.object(
                    safe_mutations, "_validate_campaign_status"
                ) as validate,
            ):
                result = safe_mutations.apply_campaign_status_change(token)

        self.assertEqual(result["execution_status"], "APPLIED_AND_VERIFIED")
        self.assertEqual(result["after"], {"status": "PAUSED"})
        validate.assert_called_once_with(
            "1234567890", self.campaign["resource_name"], "PAUSED"
        )
        mutate.assert_called_once()
        success_event = list_change_set_audit_events(preview["change_set_id"])[
            -1
        ]
        self.assertEqual(success_event["event_type"], "SUCCEEDED")
        verification = success_event["metadata"]["verification"]
        self.assertEqual(
            verification["resource_name"], self.campaign["resource_name"]
        )
        self.assertEqual(verification["target_field"], "status")
        self.assertEqual(verification["target_value"], "PAUSED")
        self.assertRegex(verification["verified_after_hash"], r"^[0-9a-f]{64}$")

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
            token = self._approve(preview)

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
                        token,
                    )
                mutate.assert_not_called()
            self.assertEqual(
                list_change_set_audit_events(preview["change_set_id"])[-1][
                    "event_type"
                ],
                "FAILED",
            )

    def test_wrong_apply_action_does_not_consume_approval(self):
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
            token = self._approve(preview)

            with patch.object(safe_mutations, "_budget_state") as budget_state:
                with self.assertRaisesRegex(
                    ToolError, "different apply action"
                ):
                    safe_mutations.apply_campaign_budget_change(token)
                budget_state.assert_not_called()

            review = get_change_set_for_review(preview["change_set_id"])
            self.assertEqual(review["status"], "APPROVED")

    def test_apply_aborts_when_material_campaign_context_drifted(self):
        drifted = {
            **self.campaign,
            "dependent_resources": {
                **self.campaign["dependent_resources"],
                "ads": {"count": 3, "sha256": "changed-ads"},
            },
        }
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
            token = self._approve(preview)

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
                        token,
                    )
                mutate.assert_not_called()

    def test_shared_budget_is_blocked(self):
        budget = self._budget(
            name="Shared", explicitly_shared=True, reference_count=2
        )
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "Shared budgets"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 9.0, "test", "test"
                )

    def test_budget_increase_above_ten_percent_is_blocked(self):
        budget = self._budget()
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "above 10 percent"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 11.01, "test", "test"
                )

    def test_non_daily_budget_is_blocked(self):
        budget = self._budget(period="CUSTOM_PERIOD")
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "Only DAILY"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 9.0, "test", "test"
                )

    def test_non_enabled_budget_is_blocked(self):
        budget = self._budget(status="REMOVED")
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_budget_state", return_value=budget),
        ):
            with self.assertRaisesRegex(ToolError, "Only ENABLED"):
                safe_mutations.preview_campaign_budget_change(
                    "1234567890", "22", 9.0, "test", "test"
                )

    def test_apply_budget_aborts_if_budget_became_shared(self):
        current = self._budget()
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
            token = self._approve(preview)

            with (
                patch.object(
                    safe_mutations, "_budget_state", return_value=shared
                ),
                patch.object(safe_mutations, "_mutate_budget") as mutate,
            ):
                with self.assertRaisesRegex(ToolError, "now shared"):
                    safe_mutations.apply_campaign_budget_change(
                        token,
                    )
                mutate.assert_not_called()

    def test_budget_preview_includes_owner_currency_and_monthly_cap(self):
        budget = self._budget()
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_budget_state", return_value=budget
                ),
                patch.object(safe_mutations, "_validate_budget"),
            ):
                preview = safe_mutations.preview_campaign_budget_change(
                    "1234567890",
                    "22",
                    9.0,
                    "control spend",
                    "less spend",
                )

        self.assertEqual(preview["object"]["campaigns"][0]["name"], "Search")
        self.assertEqual(preview["proposed"]["currency_code"], "EUR")
        self.assertEqual(preview["proposed"]["time_zone"], "Europe/Vienna")
        self.assertEqual(preview["proposed"]["estimated_monthly_cap"], 273.6)

    def test_apply_budget_runs_validate_only_immediately_before_mutation(self):
        current = self._budget()
        after = self._budget(
            amount_micros=9_000_000,
            amount=9.0,
            estimated_monthly_cap=273.6,
        )
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_budget_state", return_value=current
                ),
                patch.object(safe_mutations, "_validate_budget"),
            ):
                preview = safe_mutations.preview_campaign_budget_change(
                    "1234567890",
                    "22",
                    9.0,
                    "control spend",
                    "less spend",
                )
            token = self._approve(preview)

            with (
                patch.object(
                    safe_mutations,
                    "_budget_state",
                    side_effect=[current, after],
                ),
                patch.object(safe_mutations, "_validate_budget") as validate,
                patch.object(
                    safe_mutations,
                    "_mutate_budget",
                    return_value=current["resource_name"],
                ) as mutate,
            ):
                result = safe_mutations.apply_campaign_budget_change(token)

        validate.assert_called_once_with(
            "1234567890", current["resource_name"], 9_000_000
        )
        mutate.assert_called_once()
        self.assertEqual(result["after"]["amount"], 9.0)

    def test_budget_post_read_non_target_drift_is_uncertain(self):
        current = self._budget()
        after = self._budget(
            amount_micros=9_000_000,
            amount=9.0,
            estimated_monthly_cap=273.6,
            status="REMOVED",
        )
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
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_budget_state",
                    side_effect=[current, after],
                ),
                patch.object(safe_mutations, "_validate_budget"),
                patch.object(
                    safe_mutations,
                    "_mutate_budget",
                    return_value=current["resource_name"],
                ),
            ):
                with self.assertRaisesRegex(ToolError, "Do not retry"):
                    safe_mutations.apply_campaign_budget_change(token)

        self.assertEqual(
            list_change_set_audit_events(preview["change_set_id"])[-1][
                "event_type"
            ],
            "UNCERTAIN",
        )

    def test_non_keyword_criterion_is_blocked_before_validation(self):
        criterion = self._keyword(criterion_type="PLACEMENT", text="")
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                safe_mutations, "_keyword_state", return_value=criterion
            ),
            patch.object(
                safe_mutations, "_validate_keyword_status"
            ) as validate,
        ):
            with self.assertRaisesRegex(ToolError, "not a KEYWORD"):
                safe_mutations.preview_keyword_status_change(
                    "1234567890",
                    "33",
                    "44",
                    "PAUSED",
                    "safety",
                    "no change",
                )
        validate.assert_not_called()

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
            with self.assertRaisesRegex(ToolError, "credentials"):
                safe_mutations._validate_final_url(
                    "https://user:secret@example.com/book"
                )
            with self.assertRaisesRegex(ToolError, "non-443 port"):
                safe_mutations._validate_final_url(
                    "https://example.com:8443/book"
                )
            with self.assertRaisesRegex(ToolError, "fragment"):
                safe_mutations._validate_final_url(
                    "https://example.com/book#private"
                )
            self.assertEqual(
                safe_mutations._validate_final_url(
                    " HTTPS://EXAMPLE.COM.:443/book?q=1 "
                ),
                "https://example.com/book?q=1",
            )

    def test_final_url_uses_ad_service_ad_operation_and_ad_resource(self):
        operation = SimpleNamespace(
            update=SimpleNamespace(resource_name="", final_urls=[]),
            update_mask=SimpleNamespace(paths=[]),
        )
        response = SimpleNamespace(
            results=[
                SimpleNamespace(resource_name="customers/1234567890/ads/55")
            ]
        )

        class AdService:
            def __init__(self):
                self.calls = []

            def mutate_ads(self, **kwargs):
                self.calls.append(kwargs)
                return response

        service = AdService()
        with (
            patch.object(
                safe_mutations.utils,
                "get_googleads_service",
                return_value=service,
            ) as get_service,
            patch.object(
                safe_mutations.utils,
                "get_googleads_type",
                return_value=operation,
            ) as get_type,
        ):
            resource_name = safe_mutations._mutate_final_url(
                "1234567890",
                "customers/1234567890/ads/55",
                "https://example.com/book",
                validate_only=False,
            )

        get_service.assert_called_once_with("AdService")
        get_type.assert_called_once_with("AdOperation")
        self.assertEqual(
            operation.update.resource_name, "customers/1234567890/ads/55"
        )
        self.assertEqual(
            operation.update.final_urls, ["https://example.com/book"]
        )
        self.assertEqual(operation.update_mask.paths, ["final_urls"])
        self.assertEqual(service.calls[0]["validate_only"], False)
        self.assertIsNone(service.calls[0]["retry"])
        self.assertEqual(
            service.calls[0]["timeout"], safe_mutations._MUTATE_TIMEOUT_SECONDS
        )
        self.assertEqual(resource_name, "customers/1234567890/ads/55")

    def test_final_url_preview_validates_canonical_ad_resource(self):
        ad = self._ad()
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                safe_mutations, "_ad_final_url_state", return_value=ad
            ),
            patch.object(
                safe_mutations, "_mutate_final_url", return_value=None
            ) as mutate,
        ):
            preview = safe_mutations.preview_ad_final_url_change(
                "1234567890",
                "33",
                "55",
                "https://example.com/book",
                "better landing page",
                "more relevant visits",
            )

        mutate.assert_called_once_with(
            "1234567890",
            "customers/1234567890/ads/55",
            "https://example.com/book",
            validate_only=True,
        )
        self.assertEqual(
            preview["object"]["ad_resource_name"],
            "customers/1234567890/ads/55",
        )

    def test_final_url_blocks_unsupported_ad_type_and_shared_global_ad(self):
        unsupported = self._ad(ad_type="IMAGE_AD")
        with (
            patch.object(safe_mutations, "_ad_state", return_value=unsupported),
            patch.object(
                safe_mutations,
                "_active_ad_usages",
                return_value=unsupported["active_usages"],
            ) as usages,
        ):
            with self.assertRaisesRegex(ToolError, "not allowlisted"):
                safe_mutations._ad_final_url_state("1234567890", "33", "55")
            usages.assert_called_once()

        ad = self._ad()
        second_usage = {
            **ad["active_usages"][0],
            "ad_group_id": "34",
            "ad_group_name": "Shared",
            "ad_group_resource_name": "customers/1234567890/adGroups/34",
            "resource_name": "customers/1234567890/adGroupAds/34~55",
        }
        with (
            patch.object(safe_mutations, "_ad_state", return_value=ad),
            patch.object(
                safe_mutations,
                "_active_ad_usages",
                return_value=[ad["active_usages"][0], second_usage],
            ),
        ):
            with self.assertRaisesRegex(ToolError, "more than one non-removed"):
                safe_mutations._ad_final_url_state("1234567890", "33", "55")

    def test_final_url_state_fingerprints_every_non_removed_usage(self):
        ad = self._ad()
        usages = ad["active_usages"]
        with (
            patch.object(safe_mutations, "_ad_state", return_value=ad),
            patch.object(
                safe_mutations, "_active_ad_usages", return_value=usages
            ),
        ):
            state = safe_mutations._ad_final_url_state("1234567890", "33", "55")
        self.assertEqual(state["active_usages"], usages)
        self.assertEqual(
            state["active_usages_fingerprint"],
            safe_mutations._state_fingerprint(usages),
        )

    def test_final_url_usage_query_is_global_and_excludes_removed_rows(self):
        service = MagicMock()
        service.search.return_value = []
        with patch.object(
            safe_mutations.utils, "get_googleads_service", return_value=service
        ):
            self.assertEqual(
                safe_mutations._active_ad_usages("1234567890", "55"), []
            )
        query = service.search.call_args.kwargs["query"]
        self.assertIn("ad_group_ad.ad.id = 55", query)
        self.assertIn("ad_group_ad.status != REMOVED", query)
        self.assertNotIn("ad_group.id =", query)

    def test_final_url_apply_revalidates_before_real_mutation(self):
        before = self._ad()
        after = self._ad(final_urls=["https://example.com/book"])
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_ad_final_url_state", return_value=before
                ),
                patch.object(safe_mutations, "_mutate_final_url"),
            ):
                preview = safe_mutations.preview_ad_final_url_change(
                    "1234567890",
                    "33",
                    "55",
                    "https://example.com/book",
                    "better landing page",
                    "more relevant visits",
                )
            token = self._approve(preview)

            with (
                patch.object(
                    safe_mutations,
                    "_ad_final_url_state",
                    side_effect=[before, after],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_final_url",
                    side_effect=[None, before["ad_resource_name"]],
                ) as mutate,
            ):
                result = safe_mutations.apply_ad_final_url_change(token)

        self.assertEqual(mutate.call_count, 2)
        self.assertTrue(mutate.call_args_list[0].kwargs["validate_only"])
        self.assertFalse(mutate.call_args_list[1].kwargs["validate_only"])
        for call in mutate.call_args_list:
            self.assertEqual(call.args[1], "customers/1234567890/ads/55")
        self.assertEqual(
            result["after"]["final_urls"], ["https://example.com/book"]
        )

    def test_unverified_post_mutation_state_is_recorded_uncertain(self):
        before = self._ad()
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_ad_final_url_state", return_value=before
                ),
                patch.object(safe_mutations, "_mutate_final_url"),
            ):
                preview = safe_mutations.preview_ad_final_url_change(
                    "1234567890",
                    "33",
                    "55",
                    "https://example.com/book",
                    "better landing page",
                    "more relevant visits",
                )
            token = self._approve(preview)

            with (
                patch.object(
                    safe_mutations,
                    "_ad_final_url_state",
                    side_effect=[before, before],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_final_url",
                    side_effect=[None, before["ad_resource_name"]],
                ),
            ):
                with self.assertRaisesRegex(ToolError, "Do not retry"):
                    safe_mutations.apply_ad_final_url_change(token)

            self.assertEqual(
                list_change_set_audit_events(preview["change_set_id"])[-1][
                    "event_type"
                ],
                "UNCERTAIN",
            )

    def test_final_url_concurrent_second_usage_is_recorded_uncertain(self):
        before = self._ad()
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_ad_final_url_state", return_value=before
                ),
                patch.object(safe_mutations, "_mutate_final_url"),
            ):
                preview = safe_mutations.preview_ad_final_url_change(
                    "1234567890",
                    "33",
                    "55",
                    "https://example.com/book",
                    "better landing page",
                    "more relevant visits",
                )
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_ad_final_url_state",
                    side_effect=[
                        before,
                        ToolError("global ad now has a second usage"),
                    ],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_final_url",
                    side_effect=[None, before["ad_resource_name"]],
                ),
            ):
                with self.assertRaisesRegex(ToolError, "Do not retry"):
                    safe_mutations.apply_ad_final_url_change(token)

        self.assertEqual(
            list_change_set_audit_events(preview["change_set_id"])[-1][
                "event_type"
            ],
            "UNCERTAIN",
        )

    def test_google_ads_mutate_rejection_is_recorded_failed(self):
        mock_error = MagicMock()
        mock_error.message = "Rejected by Google Ads"
        mock_failure = MagicMock()
        mock_failure.errors = [mock_error]
        rejection = GoogleAdsException(
            MagicMock(), MagicMock(), MagicMock(), "request-id"
        )
        rejection.failure = mock_failure

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
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    return_value=self.campaign,
                ),
                patch.object(safe_mutations, "_validate_campaign_status"),
                patch.object(
                    safe_mutations,
                    "_mutate_campaign_status",
                    side_effect=rejection,
                ),
            ):
                with self.assertRaises(GoogleAdsException):
                    safe_mutations.apply_campaign_status_change(token)

        self.assertEqual(
            list_change_set_audit_events(preview["change_set_id"])[-1][
                "event_type"
            ],
            "FAILED",
        )

    def test_timeout_during_mutate_is_recorded_uncertain(self):
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
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_campaign_state",
                    return_value=self.campaign,
                ),
                patch.object(safe_mutations, "_validate_campaign_status"),
                patch.object(
                    safe_mutations,
                    "_mutate_campaign_status",
                    side_effect=TimeoutError("deadline"),
                ),
            ):
                with self.assertRaisesRegex(ToolError, "Do not retry"):
                    safe_mutations.apply_campaign_status_change(token)

        self.assertEqual(
            list_change_set_audit_events(preview["change_set_id"])[-1][
                "event_type"
            ],
            "UNCERTAIN",
        )

    def test_unallowlisted_operator_is_rejected_before_preview_reads(self):
        environment = {
            **self.environment,
            "GOOGLE_ADS_MCP_OPERATOR_EMAILS": "operator@example.com",
        }
        token = SimpleNamespace(
            claims={"email": "intruder@example.com", "sub": "intruder-subject"},
            subject="intruder-subject",
            client_id="client",
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=token,
            ),
            patch.object(safe_mutations, "_campaign_state") as campaign_state,
        ):
            with self.assertRaisesRegex(
                ToolError, "not authorized as an operator"
            ):
                safe_mutations.preview_campaign_status_change(
                    "1234567890",
                    "11",
                    "PAUSED",
                    "control spend",
                    "less spend",
                )
        campaign_state.assert_not_called()

    def test_unallowlisted_operator_is_rejected_before_apply_consumes_token(
        self,
    ):
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
            token = self._approve(preview)

        environment = {
            **self.environment,
            "GOOGLE_ADS_MCP_OPERATOR_EMAILS": "operator@example.com",
        }
        identity = SimpleNamespace(
            claims={"email": "intruder@example.com", "sub": "intruder-subject"},
            subject="intruder-subject",
            client_id="client",
        )
        with (
            patch.dict(os.environ, environment, clear=True),
            patch(
                "fastmcp.server.dependencies.get_access_token",
                return_value=identity,
            ),
            patch.object(safe_mutations, "_campaign_state") as campaign_state,
        ):
            with self.assertRaisesRegex(
                ToolError, "not authorized as an operator"
            ):
                safe_mutations.apply_campaign_status_change(token)

        campaign_state.assert_not_called()
        self.assertEqual(
            get_change_set_for_review(preview["change_set_id"])["status"],
            "APPROVED",
        )

    def test_ad_group_status_preview_apply_and_dependent_drift_guard(self):
        before = self._ad_group()
        after = self._ad_group(ad_group_status="PAUSED")
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_ad_group_state", return_value=before
                ),
                patch.object(
                    safe_mutations, "_mutate_ad_group_status", return_value=None
                ) as preview_mutate,
            ):
                preview = safe_mutations.preview_ad_group_status_change(
                    "1234567890",
                    "33",
                    "PAUSED",
                    "pause weak group",
                    "less waste",
                )
            preview_mutate.assert_called_once_with(
                "1234567890",
                before["ad_group_resource_name"],
                "PAUSED",
                validate_only=True,
            )
            self.assertIn("Manual reactivation only", preview["rollback"])
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_ad_group_state",
                    side_effect=[before, after],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_ad_group_status",
                    side_effect=[None, before["ad_group_resource_name"]],
                ) as mutate,
            ):
                result = safe_mutations.apply_ad_group_status_change(token)

        self.assertEqual(
            [call.kwargs["validate_only"] for call in mutate.call_args_list],
            [True, False],
        )
        self.assertEqual(result["after"], {"status": "PAUSED"})
        self.assertIn("Manual reactivation only", result["rollback"])

    def test_ad_group_preview_rejects_enabling_in_release_one(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(safe_mutations, "_ad_group_state") as ad_group_state,
            patch.object(safe_mutations, "_mutate_ad_group_status") as mutate,
        ):
            with self.assertRaisesRegex(
                ToolError,
                "manual until full delivery-context test-account validation",
            ):
                safe_mutations.preview_ad_group_status_change(
                    "1234567890",
                    "33",
                    "ENABLED",
                    "reactivate",
                    "resume delivery",
                )

        ad_group_state.assert_not_called()
        mutate.assert_not_called()

    def test_ad_group_apply_rejects_legacy_enable_before_mutate(self):
        before = self._ad_group(ad_group_status="PAUSED")
        details = {
            "action": "ad_group_status",
            "customer_id": "1234567890",
            "object": {
                "ad_group_id": "33",
                "resource_name": before["ad_group_resource_name"],
            },
            "current": safe_mutations._ad_group_drift_snapshot(before),
            "proposed": {"status": "ENABLED"},
            "rollback": "legacy value",
        }
        with patch.dict(os.environ, self.environment, clear=True):
            legacy = create_change_set(details)
            token = self._approve(legacy)
            with (
                patch.object(
                    safe_mutations, "_ad_group_state"
                ) as ad_group_state,
                patch.object(
                    safe_mutations, "_mutate_ad_group_status"
                ) as mutate,
            ):
                with self.assertRaisesRegex(
                    ToolError,
                    "manual until full delivery-context test-account validation",
                ):
                    safe_mutations.apply_ad_group_status_change(token)

        ad_group_state.assert_not_called()
        mutate.assert_not_called()

    def test_ad_group_status_apply_blocks_child_resource_drift(self):
        before = self._ad_group()
        drifted = self._ad_group(
            dependent_resources={
                **before["dependent_resources"],
                "ads": {"count": 3, "sha256": "changed"},
            }
        )
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_ad_group_state", return_value=before
                ),
                patch.object(safe_mutations, "_mutate_ad_group_status"),
            ):
                preview = safe_mutations.preview_ad_group_status_change(
                    "1234567890", "33", "PAUSED", "test", "test"
                )
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations, "_ad_group_state", return_value=drifted
                ),
                patch.object(
                    safe_mutations, "_mutate_ad_group_status"
                ) as mutate,
            ):
                with self.assertRaisesRegex(ToolError, "state changed"):
                    safe_mutations.apply_ad_group_status_change(token)
                mutate.assert_not_called()

    def test_ad_group_ad_status_preview_and_apply(self):
        before = self._ad()
        after = self._ad(status="PAUSED")
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(safe_mutations, "_ad_state", return_value=before),
                patch.object(
                    safe_mutations,
                    "_mutate_ad_group_ad_status",
                    return_value=None,
                ),
            ):
                preview = safe_mutations.preview_ad_group_ad_status_change(
                    "1234567890",
                    "33",
                    "55",
                    "PAUSED",
                    "pause weak ad",
                    "less waste",
                )
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations, "_ad_state", side_effect=[before, after]
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_ad_group_ad_status",
                    side_effect=[None, before["resource_name"]],
                ) as mutate,
            ):
                result = safe_mutations.apply_ad_group_ad_status_change(token)

        self.assertEqual(
            [call.kwargs["validate_only"] for call in mutate.call_args_list],
            [True, False],
        )
        self.assertEqual(result["after"], {"status": "PAUSED"})

    def test_remove_negative_keyword_preview_and_apply(self):
        negative = self._keyword(
            negative=True, text="free dentist", match_type="PHRASE"
        )
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations, "_keyword_state", return_value=negative
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_remove_negative_keyword",
                    return_value=None,
                ),
            ):
                preview = safe_mutations.preview_remove_negative_keyword(
                    "1234567890",
                    "33",
                    "44",
                    "negative is too broad",
                    "restore relevant demand",
                )
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations, "_keyword_state", return_value=negative
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_remove_negative_keyword",
                    side_effect=[None, negative["resource_name"]],
                ) as mutate,
                patch.object(
                    safe_mutations,
                    "_criterion_is_removed_or_absent",
                    return_value=True,
                ),
                patch.object(
                    safe_mutations,
                    "_ad_group_state",
                    return_value=self._ad_group(),
                ),
            ):
                result = safe_mutations.apply_remove_negative_keyword(token)

        self.assertEqual(
            [call.kwargs["validate_only"] for call in mutate.call_args_list],
            [True, False],
        )
        self.assertEqual(result["after"], {"exists": False})
        self.assertEqual(result["rollback"]["action"], "add_negative_keyword")
        self.assertTrue(result["rollback"]["requires_new_approval"])

    def test_removed_negative_is_ignored_and_can_be_readded(self):
        service = MagicMock()
        service.search.return_value = []
        with patch.object(
            safe_mutations.utils, "get_googleads_service", return_value=service
        ):
            self.assertFalse(
                safe_mutations._negative_keyword_exists(
                    "1234567890", "33", "free dentist", "PHRASE"
                )
            )
        query = service.search.call_args.kwargs["query"]
        self.assertIn("ad_group_criterion.status != REMOVED", query)

        parent = self._ad_group()
        created_resource = "customers/1234567890/adGroupCriteria/33~99"
        with patch.dict(os.environ, self.environment, clear=True):
            with (
                patch.object(
                    safe_mutations,
                    "_negative_keyword_exists",
                    return_value=False,
                ),
                patch.object(
                    safe_mutations, "_ad_group_state", return_value=parent
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_negative_keyword",
                    return_value=None,
                ),
            ):
                preview = safe_mutations.preview_add_negative_keyword(
                    "1234567890",
                    "33",
                    "free dentist",
                    "PHRASE",
                    "restore removed exclusion",
                    "exclude the phrase again",
                )
            token = self._approve(preview)
            with (
                patch.object(
                    safe_mutations,
                    "_negative_keyword_exists",
                    side_effect=[False, True],
                ),
                patch.object(
                    safe_mutations,
                    "_ad_group_state",
                    side_effect=[parent, parent],
                ),
                patch.object(
                    safe_mutations,
                    "_mutate_negative_keyword",
                    side_effect=[None, created_resource],
                ),
            ):
                result = safe_mutations.apply_add_negative_keyword(token)

        self.assertEqual(result["resource_name"], created_resource)
        self.assertTrue(result["after"]["exists"])

    def test_remove_verification_accepts_removed_or_absent_only(self):
        service = MagicMock()
        service.search.return_value = []
        with patch.object(
            safe_mutations.utils, "get_googleads_service", return_value=service
        ):
            self.assertTrue(
                safe_mutations._criterion_is_removed_or_absent(
                    "1234567890",
                    "customers/1234567890/adGroupCriteria/33~44",
                )
            )
        self.assertIn(
            "ad_group_criterion.status != REMOVED",
            service.search.call_args.kwargs["query"],
        )

        service.search.return_value = [SimpleNamespace()]
        with patch.object(
            safe_mutations.utils, "get_googleads_service", return_value=service
        ):
            self.assertFalse(
                safe_mutations._criterion_is_removed_or_absent(
                    "1234567890",
                    "customers/1234567890/adGroupCriteria/33~44",
                )
            )

    def test_remove_negative_keyword_rejects_non_keyword_or_positive(self):
        with patch.dict(os.environ, self.environment, clear=True):
            for criterion, message in (
                (self._keyword(criterion_type="PLACEMENT"), "not a KEYWORD"),
                (self._keyword(negative=False), "not negative"),
            ):
                with (
                    self.subTest(message=message),
                    patch.object(
                        safe_mutations, "_keyword_state", return_value=criterion
                    ),
                    patch.object(
                        safe_mutations, "_mutate_remove_negative_keyword"
                    ) as mutate,
                ):
                    with self.assertRaisesRegex(ToolError, message):
                        safe_mutations.preview_remove_negative_keyword(
                            "1234567890", "33", "44", "test", "test"
                        )
                    mutate.assert_not_called()

    def test_new_mutations_use_semantically_correct_services_and_operations(
        self,
    ):
        cases = (
            (
                "AdGroupService",
                "AdGroupOperation",
                "mutate_ad_groups",
                safe_mutations._mutate_ad_group_status,
                ("1234567890", "customers/1234567890/adGroups/33", "PAUSED"),
            ),
            (
                "AdGroupAdService",
                "AdGroupAdOperation",
                "mutate_ad_group_ads",
                safe_mutations._mutate_ad_group_ad_status,
                (
                    "1234567890",
                    "customers/1234567890/adGroupAds/33~55",
                    "PAUSED",
                ),
            ),
        )
        for service_name, operation_name, method_name, mutation, args in cases:
            with self.subTest(service=service_name):
                operation = SimpleNamespace(
                    update=SimpleNamespace(resource_name="", status=""),
                    update_mask=SimpleNamespace(paths=[]),
                )
                service = MagicMock()
                getattr(service, method_name).return_value = SimpleNamespace(
                    results=[]
                )
                with (
                    patch.object(
                        safe_mutations.utils,
                        "get_googleads_service",
                        return_value=service,
                    ) as get_service,
                    patch.object(
                        safe_mutations.utils,
                        "get_googleads_type",
                        return_value=operation,
                    ) as get_type,
                ):
                    mutation(*args, validate_only=True)
                get_service.assert_called_once_with(service_name)
                get_type.assert_called_once_with(operation_name)
                getattr(service, method_name).assert_called_once()
                call_kwargs = getattr(service, method_name).call_args.kwargs
                self.assertIsNone(call_kwargs["retry"])
                self.assertEqual(
                    call_kwargs["timeout"],
                    safe_mutations._MUTATE_TIMEOUT_SECONDS,
                )
                self.assertEqual(operation.update.resource_name, args[1])
                self.assertEqual(operation.update.status, "PAUSED")
                self.assertEqual(operation.update_mask.paths, ["status"])

        operation = SimpleNamespace(remove="")
        service = MagicMock()
        service.mutate_ad_group_criteria.return_value = SimpleNamespace(
            results=[]
        )
        with (
            patch.object(
                safe_mutations.utils,
                "get_googleads_service",
                return_value=service,
            ) as get_service,
            patch.object(
                safe_mutations.utils,
                "get_googleads_type",
                return_value=operation,
            ) as get_type,
        ):
            safe_mutations._mutate_remove_negative_keyword(
                "1234567890",
                "customers/1234567890/adGroupCriteria/33~44",
                validate_only=True,
            )
        get_service.assert_called_once_with("AdGroupCriterionService")
        get_type.assert_called_once_with("AdGroupCriterionOperation")
        self.assertEqual(
            operation.remove, "customers/1234567890/adGroupCriteria/33~44"
        )
        service.mutate_ad_group_criteria.assert_called_once()
        remove_kwargs = service.mutate_ad_group_criteria.call_args.kwargs
        self.assertIsNone(remove_kwargs["retry"])
        self.assertEqual(
            remove_kwargs["timeout"], safe_mutations._MUTATE_TIMEOUT_SECONDS
        )

    def test_every_mutate_rpc_disables_transport_retry_and_sets_timeout(self):
        service = MagicMock()
        response = SimpleNamespace(
            results=[SimpleNamespace(resource_name="customers/1234567890/x/1")]
        )
        for method_name in (
            "mutate_campaigns",
            "mutate_campaign_budgets",
            "mutate_ad_group_criteria",
            "mutate_ads",
            "mutate_ad_groups",
            "mutate_ad_group_ads",
        ):
            getattr(service, method_name).return_value = response

        def operation():
            return SimpleNamespace(
                update=SimpleNamespace(
                    resource_name="", status="", amount_micros=0, final_urls=[]
                ),
                create=SimpleNamespace(
                    ad_group="",
                    status="",
                    negative=False,
                    keyword=SimpleNamespace(text="", match_type=""),
                ),
                update_mask=SimpleNamespace(paths=[]),
                remove="",
            )

        with (
            patch.object(
                safe_mutations.utils,
                "get_googleads_service",
                return_value=service,
            ),
            patch.object(
                safe_mutations.utils,
                "get_googleads_type",
                side_effect=lambda _name: operation(),
            ),
        ):
            safe_mutations._validate_campaign_status(
                "1234567890", "r", "PAUSED"
            )
            safe_mutations._mutate_campaign_status("1234567890", "r", "PAUSED")
            safe_mutations._validate_budget("1234567890", "r", 1_000_000)
            safe_mutations._mutate_budget("1234567890", "r", 1_000_000)
            safe_mutations._validate_keyword_status("1234567890", "r", "PAUSED")
            safe_mutations._mutate_keyword_status("1234567890", "r", "PAUSED")
            safe_mutations._mutate_negative_keyword(
                "1234567890", "33", "free", "EXACT", validate_only=True
            )
            safe_mutations._mutate_final_url(
                "1234567890",
                "r",
                "https://example.com/",
                validate_only=True,
            )
            safe_mutations._mutate_ad_group_status(
                "1234567890", "r", "PAUSED", validate_only=True
            )
            safe_mutations._mutate_ad_group_ad_status(
                "1234567890", "r", "PAUSED", validate_only=True
            )
            safe_mutations._mutate_remove_negative_keyword(
                "1234567890", "r", validate_only=True
            )

        calls = []
        for method_name in (
            "mutate_campaigns",
            "mutate_campaign_budgets",
            "mutate_ad_group_criteria",
            "mutate_ads",
            "mutate_ad_groups",
            "mutate_ad_group_ads",
        ):
            calls.extend(getattr(service, method_name).call_args_list)
        self.assertEqual(len(calls), 11)
        for call in calls:
            self.assertIsNone(call.kwargs["retry"])
            self.assertEqual(
                call.kwargs["timeout"], safe_mutations._MUTATE_TIMEOUT_SECONDS
            )

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
        self.assertEqual(len(by_name), 16)
        self.assertIn("preview_campaign_budget_change", by_name)
        self.assertIn("apply_campaign_budget_change", by_name)
        self.assertIn("preview_ad_group_status_change", by_name)
        self.assertIn("apply_ad_group_status_change", by_name)
        self.assertIn("preview_ad_group_ad_status_change", by_name)
        self.assertIn("apply_ad_group_ad_status_change", by_name)
        self.assertIn("preview_remove_negative_keyword", by_name)
        self.assertIn("apply_remove_negative_keyword", by_name)
        preview_annotations = by_name[
            "preview_campaign_budget_change"
        ].annotations
        self.assertFalse(preview_annotations.readOnlyHint)
        self.assertFalse(preview_annotations.destructiveHint)
        self.assertFalse(preview_annotations.idempotentHint)
        self.assertTrue(preview_annotations.openWorldHint)
        apply_annotations = by_name["apply_campaign_budget_change"].annotations
        self.assertEqual(
            set(
                by_name["apply_campaign_budget_change"].parameters["properties"]
            ),
            {"change_set_token"},
        )
        self.assertFalse(apply_annotations.readOnlyHint)
        self.assertTrue(apply_annotations.destructiveHint)
        self.assertTrue(apply_annotations.openWorldHint)
        self.assertFalse(apply_annotations.idempotentHint)


if __name__ == "__main__":
    unittest.main()
