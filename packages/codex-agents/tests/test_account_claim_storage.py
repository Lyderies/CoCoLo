"""Real project/account stores with fixture frontend identity; no model launch."""
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_claim_operation as fixtures

from codex_agents.account_api import AccountError, account_request_digest, encode_account_request
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents.claim_storage import MAX_STORAGE_EVIDENCE_BYTES, lease_state_seal
from codex_agents.history_contracts import AttemptBinding, HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, MAX_RESULT_BODY_BYTES, ResultCapacity, ResultLeaseBinding
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits


class AccountClaimStorageTests(unittest.TestCase):
    def setUp(self):
        self.base = fixtures.AccountClaimOperationTests(methodName="runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.ledger, self.project = self.base.ledger, self.base.project
        source = self.base.fixture
        self.assigned = source.assignment
        self.orchestration = self.ledger.open_orchestration(self.project, client_version="codex-host-v1", now_ms=self.base.stamp)
        opened = self.ledger.open_host_run(
            self.project, assignment=self.assigned, task=source.fixture.task(), route=source.route,
            execution_configuration=source.route["execution_configuration"], client_version="bridge-v1",
            orchestration_handle=self.orchestration["orchestration_handle"], now_ms=self.base.stamp,
        )
        self.base.handle = opened["run_handle"]
        self.key = self.base.ledger.state.digest_key()  # Disposable fixture state, never a real owner credential.
        run_id = self.ledger._connection.execute("SELECT run_id FROM host_run_bindings WHERE project_id=? AND handle_digest=?",
                                                 (self.project, keyed_digest(self.key, self.base.handle))).fetchone()[0]
        self.base.launch = LaunchClaimInput(self.base.handle, content_digest(self.base.observe()[0]))
        self.request = self.base.request("storage-claim")
        self.scope = ResultLeaseBinding(
            self.project, "primary", COLLECTOR_RUNTIME_DIGEST, "project-supervisor", 1,
            self.orchestration["orchestration_handle"], self.base.handle, run_id,
            self.assigned["attempt"], self.request.request_id, account_request_digest(self.request),
        )
        self.expected, self.evidence = self.scope, None
        self.directory = Path(source.fixture.temporary.name) / "project-history"
        self.directory.mkdir(mode=0o700)
        self.store = self.open_history()
        self.addCleanup(lambda: self.store.close())
        self.jobs = None
        self.service = self.new_service()

    def open_history(self):
        return ProjectHistory(self.directory, self.project, result_key=self.key,
                              allow_legacy_orchestration_reference=True,
                              result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
                              result_clock_ms=lambda: self.base.stamp)

    def authorize(self, request, context):
        if context is not self.base.token:
            return None
        return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                    "host", frozenset({"account.claim_launch"}), "a" * 64,
                                    self.expected, self.evidence)

    def new_service(self, **changes):
        options = dict(authorizer=self.authorize)
        options.update(changes)
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.base.views, **options)
        self.addCleanup(service.close)
        return service

    def arm(self):
        self.store.reserve_result_lease(self.scope, self.assigned)
        self.store.arm_result_lease(self.scope)
        self.evidence = self.store.attest_result_lease(self.scope)
        return self.store.result_lease(self.scope)

    def call(self, *, service=None):
        return self.base.call(self.request, service=service or self.service)

    def no_consumption(self):
        self.base.assert_no_claim()
        self.assertEqual(0, self.base.count("launch_receipts"))
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity").fetchone()[0])

    def rejection(self, reason, *, service=None):
        with self.assertRaisesRegex(AccountError, reason) as caught:
            self.call(service=service)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.no_consumption()

    def signed(self, value, *, key=None):
        return canonical_json({"protocol_version": 1, "lease": asdict(value), "seal": lease_state_seal(key or self.key, value)})

    def test_default_service_requires_protected_storage_before_any_claim_consumption(self):
        self.rejection("claim_storage_required")
        lease = self.store.reserve_result_lease(self.scope, self.assigned)
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.store.attest_result_lease(self.scope)
        self.evidence = self.signed(lease)
        self.rejection("claim_storage_invalid")
        self.arm()
        receipt = self.call()
        self.assertEqual("claimed", receipt["state"])
        self.assertFalse(receipt["known"])
        self.assertEqual(1, self.base.count("launch_claims"))
        self.assertEqual(1, self.base.count("launch_receipts"))
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])

    def test_actual_lease_binding_and_lost_account_reply_retrieve_without_live_lease_or_signature_work(self):
        lease = self.arm()
        first = self.call()
        target = AttemptBinding(**{name: getattr(self.scope, name) for name in LEASE_SCOPE_FIELDS},
                                claim_id=first["bridge_run_id"], claim_digest=first["receipt_digest"])
        self.store.bind_result_lease(self.scope, target, self.assigned, hard_deadline_at_ms=first["deadline_at_ms"])
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.store.attest_result_lease(self.scope)
        self.store.close()
        self.evidence = self.expected = None
        self.base.stamp = lease.reserved_until_ms + 1
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("known reply reverified")), \
                mock.patch.object(ProjectHistory, "__init__", side_effect=AssertionError("account opened project SQLite")):
            self.assertEqual({**first, "known": True}, self.call())
        self.base.stamp -= 3_000_000
        self.assertEqual({**first, "known": True}, self.call())
        self.assertEqual(1, self.base.count("launch_claims"))

    def test_wrong_key_modified_seal_and_arbitrary_content_never_consume_or_fall_back(self):
        lease = self.arm()
        valid = self.evidence
        invalid = [self.signed(lease, key=b"x" * 32), b"PRIVATE-REJECTED-CANARY", valid + b"\n",
                   canonical_json({**json.loads(valid), "seal": "f" * 64}),
                   canonical_json({**json.loads(valid), "unexpected": "PRIVATE-SECRET"}),
                   canonical_json({**json.loads(valid), "protocol_version": True}),
                   b'{"protocol_version":1,"protocol_version":1,' + valid[1:],
                   self.signed(replace(lease, reserved_until_ms=lease.reserved_until_ms + 1))]
        reference = self.new_service(allow_unleased_claim_reference=True)
        for raw in invalid:
            self.evidence = raw
            for service in (self.service, reference):
                self.rejection("claim_storage_invalid", service=service)
        self.evidence = valid
        self.assertEqual("claimed", self.call()["state"])

    def test_expected_actor_scope_is_independent_of_the_evidence_and_account_rows_bind_run_and_orchestration(self):
        lease = self.arm()
        for name, altered in (
            ("primary_session_id", "foreign-primary"), ("supervisor_instance_id", "other-supervisor"),
            ("supervisor_generation", 2), ("runtime_digest", "b" * 64),
            ("project_id", "prj_" + "b" * 32), ("run_handle", "hrn_" + "b" * 48),
            ("run_id", "run:" + "b" * 64), ("orchestration_handle", "orc_" + "b" * 48),
            ("claim_request_id", "other-request"), ("claim_request_digest", "b" * 64),
        ):
            changed = replace(self.scope, **{name: altered})
            with self.subTest(field=name):
                self.evidence = self.signed(replace(lease, binding=changed))
                self.rejection("claim_storage_invalid")
                if name not in {"primary_session_id", "supervisor_instance_id", "supervisor_generation"}:
                    # Even matching adapter/evidence values cannot replace the
                    # authoritative account run/request/orchestration metadata.
                    self.expected = changed
                    self.rejection("claim_storage_invalid")
                    self.expected = self.scope
        self.evidence = self.signed(replace(lease, assignment_digest="b" * 64))
        self.rejection("claim_storage_invalid")

    def test_original_storage_window_must_fit_the_whole_account_deadline_before_capacity_work(self):
        lease = self.arm()
        self.base.stamp = lease.reserved_until_ms - 1
        with mock.patch.object(self.ledger, "_check_active_capacity_locked", side_effect=AssertionError("early capacity")):
            self.rejection("claim_storage_expired")
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.base.stamp = lease.reserved_until_ms
            self.store.attest_result_lease(self.scope)
        self.assertEqual("claim_pending", self.store.result_lease(self.scope).state)

    def test_bad_or_injected_authorizer_evidence_is_bounded_and_request_json_cannot_supply_it(self):
        for raw in (b"x" * (MAX_STORAGE_EVIDENCE_BYTES + 1), "PRIVATE-TEXT", {}, b""):
            self.evidence = raw
            self.rejection("account_not_authorized")
        self.evidence = None
        for field, value in (("storage_evidence", "PRIVATE-TEXT"), ("storage_required", False),
                             ("allow_unleased_claim_reference", True)):
            data = json.loads(encode_account_request(self.request))
            data["payload"][field] = value
            reply = json.loads(self.service.handle(canonical_json(data), self.base.token))
            self.assertEqual("account_invalid_request", reply["reason_code"])
            self.assertNotIn("PRIVATE", json.dumps(reply))
        self.no_consumption()
        for setting in (None, "true", 1):
            with self.assertRaisesRegex(AccountError, "account_invalid_request"):
                self.new_service(allow_unleased_claim_reference=setting)

    def async_service(self):
        self.jobs = AccountClaimJobs(
            self.base.views, SignatureIntakeLimits(2, 2, 128, 262_144, 2),
            SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
            SQLReadLimits(10_000, 1000, 1_048_576),
        )
        self.jobs.bootstrap()
        self.addCleanup(self.stop_jobs)
        return self.new_service(claim_jobs=self.jobs)

    def stop_jobs(self):
        self.jobs.request_close()
        end = time.monotonic() + 10
        while not self.jobs.drain():
            self.assertLess(time.monotonic(), end, "fixture signature workers did not drain")
            time.sleep(.002)

    def finish(self, service, ticket):
        end = time.monotonic() + 10
        while True:
            service.drive_claims()
            reply = service.take_claim_reply(ticket)
            if reply is not None:
                from codex_agents.account_api import decode_account_response
                return decode_account_response(reply, self.request)
            self.assertLess(time.monotonic(), end, "fixture deferred claim did not finish")
            time.sleep(.002)

    @unittest.skipUnless(sys.platform == "darwin", "fixed macOS signature helpers")
    def test_deferred_default_cannot_use_reference_fallback_and_freezes_original_adapter_data(self):
        service = self.async_service()
        ticket = service.begin(encode_account_request(self.request), self.base.token)
        with self.assertRaisesRegex(AccountError, "claim_storage_required"):
            self.finish(service, ticket)
        self.no_consumption()
        self.arm()
        expected = self.expected
        ticket = service.begin(encode_account_request(self.request), self.base.token)
        # Mutation of returned adapter data is not revocation. The original
        # server capture must not alias that mutable value during verification.
        object.__setattr__(expected, "primary_session_id", "mutated-adapter-output")
        self.evidence = b"PRIVATE-REPLACEMENT-CANARY"
        with mock.patch.object(ProjectHistory, "__init__", side_effect=AssertionError("account opened project store")):
            receipt = self.finish(service, ticket)
        self.assertEqual("claimed", receipt["state"])
        self.assertEqual(1, self.base.count("launch_claims"))
        with self.assertRaisesRegex(AccountError, "account_invalid_request"):
            self.new_service(claim_jobs=self.jobs, allow_unleased_claim_reference=True)

    @unittest.skipUnless(sys.platform == "darwin", "fixed macOS signature helpers")
    def test_assignment_change_during_deferred_work_is_stale_before_claim_consumption(self):
        self.arm()
        service = self.async_service()
        for column in ("assignment_digest", "orchestration_id"):
            original = self.ledger._connection.execute(
                "SELECT " + column + " FROM host_run_bindings WHERE run_id=?", (self.scope.run_id,)).fetchone()[0]
            ticket = service.begin(encode_account_request(self.request), self.base.token)
            try:
                self.ledger._connection.execute("UPDATE host_run_bindings SET " + column + "=? WHERE run_id=?",
                                                ("b" * 64, self.scope.run_id))
                with self.assertRaisesRegex(AccountError, "claim_preparation_stale"):
                    self.finish(service, ticket)
                self.no_consumption()
            finally:
                self.ledger._connection.execute("UPDATE host_run_bindings SET " + column + "=? WHERE run_id=?",
                                                (original, self.scope.run_id))


if __name__ == "__main__":
    unittest.main()
