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

"""Durable, atomic storage for guarded Google Ads change sets.

The store owns state transitions so callers cannot implement a check-then-write
sequence that permits two workers to consume the same approval.  Firestore is
the production backend; the in-memory implementation is intentionally limited
to tests and local development.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Protocol


class ChangeSetStoreError(RuntimeError):
    """Raised when a change-set transition is invalid or unavailable."""


def canonical_payload_hash(
    payload: dict[str, Any], customer_id: str, environment: str
) -> str:
    """Return the canonical SHA-256 binding for a mutation payload.

    This function deliberately lives in the transition layer. Both the
    service that creates a record and the atomic storage transitions use the
    same implementation, so approval never trusts a caller-supplied or stored
    digest without recomputing it from the current record.
    """
    if not isinstance(payload, dict):
        raise ChangeSetStoreError("Change-set payload is invalid.")
    if not isinstance(customer_id, str) or not isinstance(environment, str):
        raise ChangeSetStoreError("Change-set binding is invalid.")
    try:
        encoded = json.dumps(
            {
                "customer_id": customer_id,
                "environment": environment,
                "payload": payload,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ChangeSetStoreError("Change-set payload is invalid.") from exc
    return hashlib.sha256(encoded).hexdigest()


def approval_integrity_seal(
    *,
    change_set_id: str,
    approved_payload_hash: str,
    customer_id: str,
    environment: str,
    approver_subject: str,
    integrity_key: bytes,
) -> str:
    """Bind an approval to its immutable identity using a keyed digest."""
    if len(integrity_key) < 32:
        raise ChangeSetStoreError("Change-set integrity key is too short.")
    values = (
        change_set_id,
        approved_payload_hash,
        customer_id,
        environment,
        approver_subject,
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ChangeSetStoreError("Change-set approval binding is invalid.")
    encoded = json.dumps(
        {
            "approved_by_subject": approver_subject,
            "approved_payload_hash": approved_payload_hash,
            "change_set_id": change_set_id,
            "customer_id": customer_id,
            "environment": environment,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hmac.new(integrity_key, encoded, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class TransitionResult:
    """Result of an atomic transition, including a stable rejection code."""

    record: dict[str, Any]
    accepted: bool
    rejection_code: str | None = None


class ChangeSetStore(Protocol):
    """Storage contract used by the change-set service."""

    def create(
        self, record: dict[str, Any], audit_event: dict[str, Any]
    ) -> None:
        """Atomically creates a change set and its first audit event."""

    def get(self, change_set_id: str) -> dict[str, Any] | None:
        """Returns a change set by ID."""

    def approve(
        self,
        change_set_id: str,
        *,
        now: int,
        expected_payload_hash: str,
        expected_customer_id: str,
        expected_environment: str,
        approver: dict[str, Any],
        integrity_key: bytes,
    ) -> TransitionResult:
        """Atomically transitions PENDING to APPROVED."""

    def consume(
        self,
        change_set_id: str,
        *,
        now: int,
        token_hash: str,
        expected_environment: str,
        expected_action: str,
        executor: dict[str, Any],
        execution_id: str,
        integrity_key: bytes,
    ) -> TransitionResult:
        """Atomically transitions APPROVED to CONSUMED exactly once."""

    def record_success(
        self,
        change_set_id: str,
        *,
        now: int,
        executor: dict[str, Any],
        execution_id: str,
        result: dict[str, Any],
    ) -> TransitionResult:
        """Records successful completion for a consumed change set."""

    def record_failure(
        self,
        change_set_id: str,
        *,
        now: int,
        executor: dict[str, Any],
        execution_id: str,
        failure: dict[str, Any],
    ) -> TransitionResult:
        """Transitions a consumed change set to FAILED."""

    def record_uncertain(
        self,
        change_set_id: str,
        *,
        now: int,
        executor: dict[str, Any],
        execution_id: str,
        uncertainty: dict[str, Any],
    ) -> TransitionResult:
        """Transitions a consumed change set to terminal UNCERTAIN."""

    def list_audit_events(self, change_set_id: str) -> list[dict[str, Any]]:
        """Returns immutable audit events in chronological order."""


def _new_event(
    record: dict[str, Any],
    event_type: str,
    *,
    now: int,
    actor: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Builds a minimal event that cannot disclose the mutation payload."""
    return {
        "event_id": uuid.uuid4().hex,
        "change_set_id": record["change_set_id"],
        "sequence": int(record.get("audit_sequence", 0)) + 1,
        "event_type": event_type,
        "occurred_at": now,
        "actor": copy.deepcopy(actor),
        "customer_id": record["customer_id"],
        "environment": record["environment"],
        "payload_hash": record["payload_hash"],
        "metadata": copy.deepcopy(metadata or {}),
    }


def _expire_if_needed(
    record: dict[str, Any], *, now: int
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if record["status"] not in {"PENDING", "APPROVED"}:
        return record, None
    if now <= int(record["expires_at"]):
        return record, None
    updated = copy.deepcopy(record)
    updated.update(
        {
            "status": "EXPIRED",
            "expired_at": now,
            "updated_at": now,
        }
    )
    event = _new_event(
        updated,
        "EXPIRED",
        now=now,
        actor={"subject": "system", "role": "system"},
        metadata={"previous_status": record["status"]},
    )
    updated["audit_sequence"] = event["sequence"]
    return updated, event


def _approve_transition(
    record: dict[str, Any],
    *,
    now: int,
    expected_payload_hash: str,
    expected_customer_id: str,
    expected_environment: str,
    approver: dict[str, Any],
    integrity_key: bytes,
) -> tuple[TransitionResult, dict[str, Any]]:
    expired, expiration_event = _expire_if_needed(record, now=now)
    if expiration_event:
        return TransitionResult(expired, False, "EXPIRED"), expiration_event

    try:
        current_payload_hash = canonical_payload_hash(
            record.get("payload"),
            record.get("customer_id"),
            record.get("environment"),
        )
    except ChangeSetStoreError:
        current_payload_hash = ""

    checks = (
        (
            bool(current_payload_hash)
            and hmac.compare_digest(
                str(record.get("payload_hash", "")), current_payload_hash
            ),
            "PAYLOAD_INTEGRITY_MISMATCH",
        ),
        (
            bool(current_payload_hash)
            and hmac.compare_digest(
                current_payload_hash, expected_payload_hash
            ),
            "PAYLOAD_MISMATCH",
        ),
        (
            record.get("customer_id") == expected_customer_id,
            "CUSTOMER_MISMATCH",
        ),
        (
            record.get("environment") == expected_environment,
            "ENVIRONMENT_MISMATCH",
        ),
        (record.get("status") == "PENDING", "NOT_PENDING"),
    )
    rejection_code = next((code for ok, code in checks if not ok), None)
    if rejection_code:
        event = _new_event(
            record,
            "APPROVAL_REJECTED",
            now=now,
            actor=approver,
            metadata={"reason_code": rejection_code},
        )
        updated = copy.deepcopy(record)
        updated["audit_sequence"] = event["sequence"]
        return TransitionResult(updated, False, rejection_code), event

    approved_integrity_seal = approval_integrity_seal(
        change_set_id=record["change_set_id"],
        approved_payload_hash=current_payload_hash,
        customer_id=record["customer_id"],
        environment=record["environment"],
        approver_subject=str(approver.get("subject", "")),
        integrity_key=integrity_key,
    )
    updated = copy.deepcopy(record)
    updated.update(
        {
            "status": "APPROVED",
            "approved_at": now,
            "approved_by": copy.deepcopy(approver),
            "approved_payload_hash": current_payload_hash,
            "approved_integrity_seal": approved_integrity_seal,
            "updated_at": now,
        }
    )
    event = _new_event(updated, "APPROVED", now=now, actor=approver)
    updated["audit_sequence"] = event["sequence"]
    return TransitionResult(updated, True), event


def _consume_transition(
    record: dict[str, Any],
    *,
    now: int,
    token_hash: str,
    expected_environment: str,
    expected_action: str,
    executor: dict[str, Any],
    execution_id: str,
    integrity_key: bytes,
) -> tuple[TransitionResult, dict[str, Any]]:
    expired, expiration_event = _expire_if_needed(record, now=now)
    if expiration_event:
        return TransitionResult(expired, False, "EXPIRED"), expiration_event

    try:
        current_payload_hash = canonical_payload_hash(
            record.get("payload"),
            record.get("customer_id"),
            record.get("environment"),
        )
    except ChangeSetStoreError:
        current_payload_hash = ""
    try:
        expected_integrity_seal = approval_integrity_seal(
            change_set_id=str(record.get("change_set_id", "")),
            approved_payload_hash=str(record.get("approved_payload_hash", "")),
            customer_id=str(record.get("customer_id", "")),
            environment=str(record.get("environment", "")),
            approver_subject=str(
                record.get("approved_by", {}).get("subject", "")
            ),
            integrity_key=integrity_key,
        )
    except (AttributeError, ChangeSetStoreError):
        expected_integrity_seal = ""

    checks = (
        (
            bool(current_payload_hash)
            and hmac.compare_digest(
                str(record.get("payload_hash", "")), current_payload_hash
            ),
            "PAYLOAD_INTEGRITY_MISMATCH",
        ),
        (
            record.get("status") != "APPROVED"
            or (
                bool(current_payload_hash)
                and hmac.compare_digest(
                    str(record.get("approved_payload_hash", "")),
                    current_payload_hash,
                )
            ),
            "APPROVED_PAYLOAD_MISMATCH",
        ),
        (
            record.get("status") != "APPROVED"
            or (
                bool(expected_integrity_seal)
                and hmac.compare_digest(
                    str(record.get("approved_integrity_seal", "")),
                    expected_integrity_seal,
                )
            ),
            "APPROVAL_INTEGRITY_MISMATCH",
        ),
        (
            hmac.compare_digest(str(record.get("token_hash", "")), token_hash),
            "INVALID_TOKEN",
        ),
        (
            record.get("environment") == expected_environment,
            "ENVIRONMENT_MISMATCH",
        ),
        (
            hmac.compare_digest(
                str(record.get("payload", {}).get("action", "")),
                expected_action,
            ),
            "ACTION_MISMATCH",
        ),
        (record.get("status") == "APPROVED", "NOT_APPROVED"),
    )
    rejection_code = next((code for ok, code in checks if not ok), None)
    if rejection_code:
        event = _new_event(
            record,
            "CONSUMPTION_REJECTED",
            now=now,
            actor=executor,
            metadata={"reason_code": rejection_code},
        )
        updated = copy.deepcopy(record)
        updated["audit_sequence"] = event["sequence"]
        return TransitionResult(updated, False, rejection_code), event

    updated = copy.deepcopy(record)
    updated.update(
        {
            "status": "CONSUMED",
            "consumed_at": now,
            "consumed_by": copy.deepcopy(executor),
            "execution_id": execution_id,
            "execution_outcome": "IN_PROGRESS",
            "updated_at": now,
        }
    )
    event = _new_event(
        updated,
        "CONSUMED",
        now=now,
        actor=executor,
        metadata={"execution_id": execution_id},
    )
    updated["audit_sequence"] = event["sequence"]
    return TransitionResult(updated, True), event


def _success_transition(
    record: dict[str, Any],
    *,
    now: int,
    executor: dict[str, Any],
    execution_id: str,
    result: dict[str, Any],
) -> tuple[TransitionResult, dict[str, Any]]:
    valid = (
        record["status"] == "CONSUMED"
        and record.get("execution_outcome") == "IN_PROGRESS"
        and hmac.compare_digest(
            str(record.get("execution_id", "")), execution_id
        )
    )
    if not valid:
        event = _new_event(
            record,
            "SUCCESS_RECORDING_REJECTED",
            now=now,
            actor=executor,
            metadata={"reason_code": "INVALID_EXECUTION_STATE"},
        )
        updated = copy.deepcopy(record)
        updated["audit_sequence"] = event["sequence"]
        return (
            TransitionResult(updated, False, "INVALID_EXECUTION_STATE"),
            event,
        )

    updated = copy.deepcopy(record)
    updated.update(
        {
            "execution_outcome": "SUCCEEDED",
            "succeeded_at": now,
            "result": copy.deepcopy(result),
            "updated_at": now,
        }
    )
    event = _new_event(
        updated,
        "SUCCEEDED",
        now=now,
        actor=executor,
        metadata={
            "execution_id": execution_id,
            "verification": copy.deepcopy(result),
        },
    )
    updated["audit_sequence"] = event["sequence"]
    return TransitionResult(updated, True), event


def _failure_transition(
    record: dict[str, Any],
    *,
    now: int,
    executor: dict[str, Any],
    execution_id: str,
    failure: dict[str, Any],
) -> tuple[TransitionResult, dict[str, Any]]:
    valid = (
        record["status"] == "CONSUMED"
        and record.get("execution_outcome") == "IN_PROGRESS"
        and hmac.compare_digest(
            str(record.get("execution_id", "")), execution_id
        )
    )
    if not valid:
        event = _new_event(
            record,
            "FAILURE_RECORDING_REJECTED",
            now=now,
            actor=executor,
            metadata={"reason_code": "INVALID_EXECUTION_STATE"},
        )
        updated = copy.deepcopy(record)
        updated["audit_sequence"] = event["sequence"]
        return (
            TransitionResult(updated, False, "INVALID_EXECUTION_STATE"),
            event,
        )

    updated = copy.deepcopy(record)
    updated.update(
        {
            "status": "FAILED",
            "execution_outcome": "FAILED",
            "failed_at": now,
            "failure": copy.deepcopy(failure),
            "updated_at": now,
        }
    )
    event = _new_event(
        updated,
        "FAILED",
        now=now,
        actor=executor,
        metadata={
            "execution_id": execution_id,
            "reason_code": failure.get("reason_code", "UNKNOWN"),
        },
    )
    updated["audit_sequence"] = event["sequence"]
    return TransitionResult(updated, True), event


def _uncertain_transition(
    record: dict[str, Any],
    *,
    now: int,
    executor: dict[str, Any],
    execution_id: str,
    uncertainty: dict[str, Any],
) -> tuple[TransitionResult, dict[str, Any]]:
    valid = (
        record["status"] == "CONSUMED"
        and record.get("execution_outcome") == "IN_PROGRESS"
        and hmac.compare_digest(
            str(record.get("execution_id", "")), execution_id
        )
    )
    if not valid:
        event = _new_event(
            record,
            "UNCERTAIN_RECORDING_REJECTED",
            now=now,
            actor=executor,
            metadata={"reason_code": "INVALID_EXECUTION_STATE"},
        )
        updated = copy.deepcopy(record)
        updated["audit_sequence"] = event["sequence"]
        return (
            TransitionResult(updated, False, "INVALID_EXECUTION_STATE"),
            event,
        )

    updated = copy.deepcopy(record)
    updated.update(
        {
            "status": "UNCERTAIN",
            "execution_outcome": "UNCERTAIN",
            "uncertain_at": now,
            "uncertainty": copy.deepcopy(uncertainty),
            "updated_at": now,
        }
    )
    event = _new_event(
        updated,
        "UNCERTAIN",
        now=now,
        actor=executor,
        metadata={
            "execution_id": execution_id,
            "reason_code": uncertainty.get("reason_code", "UNKNOWN"),
        },
    )
    updated["audit_sequence"] = event["sequence"]
    return TransitionResult(updated, True), event


class InMemoryChangeSetStore:
    """Thread-safe store for deterministic unit tests and local development."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def create(
        self, record: dict[str, Any], audit_event: dict[str, Any]
    ) -> None:
        with self._lock:
            change_set_id = record["change_set_id"]
            if change_set_id in self._records:
                raise ChangeSetStoreError("Change set already exists.")
            self._records[change_set_id] = copy.deepcopy(record)
            self._events[change_set_id] = [copy.deepcopy(audit_event)]

    def get(self, change_set_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(change_set_id)
            return copy.deepcopy(record) if record else None

    def _transition(
        self,
        change_set_id: str,
        transition: Callable[
            [dict[str, Any]], tuple[TransitionResult, dict[str, Any]]
        ],
    ) -> TransitionResult:
        with self._lock:
            record = self._records.get(change_set_id)
            if record is None:
                raise ChangeSetStoreError("Change set not found.")
            result, event = transition(copy.deepcopy(record))
            self._records[change_set_id] = copy.deepcopy(result.record)
            self._events[change_set_id].append(copy.deepcopy(event))
            return copy.deepcopy(result)

    def approve(self, change_set_id: str, **kwargs: Any) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _approve_transition(record, **kwargs),
        )

    def consume(self, change_set_id: str, **kwargs: Any) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _consume_transition(record, **kwargs),
        )

    def record_success(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _success_transition(record, **kwargs),
        )

    def record_failure(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _failure_transition(record, **kwargs),
        )

    def record_uncertain(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _uncertain_transition(record, **kwargs),
        )

    def list_audit_events(self, change_set_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._events.get(change_set_id, []))


class FirestoreChangeSetStore:
    """Firestore-backed store using a transaction for every state change."""

    def __init__(
        self,
        *,
        project: str | None = None,
        database: str | None = None,
        collection: str = "google_ads_mcp_change_sets",
        audit_collection: str = "google_ads_mcp_change_set_audit",
        client: Any | None = None,
    ) -> None:
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise ChangeSetStoreError(
                "Firestore change-set storage requires the firestore extra."
            ) from exc

        self._firestore = firestore
        self._client = client or firestore.Client(
            project=project, database=database
        )
        self._records = self._client.collection(collection)
        self._audit = self._client.collection(audit_collection)

    def create(
        self, record: dict[str, Any], audit_event: dict[str, Any]
    ) -> None:
        batch = self._client.batch()
        batch.create(
            self._records.document(record["change_set_id"]),
            copy.deepcopy(record),
        )
        batch.create(
            self._audit.document(audit_event["event_id"]),
            copy.deepcopy(audit_event),
        )
        try:
            batch.commit()
        except Exception as exc:
            raise ChangeSetStoreError(
                "Could not durably create the change set."
            ) from exc

    def get(self, change_set_id: str) -> dict[str, Any] | None:
        try:
            snapshot = self._records.document(change_set_id).get()
        except Exception as exc:
            raise ChangeSetStoreError("Could not read the change set.") from exc
        if not snapshot.exists:
            return None
        return snapshot.to_dict()

    def _transition(
        self,
        change_set_id: str,
        transition: Callable[
            [dict[str, Any]], tuple[TransitionResult, dict[str, Any]]
        ],
    ) -> TransitionResult:
        record_ref = self._records.document(change_set_id)
        transaction = self._client.transaction()

        @self._firestore.transactional
        def run(transaction: Any) -> TransitionResult:
            snapshot = record_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise ChangeSetStoreError("Change set not found.")
            result, event = transition(snapshot.to_dict())
            transaction.set(record_ref, result.record)
            transaction.create(self._audit.document(event["event_id"]), event)
            return result

        try:
            return run(transaction)
        except ChangeSetStoreError:
            raise
        except Exception as exc:
            raise ChangeSetStoreError(
                "Could not atomically update the change set."
            ) from exc

    def approve(self, change_set_id: str, **kwargs: Any) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _approve_transition(record, **kwargs),
        )

    def consume(self, change_set_id: str, **kwargs: Any) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _consume_transition(record, **kwargs),
        )

    def record_success(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _success_transition(record, **kwargs),
        )

    def record_failure(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _failure_transition(record, **kwargs),
        )

    def record_uncertain(
        self, change_set_id: str, **kwargs: Any
    ) -> TransitionResult:
        return self._transition(
            change_set_id,
            lambda record: _uncertain_transition(record, **kwargs),
        )

    def list_audit_events(self, change_set_id: str) -> list[dict[str, Any]]:
        try:
            query = self._audit.where(
                filter=self._firestore.FieldFilter(
                    "change_set_id", "==", change_set_id
                )
            ).order_by("sequence")
            return [snapshot.to_dict() for snapshot in query.stream()]
        except Exception as exc:
            raise ChangeSetStoreError(
                "Could not read change-set audit events."
            ) from exc
