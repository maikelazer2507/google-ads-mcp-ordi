# Copyright 2026 Google LLC.

"""Read-only access to the internal guarded-change audit ledger."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from ads_mcp.access_policy import require_customer_access
from ads_mcp.change_sets import (
    get_change_set_execution_status,
    get_change_set_for_review,
    list_change_set_audit_events,
)

audit_mcp = FastMCP("audit")
_READ_ANNOTATIONS = ToolAnnotations(readOnlyHint=True, openWorldHint=True)


def _scoped_change_set(change_set_id: str) -> dict[str, Any]:
    record = get_change_set_for_review(change_set_id)
    require_customer_access(record["customer_id"], "read")
    return record


@audit_mcp.tool(annotations=_READ_ANNOTATIONS)
def get_change_set_status(
    change_set_id: str, stale_after_seconds: int = 300
) -> dict[str, Any]:
    """Return exact persisted change details and current execution status.

    Use this after preview, approval, apply, failure, or an uncertain response.
    It never authorizes, consumes, retries, or changes a Google Ads object.
    """
    if stale_after_seconds < 60 or stale_after_seconds > 86_400:
        raise ToolError("stale_after_seconds must be between 60 and 86400.")
    record = _scoped_change_set(change_set_id)
    execution = get_change_set_execution_status(
        change_set_id, stale_after_seconds=stale_after_seconds
    )
    requires_reconciliation = bool(
        execution.get("status") == "UNCERTAIN"
        or execution.get("execution_outcome") == "UNCERTAIN"
        or execution.get("is_stale_in_progress")
    )
    return {
        "change_set": record,
        "execution": execution,
        "requires_reconciliation": requires_reconciliation,
        "next_action": (
            "RECONCILE_LIVE_STATE_DO_NOT_RETRY"
            if requires_reconciliation
            else "FOLLOW_RECORDED_STATE"
        ),
    }


@audit_mcp.tool(annotations=_READ_ANNOTATIONS)
def get_change_set_audit_timeline(change_set_id: str) -> dict[str, Any]:
    """Return ordered internal audit events for one scoped change set."""
    record = _scoped_change_set(change_set_id)
    events = list_change_set_audit_events(change_set_id)
    return {
        "change_set_id": change_set_id,
        "customer_id": record["customer_id"],
        "payload_hash": record["payload_hash"],
        "events": events,
        "event_count": len(events),
        "execution_status": "READ_ONLY_LEDGER_QUERY",
    }
