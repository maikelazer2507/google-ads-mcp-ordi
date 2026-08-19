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

"""Tests for durable, human-approved Google Ads change sets."""

import os
import threading
import unittest
from unittest.mock import patch

from fastmcp.exceptions import ToolError

from ads_mcp.change_set_store import InMemoryChangeSetStore
from ads_mcp import change_sets
from ads_mcp.change_sets import (
    Principal,
    approval_url_for_change_set,
    approve_change_set,
    create_change_set,
    get_change_set_for_review,
    get_change_set_execution_status,
    list_change_set_audit_events,
    record_change_set_failure,
    record_change_set_success,
    record_change_set_uncertain,
    require_allowed_customer,
    reset_change_set_store,
    set_change_set_store_for_testing,
    verify_change_set,
)


class TestChangeSets(unittest.TestCase):
    """Verifies identity, state, replay, expiry, and audit boundaries."""

    def setUp(self):
        self.environment = {
            "GOOGLE_ADS_MCP_ALLOWED_CUSTOMER_IDS": "123-456-7890",
            "GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS": "900",
            "GOOGLE_ADS_MCP_ENVIRONMENT": "test",
            "GOOGLE_ADS_MCP_APPROVER_EMAILS": "owner@example.com",
            "GOOGLE_ADS_MCP_APPROVER_ROLES": "owner",
            "GOOGLE_ADS_MCP_APPROVAL_BASE_URL": "https://approval.example.com",
            "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY": "i" * 32,
        }
        self.store = InMemoryChangeSetStore()
        set_change_set_store_for_testing(self.store)
        self.approver = Principal(
            subject="google-subject-1",
            email="OWNER@example.com",
            role="owner",
            authentication_method="google_oidc",
        )

    def tearDown(self):
        reset_change_set_store()

    def _preview(self) -> dict:
        return create_change_set(
            {
                "action": "test",
                "customer_id": "1234567890",
                "current": {"status": "ENABLED"},
                "proposed": {"status": "PAUSED"},
            },
            actor=Principal(
                subject="maria-subject",
                email="maria@example.com",
                role="operator",
                authentication_method="google_oidc",
            ),
        )

    def _approve(self, preview: dict) -> dict:
        return approve_change_set(
            preview["change_set_id"],
            expected_payload_hash=preview["payload_hash"],
            expected_customer_id=preview["customer_id"],
            expected_environment=preview["environment"],
            approver=self.approver,
        )

    def test_customer_must_be_allowlisted(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(
                require_allowed_customer("123-456-7890"), "1234567890"
            )
            with self.assertRaisesRegex(ToolError, "not permitted"):
                require_allowed_customer("9999999999")
            for invalid in ("123456789", "12345678901"):
                with self.assertRaisesRegex(ToolError, "exactly 10 digits"):
                    require_allowed_customer(invalid)
            with self.assertRaisesRegex(ToolError, "between 1 and 20 digits"):
                require_allowed_customer("12345abcde")

    def test_copyable_statement_cannot_approve_pending_change(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self.assertNotIn("approval_statement", preview)
            with self.assertRaisesRegex(ToolError, "authorized human"):
                verify_change_set(
                    preview["change_set_token"],
                    f"APPROVE {preview['change_set_id']}",
                    expected_action="test",
                )

            stored = get_change_set_for_review(preview["change_set_id"])
            self.assertEqual(stored["status"], "PENDING")

    def test_authorized_approval_is_single_use_and_audited(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            approved = self._approve(preview)
            self.assertEqual(approved["status"], "APPROVED")
            self.assertEqual(
                approved["approved_by"]["email"], "owner@example.com"
            )
            self.assertEqual(
                approved["approved_payload_hash"], preview["payload_hash"]
            )
            self.assertNotIn("approved_integrity_seal", approved)

            payload = verify_change_set(
                preview["change_set_token"], expected_action="test"
            )
            self.assertEqual(payload["action"], "test")
            self.assertTrue(payload["execution_id"])
            with self.assertRaisesRegex(ToolError, "already consumed"):
                verify_change_set(
                    preview["change_set_token"], expected_action="test"
                )

            completed = record_change_set_success(
                preview["change_set_id"],
                payload["execution_id"],
                result={"verified": True},
            )
            self.assertEqual(completed["execution_outcome"], "SUCCEEDED")
            self.assertNotIn("token_hash", completed)

            events = list_change_set_audit_events(preview["change_set_id"])
            event_types = [event["event_type"] for event in events]
            self.assertEqual(
                event_types,
                [
                    "CREATED",
                    "APPROVED",
                    "CONSUMED",
                    "CONSUMPTION_REJECTED",
                    "SUCCEEDED",
                ],
            )
            self.assertEqual(
                [event["sequence"] for event in events], [1, 2, 3, 4, 5]
            )

    def test_current_payload_integrity_is_recomputed_during_approval(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self.store._records[preview["change_set_id"]]["payload"][
                "proposed"
            ]["status"] = "REMOVED"
            with self.assertRaisesRegex(ToolError, "integrity"):
                self._approve(preview)
            record = get_change_set_for_review(preview["change_set_id"])
            self.assertEqual(record["status"], "PENDING")

    def test_approved_payload_hash_is_rechecked_during_consumption(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            stored = self.store._records[preview["change_set_id"]]
            stored["payload"]["proposed"]["status"] = "REMOVED"
            stored["payload_hash"] = change_sets.canonical_payload_hash(
                stored["payload"],
                stored["customer_id"],
                stored["environment"],
            )
            with self.assertRaisesRegex(ToolError, "approved payload"):
                verify_change_set(
                    preview["change_set_token"], expected_action="test"
                )
            self.assertEqual(
                get_change_set_for_review(preview["change_set_id"])["status"],
                "APPROVED",
            )

    def test_keyed_approval_seal_rejects_coordinated_record_tampering(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            stored = self.store._records[preview["change_set_id"]]
            stored["payload"]["proposed"]["status"] = "REMOVED"
            tampered_hash = change_sets.canonical_payload_hash(
                stored["payload"],
                stored["customer_id"],
                stored["environment"],
            )
            stored["payload_hash"] = tampered_hash
            stored["approved_payload_hash"] = tampered_hash
            with self.assertRaisesRegex(ToolError, "approval integrity"):
                verify_change_set(
                    preview["change_set_token"], expected_action="test"
                )
            self.assertEqual(
                get_change_set_for_review(preview["change_set_id"])["status"],
                "APPROVED",
            )

    def test_success_evidence_is_typed_hashed_and_redacted(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            payload = verify_change_set(
                preview["change_set_token"], expected_action="test"
            )
            completed = record_change_set_success(
                preview["change_set_id"],
                payload["execution_id"],
                result={
                    "verified": True,
                    "after_snapshot": {"status": "PAUSED"},
                    "result_hash": "A" * 64,
                    "request_id": "request-123/abc",
                    "resource_name": "customers/123/campaigns/456",
                    "patient_email": "patient@example.com",
                },
            )
            result = completed["result"]
            self.assertIs(result["verified"], True)
            self.assertRegex(result["after_snapshot_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(result["result_hash"], "a" * 64)
            self.assertEqual(result["request_id"], "request-123/abc")
            self.assertEqual(
                result["resource_name"], "customers/123/campaigns/456"
            )
            self.assertEqual(result["patient_email"], "[REDACTED]")
            self.assertNotIn("PAUSED", str(result))
            self.assertNotIn("patient@example.com", str(result))
            success_event = list_change_set_audit_events(
                preview["change_set_id"]
            )[-1]
            self.assertEqual(success_event["event_type"], "SUCCEEDED")
            self.assertEqual(success_event["metadata"]["verification"], result)

    def test_unauthorized_email_role_and_authentication_are_rejected(self):
        principals = (
            Principal(
                subject="2",
                email="intruder@example.com",
                role="owner",
                authentication_method="google_oidc",
            ),
            Principal(
                subject="3",
                email="owner@example.com",
                role="operator",
                authentication_method="google_oidc",
            ),
            Principal(
                subject="4",
                email="owner@example.com",
                role="owner",
                authentication_method="unverified_header",
            ),
        )
        with patch.dict(os.environ, self.environment, clear=True):
            for principal in principals:
                preview = self._preview()
                with self.assertRaises(ToolError):
                    approve_change_set(
                        preview["change_set_id"],
                        expected_payload_hash=preview["payload_hash"],
                        expected_customer_id=preview["customer_id"],
                        expected_environment=preview["environment"],
                        approver=principal,
                    )

    def test_integrity_key_is_required_and_session_key_is_not_reused(self):
        environment = dict(self.environment)
        environment.pop("GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY")
        environment["GOOGLE_ADS_MCP_APPROVAL_SESSION_KEY"] = "s" * 64
        with patch.dict(os.environ, environment, clear=True):
            preview = self._preview()
            with self.assertRaisesRegex(
                ToolError, "GOOGLE_ADS_MCP_CHANGESET_INTEGRITY_KEY"
            ):
                self._approve(preview)
            self.assertEqual(
                get_change_set_for_review(preview["change_set_id"])["status"],
                "PENDING",
            )

    def test_payload_customer_and_environment_are_bound(self):
        attempts = (
            {"expected_payload_hash": "0" * 64},
            {"expected_customer_id": "9999999999"},
            {"expected_environment": "production"},
        )
        with patch.dict(os.environ, self.environment, clear=True):
            for override in attempts:
                preview = self._preview()
                kwargs = {
                    "expected_payload_hash": preview["payload_hash"],
                    "expected_customer_id": preview["customer_id"],
                    "expected_environment": preview["environment"],
                    "approver": self.approver,
                }
                kwargs.update(override)
                with self.assertRaises(ToolError):
                    approve_change_set(preview["change_set_id"], **kwargs)
                self.assertEqual(
                    get_change_set_for_review(preview["change_set_id"])[
                        "status"
                    ],
                    "PENDING",
                )

    def test_token_tampering_does_not_consume_valid_approval(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            token = preview["change_set_token"]
            tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
            with self.assertRaisesRegex(ToolError, "Invalid change-set token"):
                verify_change_set(tampered, expected_action="test")
            payload = verify_change_set(token, expected_action="test")
            self.assertEqual(payload["change_set_id"], preview["change_set_id"])

    def test_wrong_apply_action_does_not_consume_approval(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            with self.assertRaisesRegex(ToolError, "different apply action"):
                verify_change_set(
                    preview["change_set_token"],
                    expected_action="campaign_budget_change",
                )
            self.assertEqual(
                get_change_set_for_review(preview["change_set_id"])["status"],
                "APPROVED",
            )
            payload = verify_change_set(
                preview["change_set_token"], expected_action="test"
            )
            self.assertEqual(payload["action"], "test")

    def test_expired_change_set_transitions_and_cannot_be_approved(self):
        environment = dict(self.environment)
        environment["GOOGLE_ADS_MCP_CHANGESET_TTL_SECONDS"] = "60"
        with patch.dict(os.environ, environment, clear=True):
            with patch("ads_mcp.change_sets.time.time", return_value=1_000):
                preview = self._preview()
            with patch("ads_mcp.change_sets.time.time", return_value=1_061):
                with self.assertRaisesRegex(ToolError, "expired"):
                    self._approve(preview)
            record = get_change_set_for_review(preview["change_set_id"])
            self.assertEqual(record["status"], "EXPIRED")
            self.assertEqual(
                list_change_set_audit_events(preview["change_set_id"])[-1][
                    "event_type"
                ],
                "EXPIRED",
            )

    def test_failed_execution_has_terminal_audit_state(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            payload = verify_change_set(
                preview["change_set_token"], expected_action="test"
            )
            failed = record_change_set_failure(
                preview["change_set_id"],
                payload["execution_id"],
                reason_code="API_REJECTED",
                message="request failed",
            )
            self.assertEqual(failed["status"], "FAILED")
            self.assertNotIn("failure", failed)
            with self.assertRaisesRegex(ToolError, "active execution"):
                record_change_set_success(
                    preview["change_set_id"], payload["execution_id"]
                )

    def test_uncertain_execution_is_terminal_and_stale_status_is_visible(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            with patch("ads_mcp.change_sets.time.time", return_value=1_000):
                payload = verify_change_set(
                    preview["change_set_token"], expected_action="test"
                )
            status = get_change_set_execution_status(
                preview["change_set_id"],
                stale_after_seconds=300,
                now=1_300,
            )
            self.assertTrue(status["is_stale_in_progress"])
            self.assertEqual(status["in_progress_age_seconds"], 300)

            uncertain = record_change_set_uncertain(
                preview["change_set_id"],
                payload["execution_id"],
                reason_code="POST_VERIFY_TIMEOUT",
                message="Mutation may have succeeded; live verification timed out.",
            )
            self.assertEqual(uncertain["status"], "UNCERTAIN")
            self.assertEqual(uncertain["execution_outcome"], "UNCERTAIN")
            self.assertNotIn("uncertainty", uncertain)
            self.assertEqual(
                list_change_set_audit_events(preview["change_set_id"])[-1][
                    "event_type"
                ],
                "UNCERTAIN",
            )
            with self.assertRaisesRegex(ToolError, "active execution"):
                record_change_set_failure(
                    preview["change_set_id"],
                    payload["execution_id"],
                    reason_code="LATE_FAILURE",
                    message="late",
                )

    def test_storage_env_alias_and_conflict_are_deterministic(self):
        same = {
            "GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE": "firestore",
            "GOOGLE_ADS_MCP_CHANGESET_STORE": "firestore",
        }
        with (
            patch.dict(os.environ, same, clear=True),
            patch.object(
                change_sets,
                "FirestoreChangeSetStore",
                return_value=self.store,
            ) as constructor,
        ):
            self.assertIs(change_sets._create_configured_store(), self.store)
            constructor.assert_called_once()

        alias_only = {"GOOGLE_ADS_MCP_CHANGESET_STORE": "firestore"}
        with (
            patch.dict(os.environ, alias_only, clear=True),
            patch.object(
                change_sets,
                "FirestoreChangeSetStore",
                return_value=self.store,
            ),
        ):
            self.assertIs(change_sets._create_configured_store(), self.store)

        conflicting = {
            "GOOGLE_ADS_MCP_CHANGESET_STORAGE_TYPE": "firestore",
            "GOOGLE_ADS_MCP_CHANGESET_STORE": "memory",
        }
        with patch.dict(os.environ, conflicting, clear=True):
            with self.assertRaisesRegex(ToolError, "conflicts"):
                change_sets._create_configured_store()

    def test_concurrent_consumers_allow_exactly_one(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            self._approve(preview)
            barrier = threading.Barrier(3)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def consume() -> None:
                barrier.wait()
                try:
                    verify_change_set(
                        preview["change_set_token"], expected_action="test"
                    )
                    outcome = "accepted"
                except ToolError:
                    outcome = "rejected"
                with outcomes_lock:
                    outcomes.append(outcome)

            threads = [threading.Thread(target=consume) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["accepted", "rejected"])

    def test_review_accepts_id_or_token_and_url_does_not_contain_token(self):
        with patch.dict(os.environ, self.environment, clear=True):
            preview = self._preview()
            by_id = get_change_set_for_review(preview["change_set_id"])
            by_token = get_change_set_for_review(preview["change_set_token"])
            self.assertEqual(by_id, by_token)
            self.assertEqual(
                preview["approval_url"],
                approval_url_for_change_set(preview["change_set_id"]),
            )
            self.assertIn(preview["change_set_id"], preview["approval_url"])
            self.assertNotIn(
                preview["change_set_token"], preview["approval_url"]
            )
            self.assertNotIn("token_hash", by_id)


if __name__ == "__main__":
    unittest.main()
