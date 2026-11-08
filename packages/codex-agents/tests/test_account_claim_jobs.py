"""Deferred claims with real disposable signature workers and fake providers."""
import json
import hashlib
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time
import unittest
from unittest import mock

import tests.test_account_claim_operation as fixture_module
from tests.learning_support import admit_provider, attest_provider
from codex_agents import signature_execution as execution
from codex_agents.account_api import (
    ACCOUNT_OPERATIONS, AccountError, AccountRequest, account_request_digest,
    decode_account_response, encode_account_request,
)
from codex_agents.account_claim_contracts import ClaimError, LaunchClaimInput
from codex_agents.account_claim_jobs import AccountClaimJobs
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LedgerError
from codex_agents.signature_job_intake import SignatureIntakeLimits
from codex_agents.signature_worker_pool import SignatureWorkerLimits
from codex_agents.sql_read_budget import SQLReadLimits
from codex_agents.admission_inputs import AdmissionCaptureError, AdmissionInputPolicy
from codex_agents import claim_admission


MACOS_WORKER = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


@unittest.skipUnless(MACOS_WORKER, "fixed macOS worker guard; no Linux policy selected")
class AccountClaimJobTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.AccountClaimOperationTests("test_handler_claims_with_owner_deadline_and_returns_known_state")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.ledger, self.views = self.fixture.ledger, self.fixture.views
        self.project, self.launch = self.fixture.project, self.fixture.launch
        self.gates = []
        self.jobs = AccountClaimJobs(self.views, SignatureIntakeLimits(2, 2, 128, 262_144, 2),
                                     SignatureWorkerLimits(2, 4 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
                                     SQLReadLimits(10_000, 1000, 1_048_576), allow_unleased_claim_reference=True)
        self.jobs.bootstrap()
        self.token = object()

        def authorize(request, context):
            if context is not self.token:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                        "host", ACCOUNT_OPERATIONS, "a" * 64)

        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views, authorizer=authorize, claim_jobs=self.jobs,
                                      allow_unleased_claim_reference=True)
        self.addCleanup(self.service.close)

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        self.jobs.request_close()
        started = time.monotonic()
        while not self.jobs.drain():
            self.assertLess(time.monotonic() - started, 10, "disposable workers did not drain")
            time.sleep(.002)

    def test_fault_before_offer_can_drain_queued_inputs_without_starting_work(self):
        self.begin()
        with mock.patch.object(self.jobs._pool, "poll", side_effect=RuntimeError("fixture before offer")), \
                self.assertRaisesRegex(RuntimeError, "fixture before offer"):
            self.jobs.drive()
        self.assertTrue(self.jobs._faulted)
        self.assertEqual(1, self.jobs._intake.snapshot().queued)
        with mock.patch.object(self.jobs._pool, "offer_next", side_effect=AssertionError("shutdown offer")), \
                mock.patch.object(self.views, "_claim_from_signature_job", side_effect=AssertionError("shutdown commit")):
            self.jobs.request_close()
            started = time.monotonic()
            while not self.jobs.drain():
                self.assertLess(time.monotonic() - started, 5)
                time.sleep(.002)
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
        self.assertEqual(0, self.jobs._intake.snapshot().jobs)
        self.fixture.assert_no_claim()

    def request(self, identity="job-1", *, project=None, launch=None, operation="account.claim_launch"):
        return AccountRequest(1, operation, identity, project or self.project, COLLECTOR_RUNTIME_DIGEST,
                              (launch or self.launch).payload() if operation == "account.claim_launch" else {})

    def begin(self, request=None):
        request = request or self.request()
        return self.service.begin(encode_account_request(request), self.token)

    def pump(self, ticket):
        started = time.monotonic()
        while True:
            self.service.drive_claims()
            raw = self.service.take_claim_reply(ticket)
            if raw is not None:
                return raw
            self.assertLess(time.monotonic() - started, 10, "deferred claim did not finish")
            time.sleep(.002)

    def gate_first_project(self):
        gate, started = threading.Event(), threading.Event()
        self.gates.append(gate)
        original = execution._execute_signature_job

        def gated(inputs, *args):
            if inputs.project_id == self.project:
                started.set()
                if not gate.wait(8):
                    raise AssertionError("fixture did not release its worker")
            return original(inputs, *args)

        return gate, started, mock.patch.object(execution, "_execute_signature_job", side_effect=gated)

    def assert_wire_error(self, raw, request, reason):
        self.assertIs(type(raw), bytes)
        with self.assertRaises(AccountError) as caught:
            decode_account_response(raw, request)
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertEqual(reason, json.loads(raw)["reason_code"])

    def test_actual_deferred_claim_verifies_off_owner_and_uses_original_atomic_receipt(self):
        owner, launched = threading.current_thread(), []
        popen = subprocess.Popen

        def worker_only(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread())
            launched.append(True)
            return popen(*args, **kwargs)

        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("synchronous verifier")), \
                mock.patch("sqlite3.connect", side_effect=AssertionError("extra SQLite")), \
                mock.patch.object(threading.Thread, "start", side_effect=AssertionError("new request thread")), \
                mock.patch.object(threading.Thread, "join", side_effect=AssertionError("request join")), \
                mock.patch.object(subprocess, "Popen", side_effect=worker_only):
            ticket = self.begin()
            self.assertIs(type(ticket), object)
            self.assertIsNone(self.service.take_claim_reply(ticket))
            result = decode_account_response(self.pump(ticket), self.request())
        self.assertEqual("claimed", result["state"])
        self.assertGreaterEqual(len(launched), 2)
        self.assertEqual((1, 1), (self.fixture.count("launch_claims"), self.fixture.count("launch_receipts")))
        self.assertEqual(0, self.jobs.snapshot()["tickets"])

    def other_project(self):
        bridge = self.fixture.fixture.fixture
        path = Path(bridge.temporary.name) / "second-project"
        path.mkdir()
        project = self.ledger.register_project(path, now_ms=1)
        admit_provider(self.ledger, project, bridge.owner_key, tools=bridge.tools,
                        observed_at_ms=self.fixture.stamp - 120_000)
        self.ledger.observe_provider(project, "codex", project_root=bridge.tools, environ=fixture_module.fixture_module.bridge_fixture.CLEAN)
        route, assignment = self.fixture.fixture.route, self.fixture.fixture.assignment
        orchestration = self.ledger.open_orchestration(project, client_version="codex-host-v1", now_ms=self.fixture.stamp)
        run = self.ledger.open_host_run(project, assignment=assignment, task=bridge.task(), route=route,
                                        execution_configuration=route["execution_configuration"], client_version="bridge-v1",
                                        orchestration_handle=orchestration["orchestration_handle"], now_ms=self.fixture.stamp)
        identity = self.ledger.observe_provider(project, "codex", project_root=bridge.tools,
                                                environ=fixture_module.fixture_module.bridge_fixture.CLEAN,
                                                run_handle=run["run_handle"], phase="before")[0]
        return project, LaunchClaimInput(run["run_handle"], content_digest(identity))

    def test_slow_signature_work_leaves_health_and_other_project_claim_available(self):
        project, launch = self.other_project()
        gate, started, patcher = self.gate_first_project()
        with patcher:
            first = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            health = self.request("health", operation="account.health")
            with mock.patch.object(self.views, "project", side_effect=AssertionError("health database")):
                self.assertEqual({"transport_alive": True, "database_checked": False},
                                 decode_account_response(self.begin(health), health))
            request = self.request("other", project=project, launch=launch)
            other = self.begin(request)
            self.assertEqual("claimed", decode_account_response(self.pump(other), request)["state"])
            self.assertFalse(gate.is_set())
            self.assertIsNone(self.service.take_claim_reply(first))
            gate.set()
            self.assertEqual("claimed", decode_account_response(self.pump(first), self.request())["state"])

    def test_pending_duplicate_is_one_ticket_and_changed_parameters_conflict(self):
        ticket = self.begin()
        self.assertIs(ticket, self.begin())
        changed = self.request(launch=LaunchClaimInput(self.launch.run_handle, "f" * 64))
        self.assert_wire_error(self.begin(changed), changed, "claim_conflict")
        self.assertEqual(1, self.jobs.snapshot()["tickets"])
        self.assertEqual("claimed", decode_account_response(self.pump(ticket), self.request())["state"])
        with mock.patch.object(self.jobs._intake, "capture", side_effect=AssertionError("retry signature job")):
            retry = self.begin()
            known = decode_account_response(self.service.take_claim_reply(retry), self.request())
        self.assertIs(True, known["known"])
        self.assertEqual(1, self.fixture.count("launch_claims"))

    def test_ready_replies_keep_project_quota_until_consumed(self):
        first = self.begin()
        decode_account_response(self.pump(first), self.request())
        ready = self.begin()
        second_request = self.request("already-consumed")
        denied = self.begin(second_request)
        self.assertEqual(2, self.jobs.snapshot()["ready"])
        overflow = self.request("overflow")
        self.assert_wire_error(self.begin(overflow), overflow, "claim_busy")
        self.assertIs(True, decode_account_response(self.service.take_claim_reply(ready), self.request())["known"])
        self.assertEqual("denied", decode_account_response(self.service.take_claim_reply(denied), second_request)["state"])
        self.assertEqual(0, self.jobs.snapshot()["tickets"])

    def test_queued_cancel_releases_inputs_but_keeps_bounded_reply(self):
        ticket = self.begin()
        self.assertEqual("released", self.jobs.cancel(ticket))
        self.assertEqual(0, self.jobs._intake.snapshot().jobs)
        self.assertEqual(1, self.jobs.snapshot()["ready"])
        self.assert_wire_error(self.service.take_claim_reply(ticket), self.request(), "claim_cancelled")
        self.fixture.assert_no_claim()

    def test_active_cancel_does_not_release_until_fixed_worker_confirms_stop(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.assertEqual("pending_confirmation", self.jobs.cancel(ticket))
            self.assertEqual(1, self.jobs._intake.snapshot().selected)
            self.assertIsNone(self.service.take_claim_reply(ticket))
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_cancelled")
        self.assertEqual(0, self.jobs._intake.snapshot().jobs)
        self.fixture.assert_no_claim()

    def test_new_signed_statement_during_worker_makes_preparation_stale(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            bridge = self.fixture.fixture.fixture
            attest_provider(self.ledger, self.project, bridge.owner_key, "codex",
                            self.fixture.identity["account_fingerprint"], issued_at=2)
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.fixture.assert_no_claim()

    def test_invalid_actual_signature_cannot_become_an_affirmative_claim(self):
        self.ledger._connection.execute("UPDATE owner_attestations SET signature=?", (b"invalid-signature",))
        result = decode_account_response(self.pump(self.begin()), self.request())
        self.assertEqual(("denied", "billing_attestation_missing"), (result["state"], result["reason_code"]))
        self.fixture.assert_no_claim()

    def test_new_foreign_account_restriction_is_rechecked_after_signature_worker(self):
        with self.ledger.repository(self.project) as repository:
            record = {key: value for key, value in repository.records("provider_billing")[-1].items()
                      if not key.startswith("_") and key != "record_id"}
        record.update(spend_control_reached=True, observed_at_ms=self.fixture.stamp + 1)
        record = {**record, "record_id": "provider_billing:" + content_digest(record)}
        bridge = self.fixture.fixture.fixture
        root = Path(bridge.temporary.name) / "restrictive-project"
        root.mkdir()
        project = self.ledger.register_project(root)
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.ledger._store_provider_observations(project, [record], timestamp=self.fixture.stamp + 1)
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
            self.fixture.assert_no_claim()
            # New evidence needs explicit new bounded preparation. It cannot be
            # parsed under the final writer or silently ignored as a cache miss.
            result = decode_account_response(self.pump(self.begin()), self.request())
        self.assertEqual(("denied", "codex_spend_control_reached"), (result["state"], result["reason_code"]))
        self.fixture.assert_no_claim()

    def test_complete_admission_parsing_is_off_owner_and_legacy_json_paths_are_unused(self):
        owner, parsed, sql_json = threading.current_thread(), [], []
        decode = claim_admission.decode_admission_inputs

        def off_owner(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread())
            parsed.append(True)
            return decode(*args, **kwargs)

        self.ledger._connection.set_trace_callback(lambda sql: sql_json.append("json_extract" in sql.lower()))
        try:
            with ExitStack() as stack:
                for method in ("_claim_context_locked", "_launch_identity_locked", "admitted_client",
                               "_run_admission_locked", "_account_records_locked", "_prepared_attestations_locked",
                               "client_version_reviewed"):
                    stack.enter_context(mock.patch.object(self.ledger, method, side_effect=AssertionError("legacy owner JSON")))
                stack.enter_context(mock.patch.object(claim_admission, "decode_admission_inputs", side_effect=off_owner))
                stack.enter_context(mock.patch("sqlite3.connect", side_effect=AssertionError("extra SQLite")))
                stack.enter_context(mock.patch.object(threading.Thread, "start", side_effect=AssertionError("new thread")))
                stack.enter_context(mock.patch.object(threading.Thread, "join", side_effect=AssertionError("owner join")))
                self.assertEqual("claimed", decode_account_response(self.pump(self.begin()), self.request())["state"])
        finally:
            self.ledger._connection.set_trace_callback(None)
        self.assertEqual([True], parsed)
        self.assertFalse(any(sql_json), "deferred owner used SQL JSON parsing")

    def test_deleted_candidate_after_preparation_is_stale_without_claim(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.ledger._connection.execute("DELETE FROM records WHERE record_type='provider_headroom'")
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.fixture.assert_no_claim()

    def test_changed_policy_after_preparation_is_stale_without_claim(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            gate.set()
            with mock.patch.object(self.views, "_claim_policy", return_value=AdmissionInputPolicy("999.0.0", "1000.0.0", None)):
                self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.fixture.assert_no_claim()

    def test_changed_before_observation_after_preparation_is_rechecked(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.ledger._connection.execute("UPDATE launch_observations SET identity_digest=?", ("e" * 64,))
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.fixture.assert_no_claim()
        self.assertEqual(0, self.fixture.count("launch_receipts"))
        self.ledger._connection.execute("UPDATE launch_observations SET identity_digest=?", (self.launch.identity_digest,))
        self.assertEqual("claimed", decode_account_response(self.pump(self.begin()), self.request())["state"])

    def test_missing_captured_launch_input_is_stale_and_can_be_explicitly_prepared_again(self):
        original = dict(self.ledger._connection.execute("SELECT * FROM launch_inputs").fetchone())
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.ledger._connection.execute("DELETE FROM launch_inputs")
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.assertEqual(0, self.fixture.count("launch_receipts"))
        self.fixture.assert_no_claim()
        names = list(original)
        self.ledger._connection.execute("INSERT INTO launch_inputs (" + ",".join(names) + ") VALUES("
                                        + ",".join("?" for _ in names) + ")", [original[name] for name in names])
        self.assertEqual("claimed", decode_account_response(self.pump(self.begin()), self.request())["state"])

    def test_unchanged_expired_run_keeps_ordinary_denial_instead_of_stale_failure(self):
        expires = self.ledger._connection.execute("SELECT expires_at_ms FROM host_run_bindings").fetchone()[0]
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.fixture.stamp = expires + 1
            gate.set()
            result = decode_account_response(self.pump(ticket), self.request())
        self.assertEqual(("denied", "claim_not_open"), (result["state"], result["reason_code"]))
        self.assertEqual(1, self.fixture.count("launch_receipts"))
        self.fixture.assert_no_claim()

    def test_transaction_restoration_interrupt_preserves_nonlocal_fault_and_returned_custody(self):
        self.begin()
        self.jobs.drive()
        started = time.monotonic()
        while not any(not slot.outgoing.empty() for slot in self.jobs._pool._slots):
            self.assertLess(time.monotonic() - started, 8)
            time.sleep(.002)
        restore = self.ledger._connection._restore_read_settings
        interrupted = []

        def interrupt(prior, scope, **kwargs):
            restore(prior, scope, **kwargs)
            if kwargs.get("transaction") and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt("fixture transaction restoration")

        with mock.patch.object(self.ledger._connection, "_restore_read_settings", side_effect=interrupt), \
                self.assertRaisesRegex(KeyboardInterrupt, "fixture transaction restoration"):
            self.jobs.drive()
        self.assertTrue(self.jobs.snapshot()["faulted"])
        self.assertEqual(1, self.jobs.snapshot()["tickets"])
        self.assertEqual(1, self.jobs._intake.snapshot().returned)
        with mock.patch.object(self.jobs._pool, "offer_next", side_effect=AssertionError("fault drain offer")), \
                mock.patch.object(self.views, "_claim_from_signature_job", side_effect=AssertionError("fault drain commit")):
            self.jobs.request_close()
            started = time.monotonic()
            while not self.jobs.drain():
                self.assertLess(time.monotonic() - started, 8)
                time.sleep(.002)
        # Independent fixture verification of the poisoned/closed owner's file;
        # this read-only connection is never a normal-client fallback.
        check = sqlite3.connect(self.ledger.state.database.as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual(0, check.execute("SELECT COUNT(*) FROM launch_claims").fetchone()[0])
            self.assertEqual(0, check.execute("SELECT COUNT(*) FROM launch_receipts").fetchone()[0])
        finally:
            check.close()

    def test_changed_run_parameters_after_preparation_are_stale_without_claim(self):
        gate, started, patcher = self.gate_first_project()
        with patcher:
            ticket = self.begin()
            self.jobs.drive()
            self.assertTrue(started.wait(2))
            self.ledger._connection.execute("UPDATE launch_inputs SET hard_timeout_seconds=hard_timeout_seconds+1")
            gate.set()
            self.assert_wire_error(self.pump(ticket), self.request(), "claim_preparation_stale")
        self.fixture.assert_no_claim()

    def test_foreign_preparation_object_cannot_replace_actual_returned_capture(self):
        ticket = self.begin()
        self.jobs.drive()
        started = time.monotonic()
        while not self.jobs._pool.poll():
            self.assertLess(time.monotonic() - started, 8)
            time.sleep(.002)
        handle = self.jobs._jobs[ticket].signature_handle
        actual = self.jobs._pool.result(handle)
        forged = replace(actual, prepared=replace(actual.prepared, snapshot=replace(actual.prepared.snapshot, origin=object())))
        with mock.patch.object(self.jobs._pool, "result", return_value=forged), self.assertRaisesRegex(ClaimError, "claim_unavailable"):
            self.views._claim_from_signature_job(self.jobs._jobs[ticket].context, self.jobs._pool, handle)
        self.fixture.assert_no_claim()

    def test_complete_capture_overflow_releases_reservation_before_helper_work(self):
        base = fixture_module.AccountClaimOperationTests("test_handler_claims_with_owner_deadline_and_returns_known_state")
        base.setUp()
        try:
            jobs = AccountClaimJobs(base.views, SignatureIntakeLimits(1, 1, 1, 262_144, 1),
                                    SignatureWorkerLimits(1, 2 * 1_048_576, 30_000_000_000, 10_000_000, 5_000_000_000),
                                    SQLReadLimits(10_000, 1000, 1_048_576))
            jobs.bootstrap()
            try:
                with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("overflow helper")), \
                        self.assertRaises(AdmissionCaptureError):
                    jobs.submit(base.project, runtime_digest=COLLECTOR_RUNTIME_DIGEST, caller_scope="a" * 64,
                                request_id="overflow", launch=base.launch)
                self.assertEqual(0, jobs.snapshot()["tickets"])
                self.assertEqual(0, jobs._intake.snapshot().jobs)
                base.assert_no_claim()
            finally:
                jobs.request_close()
                started = time.monotonic()
                while not jobs.drain():
                    self.assertLess(time.monotonic() - started, 8)
                    time.sleep(.002)
        finally:
            self.assertTrue(base.doCleanups())

    def test_worker_projection_does_not_collapse_duplicate_admitted_identity_citations(self):
        ticket = self.begin()
        handle = self.jobs._jobs[ticket].signature_handle
        snapshot = self.jobs._intake._jobs[handle].admission
        prepared = claim_admission.prepare_claim_admission(snapshot, claim_admission.AdmissionCaptureLimits(128, 262_144))
        run_id = self.ledger._connection.execute("SELECT run_id FROM host_run_bindings").fetchone()[0]
        self.assertIsNone(claim_admission.claim_identity(prepared, run_id, self.launch.identity_digest)[0])
        admission = prepared.admissions[run_id][0]
        cited = next(digest for digest in admission["evidence_digests"] if digest in prepared.identities)
        admission["evidence_digests"].append(cited)
        with self.assertRaisesRegex(ClaimError, "claim_preparation_failed"):
            claim_admission.claim_identity(prepared, run_id, self.launch.identity_digest)
        self.fixture.assert_no_claim()

    def test_wrong_source_local_admission_candidate_cannot_be_hidden_from_preparation(self):
        row = self.ledger._connection.execute(
            "SELECT * FROM records WHERE project_id=? AND record_type='admission_decision' AND source='lifecycle' LIMIT 1",
            (self.project,),
        ).fetchone()
        values = dict(row)
        record = json.loads(bytes(values["content"]))
        run_id = record["run_id"]
        record["record_id"] = "admission_decision:" + hashlib.sha256(b"wrong-source-admission-fixture").hexdigest()
        values.update(record_id=record["record_id"], source="owner", content=canonical_json(record),
                      source_sequence=self.ledger._connection.execute(
                          "SELECT MAX(source_sequence)+100 FROM records WHERE project_id=?", (self.project,),
                      ).fetchone()[0])
        values["content_digest"] = hashlib.sha256(values["content"]).hexdigest()
        names = list(values)
        self.ledger._connection.execute("INSERT INTO records (" + ",".join(names) + ") VALUES("
                                        + ",".join("?" for _ in names) + ")", [values[name] for name in names])
        with self.assertRaises(LedgerError):
            self.ledger._run_admission_locked(self.project, run_id)
        self.assert_wire_error(self.pump(self.begin()), self.request(), "claim_preparation_failed")
        self.fixture.assert_no_claim()

    def test_request_mutation_in_authorizer_is_denied_before_ticket_reservation(self):
        original = self.service._authorizer

        def mutate(request, context):
            authorization = original(request, context)
            request.payload["identity_digest"] = "f" * 64
            return authorization

        with mock.patch.object(self.service, "_authorizer", side_effect=mutate):
            self.assert_wire_error(self.begin(), self.request(), "account_not_authorized")
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
        self.fixture.assert_no_claim()

    def test_default_denial_and_no_synchronous_fallback_for_deferred_entry(self):
        raw = encode_account_request(self.request())
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("synchronous fallback")):
            self.assert_wire_error(self.service.handle(raw, self.token), self.request(), "claim_unavailable")
            self.assert_wire_error(self.fixture.service.begin(raw, self.fixture.token), self.request(), "claim_unavailable")
            denied = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views, claim_jobs=self.jobs,
                                    allow_unleased_claim_reference=True)
            self.addCleanup(denied.close)
            self.assert_wire_error(denied.begin(raw), self.request(), "account_not_authorized")
        self.assertEqual(0, self.jobs.snapshot()["tickets"])

    def test_unregistered_pool_or_invented_ticket_cannot_supply_signature_proof(self):
        context, _, _, _ = self.views._claim_preflight(self.project, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                                                      caller_scope="a" * 64, request_id="job-1", launch=self.launch)
        with self.assertRaises(ClaimError):
            self.ledger._claim_from_signature_job(context, object(), object())
        with self.assertRaises(ClaimError):
            self.service.take_claim_reply(object())
        self.fixture.assert_no_claim()

    def test_wrong_thread_is_denied_before_capture_or_finalization(self):
        errors = []

        def foreign():
            for function in (self.jobs.drive, self.jobs.snapshot):
                try:
                    function()
                except AccountError as error:
                    errors.append(error.reason_code)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(["account_wrong_owner", "account_wrong_owner"], errors)

    def test_interrupted_final_commit_rolls_back_and_retains_returned_job_without_replay(self):
        class FixtureStop(BaseException):
            pass

        append = self.ledger._append_checked_locked

        def interrupted(*args, **kwargs):
            append(*args, **kwargs)
            raise FixtureStop()

        ticket = self.begin()
        with mock.patch.object(self.ledger, "_append_checked_locked", side_effect=interrupted), self.assertRaises(FixtureStop):
            self.pump(ticket)
        self.assertTrue(self.jobs._faulted)
        self.assertEqual(1, self.jobs._intake.snapshot().returned)
        self.fixture.assert_no_claim()
        self.assert_wire_error(self.begin(), self.request(), "claim_unavailable")

    def test_reply_publication_fault_keeps_committed_receipt_and_stopped_job_without_replay(self):
        ticket = self.begin()
        with mock.patch.object(self.jobs._pool, "discard", side_effect=RuntimeError("fixture publication fault")), \
                self.assertRaisesRegex(RuntimeError, "fixture publication fault"):
            self.pump(ticket)
        self.assertTrue(self.jobs._faulted)
        self.assertEqual(1, self.jobs._intake.snapshot().returned)
        self.assertEqual((1, 1), (self.fixture.count("launch_claims"), self.fixture.count("launch_receipts")))
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("reverified receipt")):
            known = self.fixture.claim(identity="job-1")
        self.assertIs(True, known["known"])
        self.assert_wire_error(self.begin(), self.request(), "claim_unavailable")

    def test_faulted_coordinator_has_public_cleanup_without_offer_or_recommit(self):
        ticket = self.begin()
        with mock.patch.object(self.jobs._pool, "discard", side_effect=RuntimeError("fixture drain fault")), \
                self.assertRaisesRegex(RuntimeError, "fixture drain fault"):
            self.pump(ticket)
        self.assertTrue(self.jobs._faulted)
        with mock.patch.object(self.jobs._pool, "offer_next", side_effect=AssertionError("cleanup offered work")), \
                mock.patch.object(self.views, "_claim_from_signature_job", side_effect=AssertionError("cleanup recommitted")):
            self.jobs.request_close()
            started = time.monotonic()
            while not self.jobs.drain():
                self.assertLess(time.monotonic() - started, 5)
                time.sleep(.002)
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
        self.assertIs(True, self.jobs.snapshot()["faulted"])
        self.assertEqual((1, 1), (self.fixture.count("launch_claims"), self.fixture.count("launch_receipts")))
        self.assert_wire_error(self.begin(), self.request(), "claim_unavailable")

    def test_fault_after_returned_input_release_can_drain_confirmed_completion_metadata(self):
        release = self.jobs._intake.discard_returned

        def interrupted(handle):
            release(handle)
            raise RuntimeError("fixture after input release")

        ticket = self.begin()
        with mock.patch.object(self.jobs._intake, "discard_returned", side_effect=interrupted), \
                self.assertRaisesRegex(RuntimeError, "fixture after input release"):
            self.pump(ticket)
        self.assertTrue(self.jobs._faulted)
        self.assertEqual(0, self.jobs._intake.snapshot().jobs)
        self.jobs.request_close()
        started = time.monotonic()
        while not self.jobs.drain():
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(.002)
        self.assertEqual(0, self.jobs.snapshot()["tickets"])
        self.assertEqual((1, 1), (self.fixture.count("launch_claims"), self.fixture.count("launch_receipts")))


if __name__ == "__main__":
    unittest.main()
