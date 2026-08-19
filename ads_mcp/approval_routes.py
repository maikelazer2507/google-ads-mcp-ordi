# Copyright 2026 Google LLC.

"""Out-of-band, human-only approval routes.

These routes are intentionally not MCP tools. A Google ID token authenticates
the approver, then a short-lived server-signed review session lets that same
browser confirm the exact persisted payload in a second step.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs

import anyio
from fastmcp.exceptions import ToolError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from ads_mcp.change_sets import (
    Principal,
    approval_url_for_change_set,
    approve_change_set,
    get_change_set_for_review,
    validate_approver_identity,
)
from ads_mcp.coordinator import mcp

_APPROVAL_CLIENT_ID_ENV = "GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID"
_APPROVAL_SESSION_KEY_ENV = "GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY"
_SESSION_TTL_SECONDS = 300
_MAX_FORM_BYTES = 32 * 1024


def _security_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store, max-age=0",
        "Content-Security-Policy": (
            "default-src 'none'; base-uri 'none'; form-action 'self' "
            "https://accounts.google.com; frame-ancestors 'none'; "
            "script-src https://accounts.google.com/gsi/client; "
            "frame-src https://accounts.google.com/gsi/; "
            "connect-src https://accounts.google.com/gsi/; "
            "style-src 'unsafe-inline'"
        ),
        "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def _html_page(title: str, body: str, status_code: int = 200) -> Response:
    document = f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 20px;color:#17212b}}
main{{border:1px solid #d8dee4;border-radius:12px;padding:24px;box-shadow:0 2px 12px #0001}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#f6f8fa;padding:16px;border-radius:8px}}
.warning{{background:#fff4ce;padding:12px;border-radius:8px}}button{{font:inherit;padding:10px 16px}}
</style></head><body><main>{body}</main></body></html>"""
    return HTMLResponse(
        document, status_code=status_code, headers=_security_headers()
    )


def _error_page(message: str, status_code: int = 400) -> Response:
    return _html_page(
        "Freigabe nicht möglich",
        f"<h1>Freigabe nicht möglich</h1><p>{html.escape(message)}</p>",
        status_code,
    )


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ToolError("Approval form is too large.")
    try:
        parsed = parse_qs(
            body.decode("utf-8"),
            strict_parsing=True,
            max_num_fields=10,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise ToolError("Approval form is invalid.") from exc
    return {key: values[0] for key, values in parsed.items() if values}


def _session_key() -> bytes:
    value = os.environ.get(_APPROVAL_SESSION_KEY_ENV, "")
    if len(value) < 32:
        raise ToolError(
            f"Approvals are disabled: {_APPROVAL_SESSION_KEY_ENV} must contain "
            "at least 32 characters."
        )
    return value.encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]*", value)
        or len(value) % 4 == 1
    ):
        raise ValueError("Invalid canonical base64url value.")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Invalid canonical base64url value.") from exc
    if not hmac.compare_digest(_b64encode(decoded), value):
        raise ValueError("Non-canonical base64url value.")
    return decoded


def _create_review_session(
    record: dict[str, Any], claims: dict[str, Any]
) -> str:
    now = int(time.time())
    payload = {
        "version": 1,
        "change_set_id": record["change_set_id"],
        "payload_hash": record["payload_hash"],
        "customer_id": record["customer_id"],
        "environment": record["environment"],
        "subject": claims["sub"],
        "email": claims["email"],
        "issued_at": now,
        "expires_at": now + _SESSION_TTL_SECONDS,
        "nonce": secrets.token_hex(16),
    }
    encoded = _b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )
    signature = hmac.new(
        _session_key(), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def _verify_review_session(
    token: str, expected_change_set_id: str
) -> dict[str, Any]:
    try:
        encoded, encoded_signature = token.split(".", 1)
        supplied_signature = _b64decode(encoded_signature)
    except (TypeError, ValueError) as exc:
        raise ToolError("Invalid approval session.") from exc
    expected_signature = hmac.new(
        _session_key(), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ToolError("Invalid approval session.")
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ToolError("Invalid approval session.") from exc
    if payload.get("version") != 1:
        raise ToolError("Unsupported approval session.")
    if not hmac.compare_digest(
        str(payload.get("change_set_id", "")), expected_change_set_id
    ):
        raise ToolError("Approval session belongs to a different change set.")
    if int(payload.get("expires_at", 0)) < int(time.time()):
        raise ToolError("Approval session expired. Sign in again.")
    return payload


def _verify_google_credential(credential: str) -> dict[str, Any]:
    client_id = os.environ.get(_APPROVAL_CLIENT_ID_ENV, "").strip()
    if not client_id:
        raise ToolError(
            f"Approvals are disabled: {_APPROVAL_CLIENT_ID_ENV} is not set."
        )
    try:
        claims = id_token.verify_oauth2_token(
            credential, GoogleAuthRequest(), client_id
        )
    except Exception as exc:
        raise ToolError("Google identity could not be verified.") from exc
    issuer = claims.get("iss")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise ToolError("Google identity issuer is invalid.")
    if claims.get("email_verified") is not True:
        raise ToolError("Google email address is not verified.")
    if not claims.get("sub") or not claims.get("email"):
        raise ToolError("Google identity is incomplete.")
    return claims


@mcp.custom_route(
    "/approve/{change_set_id}", methods=["GET"], include_in_schema=False
)
async def approval_login(request: Request) -> Response:
    """Show Google sign-in without exposing change details publicly."""
    change_set_id = request.path_params["change_set_id"]
    try:
        record = get_change_set_for_review(change_set_id)
        if record["status"] != "PENDING":
            return _error_page(
                f"Dieser Änderungssatz hat den Status {record['status']}.", 409
            )
        client_id = os.environ[_APPROVAL_CLIENT_ID_ENV]
        login_uri = approval_url_for_change_set(change_set_id) + "/identity"
    except (KeyError, ToolError) as exc:
        return _error_page(str(exc), 404)

    body = f"""
<h1>Google-Ads-Änderung prüfen</h1>
<p>Bitte melde dich mit dem freigabeberechtigten Google-Konto an. Die genauen
Änderungen werden erst nach verifizierter Anmeldung angezeigt.</p>
<p class="warning">Die Anmeldung führt noch keine Änderung aus.</p>
<script src="https://accounts.google.com/gsi/client" async></script>
<div id="g_id_onload" data-client_id="{html.escape(client_id, quote=True)}"
 data-login_uri="{html.escape(login_uri, quote=True)}" data-auto_prompt="false"></div>
<div class="g_id_signin" data-type="standard" data-theme="outline"
 data-text="signin_with" data-size="large"></div>"""
    return _html_page("Google-Ads-Freigabe", body)


@mcp.custom_route(
    "/approve/{change_set_id}/identity",
    methods=["POST"],
    include_in_schema=False,
)
async def approval_identity(request: Request) -> Response:
    """Verify Google identity and render the exact review payload."""
    change_set_id = request.path_params["change_set_id"]
    try:
        values = await _form_values(request)
        csrf_body = values.get("g_csrf_token", "")
        csrf_cookie = request.cookies.get("g_csrf_token", "")
        if not csrf_body or not hmac.compare_digest(csrf_body, csrf_cookie):
            raise ToolError("Google sign-in CSRF validation failed.")
        credential = values.get("credential", "")
        if not credential:
            raise ToolError("Google sign-in credential is missing.")
        claims = await anyio.to_thread.run_sync(
            _verify_google_credential, credential
        )
        authorized = validate_approver_identity(
            Principal(
                subject=claims["sub"],
                email=claims["email"],
                role="owner",
                authentication_method="google_oidc",
            )
        )
        claims = {
            **claims,
            "sub": authorized.subject,
            "email": authorized.email,
        }
        record = get_change_set_for_review(change_set_id)
        if record["status"] != "PENDING":
            raise ToolError(
                f"Change set is no longer pending ({record['status']})."
            )
        review_session = _create_review_session(record, claims)
    except ToolError as exc:
        return _error_page(str(exc), 403)

    exact_details = html.escape(
        json.dumps(
            record["details"],
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    confirm_uri = approval_url_for_change_set(change_set_id) + "/confirm"
    body = f"""
<h1>Exakte Änderung freigeben</h1>
<p>Angemeldet als <strong>{html.escape(claims['email'])}</strong>.</p>
<p class="warning">Prüfe Konto, aktuelle Werte, neue Werte, Risiko und Rollback.
Die Bestätigung gibt nur diesen Payload-Hash einmalig frei.</p>
<pre>{exact_details}</pre>
<p><strong>Payload-Hash:</strong> {html.escape(record['payload_hash'])}</p>
<form method="post" action="{html.escape(confirm_uri, quote=True)}">
<input type="hidden" name="review_session" value="{html.escape(review_session, quote=True)}">
<button type="submit">Diese exakte Änderung verbindlich freigeben</button>
</form>"""
    return _html_page("Exakte Google-Ads-Änderung prüfen", body)


@mcp.custom_route(
    "/approve/{change_set_id}/confirm",
    methods=["POST"],
    include_in_schema=False,
)
async def approval_confirm(request: Request) -> Response:
    """Atomically approve the exact payload after the second human action."""
    change_set_id = request.path_params["change_set_id"]
    try:
        values = await _form_values(request)
        session = _verify_review_session(
            values.get("review_session", ""), change_set_id
        )
        approved = approve_change_set(
            change_set_id,
            expected_payload_hash=session["payload_hash"],
            expected_customer_id=session["customer_id"],
            expected_environment=session["environment"],
            approver=Principal(
                subject=session["subject"],
                email=session["email"],
                role="owner",
                authentication_method="google_oidc",
            ),
        )
    except ToolError as exc:
        return _error_page(str(exc), 409)

    return _html_page(
        "Änderung freigegeben",
        "<h1>Änderung freigegeben</h1>"
        f"<p>Änderungssatz <strong>{html.escape(change_set_id)}</strong> ist "
        "jetzt einmalig ausführbar.</p>"
        f"<p>Status: {html.escape(approved['status'])}. Dieses Fenster kann "
        "geschlossen werden.</p>",
    )
