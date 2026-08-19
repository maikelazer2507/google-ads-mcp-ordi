# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Guarded Google Ads mutations with preview, approval, and drift checks."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from google.ads.googleads.errors import GoogleAdsException
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.access_policy import current_operator
from ads_mcp.change_sets import (
    Principal,
    create_change_set,
    normalize_id,
    record_change_set_failure,
    record_change_set_success,
    record_change_set_uncertain,
    require_allowed_customer,
    verify_change_set,
)

changes_mcp = FastMCP("changes")

_PREVIEW_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_APPLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

_AVERAGE_DAYS_PER_MONTH = 30.4
_MUTATE_TIMEOUT_SECONDS = 30.0
_MUTABLE_FINAL_URL_AD_TYPES = frozenset({"RESPONSIVE_SEARCH_AD"})
_RELEASE_ONE_PAUSE_ONLY_ERROR = (
    "Release 1 only supports new_status=PAUSED; enabling remains manual until "
    "full delivery-context test-account validation."
)
_MANUAL_STATUS_REACTIVATION = (
    "Manual reactivation only: enabling remains manual until full "
    "delivery-context test-account validation or a future separately "
    "reviewed workflow."
)


class _MutationOutcomeUncertain(RuntimeError):
    """Signals that a live mutation must be reconciled before any retry."""


def _require_release_one_pause_target(value: object) -> str:
    if value != "PAUSED":
        raise ToolError(_RELEASE_ONE_PAUSE_ONLY_ERROR)
    return "PAUSED"


def _perform_live_mutation(mutation: Callable[[], Any], label: str) -> Any:
    """Runs a mutate call and distinguishes API rejection from ambiguity."""
    try:
        return mutation()
    except GoogleAdsException:
        # Google Ads rejected the mutate request; no change was applied.
        raise
    except Exception as exc:
        # Transport, timeout, or client interruption can occur after the API
        # accepted the request, so the live outcome must be reconciled.
        raise _MutationOutcomeUncertain(
            f"{label} mutation outcome requires live reconciliation."
        ) from exc


def _read_after_live_mutation(read: Callable[[], Any], label: str) -> Any:
    """Runs post-mutation verification; every read failure is ambiguous."""
    try:
        return read()
    except Exception as exc:
        raise _MutationOutcomeUncertain(
            f"{label} mutation completed but live verification could not be read."
        ) from exc


def _assert_post_read_context(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    target_fields: frozenset[str],
    label: str,
) -> None:
    """Rejects a success claim when the immediate read shows other drift."""
    before_context = {
        key: value for key, value in before.items() if key not in target_fields
    }
    after_context = {
        key: value for key, value in after.items() if key not in target_fields
    }
    if before_context != after_context:
        raise _MutationOutcomeUncertain(
            f"{label} target matched, but protected non-target state changed "
            "during execution."
        )


def _verification_scope() -> dict[str, str]:
    return {
        "scope": "immediate_post_mutation_read",
        "note": "Concurrent changes after the verification read are not excluded.",
    }


def _audit_success_result(
    resource_name: str,
    *,
    target_field: str,
    target_value: Any,
    verified_snapshot: dict[str, Any],
) -> dict[str, Any]:
    canonical = json.dumps(
        verified_snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "resource_name": resource_name,
        "target_field": target_field,
        "target_value": target_value,
        "verified_after_hash": hashlib.sha256(canonical).hexdigest(),
    }


def _gaql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _single_row(customer_id: str, query: str) -> Any:
    service = utils.get_googleads_service("GoogleAdsService")
    rows = list(service.search(customer_id=customer_id, query=query))
    if len(rows) != 1:
        raise ToolError(
            f"Expected exactly one Google Ads resource, found {len(rows)}."
        )
    return rows[0]


def _state_fingerprint(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Returns a stable, compact fingerprint for dependent live resources."""
    canonical = json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _campaign_dependent_state(
    customer_id: str, campaign_id: str
) -> dict[str, dict[str, Any]]:
    """Fingerprints delivery-critical children before enabling/pausing."""
    service = utils.get_googleads_service("GoogleAdsService")

    campaign_criteria = [
        {
            "resource_name": row.campaign_criterion.resource_name,
            "criterion_id": str(row.campaign_criterion.criterion_id),
            "type": row.campaign_criterion.type_.name,
            "status": row.campaign_criterion.status.name,
            "negative": bool(row.campaign_criterion.negative),
            "bid_modifier": float(row.campaign_criterion.bid_modifier),
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT campaign_criterion.resource_name, "
                "campaign_criterion.criterion_id, campaign_criterion.type, "
                "campaign_criterion.status, campaign_criterion.negative, "
                "campaign_criterion.bid_modifier FROM campaign_criterion "
                f"WHERE campaign.id = {campaign_id}"
            ),
        )
    ]

    ad_group_criteria = [
        {
            "ad_group_id": str(row.ad_group.id),
            "resource_name": row.ad_group_criterion.resource_name,
            "criterion_id": str(row.ad_group_criterion.criterion_id),
            "type": row.ad_group_criterion.type_.name,
            "status": row.ad_group_criterion.status.name,
            "negative": bool(row.ad_group_criterion.negative),
            "bid_modifier": float(row.ad_group_criterion.bid_modifier),
            "keyword_text": row.ad_group_criterion.keyword.text,
            "keyword_match_type": (
                row.ad_group_criterion.keyword.match_type.name
            ),
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT ad_group.id, ad_group_criterion.resource_name, "
                "ad_group_criterion.criterion_id, "
                "ad_group_criterion.type, ad_group_criterion.status, "
                "ad_group_criterion.negative, "
                "ad_group_criterion.bid_modifier, "
                "ad_group_criterion.keyword.text, "
                "ad_group_criterion.keyword.match_type "
                "FROM ad_group_criterion "
                f"WHERE campaign.id = {campaign_id}"
            ),
        )
    ]

    ads = [
        {
            "ad_group_id": str(row.ad_group.id),
            "ad_group_status": row.ad_group.status.name,
            "resource_name": row.ad_group_ad.resource_name,
            "status": row.ad_group_ad.status.name,
            "ad_id": str(row.ad_group_ad.ad.id),
            "final_urls": list(row.ad_group_ad.ad.final_urls),
            "responsive_search_ad": utils.format_output_value(
                row.ad_group_ad.ad.responsive_search_ad
            ),
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT ad_group.id, ad_group.status, "
                "ad_group_ad.resource_name, ad_group_ad.status, "
                "ad_group_ad.ad.id, ad_group_ad.ad.final_urls, "
                "ad_group_ad.ad.responsive_search_ad.headlines, "
                "ad_group_ad.ad.responsive_search_ad.descriptions, "
                "ad_group_ad.ad.responsive_search_ad.path1, "
                "ad_group_ad.ad.responsive_search_ad.path2 "
                "FROM ad_group_ad "
                f"WHERE campaign.id = {campaign_id}"
            ),
        )
    ]

    conversion_goals = [
        {
            "resource_name": row.campaign_conversion_goal.resource_name,
            "category": row.campaign_conversion_goal.category.name,
            "origin": row.campaign_conversion_goal.origin.name,
            "biddable": bool(row.campaign_conversion_goal.biddable),
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT campaign_conversion_goal.resource_name, "
                "campaign_conversion_goal.category, "
                "campaign_conversion_goal.origin, "
                "campaign_conversion_goal.biddable "
                "FROM campaign_conversion_goal "
                f"WHERE campaign.id = {campaign_id}"
            ),
        )
    ]

    return {
        "campaign_criteria": _state_fingerprint(
            sorted(campaign_criteria, key=lambda item: item["resource_name"])
        ),
        "ad_group_criteria": _state_fingerprint(
            sorted(ad_group_criteria, key=lambda item: item["resource_name"])
        ),
        "ads": _state_fingerprint(
            sorted(ads, key=lambda item: item["resource_name"])
        ),
        "conversion_goals": _state_fingerprint(
            sorted(conversion_goals, key=lambda item: item["resource_name"])
        ),
    }


def _campaign_state(customer_id: str, campaign_id: str) -> dict[str, Any]:
    campaign_id = normalize_id(campaign_id, "campaign_id")
    row = _single_row(
        customer_id,
        "SELECT customer.currency_code, customer.time_zone, campaign.id, "
        "campaign.name, campaign.resource_name, campaign.status, "
        "campaign.campaign_budget, campaign.advertising_channel_type, "
        "campaign.bidding_strategy_type, campaign.start_date, "
        "campaign.end_date, "
        "campaign.network_settings.target_google_search, "
        "campaign.network_settings.target_search_network, "
        "campaign.network_settings.target_content_network, "
        "campaign.network_settings.target_partner_search_network, "
        "campaign_budget.amount_micros, "
        "campaign_budget.total_amount_micros, campaign_budget.period, "
        "campaign_budget.type, campaign_budget.status, "
        "campaign_budget.explicitly_shared, "
        "campaign_budget.reference_count "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    state = {
        "id": str(row.campaign.id),
        "name": row.campaign.name,
        "resource_name": row.campaign.resource_name,
        "status": row.campaign.status.name,
        "campaign_budget": row.campaign.campaign_budget,
        "budget_amount_micros": int(row.campaign_budget.amount_micros),
        "budget_total_amount_micros": int(
            row.campaign_budget.total_amount_micros
        ),
        "budget_period": row.campaign_budget.period.name,
        "budget_type": row.campaign_budget.type_.name,
        "budget_status": row.campaign_budget.status.name,
        "budget_daily_amount": int(row.campaign_budget.amount_micros)
        / 1_000_000,
        "budget_estimated_monthly_cap": round(
            (int(row.campaign_budget.amount_micros) / 1_000_000)
            * _AVERAGE_DAYS_PER_MONTH,
            2,
        ),
        "budget_explicitly_shared": bool(row.campaign_budget.explicitly_shared),
        "budget_reference_count": int(row.campaign_budget.reference_count),
        "currency_code": row.customer.currency_code,
        "time_zone": row.customer.time_zone,
        "advertising_channel_type": row.campaign.advertising_channel_type.name,
        "bidding_strategy_type": row.campaign.bidding_strategy_type.name,
        "start_date": row.campaign.start_date,
        "end_date": row.campaign.end_date,
        "network_settings": {
            "target_google_search": bool(
                row.campaign.network_settings.target_google_search
            ),
            "target_search_network": bool(
                row.campaign.network_settings.target_search_network
            ),
            "target_content_network": bool(
                row.campaign.network_settings.target_content_network
            ),
            "target_partner_search_network": bool(
                row.campaign.network_settings.target_partner_search_network
            ),
        },
    }
    state["dependent_resources"] = _campaign_dependent_state(
        customer_id, campaign_id
    )
    return state


def _campaign_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Returns the material campaign state protected by an approval."""
    fields = (
        "status",
        "campaign_budget",
        "budget_amount_micros",
        "budget_total_amount_micros",
        "budget_period",
        "budget_type",
        "budget_status",
        "budget_daily_amount",
        "budget_estimated_monthly_cap",
        "budget_explicitly_shared",
        "budget_reference_count",
        "currency_code",
        "time_zone",
        "advertising_channel_type",
        "bidding_strategy_type",
        "start_date",
        "end_date",
        "network_settings",
        "dependent_resources",
    )
    return {field: state[field] for field in fields}


def _account_context(customer_id: str) -> dict[str, str]:
    row = _single_row(
        customer_id,
        "SELECT customer.currency_code, customer.time_zone "
        "FROM customer LIMIT 1",
    )
    return {
        "currency_code": row.customer.currency_code,
        "time_zone": row.customer.time_zone,
    }


def _campaigns_using_budget(
    customer_id: str, budget_resource_name: str
) -> list[dict[str, str]]:
    service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status FROM campaign WHERE campaign.campaign_budget = "
        f"{_gaql_string(budget_resource_name)}"
    )
    campaigns = [
        {
            "id": str(row.campaign.id),
            "name": row.campaign.name,
            "resource_name": row.campaign.resource_name,
            "status": row.campaign.status.name,
        }
        for row in service.search(customer_id=customer_id, query=query)
    ]
    return sorted(campaigns, key=lambda campaign: campaign["id"])


def _budget_state(customer_id: str, budget_id: str) -> dict[str, Any]:
    budget_id = normalize_id(budget_id, "budget_id")
    row = _single_row(
        customer_id,
        "SELECT campaign_budget.id, campaign_budget.name, "
        "campaign_budget.resource_name, campaign_budget.status, "
        "campaign_budget.amount_micros, "
        "campaign_budget.total_amount_micros, campaign_budget.period, "
        "campaign_budget.type, "
        "campaign_budget.explicitly_shared, campaign_budget.reference_count "
        f"FROM campaign_budget WHERE campaign_budget.id = {budget_id}",
    )
    state = {
        "id": str(row.campaign_budget.id),
        "name": row.campaign_budget.name,
        "resource_name": row.campaign_budget.resource_name,
        "status": row.campaign_budget.status.name,
        "amount_micros": int(row.campaign_budget.amount_micros),
        "total_amount_micros": int(row.campaign_budget.total_amount_micros),
        "period": row.campaign_budget.period.name,
        "budget_type": row.campaign_budget.type_.name,
        "amount": int(row.campaign_budget.amount_micros) / 1_000_000,
        "explicitly_shared": bool(row.campaign_budget.explicitly_shared),
        "reference_count": int(row.campaign_budget.reference_count),
    }
    state.update(_account_context(customer_id))
    state["campaigns"] = _campaigns_using_budget(
        customer_id, state["resource_name"]
    )
    state["estimated_monthly_cap"] = round(
        state["amount"] * _AVERAGE_DAYS_PER_MONTH, 2
    )
    return state


def _budget_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Returns budget and owner context that must match the preview."""
    fields = (
        "id",
        "name",
        "resource_name",
        "status",
        "amount_micros",
        "amount",
        "total_amount_micros",
        "period",
        "budget_type",
        "explicitly_shared",
        "reference_count",
        "currency_code",
        "time_zone",
        "campaigns",
        "estimated_monthly_cap",
    )
    return {field: state[field] for field in fields}


def _keyword_state(
    customer_id: str, ad_group_id: str, criterion_id: str
) -> dict[str, Any]:
    ad_group_id = normalize_id(ad_group_id, "ad_group_id")
    criterion_id = normalize_id(criterion_id, "criterion_id")
    resource_name = (
        f"customers/{customer_id}/adGroupCriteria/"
        f"{ad_group_id}~{criterion_id}"
    )
    row = _single_row(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status, campaign.bidding_strategy_type, "
        "ad_group.id, ad_group.name, ad_group.resource_name, ad_group.status, "
        "ad_group.type, ad_group.cpc_bid_micros, ad_group.cpm_bid_micros, "
        "ad_group.cpv_bid_micros, ad_group.percent_cpc_bid_micros, "
        "ad_group.target_cpa_micros, ad_group.target_cpm_micros, "
        "ad_group.target_cpv_micros, ad_group.target_roas, "
        "ad_group.effective_cpc_bid_micros, "
        "ad_group.effective_target_cpa_micros, "
        "ad_group.effective_target_cpa_source, "
        "ad_group.effective_target_roas, "
        "ad_group.effective_target_roas_source, "
        "ad_group.optimized_targeting_enabled, "
        "ad_group_criterion.resource_name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.status, ad_group_criterion.type, "
        "ad_group_criterion.negative, ad_group_criterion.bid_modifier, "
        "ad_group_criterion.cpc_bid_micros, "
        "ad_group_criterion.cpm_bid_micros, "
        "ad_group_criterion.cpv_bid_micros, "
        "ad_group_criterion.percent_cpc_bid_micros, "
        "ad_group_criterion.effective_cpc_bid_micros, "
        "ad_group_criterion.effective_cpc_bid_source, "
        "ad_group_criterion.effective_cpm_bid_micros, "
        "ad_group_criterion.effective_cpm_bid_source, "
        "ad_group_criterion.effective_cpv_bid_micros, "
        "ad_group_criterion.effective_cpv_bid_source, "
        "ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type "
        "FROM ad_group_criterion WHERE "
        f"ad_group_criterion.resource_name = {_gaql_string(resource_name)}",
    )
    return {
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_resource_name": row.campaign.resource_name,
        "campaign_status": row.campaign.status.name,
        "campaign_bidding_strategy_type": row.campaign.bidding_strategy_type.name,
        "ad_group_id": str(row.ad_group.id),
        "ad_group_name": row.ad_group.name,
        "ad_group_resource_name": row.ad_group.resource_name,
        "ad_group_status": row.ad_group.status.name,
        "ad_group_type": row.ad_group.type_.name,
        "ad_group_cpc_bid_micros": int(row.ad_group.cpc_bid_micros),
        "ad_group_cpm_bid_micros": int(row.ad_group.cpm_bid_micros),
        "ad_group_cpv_bid_micros": int(row.ad_group.cpv_bid_micros),
        "ad_group_percent_cpc_bid_micros": int(
            row.ad_group.percent_cpc_bid_micros
        ),
        "ad_group_target_cpa_micros": int(row.ad_group.target_cpa_micros),
        "ad_group_target_cpm_micros": int(row.ad_group.target_cpm_micros),
        "ad_group_target_cpv_micros": int(row.ad_group.target_cpv_micros),
        "ad_group_target_roas": float(row.ad_group.target_roas),
        "ad_group_effective_cpc_bid_micros": int(
            row.ad_group.effective_cpc_bid_micros
        ),
        "ad_group_effective_target_cpa_micros": int(
            row.ad_group.effective_target_cpa_micros
        ),
        "ad_group_effective_target_cpa_source": (
            row.ad_group.effective_target_cpa_source.name
        ),
        "ad_group_effective_target_roas": float(
            row.ad_group.effective_target_roas
        ),
        "ad_group_effective_target_roas_source": (
            row.ad_group.effective_target_roas_source.name
        ),
        "ad_group_optimized_targeting_enabled": bool(
            row.ad_group.optimized_targeting_enabled
        ),
        "criterion_id": str(row.ad_group_criterion.criterion_id),
        "resource_name": row.ad_group_criterion.resource_name,
        "status": row.ad_group_criterion.status.name,
        "criterion_type": row.ad_group_criterion.type_.name,
        "negative": bool(row.ad_group_criterion.negative),
        "bid_modifier": float(row.ad_group_criterion.bid_modifier),
        "cpc_bid_micros": int(row.ad_group_criterion.cpc_bid_micros),
        "cpm_bid_micros": int(row.ad_group_criterion.cpm_bid_micros),
        "cpv_bid_micros": int(row.ad_group_criterion.cpv_bid_micros),
        "percent_cpc_bid_micros": int(
            row.ad_group_criterion.percent_cpc_bid_micros
        ),
        "effective_cpc_bid_micros": int(
            row.ad_group_criterion.effective_cpc_bid_micros
        ),
        "effective_cpc_bid_source": (
            row.ad_group_criterion.effective_cpc_bid_source.name
        ),
        "effective_cpm_bid_micros": int(
            row.ad_group_criterion.effective_cpm_bid_micros
        ),
        "effective_cpm_bid_source": (
            row.ad_group_criterion.effective_cpm_bid_source.name
        ),
        "effective_cpv_bid_micros": int(
            row.ad_group_criterion.effective_cpv_bid_micros
        ),
        "effective_cpv_bid_source": (
            row.ad_group_criterion.effective_cpv_bid_source.name
        ),
        "text": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
    }


def _keyword_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Returns identity and criterion semantics protected by approval."""
    fields = (
        "campaign_id",
        "campaign_name",
        "campaign_resource_name",
        "campaign_status",
        "campaign_bidding_strategy_type",
        "ad_group_id",
        "ad_group_name",
        "ad_group_resource_name",
        "ad_group_status",
        "ad_group_type",
        "ad_group_cpc_bid_micros",
        "ad_group_cpm_bid_micros",
        "ad_group_cpv_bid_micros",
        "ad_group_percent_cpc_bid_micros",
        "ad_group_target_cpa_micros",
        "ad_group_target_cpm_micros",
        "ad_group_target_cpv_micros",
        "ad_group_target_roas",
        "ad_group_effective_cpc_bid_micros",
        "ad_group_effective_target_cpa_micros",
        "ad_group_effective_target_cpa_source",
        "ad_group_effective_target_roas",
        "ad_group_effective_target_roas_source",
        "ad_group_optimized_targeting_enabled",
        "criterion_id",
        "resource_name",
        "status",
        "criterion_type",
        "negative",
        "bid_modifier",
        "cpc_bid_micros",
        "cpm_bid_micros",
        "cpv_bid_micros",
        "percent_cpc_bid_micros",
        "effective_cpc_bid_micros",
        "effective_cpc_bid_source",
        "effective_cpm_bid_micros",
        "effective_cpm_bid_source",
        "effective_cpv_bid_micros",
        "effective_cpv_bid_source",
        "text",
        "match_type",
    )
    return {field: state[field] for field in fields}


def _keyword_parent_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "campaign_id",
        "campaign_name",
        "campaign_resource_name",
        "campaign_status",
        "campaign_bidding_strategy_type",
        "ad_group_id",
        "ad_group_name",
        "ad_group_resource_name",
        "ad_group_status",
        "ad_group_type",
        "ad_group_cpc_bid_micros",
        "ad_group_cpm_bid_micros",
        "ad_group_cpv_bid_micros",
        "ad_group_percent_cpc_bid_micros",
        "ad_group_target_cpa_micros",
        "ad_group_target_cpm_micros",
        "ad_group_target_cpv_micros",
        "ad_group_target_roas",
        "ad_group_effective_cpc_bid_micros",
        "ad_group_effective_target_cpa_micros",
        "ad_group_effective_target_cpa_source",
        "ad_group_effective_target_roas",
        "ad_group_effective_target_roas_source",
        "ad_group_optimized_targeting_enabled",
    )
    return {field: state[field] for field in fields}


def _ad_group_dependent_state(
    customer_id: str, ad_group_id: str
) -> dict[str, dict[str, Any]]:
    """Fingerprints criteria and ads whose delivery follows ad-group status."""
    service = utils.get_googleads_service("GoogleAdsService")
    criteria = [
        {
            "resource_name": row.ad_group_criterion.resource_name,
            "criterion_id": str(row.ad_group_criterion.criterion_id),
            "type": row.ad_group_criterion.type_.name,
            "status": row.ad_group_criterion.status.name,
            "negative": bool(row.ad_group_criterion.negative),
            "bid_modifier": float(row.ad_group_criterion.bid_modifier),
            "keyword_text": row.ad_group_criterion.keyword.text,
            "keyword_match_type": row.ad_group_criterion.keyword.match_type.name,
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT ad_group_criterion.resource_name, "
                "ad_group_criterion.criterion_id, ad_group_criterion.type, "
                "ad_group_criterion.status, ad_group_criterion.negative, "
                "ad_group_criterion.bid_modifier, "
                "ad_group_criterion.keyword.text, "
                "ad_group_criterion.keyword.match_type "
                "FROM ad_group_criterion "
                f"WHERE ad_group.id = {ad_group_id}"
            ),
        )
    ]
    ads = [
        {
            "resource_name": row.ad_group_ad.resource_name,
            "status": row.ad_group_ad.status.name,
            "ad_id": str(row.ad_group_ad.ad.id),
            "final_urls": list(row.ad_group_ad.ad.final_urls),
        }
        for row in service.search(
            customer_id=customer_id,
            query=(
                "SELECT ad_group_ad.resource_name, ad_group_ad.status, "
                "ad_group_ad.ad.id, ad_group_ad.ad.final_urls "
                "FROM ad_group_ad "
                f"WHERE ad_group.id = {ad_group_id}"
            ),
        )
    ]
    return {
        "criteria": _state_fingerprint(
            sorted(criteria, key=lambda item: item["resource_name"])
        ),
        "ads": _state_fingerprint(
            sorted(ads, key=lambda item: item["resource_name"])
        ),
    }


def _ad_group_state(customer_id: str, ad_group_id: str) -> dict[str, Any]:
    ad_group_id = normalize_id(ad_group_id, "ad_group_id")
    row = _single_row(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status, campaign.bidding_strategy_type, "
        "ad_group.id, ad_group.name, ad_group.resource_name, ad_group.status, "
        "ad_group.type, ad_group.cpc_bid_micros, ad_group.cpm_bid_micros, "
        "ad_group.cpv_bid_micros, ad_group.percent_cpc_bid_micros, "
        "ad_group.target_cpa_micros, ad_group.target_cpm_micros, "
        "ad_group.target_cpv_micros, ad_group.target_roas, "
        "ad_group.effective_cpc_bid_micros, "
        "ad_group.effective_target_cpa_micros, "
        "ad_group.effective_target_cpa_source, "
        "ad_group.effective_target_roas, "
        "ad_group.effective_target_roas_source, "
        "ad_group.optimized_targeting_enabled "
        f"FROM ad_group WHERE ad_group.id = {ad_group_id}",
    )
    state = {
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_resource_name": row.campaign.resource_name,
        "campaign_status": row.campaign.status.name,
        "campaign_bidding_strategy_type": row.campaign.bidding_strategy_type.name,
        "ad_group_id": str(row.ad_group.id),
        "ad_group_name": row.ad_group.name,
        "ad_group_resource_name": row.ad_group.resource_name,
        "ad_group_status": row.ad_group.status.name,
        "ad_group_type": row.ad_group.type_.name,
        "ad_group_cpc_bid_micros": int(row.ad_group.cpc_bid_micros),
        "ad_group_cpm_bid_micros": int(row.ad_group.cpm_bid_micros),
        "ad_group_cpv_bid_micros": int(row.ad_group.cpv_bid_micros),
        "ad_group_percent_cpc_bid_micros": int(
            row.ad_group.percent_cpc_bid_micros
        ),
        "ad_group_target_cpa_micros": int(row.ad_group.target_cpa_micros),
        "ad_group_target_cpm_micros": int(row.ad_group.target_cpm_micros),
        "ad_group_target_cpv_micros": int(row.ad_group.target_cpv_micros),
        "ad_group_target_roas": float(row.ad_group.target_roas),
        "ad_group_effective_cpc_bid_micros": int(
            row.ad_group.effective_cpc_bid_micros
        ),
        "ad_group_effective_target_cpa_micros": int(
            row.ad_group.effective_target_cpa_micros
        ),
        "ad_group_effective_target_cpa_source": (
            row.ad_group.effective_target_cpa_source.name
        ),
        "ad_group_effective_target_roas": float(
            row.ad_group.effective_target_roas
        ),
        "ad_group_effective_target_roas_source": (
            row.ad_group.effective_target_roas_source.name
        ),
        "ad_group_optimized_targeting_enabled": bool(
            row.ad_group.optimized_targeting_enabled
        ),
    }
    state["dependent_resources"] = _ad_group_dependent_state(
        customer_id, ad_group_id
    )
    return state


def _ad_group_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "campaign_id",
        "campaign_name",
        "campaign_resource_name",
        "campaign_status",
        "campaign_bidding_strategy_type",
        "ad_group_id",
        "ad_group_name",
        "ad_group_resource_name",
        "ad_group_status",
        "ad_group_type",
        "ad_group_cpc_bid_micros",
        "ad_group_cpm_bid_micros",
        "ad_group_cpv_bid_micros",
        "ad_group_percent_cpc_bid_micros",
        "ad_group_target_cpa_micros",
        "ad_group_target_cpm_micros",
        "ad_group_target_cpv_micros",
        "ad_group_target_roas",
        "ad_group_effective_cpc_bid_micros",
        "ad_group_effective_target_cpa_micros",
        "ad_group_effective_target_cpa_source",
        "ad_group_effective_target_roas",
        "ad_group_effective_target_roas_source",
        "ad_group_optimized_targeting_enabled",
        "dependent_resources",
    )
    return {field: state[field] for field in fields}


def _ad_group_parent_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return _keyword_parent_drift_snapshot(state)


def _ad_state(customer_id: str, ad_group_id: str, ad_id: str) -> dict[str, Any]:
    ad_group_id = normalize_id(ad_group_id, "ad_group_id")
    ad_id = normalize_id(ad_id, "ad_id")
    resource_name = f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
    row = _single_row(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status, ad_group.id, ad_group.name, ad_group.resource_name, "
        "ad_group.status, ad_group_ad.resource_name, ad_group_ad.status, "
        "ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, "
        "ad_group_ad.ad.final_urls, ad_group_ad.ad.final_mobile_urls, "
        "ad_group_ad.ad.tracking_url_template, "
        "ad_group_ad.ad.final_url_suffix, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad WHERE "
        f"ad_group_ad.resource_name = {_gaql_string(resource_name)}",
    )
    return {
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "campaign_resource_name": row.campaign.resource_name,
        "campaign_status": row.campaign.status.name,
        "ad_group_id": str(row.ad_group.id),
        "ad_group_name": row.ad_group.name,
        "ad_group_resource_name": row.ad_group.resource_name,
        "ad_group_status": row.ad_group.status.name,
        "ad_id": str(row.ad_group_ad.ad.id),
        "ad_name": row.ad_group_ad.ad.name,
        "ad_type": row.ad_group_ad.ad.type_.name,
        "resource_name": row.ad_group_ad.resource_name,
        "ad_resource_name": f"customers/{customer_id}/ads/{ad_id}",
        "status": row.ad_group_ad.status.name,
        "final_urls": list(row.ad_group_ad.ad.final_urls),
        "final_mobile_urls": list(row.ad_group_ad.ad.final_mobile_urls),
        "tracking_url_template": row.ad_group_ad.ad.tracking_url_template,
        "final_url_suffix": row.ad_group_ad.ad.final_url_suffix,
        "responsive_search_ad": utils.format_output_value(
            row.ad_group_ad.ad.responsive_search_ad
        ),
    }


def _ad_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "campaign_id",
        "campaign_name",
        "campaign_resource_name",
        "campaign_status",
        "ad_group_id",
        "ad_group_name",
        "ad_group_resource_name",
        "ad_group_status",
        "ad_id",
        "ad_name",
        "ad_type",
        "resource_name",
        "ad_resource_name",
        "status",
        "final_urls",
        "final_mobile_urls",
        "tracking_url_template",
        "final_url_suffix",
        "responsive_search_ad",
    )
    return {field: state[field] for field in fields}


def _active_ad_usages(customer_id: str, ad_id: str) -> list[dict[str, Any]]:
    """Returns every non-removed AdGroupAd usage of the customer-scoped ad."""
    ad_id = normalize_id(ad_id, "ad_id")
    service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status, ad_group.id, ad_group.name, ad_group.resource_name, "
        "ad_group.status, ad_group_ad.resource_name, ad_group_ad.status, "
        "ad_group_ad.ad.id, ad_group_ad.ad.name, ad_group_ad.ad.type, "
        "ad_group_ad.ad.final_urls, ad_group_ad.ad.final_mobile_urls, "
        "ad_group_ad.ad.tracking_url_template, "
        "ad_group_ad.ad.final_url_suffix, "
        "ad_group_ad.ad.responsive_search_ad.headlines, "
        "ad_group_ad.ad.responsive_search_ad.descriptions, "
        "ad_group_ad.ad.responsive_search_ad.path1, "
        "ad_group_ad.ad.responsive_search_ad.path2 FROM ad_group_ad "
        f"WHERE ad_group_ad.ad.id = {ad_id} "
        "AND ad_group_ad.status != REMOVED"
    )
    usages = [
        {
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "campaign_resource_name": row.campaign.resource_name,
            "campaign_status": row.campaign.status.name,
            "ad_group_id": str(row.ad_group.id),
            "ad_group_name": row.ad_group.name,
            "ad_group_resource_name": row.ad_group.resource_name,
            "ad_group_status": row.ad_group.status.name,
            "resource_name": row.ad_group_ad.resource_name,
            "status": row.ad_group_ad.status.name,
            "ad_id": str(row.ad_group_ad.ad.id),
            "ad_name": row.ad_group_ad.ad.name,
            "ad_type": row.ad_group_ad.ad.type_.name,
            "final_urls": list(row.ad_group_ad.ad.final_urls),
            "final_mobile_urls": list(row.ad_group_ad.ad.final_mobile_urls),
            "tracking_url_template": row.ad_group_ad.ad.tracking_url_template,
            "final_url_suffix": row.ad_group_ad.ad.final_url_suffix,
            "responsive_search_ad": utils.format_output_value(
                row.ad_group_ad.ad.responsive_search_ad
            ),
        }
        for row in service.search(customer_id=customer_id, query=query)
    ]
    return sorted(usages, key=lambda usage: usage["resource_name"])


def _ad_final_url_state(
    customer_id: str, ad_group_id: str, ad_id: str
) -> dict[str, Any]:
    state = _ad_state(customer_id, ad_group_id, ad_id)
    usages = _active_ad_usages(customer_id, state["ad_id"])
    state["active_usages"] = usages
    state["active_usages_fingerprint"] = _state_fingerprint(usages)
    if state["ad_type"] not in _MUTABLE_FINAL_URL_AD_TYPES:
        raise ToolError(
            f"Ad type {state['ad_type']} is not allowlisted for final-URL edits."
        )
    target_usages = [
        usage
        for usage in usages
        if usage["resource_name"] == state["resource_name"]
    ]
    if len(target_usages) != 1:
        raise ToolError(
            "The requested ad-group-ad is not present as exactly one non-removed "
            "usage of the global ad."
        )
    if len(usages) != 1:
        raise ToolError(
            "Final-URL editing is blocked because the global ad has more than "
            "one non-removed ad-group usage."
        )
    return state


def _ad_final_url_drift_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return {
        **_ad_drift_snapshot(state),
        "active_usages": state["active_usages"],
        "active_usages_fingerprint": state["active_usages_fingerprint"],
    }


def _ad_final_url_non_target_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = _ad_final_url_drift_snapshot(state)
    snapshot.pop("final_urls")
    snapshot.pop("active_usages_fingerprint")
    snapshot["active_usages"] = [
        {key: value for key, value in usage.items() if key != "final_urls"}
        for usage in snapshot["active_usages"]
    ]
    return snapshot


def _validate_final_url(url: str) -> str:
    candidate = url.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise ToolError("Landing-page URL is malformed.") from exc
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        raise ToolError("Landing-page URL must be a valid HTTPS URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ToolError("Landing-page URL must not contain credentials.")
    if port not in {None, 443}:
        raise ToolError("Landing-page URL must not use a non-443 port.")
    if parsed.fragment:
        raise ToolError("Landing-page URL must not contain a fragment.")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as exc:
        raise ToolError(
            "Landing-page URL contains an invalid hostname."
        ) from exc
    hostname = hostname.rstrip(".")
    configured = os.environ.get("GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS", "")
    allowed_hosts = {
        host.strip().encode("idna").decode("ascii").casefold().rstrip(".")
        for host in configured.split(",")
        if host.strip()
    }
    if not allowed_hosts:
        raise ToolError(
            "Landing-page changes are disabled: "
            "GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS is not set."
        )
    if hostname not in allowed_hosts:
        raise ToolError(f"Landing-page host {hostname} is not allowlisted.")
    normalized_host = f"[{hostname}]" if ":" in hostname else hostname
    return urlunsplit(
        ("https", normalized_host, parsed.path or "/", parsed.query, "")
    )


def _current_principal() -> Principal | None:
    """Builds audit identity from trusted MCP auth context, never tool input."""
    actor = current_operator()
    if actor is None:
        return None
    return Principal(
        subject=actor.subject,
        email=actor.email,
        role="operator",
        authentication_method="mcp_oauth",
    )


def _authorize_preview(customer_id: str) -> tuple[str, Principal | None]:
    """Authenticates the operator before any preview performs live API reads."""
    actor = _current_principal()
    return require_allowed_customer(customer_id), actor


def _create_pending_change_set(
    details: dict[str, Any], actor: Principal | None
) -> dict[str, Any]:
    return create_change_set(details, actor=actor)


def _execute_approved_change(
    change_set_token: str,
    expected_action: str,
    execute: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Consumes an out-of-band approval and records its execution outcome."""
    executor = _current_principal()
    payload = verify_change_set(
        change_set_token,
        expected_action=expected_action,
        executor=executor,
    )
    change_set_id = payload["change_set_id"]
    execution_id = payload["execution_id"]
    try:
        result = execute(payload)
    except _MutationOutcomeUncertain as exc:
        try:
            record_change_set_uncertain(
                change_set_id,
                execution_id,
                reason_code="LIVE_OUTCOME_UNCERTAIN",
                message=str(exc),
                executor=executor,
            )
        except Exception as audit_exc:
            raise ToolError(
                "The live mutation outcome is uncertain and the uncertainty "
                "could not be recorded; inspect Google Ads and the durable "
                "ledger before any retry."
            ) from audit_exc
        raise ToolError(
            f"{exc} Do not retry; reconcile the live Google Ads resource."
        ) from exc
    except Exception as exc:
        try:
            record_change_set_failure(
                change_set_id,
                execution_id,
                reason_code="EXECUTION_FAILED",
                message=str(exc),
                executor=executor,
            )
        except Exception as audit_exc:
            raise ToolError(
                "Execution failed and its audit outcome could not be "
                "recorded; inspect the durable change-set ledger before "
                "retrying."
            ) from audit_exc
        raise

    try:
        audit_result = result.pop("_audit_result")
        record_change_set_success(
            change_set_id,
            execution_id,
            result=audit_result,
            executor=executor,
        )
    except Exception as exc:
        raise ToolError(
            "The Google Ads mutation was applied and verified, but audit "
            "completion could not be recorded. Do not retry the mutation; "
            "inspect the durable change-set ledger."
        ) from exc
    return result


def _validate_campaign_status(
    customer_id: str, resource_name: str, new_status: str
) -> None:
    service = utils.get_googleads_service("CampaignService")
    operation = utils.get_googleads_type("CampaignOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    service.mutate_campaigns(
        customer_id=customer_id,
        operations=[operation],
        validate_only=True,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )


def _mutate_campaign_status(
    customer_id: str, resource_name: str, new_status: str
) -> str:
    service = utils.get_googleads_service("CampaignService")
    operation = utils.get_googleads_type("CampaignOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    response = service.mutate_campaigns(
        customer_id=customer_id,
        operations=[operation],
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_campaign_status_change(
    customer_id: str,
    campaign_id: str,
    new_status: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Validates and previews pausing one campaign.

    This tool sends a validate-only request, not a requested account mutation.
    It returns a durable pending change set for out-of-band human approval.
    Enabling remains manual until full delivery-context test-account validation.
    """
    customer_id, actor = _authorize_preview(customer_id)
    new_status = _require_release_one_pause_target(new_status.upper())
    current = _campaign_state(customer_id, campaign_id)
    if current["status"] == new_status:
        raise ToolError(f"Campaign is already {new_status}.")
    _validate_campaign_status(customer_id, current["resource_name"], new_status)
    return _create_pending_change_set(
        {
            "action": "campaign_status",
            "customer_id": customer_id,
            "object": {
                "type": "campaign",
                "id": current["id"],
                "name": current["name"],
                "resource_name": current["resource_name"],
            },
            "current": _campaign_drift_snapshot(current),
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Campaign delivery will stop.",
            "observation_window": observation_window,
            "rollback": _MANUAL_STATUS_REACTIVATION,
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_campaign_status_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved campaign-status change with immediate read-back.

    This tool cannot approve a change. It only consumes a durable approval from
    the protected, authenticated human approval channel. The live state is
    re-read and validated again before writing. This Release 1 tool only
    pauses; enabling remains manual until full delivery-context test-account validation.
    """
    return _execute_approved_change(
        change_set_token, "campaign_status", _apply_campaign_status_payload
    )


def _apply_campaign_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    proposed = payload.get("proposed")
    new_status = _require_release_one_pause_target(
        proposed.get("status") if isinstance(proposed, dict) else None
    )
    current = _campaign_state(payload["customer_id"], payload["object"]["id"])
    if _campaign_drift_snapshot(current) != payload["current"]:
        raise ToolError("Live campaign state changed. Generate a new preview.")
    _validate_campaign_status(
        payload["customer_id"],
        current["resource_name"],
        new_status,
    )
    resource_name = _perform_live_mutation(
        lambda: _mutate_campaign_status(
            payload["customer_id"],
            current["resource_name"],
            new_status,
        ),
        "Campaign-status",
    )
    verified = _read_after_live_mutation(
        lambda: _campaign_state(
            payload["customer_id"], payload["object"]["id"]
        ),
        "Campaign-status",
    )
    if verified["status"] != new_status:
        raise _MutationOutcomeUncertain(
            "Campaign-status mutation returned but verification did not match."
        )
    verified_snapshot = _campaign_drift_snapshot(verified)
    _assert_post_read_context(
        payload["current"],
        verified_snapshot,
        target_fields=frozenset({"status"}),
        label="Campaign-status",
    )
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {"status": verified["status"]},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            resource_name,
            target_field="status",
            target_value=verified["status"],
            verified_snapshot=verified_snapshot,
        ),
    }


def _validate_budget(
    customer_id: str, resource_name: str, amount_micros: int
) -> None:
    service = utils.get_googleads_service("CampaignBudgetService")
    operation = utils.get_googleads_type("CampaignBudgetOperation")
    operation.update.resource_name = resource_name
    operation.update.amount_micros = amount_micros
    operation.update_mask.paths.append("amount_micros")
    service.mutate_campaign_budgets(
        customer_id=customer_id,
        operations=[operation],
        validate_only=True,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )


def _mutate_budget(
    customer_id: str, resource_name: str, amount_micros: int
) -> str:
    service = utils.get_googleads_service("CampaignBudgetService")
    operation = utils.get_googleads_type("CampaignBudgetOperation")
    operation.update.resource_name = resource_name
    operation.update.amount_micros = amount_micros
    operation.update_mask.paths.append("amount_micros")
    response = service.mutate_campaign_budgets(
        customer_id=customer_id,
        operations=[operation],
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_campaign_budget_change(
    customer_id: str,
    budget_id: str,
    new_daily_amount: float,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Validates and previews a daily campaign-budget change.

    Shared budgets and increases above 10 percent are blocked. The currency is
    the Google Ads account currency; no currency conversion is performed.
    """
    customer_id, actor = _authorize_preview(customer_id)
    if not math.isfinite(new_daily_amount) or new_daily_amount <= 0:
        raise ToolError("new_daily_amount must be greater than zero.")
    current = _budget_state(customer_id, budget_id)
    if current["period"] != "DAILY":
        raise ToolError(
            "Only DAILY campaign budgets are supported by this guarded tool."
        )
    if current["status"] != "ENABLED":
        raise ToolError("Only ENABLED campaign budgets can be changed.")
    if current["explicitly_shared"] or current["reference_count"] > 1:
        raise ToolError(
            "Shared budgets are blocked because one edit can affect multiple "
            "campaigns."
        )
    if len(current["campaigns"]) != 1:
        raise ToolError(
            "Budget ownership could not be resolved to exactly one campaign; "
            "the change is blocked."
        )
    proposed_micros = round(new_daily_amount * 1_000_000)
    if proposed_micros == current["amount_micros"]:
        raise ToolError("Budget already has the proposed amount.")
    if proposed_micros > round(current["amount_micros"] * 1.10):
        raise ToolError(
            "Budget increases above 10 percent are blocked. Use a smaller "
            "controlled change."
        )
    _validate_budget(customer_id, current["resource_name"], proposed_micros)
    return _create_pending_change_set(
        {
            "action": "campaign_budget",
            "customer_id": customer_id,
            "object": {
                "type": "campaign_budget",
                "id": current["id"],
                "name": current["name"],
                "resource_name": current["resource_name"],
                "campaigns": current["campaigns"],
            },
            "current": _budget_drift_snapshot(current),
            "proposed": {
                "amount_micros": proposed_micros,
                "amount": proposed_micros / 1_000_000,
                "currency_code": current["currency_code"],
                "time_zone": current["time_zone"],
                "estimated_monthly_cap": round(
                    (proposed_micros / 1_000_000) * _AVERAGE_DAYS_PER_MONTH,
                    2,
                ),
            },
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Daily advertising spend can change.",
            "observation_window": observation_window,
            "rollback": {
                "amount_micros": current["amount_micros"],
                "amount": current["amount"],
                "currency_code": current["currency_code"],
                "estimated_monthly_cap": current["estimated_monthly_cap"],
            },
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_campaign_budget_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved budget change with immediate read-back."""
    return _execute_approved_change(
        change_set_token, "campaign_budget", _apply_campaign_budget_payload
    )


def _apply_campaign_budget_payload(payload: dict[str, Any]) -> dict[str, Any]:
    current = _budget_state(payload["customer_id"], payload["object"]["id"])
    if current["explicitly_shared"] or current["reference_count"] > 1:
        raise ToolError(
            "Live budget is now shared. Generate a new preview instead of "
            "applying this change."
        )
    if _budget_drift_snapshot(current) != payload["current"]:
        raise ToolError("Live budget changed. Generate a new preview.")
    _validate_budget(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["amount_micros"],
    )
    resource_name = _perform_live_mutation(
        lambda: _mutate_budget(
            payload["customer_id"],
            current["resource_name"],
            payload["proposed"]["amount_micros"],
        ),
        "Budget",
    )
    verified = _read_after_live_mutation(
        lambda: _budget_state(payload["customer_id"], payload["object"]["id"]),
        "Budget",
    )
    if verified["amount_micros"] != payload["proposed"]["amount_micros"]:
        raise _MutationOutcomeUncertain(
            "Budget mutation returned but verification did not match."
        )
    verified_snapshot = _budget_drift_snapshot(verified)
    _assert_post_read_context(
        payload["current"],
        verified_snapshot,
        target_fields=frozenset(
            {"amount_micros", "amount", "estimated_monthly_cap"}
        ),
        label="Budget",
    )
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {
            "amount_micros": verified["amount_micros"],
            "amount": verified["amount"],
        },
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            resource_name,
            target_field="amount_micros",
            target_value=verified["amount_micros"],
            verified_snapshot=verified_snapshot,
        ),
    }


def _validate_keyword_status(
    customer_id: str, resource_name: str, new_status: str
) -> None:
    service = utils.get_googleads_service("AdGroupCriterionService")
    operation = utils.get_googleads_type("AdGroupCriterionOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    service.mutate_ad_group_criteria(
        customer_id=customer_id,
        operations=[operation],
        validate_only=True,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )


def _mutate_keyword_status(
    customer_id: str, resource_name: str, new_status: str
) -> str:
    service = utils.get_googleads_service("AdGroupCriterionService")
    operation = utils.get_googleads_type("AdGroupCriterionOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id,
        operations=[operation],
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_keyword_status_change(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    new_status: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Validates and previews pausing or enabling one keyword criterion."""
    customer_id, actor = _authorize_preview(customer_id)
    new_status = new_status.upper()
    if new_status not in {"ENABLED", "PAUSED"}:
        raise ToolError("new_status must be ENABLED or PAUSED.")
    current = _keyword_state(customer_id, ad_group_id, criterion_id)
    if current["criterion_type"] != "KEYWORD":
        raise ToolError(
            "The requested ad-group criterion is not a KEYWORD; mutation is "
            "blocked."
        )
    if current["negative"]:
        raise ToolError("Negative keywords cannot be enabled or paused here.")
    if current["status"] == new_status:
        raise ToolError(f"Keyword is already {new_status}.")
    _validate_keyword_status(customer_id, current["resource_name"], new_status)
    return _create_pending_change_set(
        {
            "action": "keyword_status",
            "customer_id": customer_id,
            "object": {
                "type": "keyword",
                **current,
            },
            "current": _keyword_drift_snapshot(current),
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Search-query eligibility can change.",
            "observation_window": observation_window,
            "rollback": {"status": current["status"]},
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_keyword_status_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved keyword-status change with immediate read-back."""
    return _execute_approved_change(
        change_set_token, "keyword_status", _apply_keyword_status_payload
    )


def _apply_keyword_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    obj = payload["object"]
    current = _keyword_state(
        payload["customer_id"], obj["ad_group_id"], obj["criterion_id"]
    )
    if current["criterion_type"] != "KEYWORD":
        raise ToolError(
            "Live criterion is not a KEYWORD. Generate a new preview."
        )
    if _keyword_drift_snapshot(current) != payload["current"]:
        raise ToolError("Live keyword state changed. Generate a new preview.")
    _validate_keyword_status(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["status"],
    )
    resource_name = _perform_live_mutation(
        lambda: _mutate_keyword_status(
            payload["customer_id"],
            current["resource_name"],
            payload["proposed"]["status"],
        ),
        "Keyword-status",
    )
    verified = _read_after_live_mutation(
        lambda: _keyword_state(
            payload["customer_id"], obj["ad_group_id"], obj["criterion_id"]
        ),
        "Keyword-status",
    )
    if verified["status"] != payload["proposed"]["status"]:
        raise _MutationOutcomeUncertain(
            "Keyword mutation returned but verification did not match."
        )
    verified_snapshot = _keyword_drift_snapshot(verified)
    _assert_post_read_context(
        payload["current"],
        verified_snapshot,
        target_fields=frozenset({"status"}),
        label="Keyword-status",
    )
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {"status": verified["status"]},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            resource_name,
            target_field="status",
            target_value=verified["status"],
            verified_snapshot=verified_snapshot,
        ),
    }


def _negative_keyword_exists(
    customer_id: str, ad_group_id: str, text: str, match_type: str
) -> bool:
    query = (
        "SELECT ad_group_criterion.resource_name FROM ad_group_criterion "
        f"WHERE ad_group.id = {normalize_id(ad_group_id, 'ad_group_id')} "
        "AND ad_group_criterion.negative = TRUE "
        "AND ad_group_criterion.status != REMOVED "
        f"AND ad_group_criterion.keyword.text = {_gaql_string(text)} "
        "AND ad_group_criterion.keyword.match_type = "
        f"{match_type}"
    )
    service = utils.get_googleads_service("GoogleAdsService")
    return any(service.search(customer_id=customer_id, query=query))


def _mutate_negative_keyword(
    customer_id: str,
    ad_group_id: str,
    text: str,
    match_type: str,
    validate_only: bool,
) -> str | None:
    service = utils.get_googleads_service("AdGroupCriterionService")
    operation = utils.get_googleads_type("AdGroupCriterionOperation")
    operation.create.ad_group = (
        f"customers/{customer_id}/adGroups/"
        f"{normalize_id(ad_group_id, 'ad_group_id')}"
    )
    operation.create.status = "ENABLED"
    operation.create.negative = True
    operation.create.keyword.text = text
    operation.create.keyword.match_type = match_type
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    if validate_only:
        return None
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_add_negative_keyword(
    customer_id: str,
    ad_group_id: str,
    keyword_text: str,
    match_type: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "14 days",
) -> dict[str, Any]:
    """Validates and previews adding one ad-group negative keyword."""
    customer_id, actor = _authorize_preview(customer_id)
    ad_group_id = normalize_id(ad_group_id, "ad_group_id")
    keyword_text = keyword_text.strip()
    if not keyword_text:
        raise ToolError("keyword_text cannot be empty.")
    match_type = match_type.upper()
    if match_type not in {"EXACT", "PHRASE", "BROAD"}:
        raise ToolError("match_type must be EXACT, PHRASE, or BROAD.")
    if _negative_keyword_exists(
        customer_id, ad_group_id, keyword_text, match_type
    ):
        raise ToolError("That negative keyword already exists.")
    ad_group = _ad_group_state(customer_id, ad_group_id)
    _mutate_negative_keyword(
        customer_id,
        ad_group_id,
        keyword_text,
        match_type,
        validate_only=True,
    )
    return _create_pending_change_set(
        {
            "action": "add_negative_keyword",
            "customer_id": customer_id,
            "object": {
                "type": "ad_group_negative_keyword",
                **ad_group,
            },
            "current": {"exists": False},
            "proposed": {
                "text": keyword_text,
                "match_type": match_type,
                "negative": True,
                "status": "ENABLED",
            },
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Relevant searches may be excluded from ad delivery.",
            "observation_window": observation_window,
            "rollback": "Remove the newly created criterion manually after "
            "a separately approved rollback change set.",
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_add_negative_keyword(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved negative-keyword add with immediate read-back."""
    return _execute_approved_change(
        change_set_token,
        "add_negative_keyword",
        _apply_add_negative_keyword_payload,
    )


def _apply_add_negative_keyword_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    obj = payload["object"]
    proposed = payload["proposed"]
    current_ad_group = _ad_group_state(
        payload["customer_id"], obj["ad_group_id"]
    )
    if _ad_group_drift_snapshot(current_ad_group) != _ad_group_drift_snapshot(
        obj
    ):
        raise ToolError("Live ad-group state changed. Generate a new preview.")
    if _negative_keyword_exists(
        payload["customer_id"],
        obj["ad_group_id"],
        proposed["text"],
        proposed["match_type"],
    ):
        raise ToolError("Live state changed: negative keyword already exists.")
    _mutate_negative_keyword(
        payload["customer_id"],
        obj["ad_group_id"],
        proposed["text"],
        proposed["match_type"],
        validate_only=True,
    )
    resource_name = _perform_live_mutation(
        lambda: _mutate_negative_keyword(
            payload["customer_id"],
            obj["ad_group_id"],
            proposed["text"],
            proposed["match_type"],
            validate_only=False,
        ),
        "Negative-keyword addition",
    )
    verified, verified_ad_group = _read_after_live_mutation(
        lambda: (
            _negative_keyword_exists(
                payload["customer_id"],
                obj["ad_group_id"],
                proposed["text"],
                proposed["match_type"],
            ),
            _ad_group_state(payload["customer_id"], obj["ad_group_id"]),
        ),
        "Negative-keyword addition",
    )
    if not verified:
        raise _MutationOutcomeUncertain(
            "Negative-keyword mutation returned but verification did not match."
        )
    parent_snapshot = _ad_group_parent_drift_snapshot(verified_ad_group)
    if parent_snapshot != _ad_group_parent_drift_snapshot(current_ad_group):
        raise _MutationOutcomeUncertain(
            "Negative-keyword target matched, but protected parent state "
            "changed during execution."
        )
    verified_snapshot = {"exists": True, "parent_context": parent_snapshot}
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {**proposed, "exists": True},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            resource_name,
            target_field="exists",
            target_value=True,
            verified_snapshot=verified_snapshot,
        ),
    }


def _mutate_final_url(
    customer_id: str,
    resource_name: str,
    final_url: str,
    validate_only: bool,
) -> str | None:
    service = utils.get_googleads_service("AdService")
    operation = utils.get_googleads_type("AdOperation")
    operation.update.resource_name = resource_name
    operation.update.final_urls.append(final_url)
    operation.update_mask.paths.append("final_urls")
    response = service.mutate_ads(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    if validate_only:
        return None
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_ad_final_url_change(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    new_final_url: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Previews one RSA final-URL edit after checking all current usages.

    The non-removed usage check is repeated during apply. A concurrent reuse
    between checks cannot be ruled out and makes read-back reconciliation
    necessary if detected.
    """
    customer_id, actor = _authorize_preview(customer_id)
    new_final_url = _validate_final_url(new_final_url)
    current = _ad_final_url_state(customer_id, ad_group_id, ad_id)
    if current["final_urls"] == [new_final_url]:
        raise ToolError("Ad already uses the proposed final URL.")
    _mutate_final_url(
        customer_id,
        current["ad_resource_name"],
        new_final_url,
        validate_only=True,
    )
    return _create_pending_change_set(
        {
            "action": "ad_final_url",
            "customer_id": customer_id,
            "object": {"type": "ad", **current},
            "current": _ad_final_url_drift_snapshot(current),
            "proposed": {"final_urls": [new_final_url]},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "The ad may return to policy review and traffic can change.",
            "observation_window": observation_window,
            "rollback": {"final_urls": current["final_urls"]},
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_ad_final_url_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved final-URL edit and immediately reads it back."""
    return _execute_approved_change(
        change_set_token, "ad_final_url", _apply_ad_final_url_payload
    )


def _apply_ad_final_url_payload(payload: dict[str, Any]) -> dict[str, Any]:
    obj = payload["object"]
    current = _ad_final_url_state(
        payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
    )
    if _ad_final_url_drift_snapshot(current) != payload["current"]:
        raise ToolError("Live ad state changed. Generate a new preview.")
    proposed_url = _validate_final_url(payload["proposed"]["final_urls"][0])
    _mutate_final_url(
        payload["customer_id"],
        current["ad_resource_name"],
        proposed_url,
        validate_only=True,
    )
    resource_name = _perform_live_mutation(
        lambda: _mutate_final_url(
            payload["customer_id"],
            current["ad_resource_name"],
            proposed_url,
            validate_only=False,
        ),
        "Final-URL",
    )
    verified = _read_after_live_mutation(
        lambda: _ad_final_url_state(
            payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
        ),
        "Final-URL",
    )
    if verified["final_urls"] != payload["proposed"]["final_urls"]:
        raise _MutationOutcomeUncertain(
            "Final-URL mutation returned but verification did not match."
        )
    if _ad_final_url_non_target_snapshot(verified) != (
        _ad_final_url_non_target_snapshot(current)
    ):
        raise _MutationOutcomeUncertain(
            "Final-URL target matched, but protected usage context changed "
            "during execution."
        )
    after = {"final_urls": verified["final_urls"]}
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": after,
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            resource_name,
            target_field="final_urls",
            target_value=verified["final_urls"],
            verified_snapshot=_ad_final_url_drift_snapshot(verified),
        ),
    }


def _mutate_ad_group_status(
    customer_id: str,
    resource_name: str,
    new_status: str,
    validate_only: bool,
) -> str | None:
    service = utils.get_googleads_service("AdGroupService")
    operation = utils.get_googleads_type("AdGroupOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    response = service.mutate_ad_groups(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    if validate_only:
        return None
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_ad_group_status_change(
    customer_id: str,
    ad_group_id: str,
    new_status: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Validates and previews pausing one ad group.

    Enabling remains manual until full delivery-context test-account validation.
    """
    customer_id, actor = _authorize_preview(customer_id)
    new_status = _require_release_one_pause_target(new_status.upper())
    current = _ad_group_state(customer_id, ad_group_id)
    if current["ad_group_status"] == new_status:
        raise ToolError(f"Ad group is already {new_status}.")
    _mutate_ad_group_status(
        customer_id,
        current["ad_group_resource_name"],
        new_status,
        validate_only=True,
    )
    return _create_pending_change_set(
        {
            "action": "ad_group_status",
            "customer_id": customer_id,
            "object": {
                "type": "ad_group",
                "campaign_id": current["campaign_id"],
                "campaign_name": current["campaign_name"],
                "ad_group_id": current["ad_group_id"],
                "ad_group_name": current["ad_group_name"],
                "resource_name": current["ad_group_resource_name"],
            },
            "current": _ad_group_drift_snapshot(current),
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Delivery for every ad and criterion in the ad group will stop.",
            "observation_window": observation_window,
            "rollback": _MANUAL_STATUS_REACTIVATION,
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_ad_group_status_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved ad-group pause with immediate read-back.

    Enabling remains manual until full delivery-context test-account validation.
    """
    return _execute_approved_change(
        change_set_token, "ad_group_status", _apply_ad_group_status_payload
    )


def _apply_ad_group_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    proposed = payload.get("proposed")
    new_status = _require_release_one_pause_target(
        proposed.get("status") if isinstance(proposed, dict) else None
    )
    current = _ad_group_state(
        payload["customer_id"], payload["object"]["ad_group_id"]
    )
    if _ad_group_drift_snapshot(current) != payload["current"]:
        raise ToolError("Live ad-group state changed. Generate a new preview.")
    resource_name = current["ad_group_resource_name"]
    _mutate_ad_group_status(
        payload["customer_id"], resource_name, new_status, validate_only=True
    )
    mutated_resource = _perform_live_mutation(
        lambda: _mutate_ad_group_status(
            payload["customer_id"],
            resource_name,
            new_status,
            validate_only=False,
        ),
        "Ad-group status",
    )
    verified = _read_after_live_mutation(
        lambda: _ad_group_state(
            payload["customer_id"], payload["object"]["ad_group_id"]
        ),
        "Ad-group status",
    )
    if verified["ad_group_status"] != new_status:
        raise _MutationOutcomeUncertain(
            "Ad-group status mutation returned but verification did not match."
        )
    verified_snapshot = _ad_group_drift_snapshot(verified)
    _assert_post_read_context(
        payload["current"],
        verified_snapshot,
        target_fields=frozenset({"ad_group_status"}),
        label="Ad-group status",
    )
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": mutated_resource,
        "before": payload["current"],
        "after": {"status": verified["ad_group_status"]},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            mutated_resource,
            target_field="status",
            target_value=verified["ad_group_status"],
            verified_snapshot=verified_snapshot,
        ),
    }


def _mutate_ad_group_ad_status(
    customer_id: str,
    resource_name: str,
    new_status: str,
    validate_only: bool,
) -> str | None:
    service = utils.get_googleads_service("AdGroupAdService")
    operation = utils.get_googleads_type("AdGroupAdOperation")
    operation.update.resource_name = resource_name
    operation.update.status = new_status
    operation.update_mask.paths.append("status")
    response = service.mutate_ad_group_ads(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    if validate_only:
        return None
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_ad_group_ad_status_change(
    customer_id: str,
    ad_group_id: str,
    ad_id: str,
    new_status: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "7 days",
) -> dict[str, Any]:
    """Validates and previews pausing or enabling one ad-group ad."""
    customer_id, actor = _authorize_preview(customer_id)
    new_status = new_status.upper()
    if new_status not in {"ENABLED", "PAUSED"}:
        raise ToolError("new_status must be ENABLED or PAUSED.")
    current = _ad_state(customer_id, ad_group_id, ad_id)
    if current["status"] == new_status:
        raise ToolError(f"Ad-group ad is already {new_status}.")
    _mutate_ad_group_ad_status(
        customer_id,
        current["resource_name"],
        new_status,
        validate_only=True,
    )
    return _create_pending_change_set(
        {
            "action": "ad_group_ad_status",
            "customer_id": customer_id,
            "object": {
                "type": "ad_group_ad",
                "campaign_id": current["campaign_id"],
                "campaign_name": current["campaign_name"],
                "ad_group_id": current["ad_group_id"],
                "ad_group_name": current["ad_group_name"],
                "ad_id": current["ad_id"],
                "resource_name": current["resource_name"],
            },
            "current": _ad_drift_snapshot(current),
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Delivery for this ad can stop or resume.",
            "observation_window": observation_window,
            "rollback": {"status": current["status"]},
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_ad_group_ad_status_change(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved ad status change with immediate read-back."""
    return _execute_approved_change(
        change_set_token,
        "ad_group_ad_status",
        _apply_ad_group_ad_status_payload,
    )


def _apply_ad_group_ad_status_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    obj = payload["object"]
    current = _ad_state(
        payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
    )
    if _ad_drift_snapshot(current) != payload["current"]:
        raise ToolError(
            "Live ad-group-ad state changed. Generate a new preview."
        )
    resource_name = current["resource_name"]
    new_status = payload["proposed"]["status"]
    _mutate_ad_group_ad_status(
        payload["customer_id"], resource_name, new_status, validate_only=True
    )
    mutated_resource = _perform_live_mutation(
        lambda: _mutate_ad_group_ad_status(
            payload["customer_id"],
            resource_name,
            new_status,
            validate_only=False,
        ),
        "Ad-group-ad status",
    )
    verified = _read_after_live_mutation(
        lambda: _ad_state(
            payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
        ),
        "Ad-group-ad status",
    )
    if verified["status"] != new_status:
        raise _MutationOutcomeUncertain(
            "Ad-group-ad status mutation returned but verification did not match."
        )
    verified_snapshot = _ad_drift_snapshot(verified)
    _assert_post_read_context(
        payload["current"],
        verified_snapshot,
        target_fields=frozenset({"status"}),
        label="Ad-group-ad status",
    )
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": mutated_resource,
        "before": payload["current"],
        "after": {"status": verified["status"]},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            mutated_resource,
            target_field="status",
            target_value=verified["status"],
            verified_snapshot=verified_snapshot,
        ),
    }


def _criterion_is_removed_or_absent(
    customer_id: str, resource_name: str
) -> bool:
    """Confirms that no non-removed row remains for the criterion."""
    service = utils.get_googleads_service("GoogleAdsService")
    query = (
        "SELECT ad_group_criterion.resource_name, ad_group_criterion.status "
        "FROM ad_group_criterion "
        "WHERE ad_group_criterion.resource_name = "
        f"{_gaql_string(resource_name)} "
        "AND ad_group_criterion.status != REMOVED"
    )
    return not any(service.search(customer_id=customer_id, query=query))


def _mutate_remove_negative_keyword(
    customer_id: str, resource_name: str, validate_only: bool
) -> str | None:
    service = utils.get_googleads_service("AdGroupCriterionService")
    operation = utils.get_googleads_type("AdGroupCriterionOperation")
    operation.remove = resource_name
    response = service.mutate_ad_group_criteria(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
        retry=None,
        timeout=_MUTATE_TIMEOUT_SECONDS,
    )
    if validate_only:
        return None
    return response.results[0].resource_name


@changes_mcp.tool(annotations=_PREVIEW_ANNOTATIONS)
def preview_remove_negative_keyword(
    customer_id: str,
    ad_group_id: str,
    criterion_id: str,
    reason: str,
    expected_effect: str,
    observation_window: str = "14 days",
) -> dict[str, Any]:
    """Validates and previews removing one existing ad-group negative keyword."""
    customer_id, actor = _authorize_preview(customer_id)
    current = _keyword_state(customer_id, ad_group_id, criterion_id)
    if current["criterion_type"] != "KEYWORD":
        raise ToolError(
            "The requested criterion is not a KEYWORD; removal is blocked."
        )
    if not current["negative"]:
        raise ToolError(
            "The requested keyword is not negative; removal is blocked."
        )
    _mutate_remove_negative_keyword(
        customer_id, current["resource_name"], validate_only=True
    )
    return _create_pending_change_set(
        {
            "action": "remove_negative_keyword",
            "customer_id": customer_id,
            "object": {"type": "ad_group_negative_keyword", **current},
            "current": _keyword_drift_snapshot(current),
            "proposed": {"exists": False},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Previously excluded searches may become eligible for delivery.",
            "observation_window": observation_window,
            "rollback": {
                "requires_new_approval": True,
                "action": "add_negative_keyword",
                "ad_group_id": current["ad_group_id"],
                "text": current["text"],
                "match_type": current["match_type"],
            },
        },
        actor,
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_remove_negative_keyword(change_set_token: str) -> dict[str, Any]:
    """Attempts one approved negative-keyword removal with read-back."""
    return _execute_approved_change(
        change_set_token,
        "remove_negative_keyword",
        _apply_remove_negative_keyword_payload,
    )


def _apply_remove_negative_keyword_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    obj = payload["object"]
    current = _keyword_state(
        payload["customer_id"], obj["ad_group_id"], obj["criterion_id"]
    )
    if current["criterion_type"] != "KEYWORD" or not current["negative"]:
        raise ToolError(
            "Live criterion is not a negative KEYWORD. Generate a new preview."
        )
    if _keyword_drift_snapshot(current) != payload["current"]:
        raise ToolError(
            "Live negative-keyword state changed. Generate a new preview."
        )
    resource_name = current["resource_name"]
    _mutate_remove_negative_keyword(
        payload["customer_id"], resource_name, validate_only=True
    )
    mutated_resource = _perform_live_mutation(
        lambda: _mutate_remove_negative_keyword(
            payload["customer_id"], resource_name, validate_only=False
        ),
        "Negative-keyword removal",
    )
    removal_verified, verified_ad_group = _read_after_live_mutation(
        lambda: (
            _criterion_is_removed_or_absent(
                payload["customer_id"], resource_name
            ),
            _ad_group_state(payload["customer_id"], obj["ad_group_id"]),
        ),
        "Negative-keyword removal",
    )
    if not removal_verified:
        raise _MutationOutcomeUncertain(
            "Negative-keyword removal returned but verification did not match."
        )
    parent_snapshot = _ad_group_parent_drift_snapshot(verified_ad_group)
    if parent_snapshot != _keyword_parent_drift_snapshot(current):
        raise _MutationOutcomeUncertain(
            "Negative-keyword removal matched, but protected parent state "
            "changed during execution."
        )
    verified_snapshot = {"exists": False, "parent_context": parent_snapshot}
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": mutated_resource,
        "before": payload["current"],
        "after": {"exists": False},
        "rollback": payload["rollback"],
        "verification": _verification_scope(),
        "_audit_result": _audit_success_result(
            mutated_resource,
            target_field="exists",
            target_value=False,
            verified_snapshot=verified_snapshot,
        ),
    }
