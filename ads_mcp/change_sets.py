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

"""Durable, human-approved change sets for Google Ads mutations.

Approval is deliberately not expressible as an MCP tool argument. A protected
out-of-band route must derive a :class:`Principal` from authenticated identity
claims and call ``approve_change_set``. The MCP apply tool can only atomically
consume an approval that already exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote, urlparse

from fastmcp.exceptions import ToolError

from ads_mcp.change_set_store import (
    ChangeSetStore,
    ChangeSetStoreError,
    FirestoreChangeSetStore,
    canonical_payload_hash,
)

_ALLOWED_CUSTOMERS_ENV = "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS"
_APPROVER_EMAILS_ENV = "GOOGLE_ADS_MCP_APPROVER_EMAILS"
_APPROVER_ROLES_ENV = "GOOGLE_ADS_MCP_APPROVER_ROLES"
_ENVIRONMENT_ENV = "GOOGLE_ADS_MCP_ENVIRONMENT"
_TTL_ENV = "GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS"
_STORE_TYPE_ENV = "GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE"
# Backward-compatible deployment alias. Supplying both with different values
# is a hard configuration error to avoid silently selecting a weaker backend.
_STORE_TYPE_ALIAS_ENV = "GOOGLE_ADS_MCP_CHANGESET_STORE"
_STORE_COLLECTION_ENV = "GOOGLE_ADS_MCP_CHANGESET_COLLECTION"
_AUDIT_COLLECTION_ENV = "GOOGLE_ADS_MCP_CHANGESET_AUDIT_COLLECTION"
_APPROVAL_BASE_URL_ENV = "GOOGLE_ADS_MCP_APPROVAL_BASE_URL"
_FIRESTORE_PROJECT_ENV = "GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT"
_FIRESTORE_DATABASE_ENV = "GOOGLE_ADS_MCP_STORAGE_FIRESTORE_DATABASE"
_INTEGRITY_KEY_ENV = "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY"
_DEFAULT_TTL_SECONDS = 900
_MAX_TTL_SECONDS = 3600
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_AUDIT_VALUE_LENGTH = 512
_TOKEN_PREFIX = "cs2"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_AUDIT_SENSITIVE_KEY_RE = re.compile(
    r"(?:patient|email|phone|address|lead|gclid|gbraid|wbraid|"
    r"user_identifier|click_id|(?:first|last|full|contact)_?name)",
    re.IGNORECASE,
)

_store_lock = threading.Lock()
_store_override: ChangeSetStore | None = None
_configured_store: ChangeSetStore | None = None


@dataclass(frozen=True)
class Principal:
    """Identity derived from trusted authentication claims by the caller."""

    subject: str
    role: str
    email: str | None = None
    authentication_method: str = "oauth"

    def to_record(self) -> dict[str, str]:
        values = asdict(self)
        return {
            key: value for key, value in values.items() if value is not None
        }


SYSTEM_PRINCIPAL = Principal(
    subject="google-ads-mcp", role="system", authentication_method="service"
)


def normalize_id(value: str, label: str = "ID") -> str:
    """Normalizes a Google Ads ID and rejects unsafe values."""
    if not isinstance(value, str):
        raise ToolError(f"{label} must be a string.")
    normalized = value.replace("-", "").strip()
    if not re.fullmatch(r"[0-9]{1,20}", normalized):
        raise ToolError(f"{label} must contain between 1 and 20 digits.")
    return normalized


def _normalize_customer_id(value: str, label: str) -> str:
    normalized = normalize_id(value, label)
    if not re.fullmatch(r"[0-9]{10}", normalized):
        raise ToolError(f"{label} must contain exactly 10 digits.")
    return normalized


def require_allowed_customer(customer_id: str) -> str:
    """Returns a normalized customer ID if it is explicitly allowlisted."""
    normalized = _normalize_customer_id(customer_id, "customer_id")
    configured = os.environ.get(_ALLOWED_CUSTOMERS_ENV, "")
    allowed = {
        _normalize_customer_id(item, "allowed customer ID")
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


def _current_environment() -> str:
    environment = os.environ.get(_ENVIRONMENT_ENV, "").strip().lower()
    if not environment:
        raise ToolError(
            f"Write tools are disabled: {_ENVIRONMENT_ENV} is not set."
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", environment):
        raise ToolError(
            f"{_ENVIRONMENT_ENV} must be a stable lowercase environment name."
        )
    return environment


def _ttl_seconds() -> int:
    try:
        configured_ttl = int(os.environ.get(_TTL_ENV, _DEFAULT_TTL_SECONDS))
    except ValueError as exc:
        raise ToolError(f"{_TTL_ENV} must be an integer.") from exc
    return max(60, min(configured_ttl, _MAX_TTL_SECONDS))


def _integrity_key() -> bytes:
    value = os.environ.get(_INTEGRITY_KEY_ENV, "")
    if len(value) < 32:
        raise ToolError(
            f"Approvals are disabled: {_INTEGRITY_KEY_ENV} must contain "
            "at least 32 characters."
        )
    return value.encode("utf-8")


def _canonical_json(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "Change-set details must be valid JSON values."
        ) from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ToolError("Change-set details are too large.")
    return encoded


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token(change_set_id: str) -> str:
    return f"{_TOKEN_PREFIX}.{change_set_id}.{secrets.token_urlsafe(32)}"


def _token_change_set_id(token: str) -> str:
    try:
        prefix, change_set_id, secret = token.split(".", 2)
    except (AttributeError, ValueError) as exc:
        raise ToolError("Invalid change-set token.") from exc
    if (
        prefix != _TOKEN_PREFIX
        or not re.fullmatch(r"[0-9a-f]{32}", change_set_id)
        or len(secret) < 32
    ):
        raise ToolError("Invalid change-set token.")
    return change_set_id


def _validated_change_set_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ToolError("Invalid change-set ID.")
    return value


def _validated_execution_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ToolError("Invalid execution ID.")
    return value


def _principal_record(principal: Principal | None) -> dict[str, str]:
    return (principal or SYSTEM_PRINCIPAL).to_record()


def _normalize_email(value: str) -> str:
    normalized = value.strip().casefold()
    if not _EMAIL_RE.fullmatch(normalized):
        raise ToolError("Approver email is invalid.")
    return normalized


def _require_authorized_approver(principal: Principal) -> Principal:
    """Checks identity attributes already verified by the approval route."""
    if (
        not isinstance(principal.subject, str)
        or not principal.subject.strip()
        or len(principal.subject) > 256
    ):
        raise ToolError("Approver identity has an invalid subject.")
    if not isinstance(principal.authentication_method, str) or (
        principal.authentication_method.lower()
        not in {
            "google_oauth",
            "google_oidc",
        }
    ):
        raise ToolError("Approver must use verified Google authentication.")
    if not isinstance(principal.email, str) or not principal.email:
        raise ToolError("Approver identity has no verified email address.")
    normalized_email = _normalize_email(principal.email)
    allowed_emails = {
        _normalize_email(item)
        for item in os.environ.get(_APPROVER_EMAILS_ENV, "").split(",")
        if item.strip()
    }
    if not allowed_emails:
        raise ToolError(
            f"Approvals are disabled: {_APPROVER_EMAILS_ENV} is not set."
        )
    allowed_roles = {
        item.strip().casefold()
        for item in os.environ.get(_APPROVER_ROLES_ENV, "owner").split(",")
        if item.strip()
    }
    if not isinstance(principal.role, str):
        raise ToolError("Approver role is invalid.")
    normalized_role = principal.role.strip().casefold()
    if (
        normalized_email not in allowed_emails
        or normalized_role not in allowed_roles
    ):
        raise ToolError("This identity is not authorized to approve changes.")
    return Principal(
        subject=principal.subject,
        email=normalized_email,
        role=normalized_role,
        authentication_method=principal.authentication_method.lower(),
    )


def validate_approver_identity(principal: Principal) -> Principal:
    """Validate and normalize an approver without changing state.

    Protected review routes use this before disclosing the exact payload. It is
    a server-internal function and must never be mounted as an MCP tool.
    """
    return _require_authorized_approver(principal)


def _create_configured_store() -> ChangeSetStore:
    configured_type = os.environ.get(_STORE_TYPE_ENV, "").strip().lower()
    alias_type = os.environ.get(_STORE_TYPE_ALIAS_ENV, "").strip().lower()
    if configured_type and alias_type and configured_type != alias_type:
        raise ToolError(
            f"{_STORE_TYPE_ENV} conflicts with legacy {_STORE_TYPE_ALIAS_ENV}."
        )
    store_type = configured_type or alias_type or "firestore"
    if store_type != "firestore":
        raise ToolError(
            f"Unsupported {_STORE_TYPE_ENV}: durable production storage must be firestore."
        )
    try:
        return FirestoreChangeSetStore(
            project=os.environ.get(_FIRESTORE_PROJECT_ENV),
            database=os.environ.get(_FIRESTORE_DATABASE_ENV),
            collection=os.environ.get(
                _STORE_COLLECTION_ENV, "google_ads_mcp_change_sets"
            ),
            audit_collection=os.environ.get(
                _AUDIT_COLLECTION_ENV, "google_ads_mcp_change_set_audit"
            ),
        )
    except ChangeSetStoreError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:
        raise ToolError("Durable change-set storage is unavailable.") from exc


def _get_store() -> ChangeSetStore:
    global _configured_store
    if _store_override is not None:
        return _store_override
    with _store_lock:
        if _configured_store is None:
            _configured_store = _create_configured_store()
        return _configured_store


def set_change_set_store_for_testing(store: ChangeSetStore | None) -> None:
    """Injects a store for tests; production code must not call this function."""
    global _store_override
    _store_override = store


def reset_change_set_store() -> None:
    """Drops cached store instances. Intended for isolated test processes."""
    global _configured_store, _store_override
    with _store_lock:
        _configured_store = None
        _store_override = None


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    public = dict(record)
    public.pop("token_hash", None)
    public.pop("payload", None)
    public.pop("failure", None)
    public.pop("uncertainty", None)
    public.pop("approved_integrity_seal", None)
    return public


def approval_url_for_change_set(change_set_id: str) -> str:
    """Builds a token-free URL for the authenticated approval page."""
    change_set_id = _validated_change_set_id(change_set_id)
    base_url = os.environ.get(_APPROVAL_BASE_URL_ENV, "").strip().rstrip("/")
    if not base_url:
        raise ToolError(f"{_APPROVAL_BASE_URL_ENV} is not configured.")
    parsed = urlparse(base_url)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
    }
    if parsed.scheme != "https" and not local_http:
        raise ToolError("Approval base URL must use HTTPS.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ToolError("Approval base URL contains unsupported components.")
    return f"{base_url}/approve/{quote(change_set_id, safe='')}"


def create_change_set(
    details: dict[str, Any], *, actor: Principal | None = None
) -> dict[str, Any]:
    """Creates a durable PENDING change set and returns its opaque bearer token."""
    if not isinstance(details, dict):
        raise ToolError("Change-set details must be an object.")
    payload = json.loads(_canonical_json(details))
    customer_id = require_allowed_customer(str(payload.get("customer_id", "")))
    payload["customer_id"] = customer_id
    environment = _current_environment()
    now = int(time.time())
    change_set_id = uuid.uuid4().hex
    token = _new_token(change_set_id)
    try:
        digest = canonical_payload_hash(payload, customer_id, environment)
    except ChangeSetStoreError as exc:
        raise ToolError("Could not bind the change-set payload.") from exc
    requested_by = _principal_record(actor)
    record = {
        "version": 2,
        "change_set_id": change_set_id,
        "status": "PENDING",
        "environment": environment,
        "customer_id": customer_id,
        "payload": payload,
        "payload_hash": digest,
        "token_hash": _token_hash(token),
        "requested_at": now,
        "requested_by": requested_by,
        "expires_at": now + _ttl_seconds(),
        "updated_at": now,
        "audit_sequence": 1,
    }
    audit_event = {
        "event_id": uuid.uuid4().hex,
        "change_set_id": change_set_id,
        "event_type": "CREATED",
        "sequence": 1,
        "occurred_at": now,
        "actor": requested_by,
        "customer_id": customer_id,
        "environment": environment,
        "payload_hash": digest,
        "metadata": {},
    }
    try:
        _get_store().create(record, audit_event)
    except ChangeSetStoreError as exc:
        raise ToolError("Could not durably create the change set.") from exc

    response = {
        **payload,
        **_public_record(record),
        "change_set_token": token,
        "approval_required": True,
        "approval_channel": "OUT_OF_BAND_AUTHENTICATED_HUMAN",
        "execution_status": "PREVIEW_ONLY_NOT_APPLIED",
    }
    if os.environ.get(_APPROVAL_BASE_URL_ENV, "").strip():
        response["approval_url"] = approval_url_for_change_set(change_set_id)
    return response


def get_change_set_for_review(change_set_id_or_token: str) -> dict[str, Any]:
    """Returns the exact persisted payload for a protected approval surface."""
    if not isinstance(change_set_id_or_token, str):
        raise ToolError("Invalid change-set ID.")
    change_set_id = (
        _token_change_set_id(change_set_id_or_token)
        if change_set_id_or_token.startswith(f"{_TOKEN_PREFIX}.")
        else change_set_id_or_token
    )
    change_set_id = _validated_change_set_id(change_set_id)
    try:
        record = _get_store().get(change_set_id)
    except ChangeSetStoreError as exc:
        raise ToolError("Could not read the change set.") from exc
    if not record:
        raise ToolError("Change set not found.")
    return {**_public_record(record), "details": record["payload"]}


def approve_change_set(
    change_set_id: str,
    *,
    expected_payload_hash: str,
    expected_customer_id: str,
    expected_environment: str,
    approver: Principal,
) -> dict[str, Any]:
    """Approves an exact payload from a protected, non-MCP human route.

    The caller is responsible for deriving ``approver`` from verified OAuth/OIDC
    claims. Never expose this function directly as a model-callable tool.
    """
    change_set_id = _validated_change_set_id(change_set_id)
    if not isinstance(expected_payload_hash, str) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_payload_hash
    ):
        raise ToolError("Reviewed payload hash is invalid.")
    if not isinstance(expected_environment, str):
        raise ToolError("Approval environment is invalid.")
    authorized = _require_authorized_approver(approver)
    customer_id = require_allowed_customer(expected_customer_id)
    environment = _current_environment()
    if expected_environment != environment:
        raise ToolError("Approval environment does not match this deployment.")
    try:
        result = _get_store().approve(
            change_set_id,
            now=int(time.time()),
            expected_payload_hash=expected_payload_hash,
            expected_customer_id=customer_id,
            expected_environment=environment,
            approver=authorized.to_record(),
            integrity_key=_integrity_key(),
        )
    except ChangeSetStoreError as exc:
        raise ToolError("Could not atomically approve the change set.") from exc
    if not result.accepted:
        messages = {
            "EXPIRED": "Change set expired. Generate a new preview.",
            "PAYLOAD_INTEGRITY_MISMATCH": "Stored change-set payload integrity check failed.",
            "PAYLOAD_MISMATCH": "Reviewed payload does not match the stored change set.",
            "CUSTOMER_MISMATCH": "Reviewed customer does not match the stored change set.",
            "ENVIRONMENT_MISMATCH": "Reviewed environment does not match this deployment.",
            "NOT_PENDING": "Change set is not pending approval.",
        }
        raise ToolError(
            messages.get(result.rejection_code, "Approval rejected.")
        )
    return _public_record(result.record)


def verify_change_set(
    change_set_token: str,
    approval_statement: str | None = None,
    *,
    expected_action: str,
    executor: Principal | None = None,
) -> dict[str, Any]:
    """Atomically consumes an already human-approved change set.

    ``approval_statement`` remains as a temporary call-site compatibility
    parameter, but is intentionally ignored and can never authorize a change.
    """
    del approval_statement
    if not isinstance(expected_action, str) or not re.fullmatch(
        r"[a-z][a-z0-9_]{1,127}", expected_action
    ):
        raise ToolError("Expected change-set action is invalid.")
    change_set_id = _token_change_set_id(change_set_token)
    execution_id = uuid.uuid4().hex
    try:
        result = _get_store().consume(
            change_set_id,
            now=int(time.time()),
            token_hash=_token_hash(change_set_token),
            expected_environment=_current_environment(),
            expected_action=expected_action,
            executor=_principal_record(executor),
            execution_id=execution_id,
            integrity_key=_integrity_key(),
        )
    except ChangeSetStoreError as exc:
        raise ToolError("Could not atomically consume the change set.") from exc
    if not result.accepted:
        messages = {
            "EXPIRED": "Change set expired. Generate a new preview.",
            "PAYLOAD_INTEGRITY_MISMATCH": "Stored change-set payload integrity check failed.",
            "APPROVED_PAYLOAD_MISMATCH": "Change-set payload no longer matches the approved payload.",
            "APPROVAL_INTEGRITY_MISMATCH": "Change-set approval integrity check failed.",
            "INVALID_TOKEN": "Invalid change-set token.",
            "ENVIRONMENT_MISMATCH": "Change set belongs to a different environment.",
            "ACTION_MISMATCH": "Change set belongs to a different apply action and was not consumed.",
            "NOT_APPROVED": "Change set has not been approved by an authorized human or was already consumed.",
        }
        raise ToolError(
            messages.get(
                result.rejection_code, "Change-set consumption rejected."
            )
        )
    customer_id = require_allowed_customer(result.record["customer_id"])
    return {
        **result.record["payload"],
        "change_set_id": change_set_id,
        "payload_hash": result.record["payload_hash"],
        "customer_id": customer_id,
        "environment": result.record["environment"],
        "execution_id": execution_id,
    }


def _sanitize_audit_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Limits persisted result/failure metadata and rejects nested payloads."""
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = str(key)[:64]
        if item is None or isinstance(item, (bool, int, float)):
            sanitized[safe_key] = item
        else:
            sanitized[safe_key] = str(item)[:_MAX_AUDIT_VALUE_LENGTH]
    _canonical_json(sanitized)
    return sanitized


def _sanitize_success_result(value: dict[str, Any]) -> dict[str, Any]:
    """Keep useful verification evidence without storing submitted user data."""
    if not isinstance(value, dict):
        raise ToolError("Change-set success result must be an object.")
    target_field = value.get("target_field")
    if target_field is not None and target_field not in {
        "status",
        "amount_micros",
        "exists",
        "final_urls",
    }:
        raise ToolError("target_field is not supported for audit storage.")
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        safe_key = str(key)[:64]
        if _AUDIT_SENSITIVE_KEY_RE.search(safe_key):
            sanitized[safe_key] = "[REDACTED]"
            continue
        if safe_key in {
            "after_snapshot",
            "verified_after_snapshot",
        } and not isinstance(item, bool):
            snapshot_bytes = _canonical_json(item)
            sanitized[f"{safe_key}_sha256"] = hashlib.sha256(
                snapshot_bytes
            ).hexdigest()
            continue
        if safe_key in {
            "result_hash",
            "after_snapshot_hash",
            "verified_after_hash",
        }:
            normalized_hash = str(item).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
                raise ToolError(f"{safe_key} must be a SHA-256 hex digest.")
            sanitized[safe_key] = normalized_hash
            continue
        if safe_key == "request_id":
            request_id = str(item).strip()
            if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,256}", request_id):
                raise ToolError("request_id contains unsupported characters.")
            sanitized[safe_key] = request_id
            continue
        if safe_key == "target_field":
            sanitized[safe_key] = target_field
            continue
        if safe_key == "target_value":
            if target_field == "status" and isinstance(item, str):
                if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", item):
                    raise ToolError("Status target_value is invalid.")
                sanitized[safe_key] = item
            elif (
                target_field == "amount_micros"
                and isinstance(item, int)
                and not isinstance(item, bool)
                and item >= 0
            ):
                sanitized[safe_key] = item
            elif target_field == "exists" and isinstance(item, bool):
                sanitized[safe_key] = item
            elif target_field == "final_urls" and (
                (
                    isinstance(item, str)
                    and len(item) <= 2_048
                    and urlparse(item).scheme in {"http", "https"}
                    and bool(urlparse(item).hostname)
                )
                or (
                    isinstance(item, list)
                    and len(item) <= 10
                    and all(
                        isinstance(url, str)
                        and len(url) <= 2_048
                        and urlparse(url).scheme in {"http", "https"}
                        and bool(urlparse(url).hostname)
                        for url in item
                    )
                )
            ):
                sanitized[safe_key] = item
            else:
                raise ToolError("target_value does not match target_field.")
            continue
        if safe_key == "verified":
            if not isinstance(item, bool):
                raise ToolError("verified must be a boolean.")
            sanitized[safe_key] = item
            continue
        if safe_key == "mutation_count":
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise ToolError(
                    "mutation_count must be a non-negative integer."
                )
            sanitized[safe_key] = item
            continue
        if safe_key in {"execution_status", "resource_name"}:
            if not isinstance(item, str):
                raise ToolError(f"{safe_key} must be a string.")
            sanitized[safe_key] = item[:_MAX_AUDIT_VALUE_LENGTH]
            continue
        structured_bytes = _canonical_json(item)
        sanitized[f"{safe_key}_sha256"] = hashlib.sha256(
            structured_bytes
        ).hexdigest()
    _canonical_json(sanitized)
    return sanitized


def record_change_set_success(
    change_set_id: str,
    execution_id: str,
    *,
    result: dict[str, Any] | None = None,
    executor: Principal | None = None,
) -> dict[str, Any]:
    """Records successful post-mutation verification for a consumed set."""
    change_set_id = _validated_change_set_id(change_set_id)
    execution_id = _validated_execution_id(execution_id)
    try:
        transition = _get_store().record_success(
            change_set_id,
            now=int(time.time()),
            executor=_principal_record(executor),
            execution_id=execution_id,
            result=_sanitize_success_result(result or {}),
        )
    except ChangeSetStoreError as exc:
        raise ToolError("Could not record change-set success.") from exc
    if not transition.accepted:
        raise ToolError(
            "Change-set success does not match the active execution."
        )
    return _public_record(transition.record)


def record_change_set_failure(
    change_set_id: str,
    execution_id: str,
    *,
    reason_code: str,
    message: str,
    executor: Principal | None = None,
) -> dict[str, Any]:
    """Records a safe failure summary without persisting exception payloads."""
    change_set_id = _validated_change_set_id(change_set_id)
    execution_id = _validated_execution_id(execution_id)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason_code):
        raise ToolError("Failure reason code is invalid.")
    failure = _sanitize_audit_mapping(
        {"reason_code": reason_code, "message": message}
    )
    try:
        transition = _get_store().record_failure(
            change_set_id,
            now=int(time.time()),
            executor=_principal_record(executor),
            execution_id=execution_id,
            failure=failure,
        )
    except ChangeSetStoreError as exc:
        raise ToolError("Could not record change-set failure.") from exc
    if not transition.accepted:
        raise ToolError(
            "Change-set failure does not match the active execution."
        )
    return _public_record(transition.record)


def record_change_set_uncertain(
    change_set_id: str,
    execution_id: str,
    *,
    reason_code: str,
    message: str,
    executor: Principal | None = None,
) -> dict[str, Any]:
    """Marks a consumed set uncertain when live mutation outcome is ambiguous."""
    change_set_id = _validated_change_set_id(change_set_id)
    execution_id = _validated_execution_id(execution_id)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason_code):
        raise ToolError("Uncertainty reason code is invalid.")
    uncertainty = _sanitize_audit_mapping(
        {"reason_code": reason_code, "message": message}
    )
    try:
        transition = _get_store().record_uncertain(
            change_set_id,
            now=int(time.time()),
            executor=_principal_record(executor),
            execution_id=execution_id,
            uncertainty=uncertainty,
        )
    except ChangeSetStoreError as exc:
        raise ToolError(
            "Could not record uncertain change-set outcome."
        ) from exc
    if not transition.accepted:
        raise ToolError(
            "Uncertain outcome does not match the active execution."
        )
    return _public_record(transition.record)


def get_change_set_execution_status(
    change_set_id_or_token: str,
    *,
    stale_after_seconds: int = 300,
    now: int | None = None,
) -> dict[str, Any]:
    """Returns execution status and flags a stale in-progress consumption."""
    if stale_after_seconds < 60 or stale_after_seconds > 86_400:
        raise ToolError("stale_after_seconds must be between 60 and 86400.")
    record = get_change_set_for_review(change_set_id_or_token)
    current_time = int(time.time()) if now is None else int(now)
    consumed_at = record.get("consumed_at")
    in_progress = (
        record.get("status") == "CONSUMED"
        and record.get("execution_outcome") == "IN_PROGRESS"
        and isinstance(consumed_at, int)
    )
    age_seconds = max(0, current_time - consumed_at) if in_progress else None
    return {
        "change_set_id": record["change_set_id"],
        "status": record["status"],
        "execution_outcome": record.get("execution_outcome"),
        "execution_id": record.get("execution_id"),
        "consumed_at": consumed_at,
        "in_progress_age_seconds": age_seconds,
        "is_stale_in_progress": bool(
            in_progress
            and age_seconds is not None
            and age_seconds >= stale_after_seconds
        ),
        "audit_sequence": record.get("audit_sequence"),
    }


def list_change_set_audit_events(
    change_set_id: str,
) -> list[dict[str, Any]]:
    """Returns immutable operational events for authorized audit consumers."""
    change_set_id = _validated_change_set_id(change_set_id)
    try:
        return _get_store().list_audit_events(change_set_id)
    except ChangeSetStoreError as exc:
        raise ToolError("Could not read change-set audit events.") from exc
