"""Account-owner launch consumption; fake providers, no child execution."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
import unittest
from unittest import mock

import tests.test_bridge as bridge_fixture

from codex_agents import learning_store
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import DEFAULT_RETENTION_MS, LearningLedger, LedgerError, StateError
from tests.learning_support import backup_body


def drop_admission_evidence_for_legacy_fixture(connection):
    """Leave a ledger as schema 13 wrote it: without the admission evidence index (derived data)."""
    connection.execute("DROP TRIGGER IF EXISTS admission_evidence_insert")
    connection.execute("DROP TABLE IF EXISTS admission_evidence")


def drop_account_observations_for_legacy_fixture(ledger):
    drop_admission_evidence_for_legacy_fixture(ledger._connection)
    for table in ('account_billing_probe_requests', 'account_billing_probe_origins',
                  'account_observation_requests', 'account_observation_origins'):
        if ledger._connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]:
            raise AssertionError('cannot label new observation authority as a historical fixture')
        ledger._connection.execute('DROP TABLE ' + table)


def drop_account_cleanup_for_legacy_fixture(ledger):
    drop_account_observations_for_legacy_fixture(ledger)
    if ledger._connection.execute('SELECT COUNT(*) FROM account_cleanup_receipts').fetchone()[0]:
        raise AssertionError('cannot label new cleanup authority as a historical fixture')
    ledger._connection.execute('ALTER TABLE capacity_executions DROP COLUMN cleanup_reservation_digest')
    ledger._connection.execute('DROP TABLE account_cleanup_receipts')


def drop_original_open_for_legacy_fixture(ledger):
    # Remove the newer original-open layout when constructing an exact old
    # fixture; a version label alone must not make newer authority look legacy.
    drop_account_cleanup_for_legacy_fixture(ledger)
    ledger._connection.execute('DROP TABLE account_open_requests')
    ledger._connection.execute('DROP TABLE account_open_receipts')
    ledger._connection.execute('ALTER TABLE host_run_bindings DROP COLUMN history_owner')


def drop_active_capacity_for_legacy_fixture(ledger):
    drop_original_open_for_legacy_fixture(ledger)
    ledger._connection.execute("DROP TRIGGER preserve_active_capacity_on_project_deletion")
    ledger._connection.execute("DROP TABLE capacity_completions")
    ledger._connection.execute("DROP TABLE capacity_executions")
    ledger._connection.execute("DROP TABLE active_run_capacity")
    ledger._connection.execute("DROP INDEX active_capacity_parent")


def downgrade_ledger_to_six(ledger):
    """Leave a ledger as release 0.2.x (schema 6) wrote it."""
    with ledger._account_transaction():
        drop_active_capacity_for_legacy_fixture(ledger)
        ledger._connection.execute("DROP TRIGGER cleanup_launch_operation_state")
        for name in ("launch_inputs", "launch_observations", "launch_receipts"):
            ledger._connection.execute("DROP TABLE " + name)
        ledger._connection.execute("DROP TABLE IF EXISTS launch_claims")
        ledger._connection.execute("PRAGMA user_version=6")


class AccountLaunchClaimsTests(unittest.TestCase):
    def setUp(self):
        self.fixture = bridge_fixture.BridgeTestCase()
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.ledger, self.project = self.fixture.ledger, self.fixture.project_id
        self.stamp = time.time_ns() // 1_000_000
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.stamp - 120_000):
            self.fixture.admit("codex")
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.stamp):
            self.ledger.observe_provider(self.project, "codex", project_root=self.fixture.tools, environ=bridge_fixture.CLEAN)
            self.route = self.fixture.bridged_route("codex")
            self.assignment = self.fixture.assignment_for(self.route)
            orchestration = self.ledger.open_orchestration(self.project, client_version="codex-host-v1", now_ms=self.stamp)
            opened = self.ledger.open_host_run(
                self.project, assignment=self.assignment, task=self.fixture.task(), route=self.route,
                execution_configuration=self.route["execution_configuration"], client_version="bridge-v1",
                orchestration_handle=orchestration["orchestration_handle"], now_ms=self.stamp,
            )
        self.arguments = {"run_handle": opened["run_handle"], "assignment": self.assignment,
                          "task": self.fixture.task(), "route": self.route, "deadline_seconds": 600}

    def claim(self, **overrides):
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.stamp - 60_000):
            return self.ledger.record_dispatch_intent(self.project, **{**self.arguments, **overrides})

    def markers(self):
        return [tuple(row) for row in self.ledger._connection.execute(
            "SELECT handle_digest,run_id,kind,claimed_at_ms,bridge_run_id FROM launch_claims WHERE project_id=?",
            (self.project,),
        ).fetchall()]

    def intents(self):
        with self.ledger.repository(self.project) as repository:
            return repository.records("dispatch_intent")

    def assert_expiry_cannot_reissue(self, bind_first):
        first = self.claim()
        if bind_first:
            self.assertTrue(self.ledger.bind_host_agent(
                self.project, run_handle=self.arguments["run_handle"], agent_id=first["bridge_run_id"],
            )["bound"])
        self.assertGreater(self.ledger.expire(self.project, now_ms=self.stamp + DEFAULT_RETENTION_MS - 30_000), 0)
        self.assertEqual([], self.intents())
        for claim in (False, True):
            with self.subTest(claim=claim), self.assertRaisesRegex(LedgerError, "already dispatched"):
                self.claim(claim=claim)
        self.assertEqual(1, len(self.markers()))
        self.assertEqual(first["bridge_run_id"], self.markers()[0][4])

    def test_normal_expiry_and_clock_rollback_cannot_reissue_unbound_claim(self):
        self.assert_expiry_cannot_reissue(False)

    def test_normal_expiry_and_clock_rollback_cannot_reissue_bound_claim(self):
        self.assert_expiry_cannot_reissue(True)

    def test_concurrent_same_run_claims_consume_exactly_once(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim():
            try:
                barrier.wait(5)
                results.append(self.ledger.record_dispatch_intent(self.project, **self.arguments))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(results))
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], LedgerError)
        self.assertIn("already dispatched", str(errors[0]))
        self.assertEqual(1, len(self.markers()))
        self.assertEqual(1, len(self.intents()))

    def test_restart_after_lost_response_does_not_reissue_claim(self):
        self.claim()  # The caller may lose this first return value.
        before = self.markers()
        self.ledger.close()
        self.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.addCleanup(self.ledger.close)
        with self.assertRaisesRegex(LedgerError, "already dispatched"):
            self.claim()
        self.assertEqual(before, self.markers())

    def test_postappend_interruption_rolls_back_intent_and_consumption(self):
        class FixtureStop(BaseException):
            pass

        append = self.ledger._append_checked_locked

        def interrupted(project, record, **arguments):
            append(project, record, **arguments)
            if record["record_type"] == "dispatch_intent":
                self.assertEqual(1, len(self.markers()))
                raise FixtureStop()

        with mock.patch.object(self.ledger, "_append_checked_locked", side_effect=interrupted), self.assertRaises(FixtureStop):
            self.claim()
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual([], self.intents())
        self.assertEqual([], self.markers())
        self.claim()
        self.assertEqual(1, len(self.markers()))

    def test_precommit_mode_refusal_rolls_back_intent_and_consumption(self):
        harden = self.fixture.state.harden_database_files
        observed = []

        def refused():
            if self.ledger._connection.in_transaction:
                observed.append((len(self.markers()), len(self.intents())))
                raise StateError("fixture launch precommit refusal")
            harden()

        with mock.patch.object(self.fixture.state, "harden_database_files", side_effect=refused), \
                self.assertRaisesRegex(StateError, "fixture launch precommit refusal"):
            self.claim()
        self.assertEqual([(1, 1)], observed)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual([], self.intents())
        self.assertEqual([], self.markers())

    def test_claims_are_included_in_verified_project_backup(self):
        self.claim()
        backup = self.ledger.create_sanitized_backup(
            self.project, file_id="claim_fixture_backup", expires_at_ms=self.stamp + 60_000,
        )
        checked = self.ledger.verify_sanitized_backup(self.project, file_id="claim_fixture_backup")
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, checked["ledger_schema_version"])
        self.assertEqual(1, checked["counts"]["launch_claims"])
        body = backup_body(backup, self.project)
        row = body["tables"]["launch_claims"][0]
        body["tables"]["launch_claims"].append(dict(row))
        body["counts"]["launch_claims"] = 2
        with self.assertRaisesRegex(LedgerError, "restoration rejected"):
            learning_store._verify_project_backup_body(body)

    def test_explicit_project_deletion_cannot_erase_unreconciled_capacity(self):
        self.claim()
        with mock.patch.object(self.ledger, "_clock_ms", return_value=self.stamp + DEFAULT_RETENTION_MS), \
                self.assertRaisesRegex(LedgerError, "project_has_active_launch_capacity"):
            self.ledger.delete_project(self.project)
        self.assertEqual(1, len(self.markers()))

    def downgrade_to_six(self):
        downgrade_ledger_to_six(self.ledger)

    def backup_six(self):
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.project, ledger_schema_version=6)
        self.assertNotIn("launch_claims", body["tables"])
        self.assertNotIn("launch_claims", body["columns"])
        self.assertNotIn("launch_claims", body["counts"])
        raw = canonical_json({"backup_sha256": hashlib.sha256(canonical_json(body)).hexdigest(), "body": body})
        path = self.ledger._write_owner_file("backup", self.project, "schema6_fixture_backup", raw)
        self.ledger._register_stored_file(
            self.project, file_id="schema6_fixture_backup", kind="backup", path=path,
            digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=self.stamp + 60_000,
        )

    def assert_six_unmodified(self):
        connection = sqlite3.connect(self.fixture.state.database)
        try:
            self.assertEqual(6, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='launch_claims'").fetchone())
        finally:
            connection.close()

    def test_schema_six_without_backup_is_refused_before_mutation(self):
        self.downgrade_to_six()
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "registered verified schema 6"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.assert_six_unmodified()

    def test_schema_six_stale_backup_is_refused_before_mutation(self):
        self.downgrade_to_six()
        self.backup_six()
        with self.ledger._account_transaction():
            self.ledger._connection.execute("INSERT INTO collector_health VALUES(?,'fixture-after-backup',1,?)", (self.project, self.stamp))
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "registered verified schema 6"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.assert_six_unmodified()

    def test_verified_schema_six_migration_fences_ambiguous_pending_run_without_intent(self):
        self.downgrade_to_six()
        self.assertEqual([], self.intents())
        signed_before = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations").fetchall()]
        records_before = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid").fetchall()]
        self.backup_six()
        self.ledger.close()
        self.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.ledger.close)
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual("legacy_fenced", self.markers()[0][2])
        self.assertEqual(signed_before, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations").fetchall()])
        self.assertEqual(records_before, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid").fetchall()])
        with self.assertRaisesRegex(LedgerError, "already dispatched"):
            self.claim()

    def test_failed_schema_six_migration_rolls_back_and_can_resume(self):
        self.downgrade_to_six()
        self.backup_six()
        self.ledger.close()
        initialize = LearningLedger._initialize_launch_claims_locked

        def fail(ledger, **arguments):
            initialize(ledger, **arguments)
            raise RuntimeError("fixture migration failure")

        with mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True, side_effect=fail), \
                self.assertRaisesRegex(RuntimeError, "fixture migration failure"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.assert_six_unmodified()
        self.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.ledger.close)
        self.assertEqual("legacy_fenced", self.markers()[0][2])

    def test_the_owners_approved_upgrade_keeps_checked_copies_first(self):
        self.downgrade_to_six()
        records_before = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid").fetchall()]
        self.ledger.close()
        state = self.fixture.state
        self.assertEqual(6, learning_store.ledger_schema_behind(state))
        for options in ({"backup_before_upgrade": True}, {"backup_before_upgrade": 1, "allow_schema_upgrade": True}):
            with self.subTest(options=options), self.assertRaisesRegex(LedgerError, "upgrade option is invalid"):
                LearningLedger(state, client_capabilities=self.fixture.clients, **options)
        self.assert_six_unmodified()
        # The whole ledger first: sound, the same schema, the same rows.
        copy = learning_store.copy_ledger_before_upgrade(state, 6)
        self.assertEqual((state.root / "ledger-before-upgrade", 0o600, 0o700), (
            copy.parent, copy.stat().st_mode & 0o777, copy.parent.stat().st_mode & 0o777))
        copied = sqlite3.connect(copy)
        try:
            self.assertEqual(6, copied.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(records_before, copied.execute("SELECT * FROM records ORDER BY rowid").fetchall())
        finally:
            copied.close()
        with mock.patch.object(learning_store.sqlite3, "connect", side_effect=sqlite3.OperationalError("disk")), \
                self.assertRaisesRegex(LedgerError, "nothing was changed"):
            learning_store.copy_ledger_before_upgrade(state, 6)
        with self.assertRaisesRegex(LedgerError, "nothing was changed"):
            learning_store.copy_ledger_before_upgrade(state, 7)  # Not the schema it was told to expect.
        self.assertEqual([copy], sorted(copy.parent.iterdir()))
        self.assert_six_unmodified()
        # Then each project's records, sealed and registered, which the migration checks like any other.
        self.ledger = LearningLedger(state, client_capabilities=self.fixture.clients,
                                     allow_schema_upgrade=True, backup_before_upgrade=True)
        self.addCleanup(self.ledger.close)
        self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertIsNone(learning_store.ledger_schema_behind(state))
        self.assertEqual(records_before, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid").fetchall()])
        kept = self.ledger._connection.execute(
            "SELECT project_id,file_id,expires_at_ms FROM stored_files WHERE kind='backup' AND file_id LIKE 'upgrade_schema6_%'").fetchall()
        self.assertEqual({self.project}, {row["project_id"] for row in kept} & {self.project})
        active = [row[0] for row in self.ledger._connection.execute("SELECT project_id FROM projects WHERE status='active'")]
        self.assertEqual(sorted(active), sorted(row["project_id"] for row in kept))
        for row in kept:
            body = backup_body(state.backups / row["project_id"] / (row["file_id"] + ".sqlite3"), row["project_id"])
            self.assertEqual(6, body["ledger_schema_version"])
            self.assertGreater(row["expires_at_ms"], time.time_ns() // 1_000_000 + 29 * 24 * 60 * 60 * 1000)

    def test_a_failed_approved_upgrade_leaves_the_earlier_records_and_can_be_run_again(self):
        self.downgrade_to_six()
        self.ledger.close()
        initialize = LearningLedger._initialize_launch_claims_locked

        def fail(ledger, **arguments):
            initialize(ledger, **arguments)
            raise RuntimeError("fixture migration failure")

        options = dict(client_capabilities=self.fixture.clients, allow_schema_upgrade=True, backup_before_upgrade=True)
        with mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True, side_effect=fail), \
                self.assertRaisesRegex(RuntimeError, "fixture migration failure"):
            LearningLedger(self.fixture.state, **options)
        self.assert_six_unmodified()
        self.assertEqual(6, learning_store.ledger_schema_behind(self.fixture.state))
        self.ledger = LearningLedger(self.fixture.state, **options)
        self.addCleanup(self.ledger.close)
        self.assertEqual("legacy_fenced", self.markers()[0][2])

    def test_schema_six_runtime_refuses_claim_schema(self):
        self.ledger.close()
        with mock.patch.object(learning_store, "LEDGER_SCHEMA_VERSION", 6), \
                self.assertRaisesRegex(LedgerError, "newer than this package"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)

    def test_missing_schema_seven_consumption_is_not_recreated_as_empty(self):
        self.claim()
        with self.ledger._account_transaction():
            drop_active_capacity_for_legacy_fixture(self.ledger)
            self.ledger._connection.execute("DROP TABLE launch_claims")
            self.ledger._connection.execute('PRAGMA user_version=7')
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "launch claim state is unavailable"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)


if __name__ == "__main__":
    unittest.main()
