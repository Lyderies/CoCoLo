"""Durable owner capacity with real SQLite and synthetic provider evidence."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import sqlite3
import threading
import unittest
from unittest import mock

import tests.test_account_claim_operation as fixtures
import tests.test_account_launch_claims as launch_fixtures
from codex_agents import learning_store
from codex_agents.account_claim_contracts import ClaimError, LaunchClaimInput
from codex_agents.active_run_capacity import ActiveRunLimits
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger, LedgerError, DEFAULT_RETENTION_MS
from tests.learning_support import backup_body


class ActiveRunCapacityTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AccountClaimOperationTests("test_handler_claims_with_owner_deadline_and_returns_known_state")
        self.case.setUp()
        self.addCleanup(lambda: self.assertTrue(self.case.doCleanups()))
        self.fixture = self.case.fixture.fixture
        self.project = self.case.project
        self.sequence = 0

    @property
    def ledger(self):
        return self.case.ledger

    def rows(self):
        return [dict(row) for row in self.ledger._connection.execute("SELECT * FROM active_run_capacity ORDER BY project_id,handle_digest")]

    def new_attempt(self, project=None):
        project = project or self.project
        self.sequence += 1
        assignment = deepcopy(self.case.fixture.assignment)
        assignment["assignment_id"] = "capacity-fixture-" + str(self.sequence)
        route = self.case.fixture.route
        orchestration = self.ledger.open_orchestration(project, client_version="codex-host-v1", now_ms=self.case.stamp)
        opened = self.ledger.open_host_run(
            project, assignment=assignment, task=self.fixture.task(), route=route,
            execution_configuration=route["execution_configuration"], client_version="bridge-v1",
            orchestration_handle=orchestration["orchestration_handle"], now_ms=self.case.stamp,
        )
        handle = opened["run_handle"]
        identity = self.ledger.observe_provider(project, "codex", project_root=self.fixture.tools,
                                                environ=launch_fixtures.bridge_fixture.CLEAN,
                                                run_handle=handle, phase="before")[0]
        return project, LaunchClaimInput(handle, content_digest(identity))

    def claim(self, attempt, name):
        project, launch = attempt
        return self.ledger.claim_launch(project, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                                        caller_scope="a" * 64, request_id=name, launch=launch)

    def second_project(self):
        root = self.fixture.project.parent / "other-capacity-project"
        root.mkdir()
        project = self.ledger.register_project(root)
        with mock.patch.object(self.fixture, "project_id", project):
            self.fixture.admit("codex")
        return project

    def reopen(self):
        self.ledger.close()
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.case.ledger.close)

    def test_C8_projects_share_six_slots_and_the_seventh_refuses_without_receipt(self):
        # 0.3.3 requirement C8 (owner: "2 is too restrictive"): six children at once, shared by every
        # project; the seventh is refused claim_busy, with no receipt, as the third was.
        first = self.case.claim()
        second = self.claim(self.new_attempt(self.second_project()), "second-project")
        others = [self.claim(self.new_attempt(), f"more-{index}") for index in range(4)]
        with self.assertRaisesRegex(ClaimError, "claim_busy"):
            self.claim(self.new_attempt(), "seventh")
        self.assertEqual(["claimed"] * 6, [item["state"] for item in (first, second, *others)])
        self.assertEqual(6, len(self.rows()))
        self.assertEqual(6, self.ledger._connection.execute("SELECT COUNT(*) FROM launch_receipts").fetchone()[0])
        self.assertEqual({**first, "known": True}, self.case.claim())
        self.assertEqual(1, len({row["account_fingerprint"] for row in self.rows()}))
        self.assertEqual(2, len({row["project_id"] for row in self.rows()}))

    def test_last_slot_race_has_one_winner_and_no_partial_loser(self):
        self.case.claim()
        for index in range(4):
            self.claim(self.new_attempt(), f"before-race-{index}")
        attempts = [self.new_attempt(), self.new_attempt()]
        barrier = threading.Barrier(2)
        results, errors = [], []

        def run(index):
            try:
                barrier.wait(5)
                results.append(self.claim(attempts[index], "race-" + str(index)))
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(results))
        self.assertEqual([ClaimError], [type(error) for error in errors])
        self.assertEqual("claim_busy", errors[0].reason_code)
        self.assertEqual(6, len(self.rows()))
        for table in ("launch_claims", "launch_receipts"):
            self.assertEqual(6, self.ledger._connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])

    def test_explicit_larger_installed_fixture_policy_still_counts_every_claim(self):
        # Larger than the installed six (C8), so its claims fill past what the installed limit allows.
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(8, 8)):
            self.case.claim()
            for number in range(7):
                self.assertEqual("claimed", self.claim(self.new_attempt(), "larger-" + str(number))["state"])
            with self.assertRaisesRegex(ClaimError, "claim_busy"):
                self.claim(self.new_attempt(), "larger-full")
        self.assertEqual(8, len(self.rows()))
        self.assertTrue(all(row["released_at_ms"] is None for row in self.rows()))
        with self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, "claim_busy"):
            self.ledger._check_active_capacity_locked("codex", self.rows()[0]["account_fingerprint"])

    def test_account_limit_is_separate_from_machine_limit_and_unknowns_count(self):
        self.case.claim()
        account = self.rows()[0]["account_fingerprint"]
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(3, 1)):
            with self.ledger._account_transaction():
                with self.assertRaisesRegex(ClaimError, "claim_busy"):
                    self.ledger._check_active_capacity_locked("codex", account)
                self.ledger._check_active_capacity_locked("codex", "f" * 64)
                self.ledger._check_active_capacity_locked("claude", account)
                with self.assertRaisesRegex(ClaimError, "claim_busy"):
                    self.ledger._check_active_capacity_locked("codex", None)
            self.ledger._connection.execute("UPDATE active_run_capacity SET account_fingerprint=NULL")
            with self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, "claim_busy"):
                self.ledger._check_active_capacity_locked("codex", "f" * 64)

    def test_H1a_3_slot_health_counts_a_slot_past_its_deadline_by_the_two_clock_rule(self):
        self.case.claim()
        row = self.rows()[0]
        grace = learning_store.CRASHED_CLAIM_GRACE_MS
        ended = row["execution_deadline_ns"] + 1
        for wall, monotonic, past in (
                (row["deadline_at_ms"] - 1, row["clock_reference_ns"] + 1, 0),       # before it
                (row["deadline_at_ms"], ended, 0),                                     # exactly at it
                (row["deadline_at_ms"] + 1, row["execution_deadline_ns"] - 1, 0),      # wall clock only
                (row["deadline_at_ms"] + 1, ended, 1),                                 # both clocks past
                (row["deadline_at_ms"] + 1, row["clock_reference_ns"] - 1, 1),         # rebooted
                (row["deadline_at_ms"] + grace + 1, ended + grace * 1_000_000, 0)):    # crashed: not counted
            with self.subTest(wall=wall, monotonic=monotonic), \
                    mock.patch.object(learning_store.time, "time_ns", return_value=wall * 1_000_000), \
                    mock.patch.object(learning_store.time, "monotonic_ns", return_value=monotonic):
                health = self.ledger.account_slot_health(self.project)
                self.assertEqual(past, health["runs_past_deadline"], health)
        self.assertIsNone(self.rows()[0]["released_at_ms"])  # reading released nothing

    def test_H1c_2_machine_idle_names_each_project_with_a_counted_slot(self):
        self.assertEqual({'run_slots': [], 'probes': []}, self.ledger.machine_idle())
        self.case.claim()
        self.assertEqual({'run_slots': [self.project], 'probes': []}, self.ledger.machine_idle())
        row = self.rows()[0]
        crashed = (row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1) * 1_000_000
        ended = row["execution_deadline_ns"] + learning_store.CRASHED_CLAIM_GRACE_MS * 1_000_000 + 1
        with mock.patch.object(learning_store.time, "time_ns", return_value=crashed), \
                mock.patch.object(learning_store.time, "monotonic_ns", return_value=ended):
            self.assertEqual({'run_slots': [], 'probes': []}, self.ledger.machine_idle())  # no longer counted

    def test_an_unsettled_claim_long_past_its_hard_deadline_stops_counting_but_stays_unreleased(self):
        self.case.claim()
        row = self.rows()[0]
        account = row["account_fingerprint"]
        grace = learning_store.CRASHED_CLAIM_GRACE_MS
        limit = row["deadline_at_ms"] + grace
        # The child's own deadline is monotonic, so both clocks must be well past it.
        ended = row["execution_deadline_ns"] + grace * 1_000_000
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(1, 1)):
            for now, monotonic, busy in (
                    (row["deadline_at_ms"] + 1, ended, True), (limit, ended, True), (limit + 1, ended, False),
                    # The wall clock was set far forward while the child may still be running.
                    (limit + 10 * grace, row["execution_deadline_ns"] - 1, True),
                    (limit + 10 * grace, ended - 1, True),
                    # The monotonic clock restarted below the claim's reference: the machine
                    # rebooted, so no child of that claim is alive.
                    (limit + 1, row["clock_reference_ns"] - 1, False),
                    (row["deadline_at_ms"] + 1, row["clock_reference_ns"] - 1, True)):
                with self.subTest(now=now, monotonic=monotonic), \
                        mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                        mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic), \
                        self.ledger._account_transaction():
                    if busy:
                        with self.assertRaisesRegex(ClaimError, "claim_busy"):
                            self.ledger._check_active_capacity_locked("codex", account)
                    else:
                        self.ledger._check_active_capacity_locked("codex", account)
        # Nothing was released: the crashed run stays recorded as unconfirmed.
        self.assertEqual([row], self.rows())

    def test_history_expiry_clock_change_and_restart_keep_capacity_and_known_receipt(self):
        first = self.case.claim()
        rows = self.rows()
        self.ledger.expire(self.project, now_ms=self.case.stamp + DEFAULT_RETENTION_MS + 1)
        self.reopen()
        self.case.stamp -= 60_000
        self.assertEqual(rows, self.rows())
        with mock.patch.object(self.ledger, "_prepare_attestations", side_effect=AssertionError("known reply re-prepared")):
            self.assertEqual({**first, "known": True}, self.case.claim())

    def test_interrupted_intent_and_receipt_roll_back_the_reservation_too(self):
        class FixtureStop(BaseException):
            pass

        for method in ("_append_checked_locked", "_store_claim_receipt_locked"):
            original = getattr(self.ledger, method)

            def stop(*args, **kwargs):
                original(*args, **kwargs)
                self.assertEqual(1, len(self.rows()))
                raise FixtureStop()

            with mock.patch.object(self.ledger, method, side_effect=stop), self.assertRaises(FixtureStop):
                self.case.claim()
            self.assertEqual([], self.rows())
            self.case.assert_no_claim()
        self.assertEqual("claimed", self.case.claim()["state"])

    def test_deletion_cannot_lift_capacity_even_after_retention(self):
        self.case.claim()
        rows = self.rows()
        self.case.stamp += DEFAULT_RETENTION_MS
        with self.assertRaisesRegex(LedgerError, "project_has_active_launch_capacity"):
            self.ledger.delete_project(self.project)
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,))
        self.assertEqual(rows, self.rows())
        self.assertEqual("active", self.ledger._connection.execute("SELECT status FROM projects WHERE project_id=?", (self.project,)).fetchone()[0])

    def test_missing_durable_reservation_refuses_restart_without_recreating_it(self):
        self.case.claim()
        self.ledger._connection.execute("DELETE FROM active_run_capacity")
        self.ledger.close()
        with self.assertRaisesRegex(LedgerError, "active capacity coverage is unavailable"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        check = sqlite3.connect(self.fixture.state.database)
        try:
            self.assertEqual(0, check.execute("SELECT COUNT(*) FROM active_run_capacity").fetchone()[0])
            self.assertEqual(1, check.execute("SELECT COUNT(*) FROM launch_claims").fetchone()[0])
        finally:
            check.close()

    def test_verified_backup_retains_capacity_and_refuses_missing_or_cross_bound_rows(self):
        self.case.claim()
        backup = self.ledger.create_sanitized_backup(self.project, file_id="capacity_backup", expires_at_ms=self.case.stamp + 60_000)
        body = backup_body(backup, self.project)
        self.assertEqual(1, learning_store._verify_project_backup_body(body)["active_run_capacity"])
        for change in ("missing", "run", "bridge"):
            broken = deepcopy(body)
            if change == "missing":
                broken["tables"]["active_run_capacity"] = []
                broken["counts"]["active_run_capacity"] = 0
            else:
                broken["tables"]["active_run_capacity"][0]["run_id" if change == "run" else "bridge_run_id"] = (
                    "run:" + "f" * 64 if change == "run" else "brg_" + "f" * 32)
            with self.subTest(change=change), self.assertRaises(LedgerError):
                learning_store._verify_project_backup_body(broken)

    def schema_eight(self, *, backup):
        self.case.claim()
        with self.ledger._account_transaction():
            launch_fixtures.drop_active_capacity_for_legacy_fixture(self.ledger)
            self.ledger._connection.execute("PRAGMA user_version=8")
        if backup:
            body = learning_store._snapshot_project_partition(self.ledger._connection, self.project, ledger_schema_version=8)
            self.assertNotIn("active_run_capacity", body["tables"])
            raw = canonical_json({"backup_sha256": hashlib.sha256(canonical_json(body)).hexdigest(), "body": body})
            path = self.ledger._write_owner_file("backup", self.project, "schema8_capacity_backup", raw)
            self.ledger._register_stored_file(self.project, file_id="schema8_capacity_backup", kind="backup", path=path,
                                              digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=self.case.stamp + 60_000)
        self.ledger.close()

    def test_C8_an_earlier_versions_slot_is_held_until_it_ages_out_by_the_capacity_rule(self):
        from codex_agents.active_run_capacity import LEGACY_RUN_MAX_MS
        self.schema_eight(backup=True)
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.case.ledger.close)
        row = self.rows()[0]
        self.assertEqual(1, self.ledger.account_slot_health(self.project)['runs_held_without_deadline'])
        aged = (row['claimed_at_ms'] + LEGACY_RUN_MAX_MS + 1) * 1_000_000
        with mock.patch.object(learning_store.time, 'time_ns', return_value=aged):
            self.assertEqual(0, self.ledger.account_slot_health(self.project)['runs_held_without_deadline'])

    def test_schema_eight_requires_verified_backup_and_stays_unmodified_on_refusal(self):
        self.schema_eight(backup=False)
        with self.assertRaisesRegex(LedgerError, "registered verified schema 8"):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        check = sqlite3.connect(self.fixture.state.database)
        try:
            self.assertEqual(8, check.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(check.execute("SELECT 1 FROM sqlite_master WHERE name='active_run_capacity'").fetchone())
        finally:
            check.close()

    def test_schema_eight_backfills_unknown_capacity_and_preserves_signed_bytes(self):
        before = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid")]
        signatures = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations")]
        self.schema_eight(backup=True)
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.case.ledger.close)
        self.assertEqual(14, self.ledger._connection.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(["legacy_unknown"], [row["kind"] for row in self.rows()])
        self.assertTrue(all(self.rows()[0][field] is None for field in ("provider", "account_fingerprint", "released_at_ms")))
        after = [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM records ORDER BY rowid")]
        self.assertEqual(before, after[:len(before)])
        self.assertEqual(signatures, [tuple(row) for row in self.ledger._connection.execute("SELECT * FROM owner_attestations")])
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(3, 1)), \
                self.ledger._account_transaction(), self.assertRaisesRegex(ClaimError, "claim_busy"):
            self.ledger._check_active_capacity_locked("claude", "f" * 64)

    def test_schema_eight_interruption_rolls_back_table_and_version_then_reuses_backup(self):
        class FixtureStop(BaseException):
            pass

        self.schema_eight(backup=True)
        initialize = LearningLedger._initialize_active_capacity_locked

        def interrupted(ledger):
            initialize(ledger)
            raise FixtureStop()

        with mock.patch.object(LearningLedger, "_initialize_active_capacity_locked", autospec=True, side_effect=interrupted), \
                self.assertRaises(FixtureStop):
            LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        check = sqlite3.connect(self.fixture.state.database)
        try:
            self.assertEqual(8, check.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(check.execute("SELECT 1 FROM sqlite_master WHERE name='active_run_capacity'").fetchone())
            self.assertEqual(1, check.execute("SELECT COUNT(*) FROM launch_claims").fetchone()[0])
        finally:
            check.close()
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients, allow_schema_upgrade=True)
        self.addCleanup(self.case.ledger.close)
        self.assertEqual(["legacy_unknown"], [row["kind"] for row in self.rows()])


if __name__ == "__main__":
    unittest.main()
