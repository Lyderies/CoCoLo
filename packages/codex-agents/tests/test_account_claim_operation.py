"""Real owner/handler operations on fake-provider state; no model execution."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest import mock

import tests.test_account_launch_claims as fixture_module

from codex_agents import account_api, learning_store
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, account_request_digest, decode_account_response, encode_account_request
from codex_agents.account_claim_contracts import ClaimError, LaunchClaimInput
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.learning_contracts import content_digest
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, DEFAULT_RETENTION_MS, LearningLedger, LedgerError, StateError
from codex_agents.service_transport import ServiceTransportError
from tests.learning_support import backup_body


class AccountClaimOperationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.AccountLaunchClaimsTests("test_concurrent_same_run_claims_consume_exactly_once")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.ledger, self.project, self.stamp = self.fixture.ledger, self.fixture.project, self.fixture.stamp + 1
        patch = mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: self.stamp)
        patch.start()
        self.addCleanup(patch.stop)
        self.handle = self.fixture.arguments["run_handle"]
        self.identity = self.observe()[0]
        self.launch = LaunchClaimInput(self.handle, content_digest(self.identity))
        self.token = object()
        self.views = AccountOwnerViews(self.ledger)
        self.service = self.make_service()

    def observe(self):
        return self.ledger.observe_provider(self.project, "codex", project_root=self.fixture.fixture.tools,
                                            environ=fixture_module.bridge_fixture.CLEAN, run_handle=self.handle, phase="before")

    def make_service(self, *, source="host", scope="a" * 64):
        def authorize(request, context):
            if context is not self.token:
                return None
            return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                        source, frozenset({"account.claim_launch"}), scope)
        # Explicit old-path reference fixture; new services require armed storage.
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views, authorizer=authorize,
                                 allow_unleased_claim_reference=True)
        self.addCleanup(service.close)
        return service

    def claim(self, identity="request-1", *, scope="a" * 64, launch=None):
        return self.ledger.claim_launch(self.project, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                                        caller_scope=scope, request_id=identity, launch=launch or self.launch)

    def request(self, identity="request-1", payload=None):
        return AccountRequest(1, "account.claim_launch", identity, self.project, COLLECTOR_RUNTIME_DIGEST,
                              self.launch.payload() if payload is None else payload)

    def call(self, request=None, *, service=None, context=None):
        request = request or self.request()
        raw = (service or self.service).handle(encode_account_request(request), self.token if context is None else context)
        return decode_account_response(raw, request)

    def count(self, table):
        assert table in {"launch_claims", "launch_receipts", "launch_inputs", "launch_observations"}
        return self.ledger._connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]

    def assert_no_claim(self):
        self.assertEqual(0, self.count("launch_claims"))
        self.assertEqual([], self.fixture.intents())

    def test_handler_claims_with_owner_deadline_and_returns_known_state(self):
        first, known = self.call(), self.call()
        self.assertEqual("claimed", first["state"])
        self.assertIs(False, first["known"])
        self.assertEqual({**first, "known": True}, known)
        self.assertEqual(self.stamp + min(self.fixture.route["bounds"]["timeout_seconds"],
                                         self.fixture.assignment["bounds"]["timeout_seconds"]) * 1000,
                         first["deadline_at_ms"])
        self.assertEqual((1, 1), (self.count("launch_claims"), self.count("launch_receipts")))
        raw = json.dumps([tuple(row) for row in self.ledger._connection.execute("SELECT * FROM launch_receipts")])
        self.assertNotIn(self.handle, raw)
        self.assertNotIn("request-1", raw)

    def test_changed_retry_conflicts_and_other_caller_scope_cannot_consume_again(self):
        first = self.claim()
        with self.assertRaisesRegex(ClaimError, "claim_conflict"):
            self.claim(launch=LaunchClaimInput(self.handle, "f" * 64))
        other = self.claim(scope="b" * 64)
        self.assertEqual(("denied", "claim_already_consumed"), (other["state"], other["reason_code"]))
        self.assertNotEqual(first["receipt_digest"], other["receipt_digest"])
        self.assertTrue(self.claim(scope="b" * 64)["known"])
        self.assertEqual(1, self.count("launch_claims"))

    def test_lost_reply_reopen_needs_no_signature_work_or_new_claim(self):
        first = self.claim()
        self.ledger.close()
        self.ledger = LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients)
        self.addCleanup(self.ledger.close)
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("reverified known reply")):
            self.assertEqual({**first, "known": True}, self.claim())
        self.assertEqual(1, self.count("launch_claims"))

    def test_history_expiry_and_clock_rollback_preserve_receipt_and_consumption(self):
        first = self.claim()
        self.assertGreater(self.ledger.expire(self.project, now_ms=self.stamp + DEFAULT_RETENTION_MS + 1), 0)
        self.stamp -= 60_000
        self.assertEqual({**first, "known": True}, self.claim())
        self.assertEqual("claim_already_consumed", self.claim("new-request")["reason_code"])

    def race(self, same_identity):
        barrier, results, errors = threading.Barrier(2), [], []

        def run(index):
            try:
                barrier.wait(5)
                results.append(self.claim("same" if same_identity else "request-" + str(index)))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, self.count("launch_claims"))
        return results

    def test_same_request_race_returns_one_committed_and_one_known(self):
        replies = self.race(True)
        self.assertEqual([False, True], sorted(reply["known"] for reply in replies))
        self.assertEqual(1, len({reply["receipt_digest"] for reply in replies}))
        self.assertEqual(1, self.count("launch_receipts"))

    def test_different_request_race_consumes_run_only_once(self):
        replies = self.race(False)
        self.assertEqual(["claimed", "denied"], sorted(reply["state"] for reply in replies))
        self.assertEqual(2, self.count("launch_receipts"))

    def test_interruption_after_intent_or_receipt_rolls_back_the_whole_operation(self):
        class FixtureStop(BaseException):
            pass

        for method in ("_append_checked_locked", "_store_claim_receipt_locked"):
            original = getattr(self.ledger, method)

            def interrupted(*arguments, **keywords):
                original(*arguments, **keywords)
                self.assertEqual(1, self.count("launch_claims"))
                if method == "_store_claim_receipt_locked":
                    self.assertEqual(1, self.count("launch_receipts"))
                raise FixtureStop()

            with self.subTest(method=method), mock.patch.object(self.ledger, method, side_effect=interrupted), self.assertRaises(FixtureStop):
                self.claim()
            self.assertFalse(self.ledger._connection.in_transaction)
            self.assert_no_claim()
            self.assertEqual(0, self.count("launch_receipts"))
        self.assertEqual("claimed", self.claim()["state"])

    def test_precommit_hardening_failure_rolls_back_receipt_marker_and_intent(self):
        harden, observed = self.fixture.fixture.state.harden_database_files, []

        def refused():
            if self.ledger._connection.in_transaction and self.count("launch_receipts"):
                observed.append((self.count("launch_claims"), len(self.fixture.intents())))
                raise StateError("fixture precommit refusal")
            harden()

        with mock.patch.object(self.fixture.fixture.state, "harden_database_files", side_effect=refused), \
                self.assertRaisesRegex(StateError, "fixture precommit refusal"):
            self.claim()
        self.assertEqual([(1, 1)], observed)
        self.assert_no_claim()
        self.assertEqual(0, self.count("launch_receipts"))

    def test_another_projects_restriction_during_signature_work_is_committed_as_denial(self):
        with self.ledger.repository(self.project) as repository:
            record = deepcopy(repository.records("provider_billing")[-1])
        record = {key: value for key, value in record.items() if not key.startswith("_")}
        record.pop("record_id")
        record.update(spend_control_reached=True, observed_at_ms=self.stamp + 1)
        record = {**record, "record_id": "provider_billing:" + content_digest(record)}
        root = Path(self.fixture.fixture.temporary.name) / "other-project"
        root.mkdir()
        project = self.ledger.register_project(root)
        verify = OwnerGrantAuthority.verify_owner_record

        def changed(authority, *arguments):
            value = verify(authority, *arguments)
            self.assertFalse(self.ledger._connection.in_transaction)
            self.ledger._store_provider_observations(project, [record], timestamp=self.stamp + 1)
            return value

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=changed):
            receipt = self.claim()
        self.assertEqual(("denied", "codex_spend_control_reached"), (receipt["state"], receipt["reason_code"]))
        self.assert_no_claim()
        self.assertTrue(self.claim()["known"])

    def test_changed_owner_generation_during_signature_work_refuses_without_partial_writes(self):
        verify = OwnerGrantAuthority.verify_owner_record

        def changed(authority, *arguments):
            value = verify(authority, *arguments)
            with self.ledger._account_transaction():
                self.ledger._connection.execute("INSERT INTO owner_key_history(signer_line,reason,recorded_at_ms) VALUES(?,'replaced',?)",
                                                (self.fixture.fixture.state.allowed_signers_path.read_bytes(), self.stamp))
            return value

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=changed), \
                self.assertRaisesRegex(LedgerError, "preparation is stale"):
            self.claim()
        self.assert_no_claim()
        self.assertEqual(0, self.count("launch_receipts"))

    def test_failed_fresh_observation_invalidates_its_previous_binding(self):
        with mock.patch("codex_agents.providers.observe_codex", side_effect=RuntimeError("fixture observation failed")), \
                self.assertRaisesRegex(RuntimeError, "fixture observation failed"):
            self.observe()
        self.assertEqual(0, self.count("launch_observations"))
        self.assertEqual("claim_observation_missing", self.claim()["reason_code"])
        self.assert_no_claim()

    def test_default_denial_hook_owner_and_missing_scope_do_no_claim_work(self):
        default = AccountService(COLLECTOR_RUNTIME_DIGEST, self.views)
        self.addCleanup(default.close)
        with mock.patch.object(self.views, "claim_launch", side_effect=AssertionError("unauthorized backend work")):
            for service in (default, self.make_service(source="hook"), self.make_service(source="owner"), self.make_service(scope=None)):
                with self.subTest(service=service), self.assertRaisesRegex(AccountError, "account_not_authorized"):
                    self.call(service=service)
        self.assert_no_claim()

    def test_closed_payload_cannot_choose_authority_deadline_sql_or_process(self):
        for key, value in (("role", "host"), ("hard_timeout_seconds", 1), ("sql", "SELECT 1"),
                           ("pid", 123), ("environment", {}), ("executable", "/fixture/client")):
            with self.subTest(key=key), self.assertRaisesRegex(AccountError, "account_invalid_request"):
                self.request(payload={**self.launch.payload(), key: value})
        self.assert_no_claim()

    def test_client_makes_one_exchange_and_never_retries_after_lost_reply(self):
        client = AccountClient(self.fixture.fixture.state.socket, self.project, COLLECTOR_RUNTIME_DIGEST)
        with mock.patch.object(account_api, "exchange_frame", side_effect=ServiceTransportError("service_unavailable")) as exchange, \
                self.assertRaisesRegex(AccountError, "account_unavailable"):
            client.claim_launch(self.handle, self.launch.identity_digest, request_id="lost-request")
        self.assertEqual(1, exchange.call_count)
        self.assert_no_claim()

    def test_tampered_receipt_cannot_be_reinterpreted_as_a_known_claim(self):
        self.claim()
        with self.ledger._account_transaction():
            self.ledger._connection.execute("UPDATE launch_receipts SET receipt_digest=?", ("f" * 64,))
        with self.assertRaisesRegex(ClaimError, "claim_unavailable"):
            self.claim()
        self.assertEqual(1, self.count("launch_claims"))

    def test_restore_rejects_a_claimed_receipt_without_its_consumed_marker(self):
        self.claim()
        path = self.ledger.create_sanitized_backup(self.project, file_id="claim_backup", expires_at_ms=self.stamp + 60_000)
        body = backup_body(path, self.project)
        self.assertEqual(1, learning_store._verify_project_backup_body(body)["launch_receipts"])
        body["tables"]["launch_claims"] = []
        body["counts"]["launch_claims"] = 0
        with self.assertRaisesRegex(LedgerError, "restoration rejected|foreign-key"):
            learning_store._verify_project_backup_body(body)

    def test_consumed_marker_cannot_be_deleted_while_its_claimed_receipt_survives(self):
        self.claim()
        with self.assertRaises(sqlite3.IntegrityError), self.ledger._account_transaction():
            self.ledger._connection.execute("DELETE FROM launch_claims")
        self.assertEqual((1, 1), (self.count("launch_claims"), self.count("launch_receipts")))

    def test_project_deletion_preserves_unreconciled_capacity_and_receipts(self):
        self.claim()
        self.stamp += DEFAULT_RETENTION_MS
        with self.assertRaisesRegex(LedgerError, "project_has_active_launch_capacity"):
            self.ledger.delete_project(self.project)
        self.assertEqual((1, 1, 1, 1), tuple(self.count(table) for table in
                         ("launch_claims", "launch_receipts", "launch_inputs", "launch_observations")))

    def test_unknown_run_denial_is_durable_and_changed_retry_conflicts(self):
        missing = LaunchClaimInput("hrn_" + "f" * 48, "e" * 64)
        first = self.claim(launch=missing)
        self.assertEqual(("denied", "claim_not_open", None), (first["state"], first["reason_code"], first["run_id"]))
        self.assertEqual({**first, "known": True}, self.claim(launch=missing))
        with self.assertRaisesRegex(ClaimError, "claim_conflict"):
            self.claim()
        self.assert_no_claim()

    def test_another_project_cannot_claim_this_projects_handle(self):
        root = Path(self.fixture.fixture.temporary.name) / "wrong-project"
        root.mkdir()
        project = self.ledger.register_project(root)
        denied = self.ledger.claim_launch(project, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                                           caller_scope="a" * 64, request_id="wrong-project", launch=self.launch)
        self.assertEqual("claim_not_open", denied["reason_code"])
        self.assert_no_claim()
        self.assertEqual("claimed", self.claim()["state"])

    def test_caller_transaction_refuses_before_signature_work_without_committing_it(self):
        with self.ledger._account_transaction():
            with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("crypto in outer transaction")), \
                    self.assertRaisesRegex(LedgerError, "write transaction is already active"):
                self.claim()
            self.assertTrue(self.ledger._connection.in_transaction)
        self.assert_no_claim()

    def test_weakened_consumption_schema_is_refused_on_reopen(self):
        with self.ledger._account_transaction():
            self.ledger._connection.execute("DROP TABLE launch_claims")
            self.ledger._connection.execute(learning_store._LAUNCH_CLAIMS_SCHEMA.replace("    UNIQUE(project_id,run_id),\n", ""))
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "launch claim state is unavailable or incompatible"):
            LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients)

    def legacy_seven(self):
        first = self.claim()
        orchestration = self.ledger.open_orchestration(self.project, client_version="codex-host-v1", now_ms=self.stamp)
        pending = self.ledger.open_host_run(
            self.project, assignment=self.fixture.assignment, task=self.fixture.fixture.task(), route=self.fixture.route,
            execution_configuration=self.fixture.route["execution_configuration"], client_version="bridge-v1",
            orchestration_handle=orchestration["orchestration_handle"], now_ms=self.stamp,
        )
        with self.ledger._account_transaction():
            fixture_module.drop_active_capacity_for_legacy_fixture(self.ledger)
            self.ledger._connection.execute("DROP TRIGGER cleanup_launch_operation_state")
            for table in ("launch_receipts", "launch_inputs", "launch_observations"):
                self.ledger._connection.execute("DROP TABLE " + table)
            self.ledger._connection.execute("DROP INDEX launch_receipt_parent")
            self.ledger._connection.execute("PRAGMA user_version=7")
        return first, pending

    def backup_seven(self):
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.project, ledger_schema_version=7)
        self.assertNotIn("launch_receipts", body["tables"])
        raw = learning_store.canonical_json({"backup_sha256": learning_store.hashlib.sha256(learning_store.canonical_json(body)).hexdigest(),
                                             "body": body})
        path = self.ledger._write_owner_file("backup", self.project, "schema7_backup", raw)
        self.ledger._register_stored_file(self.project, file_id="schema7_backup", kind="backup", path=path,
                                          digest=learning_store.hashlib.sha256(raw).hexdigest(), expires_at_ms=self.stamp + 60_000)

    def test_verified_seven_upgrade_preserves_claims_and_fences_other_old_runs(self):
        first, _ = self.legacy_seven()
        before = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid")]
        signed = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations")]
        self.backup_seven()
        self.ledger.close()
        self.ledger = LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.ledger.close)
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])
        markers = [tuple(row) for row in self.ledger._connection.execute("SELECT kind,bridge_run_id FROM launch_claims ORDER BY kind")]
        self.assertEqual([("claimed", first["bridge_run_id"]), ("legacy_fenced", None)], markers)
        self.assertEqual(before, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid")])
        self.assertEqual(signed, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations")])
        self.assertEqual("claim_already_consumed", self.claim("after-upgrade")["reason_code"])

    def test_seven_upgrade_without_backup_refuses_without_creating_operation_tables(self):
        self.legacy_seven()
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "registered verified schema 7"):
            LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients, allow_schema_upgrade=True)
        connection = sqlite3.connect(self.fixture.fixture.state.database)
        try:
            self.assertEqual(7, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='launch_receipts'").fetchone())
        finally:
            connection.close()

    def test_interrupted_seven_upgrade_rolls_back_fencing_and_resumes_from_same_backup(self):
        class FixtureStop(BaseException):
            pass

        first, _ = self.legacy_seven()
        self.backup_seven()
        self.ledger.close()
        initialize = LearningLedger._initialize_launch_operations_locked

        def interrupted(ledger):
            initialize(ledger)
            raise FixtureStop()

        with mock.patch.object(LearningLedger, "_initialize_launch_operations_locked", autospec=True, side_effect=interrupted), \
                self.assertRaises(FixtureStop):
            LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients, allow_schema_upgrade=True)
        connection = sqlite3.connect(self.fixture.fixture.state.database)
        try:
            self.assertEqual(7, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual([("claimed", first["bridge_run_id"])], connection.execute("SELECT kind,bridge_run_id FROM launch_claims").fetchall())
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='launch_receipts'").fetchone())
        finally:
            connection.close()
        self.ledger = LearningLedger(self.fixture.fixture.state, client_capabilities=self.fixture.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.ledger.close)
        self.assertEqual(2, self.count("launch_claims"))


if __name__ == "__main__":
    unittest.main()
