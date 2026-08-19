# Copyright 2026 Google LLC.

"""Tests for the non-MCP human approval boundary."""

import os
import html
import threading
import time
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient

from ads_mcp import approval_routes
from ads_mcp.change_set_store import InMemoryChangeSetStore
from ads_mcp.change_sets import (
    Principal,
    create_change_set,
    get_change_set_for_review,
    reset_change_set_store,
    set_change_set_store_for_testing,
)


class _ReviewSessionParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.value = ""

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "input" and attributes.get("name") == "review_session":
            self.value = html.unescape(attributes.get("value", ""))


class TestApprovalRoutes(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY": "s" * 32,
            "GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID": "client.apps.googleusercontent.com",
            "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
        }
        self.record = {
            "change_set_id": "a" * 32,
            "payload_hash": "b" * 64,
            "customer_id": "1234567890",
            "environment": "production",
        }
        self.claims = {
            "sub": "google-subject",
            "email": "owner@example.com",
        }

    def test_review_session_binds_change_payload_identity_and_expiry(self):
        with patch.dict(os.environ, self.environment, clear=True):
            token = approval_routes._create_review_session(
                self.record, self.claims
            )
            payload = approval_routes._verify_review_session(
                token, self.record["change_set_id"]
            )
        self.assertEqual(payload["payload_hash"], self.record["payload_hash"])
        self.assertEqual(payload["email"], self.claims["email"])

    def test_tampered_review_session_is_rejected(self):
        with patch.dict(os.environ, self.environment, clear=True):
            token = approval_routes._create_review_session(
                self.record, self.claims
            )
            encoded, signature = token.split(".")
            significant_tamper = ("A" if encoded[0] != "A" else "B") + encoded[
                1:
            ]
            with self.assertRaisesRegex(ToolError, "Invalid"):
                approval_routes._verify_review_session(
                    f"{significant_tamper}.{signature}",
                    self.record["change_set_id"],
                )

    def test_noncanonical_base64url_padding_bits_are_rejected(self):
        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )
        with patch.dict(os.environ, self.environment, clear=True):
            token = approval_routes._create_review_session(
                self.record, self.claims
            )
            encoded, signature = token.split(".")
            last_index = alphabet.index(signature[-1])
            self.assertEqual(last_index % 4, 0)
            noncanonical = signature[:-1] + alphabet[last_index + 1]
            with self.assertRaisesRegex(ToolError, "Invalid"):
                approval_routes._verify_review_session(
                    f"{encoded}.{noncanonical}",
                    self.record["change_set_id"],
                )

    def test_expired_review_session_is_rejected(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch("ads_mcp.approval_routes.time.time", return_value=1_000),
        ):
            token = approval_routes._create_review_session(
                self.record, self.claims
            )
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch(
                "ads_mcp.approval_routes.time.time",
                return_value=1_000 + approval_routes._SESSION_TTL_SECONDS + 1,
            ),
        ):
            with self.assertRaisesRegex(ToolError, "expired"):
                approval_routes._verify_review_session(
                    token, self.record["change_set_id"]
                )

    def test_google_claims_require_verified_email_and_issuer(self):
        base_claims = {
            "iss": "https://accounts.google.com",
            "sub": "subject",
            "email": "owner@example.com",
            "email_verified": True,
            "exp": int(time.time()) + 300,
        }
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                approval_routes.id_token,
                "verify_oauth2_token",
                return_value=base_claims,
            ),
        ):
            result = approval_routes._verify_google_credential("token")
        self.assertEqual(result["email"], "owner@example.com")

        unverified = {**base_claims, "email_verified": False}
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                approval_routes.id_token,
                "verify_oauth2_token",
                return_value=unverified,
            ),
        ):
            with self.assertRaisesRegex(ToolError, "not verified"):
                approval_routes._verify_google_credential("token")


class TestApprovalRouteASGI(unittest.TestCase):
    """Exercise the complete browser approval ceremony through ASGI."""

    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "1234567890",
            "GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS": "900",
            "GOOGLE_ADS_MCP_ENVIRONMENT": "test",
            "GOOGLE_ADS_MCP_APPROVER_EMAILS": "owner@example.com",
            "GOOGLE_ADS_MCP_APPROVER_ROLES": "owner",
            "GOOGLE_ADS_MCP_APPROVAL_BASE_URL": "https://approval.example.com",
            "GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY": "s" * 32,
            "GOOGLE_ADS_MCP_APPROVAL_GOOGLE_CLIENT_ID": "client.apps.googleusercontent.com",
            "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
        }
        self.environment_patch = patch.dict(
            os.environ, self.environment, clear=True
        )
        self.environment_patch.start()
        set_change_set_store_for_testing(InMemoryChangeSetStore())
        self.preview = create_change_set(
            {
                "action": "campaign_status_change",
                "customer_id": "1234567890",
                "current": {"status": "ENABLED"},
                "proposed": {"status": "PAUSED"},
            },
            actor=Principal(
                subject="operator-subject",
                email="operator@example.com",
                role="operator",
                authentication_method="google_oidc",
            ),
        )
        self.app = approval_routes.mcp.http_app()

    def tearDown(self):
        reset_change_set_store()
        self.environment_patch.stop()

    @staticmethod
    def _claims(email="owner@example.com"):
        return {
            "iss": "https://accounts.google.com",
            "sub": "owner-google-subject",
            "email": email,
            "email_verified": True,
            "exp": int(time.time()) + 300,
        }

    def _identity(self, client, *, claims=None):
        client.cookies.set("g_csrf_token", "csrf-value")
        with patch.object(
            approval_routes.id_token,
            "verify_oauth2_token",
            return_value=claims or self._claims(),
        ):
            return client.post(
                f"/approve/{self.preview['change_set_id']}/identity",
                data={
                    "g_csrf_token": "csrf-value",
                    "credential": "mock-google-id-token",
                },
            )

    def _review_session(self, response):
        parser = _ReviewSessionParser()
        parser.feed(response.text)
        self.assertTrue(parser.value)
        return parser.value

    def test_get_identity_confirm_replay_and_security_headers(self):
        with TestClient(self.app) as client:
            login = client.get(f"/approve/{self.preview['change_set_id']}")
            self.assertEqual(login.status_code, 200)
            self.assertNotIn("PAUSED", login.text)
            self.assertNotIn("ENABLED", login.text)
            self.assertNotIn("campaign_status_change", login.text)
            self.assertNotIn("1234567890", login.text)
            self.assertNotIn(self.preview["payload_hash"], login.text)
            self.assertEqual(
                login.headers["cache-control"], "no-store, max-age=0"
            )
            self.assertEqual(login.headers["x-frame-options"], "DENY")

            identity = self._identity(client)
            self.assertEqual(identity.status_code, 200)
            self.assertIn("PAUSED", identity.text)
            self.assertIn(self.preview["payload_hash"], identity.text)
            session = self._review_session(identity)

            confirm = client.post(
                f"/approve/{self.preview['change_set_id']}/confirm",
                data={"review_session": session},
            )
            self.assertEqual(confirm.status_code, 200)
            self.assertEqual(
                get_change_set_for_review(self.preview["change_set_id"])[
                    "status"
                ],
                "APPROVED",
            )
            replay = client.post(
                f"/approve/{self.preview['change_set_id']}/confirm",
                data={"review_session": session},
            )
            self.assertEqual(replay.status_code, 409)

    def test_identity_hides_payload_on_csrf_oidc_and_authorization_failure(
        self,
    ):
        path = f"/approve/{self.preview['change_set_id']}/identity"
        with TestClient(self.app) as client:
            client.cookies.set("g_csrf_token", "cookie-token")
            csrf_failure = client.post(
                path,
                data={
                    "g_csrf_token": "body-token",
                    "credential": "credential",
                },
            )
            self.assertEqual(csrf_failure.status_code, 403)
            self.assertNotIn("PAUSED", csrf_failure.text)

            client.cookies.set("g_csrf_token", "csrf-value")
            with patch.object(
                approval_routes.id_token,
                "verify_oauth2_token",
                side_effect=ValueError("bad token"),
            ):
                oidc_failure = client.post(
                    path,
                    data={
                        "g_csrf_token": "csrf-value",
                        "credential": "bad-token",
                    },
                )
            self.assertEqual(oidc_failure.status_code, 403)
            self.assertNotIn("PAUSED", oidc_failure.text)

            unauthorized = self._identity(
                client, claims=self._claims("intruder@example.com")
            )
            self.assertEqual(unauthorized.status_code, 403)
            self.assertNotIn("PAUSED", unauthorized.text)

    def test_simultaneous_confirmation_is_atomic(self):
        with TestClient(self.app) as client:
            session = self._review_session(self._identity(client))

        barrier = threading.Barrier(3)
        statuses = []
        status_lock = threading.Lock()

        def confirm():
            client = TestClient(self.app)
            barrier.wait()
            response = client.post(
                f"/approve/{self.preview['change_set_id']}/confirm",
                data={"review_session": session},
            )
            with status_lock:
                statuses.append(response.status_code)

        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(statuses, [200, 409])


if __name__ == "__main__":
    unittest.main()
