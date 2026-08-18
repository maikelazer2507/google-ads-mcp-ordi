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

"""Short-lived, signed change sets for guarded Google Ads mutations."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

from fastmcp.exceptions import ToolError

_SIGNING_KEY_ENV = "GOOGLE_ADS_MCP_CHANGESET_SIGNING_KEY"
_ALLOWED_CUSTOMERS_ENV = "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS"
_TTL_ENV = "GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 900
_MAX_TTL_SECONDS = 3600


def normalize_id(value: str, label: str = "ID") -> str:
    """Normalizes a Google Ads ID and rejects unsafe values."""
    normalized = value.replace("-", "").strip()
    if not normalized.isdigit():
        raise ToolError(f"{label} must contain digits only.")
    return normalized


def require_allowed_customer(customer_id: str) -> str:
    """Returns a normalized customer ID if it is explicitly allowlisted."""
    normalized = normalize_id(customer_id, "customer_id")
    configured = os.environ.get(_ALLOWED_CUSTOMERS_ENV, "")
    allowed = {
        normalize_id(item, "allowed customer ID")
        for item in configured.split(",")
        if item.strip()
    }
    if not allowed:
        raise ToolError(
            f"Write tools are disabled: {_ALLOWED_CUSTOMERS_ENV} is not set."
        )
    if normalized not in allowed:
        raise ToolError(
            f"Customer {normalized} is not permitted for write operations."
        )
    return normalized


def _signing_key() -> bytes:
    key = os.environ.get(_SIGNING_KEY_ENV)
    if not key or len(key) < 32:
        raise ToolError(
            f"Write tools are disabled: {_SIGNING_KEY_ENV} must contain "
            "at least 32 characters."
        )
    return key.encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_change_set(details: dict[str, Any]) -> dict[str, Any]:
    """Creates a signed, short-lived change-set token."""
    try:
        configured_ttl = int(os.environ.get(_TTL_ENV, _DEFAULT_TTL_SECONDS))
    except ValueError as exc:
        raise ToolError(f"{_TTL_ENV} must be an integer.") from exc
    ttl = max(60, min(configured_ttl, _MAX_TTL_SECONDS))
    now = int(time.time())
    payload = {
        "version": 1,
        "change_set_id": uuid.uuid4().hex,
        "issued_at": now,
        "expires_at": now + ttl,
        **details,
    }
    encoded_payload = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signature = hmac.new(
        _signing_key(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    token = f"{encoded_payload}.{_b64encode(signature)}"
    return {
        **payload,
        "change_set_token": token,
        "approval_required": True,
        "approval_statement": f"APPROVE {payload['change_set_id']}",
        "execution_status": "PREVIEW_ONLY_NOT_APPLIED",
    }


def verify_change_set(
    change_set_token: str, approval_statement: str
) -> dict[str, Any]:
    """Verifies signature, expiration, and exact approval statement."""
    try:
        encoded_payload, encoded_signature = change_set_token.split(".", 1)
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ToolError("Invalid change-set token.") from exc

    expected_signature = hmac.new(
        _signing_key(), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ToolError("Invalid change-set signature.")

    try:
        payload = json.loads(_b64decode(encoded_payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ToolError("Invalid change-set payload.") from exc

    if payload.get("version") != 1:
        raise ToolError("Unsupported change-set version.")
    if int(payload.get("expires_at", 0)) < int(time.time()):
        raise ToolError("Change set expired. Generate a new preview.")

    expected_approval = f"APPROVE {payload.get('change_set_id', '')}"
    if approval_statement != expected_approval:
        raise ToolError(
            "Explicit approval does not match this change set. "
            f"Expected exactly: {expected_approval}"
        )

    payload["customer_id"] = require_allowed_customer(
        str(payload.get("customer_id", ""))
    )
    return payload
