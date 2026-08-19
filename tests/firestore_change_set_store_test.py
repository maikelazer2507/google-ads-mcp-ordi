# Copyright 2026 Google LLC.

"""Transaction-contract tests for the Firestore change-set store."""

import copy
import hashlib
import threading
import unittest

from ads_mcp.change_set_store import (
    FirestoreChangeSetStore,
    canonical_payload_hash,
)


class _FakeSnapshot:
    def __init__(self, value):
        self.exists = value is not None
        self._value = copy.deepcopy(value)

    def to_dict(self):
        return copy.deepcopy(self._value)


class _FakeDocument:
    def __init__(self, client, collection, document_id):
        self.client = client
        self.key = (collection, document_id)

    def get(self, transaction=None):
        if transaction is not None:
            return transaction.get(self)
        with self.client.lock:
            return _FakeSnapshot(self.client.data.get(self.key))


class _FakeQuery:
    def __init__(self, client, collection, field=None, value=None, order=None):
        self.client = client
        self.collection = collection
        self.field = field
        self.value = value
        self.order = order

    def where(self, *, filter):
        return _FakeQuery(
            self.client,
            self.collection,
            field=filter.field_path,
            value=filter.value,
            order=self.order,
        )

    def order_by(self, field):
        return _FakeQuery(
            self.client,
            self.collection,
            field=self.field,
            value=self.value,
            order=field,
        )

    def stream(self):
        with self.client.lock:
            values = [
                copy.deepcopy(value)
                for (collection, _), value in self.client.data.items()
                if collection == self.collection
                and (self.field is None or value.get(self.field) == self.value)
            ]
        if self.order:
            values.sort(key=lambda value: value.get(self.order))
        return [_FakeSnapshot(value) for value in values]


class _FakeCollection(_FakeQuery):
    def __init__(self, client, collection):
        super().__init__(client, collection)

    def document(self, document_id):
        return _FakeDocument(self.client, self.collection, document_id)


class _FakeBatch:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def create(self, document, value):
        self.operations.append(("create", document, copy.deepcopy(value)))

    def commit(self):
        with self.client.lock:
            for operation, document, _ in self.operations:
                if operation == "create" and document.key in self.client.data:
                    raise RuntimeError("document exists")
            for _, document, value in self.operations:
                self.client.data[document.key] = copy.deepcopy(value)


class _FakeTransaction:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def get(self, document):
        return _FakeSnapshot(self.client.data.get(document.key))

    def set(self, document, value):
        self.operations.append(("set", document, copy.deepcopy(value)))

    def create(self, document, value):
        self.operations.append(("create", document, copy.deepcopy(value)))

    def commit(self):
        for operation, document, _ in self.operations:
            if operation == "create" and document.key in self.client.data:
                raise RuntimeError("document exists")
        for _, document, value in self.operations:
            self.client.data[document.key] = copy.deepcopy(value)


class _FakeClient:
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()

    def collection(self, name):
        return _FakeCollection(self, name)

    def batch(self):
        return _FakeBatch(self)

    def transaction(self):
        return _FakeTransaction(self)


class _FakeFieldFilter:
    def __init__(self, field_path, op_string, value):
        if op_string != "==":
            raise ValueError("fake supports equality only")
        self.field_path = field_path
        self.value = value


class _FakeFirestore:
    FieldFilter = _FakeFieldFilter

    @staticmethod
    def transactional(function):
        def run(transaction):
            with transaction.client.lock:
                result = function(transaction)
                transaction.commit()
                return result

        return run


class TestFirestoreChangeSetStore(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.store = self._new_store()
        self.change_set_id = "a" * 32
        self.customer_id = "1234567890"
        self.environment = "test"
        self.payload = {
            "action": "campaign_status_change",
            "customer_id": self.customer_id,
            "proposed": {"status": "PAUSED"},
        }
        self.token_hash = hashlib.sha256(b"opaque-token").hexdigest()
        self.payload_hash = canonical_payload_hash(
            self.payload, self.customer_id, self.environment
        )
        self.record = {
            "version": 2,
            "change_set_id": self.change_set_id,
            "status": "PENDING",
            "environment": self.environment,
            "customer_id": self.customer_id,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "token_hash": self.token_hash,
            "requested_at": 100,
            "requested_by": {"subject": "operator", "role": "operator"},
            "expires_at": 1_000,
            "updated_at": 100,
            "audit_sequence": 1,
        }
        self.created_event = {
            "event_id": "created-event",
            "change_set_id": self.change_set_id,
            "event_type": "CREATED",
            "sequence": 1,
            "occurred_at": 100,
            "actor": {"subject": "operator", "role": "operator"},
            "customer_id": self.customer_id,
            "environment": self.environment,
            "payload_hash": self.payload_hash,
            "metadata": {},
        }
        self.approver = {"subject": "owner", "role": "owner"}
        self.executor = {"subject": "operator", "role": "operator"}
        self.integrity_key = b"i" * 32

    def _new_store(self):
        store = FirestoreChangeSetStore(client=self.client)
        store._firestore = _FakeFirestore
        return store

    def _approve(self, store=None):
        return (store or self.store).approve(
            self.change_set_id,
            now=200,
            expected_payload_hash=self.payload_hash,
            expected_customer_id=self.customer_id,
            expected_environment=self.environment,
            approver=self.approver,
            integrity_key=self.integrity_key,
        )

    def _consume(self, store, execution_id):
        return store.consume(
            self.change_set_id,
            now=300,
            token_hash=self.token_hash,
            expected_environment=self.environment,
            expected_action="campaign_status_change",
            executor=self.executor,
            execution_id=execution_id,
            integrity_key=self.integrity_key,
        )

    def test_approve_and_consume_persist_ordered_audit_events(self):
        self.store.create(self.record, self.created_event)
        approved = self._approve()
        self.assertTrue(approved.accepted)
        self.assertEqual(
            approved.record["approved_payload_hash"], self.payload_hash
        )
        consumed = self._consume(self.store, "b" * 32)
        self.assertTrue(consumed.accepted)
        events = self.store.list_audit_events(self.change_set_id)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["CREATED", "APPROVED", "CONSUMED"],
        )
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])

    def test_live_firestore_payload_is_rehashed_inside_approval_transaction(
        self,
    ):
        self.store.create(self.record, self.created_event)
        record_key = ("google_ads_mcp_change_sets", self.change_set_id)
        self.client.data[record_key]["payload"]["proposed"][
            "status"
        ] = "REMOVED"
        rejected = self._approve()
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.rejection_code, "PAYLOAD_INTEGRITY_MISMATCH")
        self.assertEqual(rejected.record["status"], "PENDING")

    def test_two_store_instances_allow_exactly_one_consumer(self):
        second_store = self._new_store()
        self.store.create(self.record, self.created_event)
        self.assertTrue(self._approve().accepted)
        barrier = threading.Barrier(3)
        results = []
        result_lock = threading.Lock()

        def consume(store, execution_id):
            barrier.wait()
            outcome = self._consume(store, execution_id)
            with result_lock:
                results.append(outcome)

        threads = [
            threading.Thread(target=consume, args=(self.store, "b" * 32)),
            threading.Thread(target=consume, args=(second_store, "c" * 32)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(result.accepted for result in results), 1)
        rejected = [result for result in results if not result.accepted]
        self.assertEqual(rejected[0].rejection_code, "NOT_APPROVED")
        events = self.store.list_audit_events(self.change_set_id)
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3, 4])

    def test_keyed_seal_rejects_payload_and_hash_tampering(self):
        self.store.create(self.record, self.created_event)
        self.assertTrue(self._approve().accepted)
        record_key = ("google_ads_mcp_change_sets", self.change_set_id)
        stored = self.client.data[record_key]
        stored["payload"]["proposed"]["status"] = "REMOVED"
        tampered_hash = canonical_payload_hash(
            stored["payload"], stored["customer_id"], stored["environment"]
        )
        stored["payload_hash"] = tampered_hash
        stored["approved_payload_hash"] = tampered_hash

        rejected = self._consume(self.store, "b" * 32)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.rejection_code, "APPROVAL_INTEGRITY_MISMATCH")
        self.assertEqual(rejected.record["status"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
