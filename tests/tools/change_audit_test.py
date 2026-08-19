# Copyright 2026 Google LLC.

"""Tests for scoped read-only change-set audit tools."""

import os
import unittest
from unittest.mock import patch

from ads_mcp.tools import change_audit


class TestChangeAudit(unittest.TestCase):
    def setUp(self):
        self.environment = {"GOOGLE_ADS_MCP_READ_CUSTOMER_IDS": "1234567890"}
        self.record = {
            "change_set_id": "a" * 32,
            "customer_id": "1234567890",
            "payload_hash": "b" * 64,
            "details": {"action": "campaign_status"},
            "status": "UNCERTAIN",
        }

    def test_status_requires_reconciliation(self):
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                change_audit,
                "get_change_set_for_review",
                return_value=self.record,
            ),
            patch.object(
                change_audit,
                "get_change_set_execution_status",
                return_value={
                    "status": "UNCERTAIN",
                    "execution_outcome": "UNCERTAIN",
                },
            ),
        ):
            result = change_audit.get_change_set_status("a" * 32)
        self.assertEqual(
            result["next_action"], "RECONCILE_LIVE_STATE_DO_NOT_RETRY"
        )

    def test_timeline_is_read_only(self):
        events = [{"sequence": 1, "event_type": "CREATED"}]
        with (
            patch.dict(os.environ, self.environment, clear=True),
            patch.object(
                change_audit,
                "get_change_set_for_review",
                return_value=self.record,
            ),
            patch.object(
                change_audit,
                "list_change_set_audit_events",
                return_value=events,
            ),
        ):
            result = change_audit.get_change_set_audit_timeline("a" * 32)
        self.assertEqual(result["events"], events)
        self.assertEqual(result["execution_status"], "READ_ONLY_LEDGER_QUERY")


class TestChangeAuditSchemas(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_read_only(self):
        tools = await change_audit.audit_mcp.list_tools()
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(tool.annotations.readOnlyHint for tool in tools))
        self.assertTrue(all(tool.annotations.openWorldHint for tool in tools))


if __name__ == "__main__":
    unittest.main()
