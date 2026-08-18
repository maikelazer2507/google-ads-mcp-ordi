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

import os
from typing import Any
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

import ads_mcp.utils as utils
from ads_mcp.change_sets import (
    create_change_set,
    normalize_id,
    require_allowed_customer,
    verify_change_set,
)

changes_mcp = FastMCP("changes")

_PREVIEW_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_APPLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


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


def _campaign_state(customer_id: str, campaign_id: str) -> dict[str, Any]:
    campaign_id = normalize_id(campaign_id, "campaign_id")
    row = _single_row(
        customer_id,
        "SELECT campaign.id, campaign.name, campaign.resource_name, "
        "campaign.status, campaign.campaign_budget "
        f"FROM campaign WHERE campaign.id = {campaign_id}",
    )
    return {
        "id": str(row.campaign.id),
        "name": row.campaign.name,
        "resource_name": row.campaign.resource_name,
        "status": row.campaign.status.name,
        "campaign_budget": row.campaign.campaign_budget,
    }


def _budget_state(customer_id: str, budget_id: str) -> dict[str, Any]:
    budget_id = normalize_id(budget_id, "budget_id")
    row = _single_row(
        customer_id,
        "SELECT campaign_budget.id, campaign_budget.name, "
        "campaign_budget.resource_name, campaign_budget.amount_micros, "
        "campaign_budget.explicitly_shared, campaign_budget.reference_count "
        f"FROM campaign_budget WHERE campaign_budget.id = {budget_id}",
    )
    return {
        "id": str(row.campaign_budget.id),
        "name": row.campaign_budget.name,
        "resource_name": row.campaign_budget.resource_name,
        "amount_micros": int(row.campaign_budget.amount_micros),
        "amount": int(row.campaign_budget.amount_micros) / 1_000_000,
        "explicitly_shared": bool(row.campaign_budget.explicitly_shared),
        "reference_count": int(row.campaign_budget.reference_count),
    }


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
        "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
        "ad_group_criterion.resource_name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.status, ad_group_criterion.negative, "
        "ad_group_criterion.keyword.text, "
        "ad_group_criterion.keyword.match_type "
        "FROM ad_group_criterion WHERE "
        f"ad_group_criterion.resource_name = {_gaql_string(resource_name)}",
    )
    return {
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "ad_group_id": str(row.ad_group.id),
        "ad_group_name": row.ad_group.name,
        "criterion_id": str(row.ad_group_criterion.criterion_id),
        "resource_name": row.ad_group_criterion.resource_name,
        "status": row.ad_group_criterion.status.name,
        "negative": bool(row.ad_group_criterion.negative),
        "text": row.ad_group_criterion.keyword.text,
        "match_type": row.ad_group_criterion.keyword.match_type.name,
    }


def _ad_state(customer_id: str, ad_group_id: str, ad_id: str) -> dict[str, Any]:
    ad_group_id = normalize_id(ad_group_id, "ad_group_id")
    ad_id = normalize_id(ad_id, "ad_id")
    resource_name = f"customers/{customer_id}/adGroupAds/{ad_group_id}~{ad_id}"
    row = _single_row(
        customer_id,
        "SELECT campaign.id, campaign.name, ad_group.id, ad_group.name, "
        "ad_group_ad.resource_name, ad_group_ad.status, ad_group_ad.ad.id, "
        "ad_group_ad.ad.final_urls FROM ad_group_ad WHERE "
        f"ad_group_ad.resource_name = {_gaql_string(resource_name)}",
    )
    return {
        "campaign_id": str(row.campaign.id),
        "campaign_name": row.campaign.name,
        "ad_group_id": str(row.ad_group.id),
        "ad_group_name": row.ad_group.name,
        "ad_id": str(row.ad_group_ad.ad.id),
        "resource_name": row.ad_group_ad.resource_name,
        "status": row.ad_group_ad.status.name,
        "final_urls": list(row.ad_group_ad.ad.final_urls),
    }


def _validate_final_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ToolError("Landing-page URL must be a valid HTTPS URL.")
    configured = os.environ.get("GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS", "")
    allowed_hosts = {
        host.strip().lower() for host in configured.split(",") if host.strip()
    }
    if not allowed_hosts:
        raise ToolError(
            "Landing-page changes are disabled: "
            "GOOGLE_ADS_MCP_ALLOWED_FINAL_URL_HOSTS is not set."
        )
    hostname = parsed.hostname.lower()
    if hostname not in allowed_hosts:
        raise ToolError(f"Landing-page host {hostname} is not allowlisted.")
    return url


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
        customer_id=customer_id, operations=[operation]
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
    """Validates and previews pausing or enabling one campaign.

    This tool never changes account state. It returns a signed change set that
    can only be applied by `apply_campaign_status_change` after the user has
    explicitly approved the exact preview.
    """
    customer_id = require_allowed_customer(customer_id)
    new_status = new_status.upper()
    if new_status not in {"ENABLED", "PAUSED"}:
        raise ToolError("new_status must be ENABLED or PAUSED.")
    current = _campaign_state(customer_id, campaign_id)
    if current["status"] == new_status:
        raise ToolError(f"Campaign is already {new_status}.")
    _validate_campaign_status(customer_id, current["resource_name"], new_status)
    return create_change_set(
        {
            "action": "campaign_status",
            "customer_id": customer_id,
            "object": {
                "type": "campaign",
                "id": current["id"],
                "name": current["name"],
                "resource_name": current["resource_name"],
            },
            "current": {"status": current["status"]},
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Campaign delivery will stop or resume.",
            "observation_window": observation_window,
            "rollback": {"status": current["status"]},
        }
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_campaign_status_change(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Applies one previously previewed campaign-status change.

    Never call this tool unless the user explicitly approved the exact change
    set in the current conversation. The live state is re-read before writing.
    """
    payload = verify_change_set(change_set_token, approval_statement)
    if payload["action"] != "campaign_status":
        raise ToolError("Change set is not for a campaign-status change.")
    current = _campaign_state(payload["customer_id"], payload["object"]["id"])
    if current["status"] != payload["current"]["status"]:
        raise ToolError("Live campaign state changed. Generate a new preview.")
    resource_name = _mutate_campaign_status(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["status"],
    )
    verified = _campaign_state(payload["customer_id"], payload["object"]["id"])
    if verified["status"] != payload["proposed"]["status"]:
        raise ToolError("Mutation returned but verification did not match.")
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {"status": verified["status"]},
        "rollback": payload["rollback"],
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
        customer_id=customer_id, operations=[operation]
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
    customer_id = require_allowed_customer(customer_id)
    if new_daily_amount <= 0:
        raise ToolError("new_daily_amount must be greater than zero.")
    current = _budget_state(customer_id, budget_id)
    if current["explicitly_shared"] or current["reference_count"] > 1:
        raise ToolError(
            "Shared budgets are blocked because one edit can affect multiple "
            "campaigns."
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
    return create_change_set(
        {
            "action": "campaign_budget",
            "customer_id": customer_id,
            "object": {
                "type": "campaign_budget",
                "id": current["id"],
                "name": current["name"],
                "resource_name": current["resource_name"],
            },
            "current": {
                "amount_micros": current["amount_micros"],
                "amount": current["amount"],
            },
            "proposed": {
                "amount_micros": proposed_micros,
                "amount": proposed_micros / 1_000_000,
            },
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Daily advertising spend can change.",
            "observation_window": observation_window,
            "rollback": {
                "amount_micros": current["amount_micros"],
                "amount": current["amount"],
            },
        }
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_campaign_budget_change(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Applies one explicitly approved, previously previewed budget change."""
    payload = verify_change_set(change_set_token, approval_statement)
    if payload["action"] != "campaign_budget":
        raise ToolError("Change set is not for a campaign-budget change.")
    current = _budget_state(payload["customer_id"], payload["object"]["id"])
    if current["explicitly_shared"] or current["reference_count"] > 1:
        raise ToolError(
            "Live budget is now shared. Generate a new preview instead of "
            "applying this change."
        )
    if current["amount_micros"] != payload["current"]["amount_micros"]:
        raise ToolError("Live budget changed. Generate a new preview.")
    resource_name = _mutate_budget(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["amount_micros"],
    )
    verified = _budget_state(payload["customer_id"], payload["object"]["id"])
    if verified["amount_micros"] != payload["proposed"]["amount_micros"]:
        raise ToolError("Mutation returned but verification did not match.")
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
        customer_id=customer_id, operations=[operation]
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
    customer_id = require_allowed_customer(customer_id)
    new_status = new_status.upper()
    if new_status not in {"ENABLED", "PAUSED"}:
        raise ToolError("new_status must be ENABLED or PAUSED.")
    current = _keyword_state(customer_id, ad_group_id, criterion_id)
    if current["negative"]:
        raise ToolError("Negative keywords cannot be enabled or paused here.")
    if current["status"] == new_status:
        raise ToolError(f"Keyword is already {new_status}.")
    _validate_keyword_status(customer_id, current["resource_name"], new_status)
    return create_change_set(
        {
            "action": "keyword_status",
            "customer_id": customer_id,
            "object": {
                "type": "keyword",
                **current,
            },
            "current": {"status": current["status"]},
            "proposed": {"status": new_status},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "Search-query eligibility can change.",
            "observation_window": observation_window,
            "rollback": {"status": current["status"]},
        }
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_keyword_status_change(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Applies one explicitly approved keyword-status change."""
    payload = verify_change_set(change_set_token, approval_statement)
    if payload["action"] != "keyword_status":
        raise ToolError("Change set is not for a keyword-status change.")
    obj = payload["object"]
    current = _keyword_state(
        payload["customer_id"], obj["ad_group_id"], obj["criterion_id"]
    )
    if current["status"] != payload["current"]["status"]:
        raise ToolError("Live keyword state changed. Generate a new preview.")
    resource_name = _mutate_keyword_status(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["status"],
    )
    verified = _keyword_state(
        payload["customer_id"], obj["ad_group_id"], obj["criterion_id"]
    )
    if verified["status"] != payload["proposed"]["status"]:
        raise ToolError("Mutation returned but verification did not match.")
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {"status": verified["status"]},
        "rollback": payload["rollback"],
    }


def _negative_keyword_exists(
    customer_id: str, ad_group_id: str, text: str, match_type: str
) -> bool:
    query = (
        "SELECT ad_group_criterion.resource_name FROM ad_group_criterion "
        f"WHERE ad_group.id = {normalize_id(ad_group_id, 'ad_group_id')} "
        "AND ad_group_criterion.negative = TRUE "
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
    customer_id = require_allowed_customer(customer_id)
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
    _mutate_negative_keyword(
        customer_id,
        ad_group_id,
        keyword_text,
        match_type,
        validate_only=True,
    )
    return create_change_set(
        {
            "action": "add_negative_keyword",
            "customer_id": customer_id,
            "object": {
                "type": "ad_group_negative_keyword",
                "ad_group_id": ad_group_id,
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
        }
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_add_negative_keyword(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Adds one explicitly approved negative keyword after a duplicate check."""
    payload = verify_change_set(change_set_token, approval_statement)
    if payload["action"] != "add_negative_keyword":
        raise ToolError("Change set is not for a negative keyword.")
    obj = payload["object"]
    proposed = payload["proposed"]
    if _negative_keyword_exists(
        payload["customer_id"],
        obj["ad_group_id"],
        proposed["text"],
        proposed["match_type"],
    ):
        raise ToolError("Live state changed: negative keyword already exists.")
    resource_name = _mutate_negative_keyword(
        payload["customer_id"],
        obj["ad_group_id"],
        proposed["text"],
        proposed["match_type"],
        validate_only=False,
    )
    if not _negative_keyword_exists(
        payload["customer_id"],
        obj["ad_group_id"],
        proposed["text"],
        proposed["match_type"],
    ):
        raise ToolError("Mutation returned but verification did not match.")
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {**proposed, "exists": True},
        "rollback": payload["rollback"],
    }


def _mutate_final_url(
    customer_id: str,
    resource_name: str,
    final_url: str,
    validate_only: bool,
) -> str | None:
    service = utils.get_googleads_service("AdGroupAdService")
    operation = utils.get_googleads_type("AdGroupAdOperation")
    operation.update.resource_name = resource_name
    operation.update.ad.final_urls.append(final_url)
    operation.update_mask.paths.append("ad.final_urls")
    response = service.mutate_ad_group_ads(
        customer_id=customer_id,
        operations=[operation],
        validate_only=validate_only,
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
    """Validates and previews replacing all final URLs on one ad."""
    customer_id = require_allowed_customer(customer_id)
    new_final_url = _validate_final_url(new_final_url)
    current = _ad_state(customer_id, ad_group_id, ad_id)
    if current["final_urls"] == [new_final_url]:
        raise ToolError("Ad already uses the proposed final URL.")
    _mutate_final_url(
        customer_id,
        current["resource_name"],
        new_final_url,
        validate_only=True,
    )
    return create_change_set(
        {
            "action": "ad_final_url",
            "customer_id": customer_id,
            "object": {"type": "ad_group_ad", **current},
            "current": {"final_urls": current["final_urls"]},
            "proposed": {"final_urls": [new_final_url]},
            "reason": reason,
            "expected_effect": expected_effect,
            "risk": "The ad may return to policy review and traffic can change.",
            "observation_window": observation_window,
            "rollback": {"final_urls": current["final_urls"]},
        }
    )


@changes_mcp.tool(annotations=_APPLY_ANNOTATIONS)
def apply_ad_final_url_change(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Applies one explicitly approved final-URL replacement."""
    payload = verify_change_set(change_set_token, approval_statement)
    if payload["action"] != "ad_final_url":
        raise ToolError("Change set is not for an ad final URL.")
    obj = payload["object"]
    current = _ad_state(
        payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
    )
    if current["final_urls"] != payload["current"]["final_urls"]:
        raise ToolError("Live ad URL changed. Generate a new preview.")
    resource_name = _mutate_final_url(
        payload["customer_id"],
        current["resource_name"],
        payload["proposed"]["final_urls"][0],
        validate_only=False,
    )
    verified = _ad_state(
        payload["customer_id"], obj["ad_group_id"], obj["ad_id"]
    )
    if verified["final_urls"] != payload["proposed"]["final_urls"]:
        raise ToolError("Mutation returned but verification did not match.")
    return {
        "change_set_id": payload["change_set_id"],
        "execution_status": "APPLIED_AND_VERIFIED",
        "resource_name": resource_name,
        "before": payload["current"],
        "after": {"final_urls": verified["final_urls"]},
        "rollback": payload["rollback"],
    }
