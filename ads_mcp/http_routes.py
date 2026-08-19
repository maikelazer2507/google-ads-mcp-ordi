# Copyright 2026 Google LLC.

"""Minimal health/readiness endpoints for Cloud Run operations."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ads_mcp.coordinator import mcp, parse_allowed_client_redirect_uris

_EMAIL_PATTERN = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")
_ENVIRONMENT_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_CUSTOMER_ID_PATTERN = re.compile(r"^[0-9]{10}$")


def _valid_https_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and "*" not in parsed.hostname
        and parsed.hostname.casefold() not in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
        and (port is None or 1 <= port <= 65535)
    )


def _customer_ids(value: str) -> set[str] | None:
    result: set[str] = set()
    for item in value.split(","):
        normalized = item.strip().replace("-", "")
        if not normalized or not _CUSTOMER_ID_PATTERN.fullmatch(normalized):
            return None
        result.add(normalized)
    return result or None


def _emails(value: str) -> set[str] | None:
    values = {item.strip().casefold() for item in value.split(",")}
    if (
        not values
        or "" in values
        or not all(_EMAIL_PATTERN.fullmatch(item) for item in values)
    ):
        return None
    return values


def _production_configuration_errors() -> list[str]:
    """Return stable error codes for the secure HTTP configuration contract."""
    errors: list[str] = []
    if os.environ.get("GOOGLE_ADS_MCP_PRODUCTION_MODE", "").strip() != "true":
        errors.append("production-mode")
    if (
        os.environ.get("GOOGLE_ADS_MCP_TRANSPORT", "").strip()
        != "streamable-http"
    ):
        errors.append("transport")

    environment = os.environ.get("GOOGLE_ADS_MCP_ENVIRONMENT", "").strip()
    if not _ENVIRONMENT_PATTERN.fullmatch(environment) or environment in {
        "dev",
        "development",
        "local",
        "test",
    }:
        errors.append("environment")

    required = (
        "GOOGLE_ADS_DEVELOPER_TOKEN",
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID",
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET",
        "GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID",
        "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY",
    )
    if any(not os.environ.get(name, "").strip() for name in required):
        errors.append("required-credentials")

    for name in (
        "GOOGLE_ADS_MCP_BASE_URL",
        "GOOGLE_ADS_MCP_APPROVAL_BASE_URL",
    ):
        if not _valid_https_origin(os.environ.get(name, "").strip()):
            errors.append("public-origin")
            break

    login_id = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    if not _customer_ids(login_id):
        errors.append("login-customer")
    read_ids = _customer_ids(
        os.environ.get("GOOGLE_ADS_MCP_READ_CUSTOMER_IDS", "")
    )
    write_ids = _customer_ids(
        os.environ.get("GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS", "")
    )
    if read_ids is None or write_ids is None or not write_ids <= read_ids:
        errors.append("customer-scope")

    operators = _emails(os.environ.get("GOOGLE_ADS_MCP_OPERATOR_EMAILS", ""))
    approvers = _emails(os.environ.get("GOOGLE_ADS_MCP_APPROVER_EMAILS", ""))
    if operators is None or approvers is None or not approvers <= operators:
        errors.append("identity-scope")

    try:
        redirects = parse_allowed_client_redirect_uris(
            os.environ.get("GOOGLE_ADS_MCP_ALLOWED_CLIENT_REDIRECT_URIS"),
            secure_mode=True,
        )
    except ValueError:
        redirects = []
    if not redirects:
        errors.append("redirect-allowlist")

    if os.environ.get("GOOGLE_ADS_MCP_ALLOW_UNSCOPED_READS") != "false":
        errors.append("unscoped-reads")
    if os.environ.get("GOOGLE_ADS_MCP_ALLOW_SENSITIVE_READS") != "false":
        errors.append("sensitive-reads")
    if (
        os.environ.get("GOOGLE_ADS_MCP_STORAGE_DISABLE_ENCRYPTION", "false")
        != "false"
    ):
        errors.append("storage-encryption")

    if os.environ.get("GOOGLE_ADS_MCP_STORAGE_TYPE") != "firestore":
        errors.append("oauth-storage")
    change_store = os.environ.get("GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE")
    change_store_alias = os.environ.get("GOOGLE_ADS_MCP_CHANGESET_STORE")
    if change_store != "firestore":
        errors.append("change-storage")
    if change_store_alias and change_store_alias != change_store:
        errors.append("storage-alias-conflict")
    firestore_project = os.environ.get(
        "GOOGLE_ADS_MCP_STORAGE_FIRESTORE_PROJECT", ""
    ).strip()
    google_project = os.environ.get("GOOGLE_PROJECT_ID", "").strip()
    if not firestore_project or (
        google_project and firestore_project != google_project
    ):
        errors.append("firestore-project")

    minimum_lengths = {
        "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET": 16,
        "GOOGLE_ADS_MCP_JWT_SIGNING_KEY": 32,
        "GOOGLE_ADS_MCP_STORAGE_ENCRYPTION_KEY": 32,
        "GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY": 32,
        "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": 32,
    }
    if any(
        len(os.environ.get(name, "")) < minimum
        for name, minimum in minimum_lengths.items()
    ):
        errors.append("key-strength")
    return errors


def _json(payload: dict[str, object], status_code: int = 200) -> Response:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
        },
    )


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(_: Request) -> Response:
    """Liveness proves only that the process and router are responsive."""
    return _json({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"], include_in_schema=False)
async def readyz(_: Request) -> Response:
    """Readiness fails closed when production safety settings are incomplete."""
    errors = _production_configuration_errors()
    if errors:
        return _json({"status": "not_ready"}, status_code=503)
    return _json({"status": "ready"})
