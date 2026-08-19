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

"""Fail-closed account scoping for every Google Ads read and write."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

from fastmcp.exceptions import ToolError

_CUSTOMER_ID_PATTERN = re.compile(r"^[0-9]{10}$")
_READ_CUSTOMERS_ENV = "GOOGLE_ADS_MCP_READ_CUSTOMER_IDS"
_WRITE_CUSTOMERS_ENV = "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS"
_ALLOW_UNSCOPED_ENV = "GOOGLE_ADS_MCP_ALLOW_UNSCOPED_READS"
_OPERATOR_EMAILS_ENV = "GOOGLE_ADS_MCP_OPERATOR_EMAILS"
_PRODUCTION_MODE_ENV = "GOOGLE_ADS_MCP_PRODUCTION_MODE"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class AuthenticatedActor:
    """Identity derived only from the FastMCP OAuth access token."""

    subject: str
    email: str


def normalize_customer_id(value: str) -> str:
    """Return a canonical ten-digit customer ID or fail safely."""
    normalized = str(value).replace("-", "").strip()
    if not _CUSTOMER_ID_PATTERN.fullmatch(normalized):
        raise ToolError("customer_id must contain exactly 10 digits.")
    return normalized


def current_operator() -> AuthenticatedActor | None:
    """Validate the current OAuth identity against the operator allowlist.

    Local/stdio development remains usable without OAuth only when neither
    production mode nor an operator allowlist is configured.
    """
    production = (
        os.environ.get(_PRODUCTION_MODE_ENV, "").strip() == "true"
        or os.environ.get("GOOGLE_ADS_MCP_TRANSPORT", "").strip()
        == "streamable-http"
    )
    configured = os.environ.get(_OPERATOR_EMAILS_ENV, "")
    allowed = {
        item.strip().casefold()
        for item in configured.split(",")
        if item.strip()
    }
    if production and not allowed:
        raise ToolError(
            f"Operator access is disabled: configure {_OPERATOR_EMAILS_ENV}."
        )

    from fastmcp.server.dependencies import get_access_token

    token = get_access_token()
    if token is None:
        if production or allowed:
            raise ToolError("An authenticated operator identity is required.")
        return None
    claims = token.claims or {}
    email_claim = claims.get("email")
    if not isinstance(email_claim, str) or not _EMAIL_PATTERN.fullmatch(
        email_claim.strip()
    ):
        raise ToolError("Authenticated operator has no valid email claim.")
    email = email_claim.strip().casefold()
    if allowed and email not in allowed:
        raise ToolError("This identity is not authorized as an operator.")
    subject = token.subject or claims.get("sub") or token.client_id
    if not subject:
        raise ToolError("Authenticated operator has no stable subject.")
    return AuthenticatedActor(subject=str(subject), email=email)


def configured_customer_ids(access: str = "read") -> frozenset[str]:
    """Return the configured customer scope for the requested access type."""
    if access not in {"read", "write"}:
        raise ValueError("access must be 'read' or 'write'")

    configured = os.environ.get(
        _READ_CUSTOMERS_ENV if access == "read" else _WRITE_CUSTOMERS_ENV,
        "",
    )

    values: set[str] = set()
    for item in configured.split(","):
        if item.strip():
            values.add(normalize_customer_id(item))
    return frozenset(values)


def require_customer_access(customer_id: str, access: str = "read") -> str:
    """Enforce an explicit per-deployment customer allowlist."""
    current_operator()
    normalized = normalize_customer_id(customer_id)
    allowed = configured_customer_ids(access)

    # Upstream/local development can opt in to unscoped reads. Production
    # remains fail-closed unless an account list is configured.
    allow_unscoped = os.environ.get(_ALLOW_UNSCOPED_ENV, "").lower() in {
        "1",
        "true",
        "yes",
    }
    if access == "read" and not allowed and allow_unscoped:
        return normalized

    if not allowed:
        variable = (
            _READ_CUSTOMERS_ENV if access == "read" else _WRITE_CUSTOMERS_ENV
        )
        raise ToolError(
            f"{access.title()} access is disabled: configure {variable}."
        )
    if normalized not in allowed:
        raise ToolError(
            f"Customer {normalized} is outside this deployment's {access} scope."
        )
    return normalized


def filter_accessible_customers(resource_names: Iterable[str]) -> list[str]:
    """Return only directly accessible customers inside the configured scope."""
    current_operator()
    allowed = configured_customer_ids("read")
    allow_unscoped = os.environ.get(_ALLOW_UNSCOPED_ENV, "").lower() in {
        "1",
        "true",
        "yes",
    }
    if not allowed and not allow_unscoped:
        raise ToolError(
            f"Read access is disabled: configure {_READ_CUSTOMERS_ENV}."
        )

    result: list[str] = []
    for resource_name in resource_names:
        customer_id = normalize_customer_id(
            str(resource_name).removeprefix("customers/")
        )
        if (allow_unscoped and not allowed) or customer_id in allowed:
            result.append(customer_id)
    return sorted(set(result))
