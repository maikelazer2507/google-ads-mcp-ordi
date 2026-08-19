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

"""Opinionated, read-only diagnostics for a controlled Ads manager."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.access_policy import require_customer_access
from ads_mcp.tools.search import search

diagnostics_mcp = FastMCP("diagnostics")

_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _date_range(
    start_date: str,
    end_date: str,
    *,
    maximum_days: int = 366,
) -> tuple[str, str]:
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError as exc:
        raise ToolError("Dates must use YYYY-MM-DD format.") from exc
    if start > end:
        raise ToolError("start_date must not be after end_date.")
    if end > date.today():
        raise ToolError("end_date must not be in the future.")
    if end - start > timedelta(days=maximum_days - 1):
        raise ToolError(
            f"The selected date range must not exceed {maximum_days} days."
        )
    return start.isoformat(), end.isoformat()


def _micros(value: Any) -> float:
    try:
        return round(float(value or 0) / 1_000_000, 2)
    except (TypeError, ValueError):
        return 0.0


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_ratio(numerator: float, denominator: float) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 2)


def _bounded_search(
    customer_id: str,
    fields: list[str],
    resource: str,
    *,
    limit: int,
    maximum_api_limit: int = 2_000,
    **kwargs: Any,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Fetch one extra row when possible and make truncation explicit."""
    query_limit = min(limit + 1, maximum_api_limit)
    rows = search(
        customer_id,
        fields,
        resource,
        limit=query_limit,
        **kwargs,
    )
    truncated = len(rows) > limit
    truncation_unknown = query_limit == limit and len(rows) == limit
    return rows[:limit], truncated, truncation_unknown


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def account_configuration_audit(customer_id: str) -> dict[str, Any]:
    """Return a structured account/campaign configuration evidence pack.

    This is read-only evidence, not a profitability verdict. It intentionally
    separates account-level configuration, campaign delivery settings, and
    conversion actions so a reviewer can identify tracking and targeting risks.
    It does not prove billing ownership or enumerate user-access permissions.
    """
    customer_id = require_customer_access(customer_id, "read")
    account = search(
        customer_id,
        [
            "customer.id",
            "customer.descriptive_name",
            "customer.currency_code",
            "customer.time_zone",
            "customer.auto_tagging_enabled",
            "customer.test_account",
            "customer.manager",
            "customer.status",
            "customer.optimization_score",
            "customer.tracking_url_template",
            "customer.final_url_suffix",
        ],
        "customer",
        limit=1,
    )
    campaigns, campaigns_truncated, _ = _bounded_search(
        customer_id,
        [
            "campaign.id",
            "campaign.name",
            "campaign.status",
            "campaign.advertising_channel_type",
            "campaign.bidding_strategy_type",
            "campaign.start_date",
            "campaign.end_date",
            "campaign_budget.id",
            "campaign_budget.name",
            "campaign_budget.amount_micros",
            "campaign_budget.explicitly_shared",
            "campaign.network_settings.target_google_search",
            "campaign.network_settings.target_search_network",
            "campaign.network_settings.target_content_network",
            "campaign.network_settings.target_partner_search_network",
            "campaign.geo_target_type_setting.positive_geo_target_type",
            "campaign.geo_target_type_setting.negative_geo_target_type",
        ],
        "campaign",
        conditions=["campaign.status != 'REMOVED'"],
        orderings=["campaign.id ASC"],
        limit=500,
    )
    conversion_actions, conversions_truncated, _ = _bounded_search(
        customer_id,
        [
            "conversion_action.id",
            "conversion_action.name",
            "conversion_action.status",
            "conversion_action.type",
            "conversion_action.category",
            "conversion_action.primary_for_goal",
            "conversion_action.include_in_conversions_metric",
            "conversion_action.counting_type",
            "conversion_action.click_through_lookback_window_days",
            "conversion_action.view_through_lookback_window_days",
            "conversion_action.value_settings.default_value",
            "conversion_action.value_settings.always_use_default_value",
        ],
        "conversion_action",
        conditions=["conversion_action.status != 'REMOVED'"],
        orderings=["conversion_action.id ASC"],
        limit=500,
    )

    enabled = [
        row for row in campaigns if row.get("campaign.status") == "ENABLED"
    ]
    included_conversion_actions = [
        row
        for row in conversion_actions
        if row.get("conversion_action.primary_for_goal") is True
        or row.get("conversion_action.include_in_conversions_metric") is True
    ]
    evidence_flags = {
        "auto_tagging_disabled": bool(account)
        and account[0].get("customer.auto_tagging_enabled") is False,
        "enabled_campaign_count": len(enabled),
        "primary_or_included_conversion_action_count": len(
            included_conversion_actions
        ),
        "enabled_search_campaigns_with_display_network": [
            {
                "campaign_id": row.get("campaign.id"),
                "campaign_name": row.get("campaign.name"),
            }
            for row in enabled
            if row.get("campaign.advertising_channel_type") == "SEARCH"
            and row.get("campaign.network_settings.target_content_network")
            is True
        ],
        "shared_budget_campaigns": [
            {
                "campaign_id": row.get("campaign.id"),
                "campaign_name": row.get("campaign.name"),
                "budget_id": row.get("campaign_budget.id"),
            }
            for row in campaigns
            if row.get("campaign_budget.explicitly_shared") is True
        ],
    }
    return {
        "customer_id": customer_id,
        "evidence_status": "READ_ONLY_NOT_A_PROFITABILITY_VERDICT",
        "account": account[0] if account else None,
        "campaigns": campaigns,
        "conversion_actions": conversion_actions,
        "evidence_flags": evidence_flags,
        "truncation": {
            "campaigns": campaigns_truncated,
            "conversion_actions": conversions_truncated,
        },
        "limitations": [
            "This report does not establish billing ownership or list account users.",
            "Platform conversions are not proof of attended appointments or treatment value.",
            "Tag firing, consent behavior, call quality, and booking outcomes require downstream evidence.",
        ],
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def campaign_performance_snapshot(
    customer_id: str, start_date: str, end_date: str
) -> dict[str, Any]:
    """Summarize campaign delivery and platform conversion metrics.

    The output calls out that platform conversions and conversion value must be
    reconciled with qualified leads, attended appointments, accepted treatment,
    and collected value before making a profitability claim.
    """
    customer_id = require_customer_access(customer_id, "read")
    start_date, end_date = _date_range(start_date, end_date)
    rows, truncated, _ = _bounded_search(
        customer_id,
        [
            "campaign.id",
            "campaign.name",
            "campaign.status",
            "campaign.advertising_channel_type",
            "campaign.bidding_strategy_type",
            "campaign_budget.amount_micros",
            "metrics.impressions",
            "metrics.clicks",
            "metrics.cost_micros",
            "metrics.conversions",
            "metrics.all_conversions",
            "metrics.conversions_value",
        ],
        "campaign",
        conditions=[
            f"segments.date BETWEEN '{start_date}' AND '{end_date}'",
            "campaign.status != 'REMOVED'",
        ],
        orderings=["metrics.cost_micros DESC"],
        limit=500,
    )
    output: list[dict[str, Any]] = []
    totals = {
        "impressions": 0.0,
        "clicks": 0.0,
        "cost": 0.0,
        "platform_conversions": 0.0,
        "platform_conversion_value": 0.0,
    }
    for row in rows:
        cost = _micros(row.get("metrics.cost_micros"))
        conversions = _number(row.get("metrics.conversions"))
        conversion_value = _number(row.get("metrics.conversions_value"))
        impressions = _number(row.get("metrics.impressions"))
        clicks = _number(row.get("metrics.clicks"))
        totals["impressions"] += impressions
        totals["clicks"] += clicks
        totals["cost"] += cost
        totals["platform_conversions"] += conversions
        totals["platform_conversion_value"] += conversion_value
        output.append(
            {
                **row,
                "cost": cost,
                "platform_cpa": _safe_ratio(cost, conversions),
                "platform_roas": _safe_ratio(conversion_value, cost),
            }
        )
    totals = {key: round(value, 2) for key, value in totals.items()}
    totals["platform_cpa"] = _safe_ratio(
        totals["cost"], totals["platform_conversions"]
    )
    totals["platform_roas"] = _safe_ratio(
        totals["platform_conversion_value"], totals["cost"]
    )
    return {
        "customer_id": customer_id,
        "date_range": {"start": start_date, "end": end_date},
        "campaigns": output,
        "totals": totals,
        "is_truncated": truncated,
        "verdict": "PROFITABILITY_UNVERIFIED_WITHOUT_DOWNSTREAM_OUTCOMES",
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def performance_breakdown(
    customer_id: str,
    start_date: str,
    end_date: str,
    dimension: str,
    limit: int = 500,
) -> dict[str, Any]:
    """Break performance down by device, time, network, geography, or keyword."""
    customer_id = require_customer_access(customer_id, "read")
    start_date, end_date = _date_range(start_date, end_date)
    if limit < 1 or limit > 1_000:
        raise ToolError("limit must be between 1 and 1000.")

    dimensions: dict[str, tuple[str, list[str]]] = {
        "device": ("campaign", ["segments.device"]),
        "hour": ("campaign", ["segments.hour"]),
        "weekday": ("campaign", ["segments.day_of_week"]),
        "network": ("campaign", ["segments.ad_network_type"]),
        "geography": (
            "user_location_view",
            [
                "user_location_view.country_criterion_id",
                "user_location_view.targeting_location",
            ],
        ),
        "keyword": (
            "keyword_view",
            [
                "ad_group_criterion.criterion_id",
                "ad_group_criterion.keyword.text",
                "ad_group_criterion.keyword.match_type",
                "ad_group_criterion.status",
            ],
        ),
    }
    if dimension not in dimensions:
        raise ToolError(
            "dimension must be one of: device, hour, weekday, network, "
            "geography, keyword."
        )
    resource, dimension_fields = dimensions[dimension]
    fields = [
        "campaign.id",
        "campaign.name",
        *dimension_fields,
        "metrics.impressions",
        "metrics.clicks",
        "metrics.cost_micros",
        "metrics.conversions",
        "metrics.conversions_value",
    ]
    rows, truncated, _ = _bounded_search(
        customer_id,
        fields,
        resource,
        conditions=[f"segments.date BETWEEN '{start_date}' AND '{end_date}'"],
        orderings=["metrics.cost_micros DESC"],
        limit=limit,
    )
    normalized = []
    for row in rows:
        cost = _micros(row.get("metrics.cost_micros"))
        conversions = _number(row.get("metrics.conversions"))
        normalized.append(
            {
                **row,
                "cost": cost,
                "platform_cpa": _safe_ratio(cost, conversions),
            }
        )
    return {
        "customer_id": customer_id,
        "date_range": {"start": start_date, "end": end_date},
        "dimension": dimension,
        "rows": normalized,
        "is_truncated": truncated,
        "verdict": "DIAGNOSTIC_ONLY_DOWNSTREAM_QUALITY_NOT_INCLUDED",
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def budget_pacing_snapshot(
    customer_id: str, start_date: str, end_date: str
) -> dict[str, Any]:
    """Compare campaign spend with configured average daily budgets.

    Monthly caps are estimates using 30.4 average days. Shared budgets are
    flagged because attributing the cap to an individual campaign is unsafe.
    """
    customer_id = require_customer_access(customer_id, "read")
    start_date, end_date = _date_range(start_date, end_date, maximum_days=62)
    days = (
        date.fromisoformat(end_date) - date.fromisoformat(start_date)
    ).days + 1
    rows, truncated, _ = _bounded_search(
        customer_id,
        [
            "campaign.id",
            "campaign.name",
            "campaign.status",
            "campaign_budget.id",
            "campaign_budget.name",
            "campaign_budget.amount_micros",
            "campaign_budget.explicitly_shared",
            "metrics.cost_micros",
        ],
        "campaign",
        conditions=[
            f"segments.date BETWEEN '{start_date}' AND '{end_date}'",
            "campaign.status != 'REMOVED'",
        ],
        orderings=["metrics.cost_micros DESC"],
        limit=500,
    )
    campaigns = []
    for row in rows:
        average_daily_budget = _micros(row.get("campaign_budget.amount_micros"))
        spend = _micros(row.get("metrics.cost_micros"))
        current_budget_period_reference = round(average_daily_budget * days, 2)
        campaigns.append(
            {
                **row,
                "average_daily_budget": average_daily_budget,
                "estimated_30_4_day_cap": round(average_daily_budget * 30.4, 2),
                "period_spend": spend,
                "current_budget_period_reference": current_budget_period_reference,
                "current_budget_pacing_ratio": _safe_ratio(
                    spend, current_budget_period_reference
                ),
                "shared_budget_requires_group_review": row.get(
                    "campaign_budget.explicitly_shared"
                )
                is True,
            }
        )
    return {
        "customer_id": customer_id,
        "date_range": {"start": start_date, "end": end_date},
        "days": days,
        "campaigns": campaigns,
        "is_truncated": truncated,
        "execution_status": "READ_ONLY_NO_BUDGET_CHANGED",
        "limitation": (
            "The reference uses the current daily budget for every selected "
            "day; historical budget changes are not reconstructed."
        ),
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def performance_exception_report(
    customer_id: str,
    current_start_date: str,
    current_end_date: str,
    comparison_start_date: str,
    comparison_end_date: str,
    minimum_spend: float = 20.0,
    relative_change_threshold: float = 0.3,
) -> dict[str, Any]:
    """Flag material platform-performance exceptions between two periods.

    Findings are deterministic triage signals, not automatic optimization
    decisions. Tracking changes, seasonality, lead quality, and sample size must
    be checked before changing campaigns.
    """
    if minimum_spend < 0 or minimum_spend > 100_000:
        raise ToolError("minimum_spend must be between 0 and 100000.")
    if relative_change_threshold < 0.1 or relative_change_threshold > 5:
        raise ToolError("relative_change_threshold must be between 0.1 and 5.")
    current_start, current_end = _date_range(
        current_start_date, current_end_date
    )
    comparison_start, comparison_end = _date_range(
        comparison_start_date, comparison_end_date
    )
    current_days = (
        date.fromisoformat(current_end) - date.fromisoformat(current_start)
    ).days
    comparison_days = (
        date.fromisoformat(comparison_end)
        - date.fromisoformat(comparison_start)
    ).days
    if current_days != comparison_days:
        raise ToolError(
            "Comparison periods must contain the same number of days."
        )
    current = campaign_performance_snapshot(
        customer_id, current_start, current_end
    )
    comparison = campaign_performance_snapshot(
        customer_id, comparison_start, comparison_end
    )

    def index(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("campaign.id")): row
            for row in report["campaigns"]
            if row.get("campaign.id") is not None
        }

    current_by_id = index(current)
    comparison_by_id = index(comparison)
    findings: list[dict[str, Any]] = []
    for campaign_id in sorted(set(current_by_id) | set(comparison_by_id)):
        current_row = current_by_id.get(campaign_id, {})
        previous_row = comparison_by_id.get(campaign_id, {})
        name = current_row.get("campaign.name") or previous_row.get(
            "campaign.name"
        )
        current_cost = _number(current_row.get("cost"))
        previous_cost = _number(previous_row.get("cost"))
        current_conversions = _number(current_row.get("metrics.conversions"))
        previous_conversions = _number(previous_row.get("metrics.conversions"))
        current_cpa = current_row.get("platform_cpa")
        previous_cpa = previous_row.get("platform_cpa")

        reasons: list[str] = []
        if current_cost >= minimum_spend and current_conversions == 0:
            reasons.append("SPEND_WITH_ZERO_PLATFORM_CONVERSIONS")
        if previous_cost >= minimum_spend and current_cost == 0:
            reasons.append("PREVIOUSLY_SPENDING_NOW_ZERO")
        if previous_cost > 0:
            spend_change = (current_cost - previous_cost) / previous_cost
            if spend_change >= relative_change_threshold:
                reasons.append("SPEND_INCREASE")
            elif spend_change <= -relative_change_threshold:
                reasons.append("SPEND_DECREASE")
        if previous_cpa and current_cpa:
            cpa_change = (current_cpa - previous_cpa) / previous_cpa
            if cpa_change >= relative_change_threshold:
                reasons.append("PLATFORM_CPA_DETERIORATION")
        if (
            previous_conversions > 0
            and current_conversions
            <= previous_conversions * (1 - relative_change_threshold)
        ):
            reasons.append("PLATFORM_CONVERSION_DROP")

        if reasons:
            findings.append(
                {
                    "campaign_id": campaign_id,
                    "campaign_name": name,
                    "reason_codes": sorted(set(reasons)),
                    "current": {
                        "cost": current_cost,
                        "platform_conversions": current_conversions,
                        "platform_cpa": current_cpa,
                    },
                    "comparison": {
                        "cost": previous_cost,
                        "platform_conversions": previous_conversions,
                        "platform_cpa": previous_cpa,
                    },
                    "decision": "INVESTIGATE_NO_AUTOMATIC_CHANGE",
                }
            )
    return {
        "customer_id": current["customer_id"],
        "current_date_range": current["date_range"],
        "comparison_date_range": comparison["date_range"],
        "thresholds": {
            "minimum_spend": minimum_spend,
            "relative_change_threshold": relative_change_threshold,
        },
        "findings": findings,
        "finding_count": len(findings),
        "limitations": [
            "Periods are not automatically adjusted for weekdays or seasonality.",
            "Platform conversions do not include verified lead quality unless separately imported.",
        ],
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def search_term_waste_candidates(
    customer_id: str,
    start_date: str,
    end_date: str,
    minimum_cost: float = 1.0,
    limit: int = 100,
) -> dict[str, Any]:
    """Return costly search terms as review candidates, never auto-negatives.

    A human must confirm clinical relevance, ambiguity, match type, and whether
    the term converted into a qualified/attended patient before exclusion.
    """
    customer_id = require_customer_access(customer_id, "read")
    start_date, end_date = _date_range(start_date, end_date)
    if minimum_cost < 0 or minimum_cost > 100_000:
        raise ToolError("minimum_cost must be between 0 and 100000.")
    if limit < 1 or limit > 200:
        raise ToolError("limit must be between 1 and 200.")
    cost_micros = int(round(minimum_cost * 1_000_000))
    rows, truncated, _ = _bounded_search(
        customer_id,
        [
            "campaign.id",
            "campaign.name",
            "ad_group.id",
            "ad_group.name",
            "search_term_view.search_term",
            "search_term_view.status",
            "metrics.impressions",
            "metrics.clicks",
            "metrics.cost_micros",
            "metrics.conversions",
            "metrics.conversions_value",
        ],
        "search_term_view",
        conditions=[
            f"segments.date BETWEEN '{start_date}' AND '{end_date}'",
            f"metrics.cost_micros >= {cost_micros}",
        ],
        orderings=["metrics.cost_micros DESC"],
        limit=limit,
    )
    candidates = []
    for row in rows:
        cost = _micros(row.get("metrics.cost_micros"))
        conversions = _number(row.get("metrics.conversions"))
        candidates.append(
            {
                **row,
                "cost": cost,
                "platform_cpa": _safe_ratio(cost, conversions),
                "review_status": "HUMAN_REVIEW_REQUIRED",
            }
        )
    return {
        "customer_id": customer_id,
        "date_range": {"start": start_date, "end": end_date},
        "candidates": candidates,
        "is_truncated": truncated,
        "execution_status": "READ_ONLY_NO_NEGATIVES_ADDED",
        "privacy_notice": (
            "Search terms can reveal sensitive intent. Minimize onward sharing, "
            "do not persist them outside the authorized review, and never infer "
            "an identifiable person's health condition."
        ),
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def change_history_timeline(
    customer_id: str,
    start_date: str,
    end_date: str,
    limit: int = 1_000,
) -> dict[str, Any]:
    """Return Google Ads change events for the selected period.

    Google Ads change events are not a login log and may not include every
    possible account-side event. They are evidence of recorded changes only.
    """
    customer_id = require_customer_access(customer_id, "read")
    start_date, end_date = _date_range(start_date, end_date, maximum_days=30)
    if limit < 1 or limit > 10_000:
        raise ToolError("limit must be between 1 and 10000.")
    rows, truncated, truncation_unknown = _bounded_search(
        customer_id,
        [
            "change_event.change_date_time",
            "change_event.user_email",
            "change_event.client_type",
            "change_event.change_resource_type",
            "change_event.resource_change_operation",
            "change_event.changed_fields",
            "change_event.resource_name",
            "campaign.id",
            "campaign.name",
            "ad_group.id",
            "ad_group.name",
        ],
        "change_event",
        conditions=[
            f"change_event.change_date_time >= '{start_date} 00:00:00'",
            f"change_event.change_date_time <= '{end_date} 23:59:59'",
        ],
        orderings=["change_event.change_date_time DESC"],
        limit=limit,
        maximum_api_limit=10_000,
    )
    return {
        "customer_id": customer_id,
        "date_range": {"start": start_date, "end": end_date},
        "events": rows,
        "event_count": len(rows),
        "is_truncated": truncated,
        "truncation_unknown_at_api_maximum": truncation_unknown,
        "limitations": [
            "This is not a sign-in or browser login protocol.",
            "Only changes exposed by Google Ads change_event are included.",
            "The manager's own durable approval/audit ledger is separate.",
        ],
    }


@diagnostics_mcp.tool(annotations=_READ_ANNOTATIONS)
def policy_and_recommendation_snapshot(customer_id: str) -> dict[str, Any]:
    """Return disapproved/limited ads and Google recommendations read-only."""
    customer_id = require_customer_access(customer_id, "read")
    policy_items, policy_truncated, _ = _bounded_search(
        customer_id,
        [
            "campaign.id",
            "campaign.name",
            "ad_group.id",
            "ad_group.name",
            "ad_group_ad.ad.id",
            "ad_group_ad.status",
            "ad_group_ad.ad.type",
            "ad_group_ad.ad_strength",
            "ad_group_ad.policy_summary.approval_status",
            "ad_group_ad.policy_summary.policy_topic_entries",
        ],
        "ad_group_ad",
        conditions=[
            "ad_group_ad.status != 'REMOVED'",
            "ad_group_ad.policy_summary.approval_status != 'APPROVED'",
        ],
        limit=500,
    )
    recommendations, recommendations_truncated, _ = _bounded_search(
        customer_id,
        [
            "recommendation.resource_name",
            "recommendation.type",
            "recommendation.dismissed",
        ],
        "recommendation",
        conditions=["recommendation.dismissed = FALSE"],
        limit=500,
    )
    return {
        "customer_id": customer_id,
        "policy_items": policy_items,
        "recommendations": recommendations,
        "truncation": {
            "policy_items": policy_truncated,
            "recommendations": recommendations_truncated,
        },
        "execution_status": "READ_ONLY_NOT_AUTO_APPLIED",
        "warning": (
            "Google recommendations are platform suggestions, not independent "
            "business recommendations. Review measurement and downstream value first."
        ),
    }
