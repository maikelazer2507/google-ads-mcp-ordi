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

"""Bounded, account-scoped Google Ads API search tools."""

import logging
import re
from typing import Any, Dict, List
from fastmcp import FastMCP
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

search_mcp = FastMCP("search")

import ads_mcp.utils as utils
from ads_mcp.access_policy import require_customer_access
from google.ads.googleads.errors import GoogleAdsException
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2_000
_CHANGE_EVENT_MAX_LIMIT = 10_000
_MAX_FIELDS = 100
_MAX_FILTERS = 30
_MAX_ORDERINGS = 10
_MAX_FILTER_LENGTH = 1_000
_FORBIDDEN_FILTER_TOKENS = re.compile(
    r"(?:;|--|/\*|\*/|\bSELECT\b|\bFROM\b|\bWHERE\b|"
    r"\bORDER\s+BY\b|\bLIMIT\b|\bPARAMETERS\b)",
    re.IGNORECASE,
)
_SENSITIVE_RESOURCES = frozenset(
    {
        "lead_form_submission_data",
        "local_services_lead",
        "local_services_lead_conversation",
        "offline_user_data_job",
        "click_view",
    }
)


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ToolError(f"Invalid {label}: {value!r}.")
    return value


def _validate_filter(value: str) -> str:
    if not value or len(value) > _MAX_FILTER_LENGTH:
        raise ToolError(
            f"Each condition must contain 1-{_MAX_FILTER_LENGTH} characters."
        )
    if _FORBIDDEN_FILTER_TOKENS.search(value):
        raise ToolError("A condition contains a forbidden GAQL query clause.")
    return value


def _resolved_limit(resource: str, requested: int | None) -> int:
    maximum = (
        _CHANGE_EVENT_MAX_LIMIT if resource == "change_event" else _MAX_LIMIT
    )
    if requested is None:
        return min(_DEFAULT_LIMIT, maximum)
    if isinstance(requested, bool) or not isinstance(requested, int):
        raise ToolError("limit must be an integer.")
    if requested < 1 or requested > maximum:
        raise ToolError(f"limit must be between 1 and {maximum}.")
    return requested


def search(
    customer_id: str,
    fields: List[str],
    resource: str,
    conditions: List[str] = [],
    orderings: List[str] = [],
    limit: int | None = None,
) -> List[Dict[str, Any]]:
    """Fetches data from the Google Ads API using the search method

    Args:
        customer_id: The id of the customer
        fields: The fields to fetch
        resource: The resource to return fields from
        conditions: List of conditions to filter the data, combined using AND clauses
        orderings: How the data is ordered
        limit: The maximum number of rows to return

    """

    customer_id = require_customer_access(customer_id, "read")
    conditions = list(conditions or [])
    orderings = list(orderings or [])
    resource = _validate_identifier(resource, "resource")
    if resource in _SENSITIVE_RESOURCES:
        raise ToolError(
            f"Resource {resource} is blocked because it can expose lead or "
            "pseudonymous customer data. Use aggregate diagnostics instead."
        )
    if not fields or len(fields) > _MAX_FIELDS:
        raise ToolError(f"fields must contain 1-{_MAX_FIELDS} entries.")
    fields = [_validate_identifier(field, "field") for field in fields]
    if len(conditions) > _MAX_FILTERS:
        raise ToolError(f"At most {_MAX_FILTERS} conditions are allowed.")
    conditions = [_validate_filter(condition) for condition in conditions]
    if len(orderings) > _MAX_ORDERINGS:
        raise ToolError(f"At most {_MAX_ORDERINGS} orderings are allowed.")
    orderings = [_validate_filter(ordering) for ordering in orderings]
    resolved_limit = _resolved_limit(resource, limit)

    ga_service = utils.get_googleads_service("GoogleAdsService")

    query_parts = [f"SELECT {','.join(fields)} FROM {resource}"]

    if conditions:
        query_parts.append(f" WHERE {' AND '.join(conditions)}")

    if orderings:
        query_parts.append(f" ORDER BY {','.join(orderings)}")

    query_parts.append(f" LIMIT {resolved_limit}")

    query_parts.append(" PARAMETERS omit_unselected_resource_names=true")

    query = "".join(query_parts)
    # Do not log GAQL conditions: search strings can contain sensitive intent.
    logger.info(
        "Google Ads search customer=%s resource=%s fields=%d limit=%d",
        customer_id,
        resource,
        len(fields),
        resolved_limit,
    )

    try:
        query_result = ga_service.search_stream(
            customer_id=customer_id, query=query
        )

        final_output: List = []
        for batch in query_result:
            for row in batch.results:
                final_output.append(
                    utils.format_output_row(row, batch.field_mask.paths)
                )
                if len(final_output) >= resolved_limit:
                    return final_output
        return final_output
    except GoogleAdsException as ex:
        error_msgs = [
            f"Google Ads API Error: {error.message}"
            for error in ex.failure.errors
        ]
        raise ToolError(
            f"Request ID: {ex.request_id}\n" + "\n".join(error_msgs)
        )


def _search_tool_description() -> str:
    """Returns the description for the `search` tool."""
    # Add a warning that will be part of the description
    file_content = (
        "WARNING: The list of valid resources is missing. "
        "Tool may not function correctly."
    )

    try:
        with open(utils.get_gaql_resources_filepath(), "r") as file:
            file_content = file.read()
    except FileNotFoundError:
        utils.logger.error("The specified file was not found.")

    return f"""
{search.__doc__}

### Hints
    Language Grammar can be found at https://developers.google.com/google-ads/api/docs/query/grammar
    All resources and descriptions are found at https://developers.google.com/google-ads/api/fields/latest/overview
    If the query fails, a ToolError will be raised with the error details.

    For Conversion issues try looking in offline_conversion_upload_conversion_action_summary

### Hint for customer_id
    should be a string of numbers without punctuation
    if presented in the form 123-456-7890 remove the hyphens and use 1234567890

### Hints for Dates
    All dates should be in the form YYYY-MM-DD and must include the dashes (-)
    Date ranges must be finite and must include a start and end date

### Hints for limits
    A LIMIT is always enforced. The default is 500, the maximum is 2000, and
    change_event allows up to 10000.

### Privacy
    Lead-form, local-services-lead, click-level, and offline-user-data resources
    are always blocked in this dental-practice edition. Use aggregate conversion
    resources instead. Query filter text is not written to application logs.

### Hints for conversions questions
    https://developers.google.com/google-ads/api/docs/conversions/upload-summaries 


### Hints for all resources
    To find out which specific fields (including compatible metrics and segments) you can select, filter by, or sort by for a given resource, you MUST use the `get_resource_metadata` tool.
    Do not guess the fields. Use the tool to look them up.
    Once you have the fields, ensure the whole field name is used (e.g., 'campaign.id', not just 'id'). Wildcards and partial fields are not allowed.

### Valid resources
    What follows is a list of valid resources that can be queried.
    {file_content}
"""


# The `search` tool requires a more complex description that's generated at
# runtime. Uses the `add_tool` method instead of an annnotation since `add_tool`
# provides the flexibility needed to generate the description while also
# including the `search` method's docstring.
search.__doc__ = _search_tool_description()
search_mcp.add_tool(
    Tool.from_function(search, annotations=ToolAnnotations(readOnlyHint=True))
)
