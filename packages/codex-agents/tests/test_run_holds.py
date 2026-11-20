"""O0b run supervisor, stage RS-A: the register and the account's own holds (DESIGN-0.4.0-RS-A.md, RS-A-1 to
RS-A-8; written before the code, each fails first).

Integration stage: a real ledger with a claimed run (the test_active_run_capacity fixture); clocks are
patched, never slept.
"""
import hashlib
import sqlite3
import unittest
from unittest import mock

from tests.test_active_run_capacity import ActiveRunCapacityTests

from codex_agents import learning_store
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import LearningLedger, LedgerError
from codex_agents.account_claim_contracts import ClaimError
from codex_agents.active_run_capacity import ActiveRunLimits


class RunHoldTests(ActiveRunCapacityTests):
    def counted(self, account, now, monotonic):
        """Whether the machine's one slot is taken, as admission sees it."""
        with mock.patch.object(learning_store, "INSTALLED_ACTIVE_RUN_LIMITS", ActiveRunLimits(1, 1)), \
                mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic), \
                self.ledger._account_transaction():
            try:
                self.ledger._check_active_capacity_locked("codex", account)
            except ClaimError:
                return True
            return False

    def sweep(self, now, monotonic):
        with mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic):
            return self.ledger.release_due_account_holds(limit=64)

    def test_RS_A_3_a_slot_freed_by_a_reboot_never_counts_again_as_uptime_grows(self):
        """CR-1: today the reboot inference frees the slot only while uptime is below the claim's old
        reference; once uptime passes it, the slot counts again with no clock change. Written once, for good."""
        self.case.claim()
        row = self.rows()[0]
        account = row["account_fingerprint"]
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        rebooted = row["clock_reference_ns"] - 1           # uptime below the claim's reference
        later = row["clock_reference_ns"] + 1               # uptime grown past it, still before the deadline
        self.assertFalse(self.counted(account, now, rebooted))  # today's rule frees it
        self.assertEqual(1, self.sweep(now, rebooted))      # the supervisor writes the decision
        released = self.rows()[0]
        self.assertIsNotNone(released["released_at_ms"])
        # The kind is in the register row, written with the release (design: implementation decisions).
        self.assertEqual("crashed", self.ledger._connection.execute(
            "SELECT release_kind FROM run_holds WHERE kind='run_slot' AND identity=?",
            (released["handle_digest"],)).fetchone()[0])
        self.assertFalse(self.counted(account, now, later))  # and it never counts again

    def test_RS_A_3_a_live_slot_is_never_released_by_the_sweep(self):
        self.case.claim()
        row = self.rows()[0]
        self.assertEqual(0, self.sweep(row["deadline_at_ms"] - 1, row["clock_reference_ns"] + 1))
        self.assertIsNone(self.rows()[0]["released_at_ms"])
        self.assertTrue(self.counted(row["account_fingerprint"], row["deadline_at_ms"] - 1,
                                     row["clock_reference_ns"] + 1))

    def bindings(self):
        return [dict(row) for row in self.ledger._connection.execute(
            "SELECT handle_digest, binding_state, abandoned_reason, expires_at_ms FROM host_run_bindings")]

    def test_RS_A_2_an_expired_binding_is_released_by_the_sweep_with_its_reason(self):
        self.case.claim()
        binding = next(b for b in self.bindings() if b["binding_state"] == "pending")
        row = self.rows()[0]
        self.assertEqual(1, self.sweep(binding["expires_at_ms"] + 1, row["clock_reference_ns"] + 1))
        hold = dict(self.ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='binding'").fetchone())
        self.assertEqual({"state": "released", "release_kind": "expired"}, hold)
        with mock.patch.object(self.ledger, "_clock_ms", return_value=binding["expires_at_ms"] + 1):
            self.assertEqual(0, self.ledger.account_capacity(self.project)["pending_bindings"]["live"])

    def test_RS_A_F7_an_expired_claimed_binding_never_counts_again_after_a_clock_rollback(self):
        """Round 1 F7: the cap honours the written release, not only the wall clock."""
        self.case.claim()
        binding = next(b for b in self.bindings() if b["binding_state"] == "pending")
        row = self.rows()[0]
        self.assertEqual(1, self.sweep(binding["expires_at_ms"] + 1, row["clock_reference_ns"] + 1))
        with mock.patch.object(self.ledger, "_clock_ms", return_value=binding["expires_at_ms"] - 1):  # rolled back
            self.assertEqual(0, self.ledger.account_capacity(self.project)["pending_bindings"]["live"])
        self.assertEqual("pending", next(b for b in self.bindings()
                                         if b["handle_digest"] == binding["handle_digest"])["binding_state"])  # settlement still needs it

    def test_RS_A_F7_an_expired_unclaimed_binding_is_released_in_its_own_row_too(self):
        """Round 1 F7: abandoned `expired`, so a claim after a clock rollback is refused as not open."""
        project, launch = self.new_attempt()
        from codex_agents.learning_contracts import keyed_digest
        digest = keyed_digest(self.ledger.state.digest_key(), launch.run_handle)
        expires = self.ledger._connection.execute(
            "SELECT expires_at_ms FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone()[0]
        self.assertGreaterEqual(self.sweep(expires + 1, 1), 1)  # the fixture keeps its own binding too
        state = tuple(self.ledger._connection.execute(
            "SELECT binding_state, abandoned_reason FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone())
        self.assertEqual(("abandoned", "expired"), state)
        self.assertEqual(("released", "expired"), self.binding_hold(launch.run_handle))
        with mock.patch.object(self.ledger, "_clock_ms", return_value=expires - 1):
            denied = self.claim((project, launch), "after-expiry")
        self.assertEqual(("denied", "claim_not_open"), (denied["state"], denied["reason_code"]))

    def test_RS_A_5_an_identical_sweep_releases_nothing_twice(self):
        self.case.claim()
        row = self.rows()[0]
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        first = self.sweep(now, row["clock_reference_ns"] - 1)
        self.assertGreaterEqual(first, 1)
        self.assertEqual(0, self.sweep(now, row["clock_reference_ns"] - 1))
        self.assertEqual(1, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM run_holds WHERE kind='run_slot' AND state='released'").fetchone()[0])

    def test_RS_A_6_status_shows_each_kind_with_its_next_eligible_time(self):
        self.case.claim()
        row = self.rows()[0]
        status = self.ledger.account_holds_status(self.project)
        self.assertEqual(1, status["run_slot"]["held"])
        self.assertEqual(0, status["run_slot"]["past_due"])
        self.assertEqual(row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1,
                         status["run_slot"]["next_eligible"])
        self.assertIn("binding", status)
        self.assertEqual({"held": 0, "past_due": 0, "next_eligible": None}, status["probe"])

    def schema_fourteen(self, *, backup):
        """The ledger as 0.8.38 leaves it (schema 14): no register, no release kinds."""
        with self.ledger._account_transaction():
            from codex_agents.run_holds_schema import RUN_HOLDS_OBJECTS
            for kind, name, _ in RUN_HOLDS_OBJECTS:  # a 0.8.38 ledger has none of them
                if kind == "trigger":
                    self.ledger._connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            self.ledger._connection.execute("DROP TABLE IF EXISTS run_holds")
            columns = {row[1] for row in self.ledger._connection.execute("PRAGMA table_info(active_run_capacity)")}
            if "release_kind" in columns:
                self.ledger._connection.execute("ALTER TABLE active_run_capacity DROP COLUMN release_kind")
            self.ledger._connection.execute("PRAGMA user_version=14")
        if backup:
            body = learning_store._snapshot_project_partition(self.ledger._connection, self.project,
                                                              ledger_schema_version=14)
            raw = canonical_json({"backup_sha256": hashlib.sha256(canonical_json(body)).hexdigest(), "body": body})
            path = self.ledger._write_owner_file("backup", self.project, "schema14_holds_backup", raw)
            self.ledger._register_stored_file(self.project, file_id="schema14_holds_backup", kind="backup", path=path,
                                              digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=self.case.stamp + 60_000)
        self.ledger.close()

    def upgraded(self):
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients,
                                          allow_schema_upgrade=True)
        self.addCleanup(self.case.ledger.close)
        return self.case.ledger

    def test_RS_A_8_the_register_comes_with_a_new_ledger_version(self):
        self.case.claim()
        version = self.ledger._connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertGreater(version, 14)
        self.assertIsNotNone(self.ledger._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_holds'").fetchone())

    def test_RS_A_8_an_upgrade_without_a_verified_backup_is_refused_and_changes_nothing(self):
        self.case.claim()
        self.schema_fourteen(backup=False)
        with self.assertRaises(LedgerError):
            self.upgraded()
        check = sqlite3.connect(self.fixture.state.database)
        try:
            self.assertEqual(14, check.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(check.execute("SELECT 1 FROM sqlite_master WHERE name='run_holds'").fetchone())
        finally:
            check.close()

    def test_RS_A_8_existing_holds_are_adopted_by_their_evidence(self):
        self.case.claim()
        self.schema_fourteen(backup=True)
        ledger = self.upgraded()
        holds = sorted((row["kind"], row["state"]) for row in ledger._connection.execute(
            "SELECT kind, state FROM run_holds"))
        self.assertIn(("run_slot", "held"), holds)
        self.assertIn(("binding", "held"), holds)
        self.assertEqual(len(holds), len(set(
            (row["kind"], row["identity"]) for row in ledger._connection.execute("SELECT kind, identity FROM run_holds"))))

    def binding_hold(self, handle):
        from codex_agents.learning_contracts import keyed_digest
        digest = keyed_digest(self.ledger.state.digest_key(), handle)
        row = self.ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='binding' AND project_id=? AND identity=?",
            (self.project, digest)).fetchone()
        return None if row is None else tuple(row)

    def test_RS_A_2_a_never_claimed_binding_is_withdrawn_for_good(self):
        project, launch = self.new_attempt()
        first = self.ledger.withdraw_open(project, run_handle=launch.run_handle)
        self.assertEqual(("released", "withdrawn"), self.binding_hold(launch.run_handle))
        self.assertEqual("withdrawn", first["state"])
        self.assertEqual({**first, "known": True}, self.ledger.withdraw_open(project, run_handle=launch.run_handle))
        denied = self.claim((project, launch), "after-withdrawal")  # a claim after it is refused
        self.assertEqual(("denied", "claim_not_open"), (denied["state"], denied["reason_code"]))

    def test_RS_A_2_a_binding_whose_claim_was_admitted_is_never_withdrawn(self):
        project, launch = self.new_attempt()
        self.claim((project, launch), "admitted")
        with self.assertRaises(LedgerError):
            self.ledger.withdraw_open(project, run_handle=launch.run_handle)
        self.assertEqual(("held", None), self.binding_hold(launch.run_handle))

    def test_RS_A_5_withdrawal_for_another_project_or_handle_changes_nothing(self):
        project, launch = self.new_attempt()
        other = self.second_project()
        for target, handle in ((other, launch.run_handle), (project, "hrn_" + "0" * 48)):
            with self.subTest(target=target, handle=handle), self.assertRaises(LedgerError):
                self.ledger.withdraw_open(target, run_handle=handle)
        self.assertEqual(("held", None), self.binding_hold(launch.run_handle))

    def test_RS_A_deletion_keeps_todays_caution_inside_the_day_after_a_crashed_release(self):
        """Draft 4b: a crashed or lost release inside machine_idle's day still blocks deletion, since its
        child may still be running; before RS-A the unreleased row blocked it the same way."""
        self.case.claim()
        row = self.rows()[0]
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        self.assertEqual(1, self.sweep(now, row["clock_reference_ns"] - 1))  # released crashed
        with self.assertRaisesRegex(LedgerError, "project_has_active_launch_capacity"):
            self.ledger.delete_project(self.project, now_ms=now)

    def crashed_release(self):
        self.case.claim()
        row = self.rows()[0]
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        self.assertEqual(1, self.sweep(now, row["clock_reference_ns"] - 1))
        return row

    def test_RS_A_F5_a_crashed_decision_keeps_its_evidence_with_its_digest(self):
        """Round 1 F5 (design: the release evidence): the observed clocks are kept, and hash to the receipt."""
        import json
        from codex_agents.learning_contracts import content_digest
        row = self.crashed_release()
        hold = self.ledger._connection.execute(
            "SELECT receipt, evidence FROM run_holds WHERE kind='run_slot' AND identity=?",
            (row["handle_digest"],)).fetchone()
        evidence = json.loads(hold["evidence"])
        self.assertEqual(hold["receipt"], content_digest(evidence))
        self.assertEqual(("crashed", row["clock_reference_ns"] - 1),
                         (evidence["kind"], evidence["observed_monotonic_ns"]))

    def test_RS_A_F5_a_withdrawn_binding_records_the_time_it_was_withdrawn(self):
        project, launch = self.new_attempt()
        at = self.case.stamp + 12_345
        with mock.patch.object(self.ledger, "_clock_ms", return_value=at):
            self.ledger.withdraw_open(project, run_handle=launch.run_handle)
        from codex_agents.learning_contracts import keyed_digest
        self.assertEqual(at, self.ledger._connection.execute(
            "SELECT released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (keyed_digest(self.ledger.state.digest_key(), launch.run_handle),)).fetchone()[0])

    def test_RS_A_F5_an_adopted_release_whose_time_history_lacks_keeps_it_unknown(self):
        """Round 1 F5: adoption never writes a creation time as a release time."""
        project, launch = self.new_attempt()
        self.ledger.withdraw_open(project, run_handle=launch.run_handle)
        self.schema_fourteen(backup=True)
        ledger = self.upgraded()
        from codex_agents.learning_contracts import keyed_digest
        row = ledger._connection.execute(
            "SELECT state, release_kind, released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (keyed_digest(ledger.state.digest_key(), launch.run_handle),)).fetchone()
        self.assertEqual(("released", "withdrawn", None), tuple(row))

    def reopened(self):
        self.ledger.close()
        self.case.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.addCleanup(self.case.ledger.close)
        return self.case.ledger

    def broken_register(self, change):
        self.case.claim()
        with self.ledger._account_transaction():
            change(self.ledger._connection)

    def test_RS_A_F2_open_refuses_a_register_row_without_its_hold(self):
        self.broken_register(lambda c: c.execute(
            "INSERT INTO run_holds(kind,project_id,identity,state,taken_at_ms) VALUES('binding',?,?,'held',1)",
            (self.project, "f" * 64)))
        with self.assertRaisesRegex(LedgerError, "run hold"):
            self.reopened()

    def test_RS_A_F2_open_refuses_a_register_state_that_contradicts_its_hold(self):
        self.broken_register(lambda c: c.execute(
            "UPDATE run_holds SET state='released',released_at_ms=1,release_kind='completed' WHERE kind='run_slot'"))
        with self.assertRaisesRegex(LedgerError, "run hold"):
            self.reopened()

    def test_RS_A_F2_open_refuses_a_decision_whose_time_does_not_match_its_slot(self):
        row = self.crashed_release()
        with self.ledger._account_transaction():
            self.ledger._connection.execute(
                "UPDATE run_holds SET released_at_ms=released_at_ms+1 WHERE kind='run_slot' AND identity=?",
                (row["handle_digest"],))
        with self.assertRaisesRegex(LedgerError, "coverage|run hold"):
            self.reopened()

    def test_RS_A_F2_a_backup_with_a_register_row_without_its_hold_is_refused(self):
        self.broken_register(lambda c: c.execute(
            "INSERT INTO run_holds(kind,project_id,identity,state,taken_at_ms) VALUES('probe',?,?,'held',1)",
            (self.project, "e" * 64)))
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.project,
                                                         ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION)
        with self.assertRaisesRegex(LedgerError, "coverage|run hold"):
            learning_store._verify_project_backup_body(body)

    def test_RS_A_F2_a_valid_ledger_with_every_kind_of_release_still_opens(self):
        row = self.crashed_release()
        project, launch = self.new_attempt()
        self.ledger.withdraw_open(project, run_handle=launch.run_handle)
        self.reopened()

    def test_RS_A_F4_a_caller_time_never_moves_deletion_past_the_caution_day(self):
        """Round 1 F4: deletion's caution uses the ledger's clock, never a caller-supplied time."""
        row = self.crashed_release()
        future = row["claimed_at_ms"] + learning_store.LEGACY_RUN_MAX_MS + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=row["claimed_at_ms"] + 1):
            with self.assertRaisesRegex(LedgerError, "project_has_active_launch_capacity"):
                self.ledger.delete_project(self.project, now_ms=future)

    def test_RS_A_F4_deletion_after_the_day_removes_the_project_s_register_rows(self):
        """Round 1 F4 (draft 4b item 1): capacity rows, bindings and register rows go together."""
        row = self.crashed_release()
        after = row["claimed_at_ms"] + learning_store.LEGACY_RUN_MAX_MS + 1
        with mock.patch.object(self.ledger, "_clock_ms", return_value=after), \
                mock.patch.object(self.ledger, "_holds_account_restrictions_locked", return_value=False):
            self.ledger.delete_project(self.project, now_ms=after)
        self.assertEqual(0, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM run_holds WHERE project_id=?", (self.project,)).fetchone()[0])

    def test_RS_A_1_each_account_hold_has_its_register_row(self):
        self.case.claim()
        holds = [dict(r) for r in self.ledger._connection.execute(
            "SELECT kind, state FROM run_holds ORDER BY kind")]
        self.assertIn({"kind": "run_slot", "state": "held"}, holds)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited capacity tests run in test_active_run_capacity."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(RunHoldTests):
        if name in RunHoldTests.__dict__:
            suite.addTest(RunHoldTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
