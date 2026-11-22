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


def _snapshot_verified(ledger, project, root, name):
    learning_store._verify_project_backup_body(learning_store._snapshot_project_partition(
        ledger._connection, project, ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION))


def _streamed_verified(ledger, project, root, name):
    import os
    from codex_agents import project_backup
    path = root / f"{name}.sqlite"
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    project_backup.write(ledger._connection, path, project, learning_store.LEDGER_SCHEMA_VERSION)
    project_backup.verify(path, project_id=project, schema_version=learning_store.LEDGER_SCHEMA_VERSION)


def assert_refused_everywhere(test, ledger, project, root, reopen):
    """O0b RS-A round 2 (G1, G7): both backup formats and the next open refuse the ledger as it is now."""
    for check in (_snapshot_verified, _streamed_verified):
        with test.subTest(check=check.__name__), test.assertRaisesRegex(LedgerError, "coverage|run hold"):
            check(ledger, project, root, "refused")
    with test.subTest(check="open"), test.assertRaisesRegex(LedgerError, "coverage|run hold"):
        reopen()


def assert_accepted_everywhere(ledger, project, root, reopen):
    """O0b RS-A round 2: both backup formats verify, and the ledger opens again."""
    _snapshot_verified(ledger, project, root, "accepted")
    _streamed_verified(ledger, project, root, "accepted")
    return reopen()


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
        # Round 1 F8 (requirement, design draft 4 item 4): each kind shows its nearest deadline, and probes
        # how many only evidence can release.
        self.assertEqual({"held": 0, "past_due": 0, "next_eligible": None, "deadline": None,
                          "release_by_evidence": 0}, status["probe"])

    def status_at(self, now, monotonic):
        with mock.patch.object(self.ledger, "_clock_ms", return_value=now), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=monotonic):
            return self.ledger.account_holds_status(self.project)["run_slot"]

    def test_RS_A_F8_next_eligible_waits_for_the_monotonic_clock_too(self):
        """Round 1 F8: wall time past the deadline, monotonic clock not: the slot is not eligible yet."""
        self.case.claim()
        row = self.rows()[0]
        grace = learning_store.CRASHED_CLAIM_GRACE_MS
        now = row["deadline_at_ms"] + grace + 1
        monotonic = row["clock_reference_ns"] + 1
        remaining_ns = row["execution_deadline_ns"] + grace * 1_000_000 - monotonic
        status = self.status_at(now, monotonic)
        self.assertEqual(0, status["past_due"])
        self.assertEqual(now - (-remaining_ns // 1_000_000), status["next_eligible"])  # now + ceil(ms)
        self.assertGreater(status["next_eligible"], now)
        self.assertEqual(row["deadline_at_ms"], status["deadline"])

    def test_RS_A_F8_after_a_reboot_only_the_wall_deadline_remains(self):
        self.case.claim()
        row = self.rows()[0]
        grace = learning_store.CRASHED_CLAIM_GRACE_MS
        status = self.status_at(row["deadline_at_ms"], row["clock_reference_ns"] - 1)
        self.assertEqual(row["deadline_at_ms"] + grace + 1, status["next_eligible"])

    def test_RS_A_F8_next_eligible_is_unknown_while_the_wall_clock_is_behind_the_claim(self):
        self.case.claim()
        row = self.rows()[0]
        self.assertIsNone(self.status_at(row["claimed_at_ms"] - 1, row["clock_reference_ns"] + 1)["next_eligible"])

    def test_RS_A_F14_the_capacity_report_carries_the_holds_by_kind(self):
        """Round 1 F14 (RS-A-6): the account capacity report, which reaches learning-status, carries the
        holds by kind as account_holds_status reads them."""
        self.case.claim()
        row = self.rows()[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=row["claimed_at_ms"] + 1), \
                mock.patch.object(self.ledger, "_capacity_clock_reference", return_value=row["clock_reference_ns"] + 1):
            capacity = self.ledger.account_capacity(self.project)
            holds = self.ledger.account_holds_status(self.project)
        self.assertEqual(holds, capacity["holds"])
        self.assertEqual(1, capacity["holds"]["run_slot"]["held"])
        # The counts the earlier report carried are unchanged. Corrected for OP-3 (requirement): the report also
        # carries the open requests in flight.
        self.assertEqual({"observations", "cleanup_receipts", "pending_bindings", "billing_probes", "open_requests",
                          "holds"}, set(capacity))

    HOLDS = {"run_slot": {"held": 1, "past_due": 0, "next_eligible": 1_800_000_300_001, "deadline": 1_800_000_000_000},
             "binding": {"held": 0, "past_due": 0, "next_eligible": None, "deadline": None},
             "probe": {"held": 0, "past_due": 0, "next_eligible": None, "deadline": None, "release_by_evidence": 0}}
    COUNTS = {"observations": {"live": 1, "limit": 4096}, "cleanup_receipts": {"live": 0, "limit": 4096},
              "pending_bindings": {"live": 0, "limit": 4096}, "billing_probes": {"live": 0, "limit": 4096}}

    def test_RS_A_F14_status_prints_the_holds_plainly(self):
        """Round 1 F14: learning-status shows the holds by kind, and one plain line for the owner."""
        from codex_agents.client_runtime import _status_account_capacity, _status_account_holds
        report = {**self.COUNTS, "holds": self.HOLDS}
        self.assertEqual(self.COUNTS, _status_account_capacity(report))  # the counts keep their shape
        self.assertEqual({"reported": True, "kinds": self.HOLDS,
                          "summary": "Account holds: 1 run slot held (next can be freed at 2027-01-15 08:05 UTC; "
                                     "deadline 2027-01-15 08:00 UTC), 0 bindings, 0 probes."},
                         _status_account_holds(report))
        several = {"run_slot": {"held": 2, "past_due": 1, "next_eligible": 1_800_000_300_001,
                                "deadline": 1_800_000_000_000},
                   "binding": {"held": 1, "past_due": 0, "next_eligible": 1_800_003_600_000,
                               "deadline": 1_800_003_600_000},
                   "probe": {"held": 1, "past_due": 0, "next_eligible": None, "deadline": 1_800_007_200_000,
                             "release_by_evidence": 1}}
        self.assertEqual(
            "Account holds: 2 run slots held (1 past due, freed at the next check; next can be freed at "
            "2027-01-15 08:05 UTC; nearest deadline 2027-01-15 08:00 UTC), 1 binding held (next can be freed at "
            "2027-01-15 09:00 UTC), 1 probe held (1 freed only by its result).",
            _status_account_holds({**self.COUNTS, "holds": several})["summary"])

    def test_RS_A_F14_status_accepts_an_earlier_collectors_report_without_holds(self):
        from codex_agents.client_runtime import _status_account_capacity, _status_account_holds
        earlier = {name: self.COUNTS[name] for name in ("observations", "cleanup_receipts")}
        for report in (earlier, self.COUNTS):
            with self.subTest(fields=sorted(report)):
                self.assertEqual(report, _status_account_capacity(report))
                self.assertEqual({"reported": False, "kinds": None,
                                  "summary": "Account holds: not reported by this collector version."},
                                 _status_account_holds(report))
        self.assertIsNone(_status_account_holds(None))  # no report at all: nothing is shown

    def test_RS_A_F14_malformed_holds_are_not_shown(self):
        from codex_agents.client_runtime import _status_account_holds
        bad_count = {**self.HOLDS, "binding": {**self.HOLDS["binding"], "held": -1}}
        missing = {name: self.HOLDS[name] for name in ("run_slot", "binding")}
        extra = {**self.HOLDS, "probe": {**self.HOLDS["probe"], "other": 0}}
        for holds in (bad_count, missing, extra, None, []):
            with self.subTest(holds=holds):
                self.assertIsNone(_status_account_holds({**self.COUNTS, "holds": holds}))

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
            self.ledger._connection.execute("ALTER TABLE records DROP COLUMN probe_id")  # schema 16 added it
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

    def upgraded_at(self, now, monotonic):
        with mock.patch.object(LearningLedger, "_clock_ms", return_value=now), \
                mock.patch.object(LearningLedger, "_capacity_clock_reference", return_value=monotonic):
            return self.upgraded()

    def slot_hold(self, ledger, handle_digest):
        return tuple(ledger._connection.execute(
            "SELECT r.state, r.release_kind, a.released_at_ms IS NOT NULL FROM run_holds r JOIN active_run_capacity a "
            "ON a.project_id=r.project_id AND a.handle_digest=r.identity WHERE r.kind='run_slot' AND r.identity=?",
            (handle_digest,)).fetchone())

    def test_RS_A_F1_the_upgrade_releases_a_slot_already_settled_lost(self):
        """Round 1 F1 (draft 3 item 5): its binding says service_lost, so it is adopted released `lost`."""
        self.case.claim()
        row = self.rows()[0]
        with self.ledger._account_transaction():  # as 0.8.37's lost settlement left it: binding settled, slot held
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                "WHERE handle_digest=?", (row["handle_digest"],))
        self.schema_fourteen(backup=True)
        ledger = self.upgraded_at(row["claimed_at_ms"] + 1, row["clock_reference_ns"] + 1)
        self.assertEqual(("released", "lost", 1), self.slot_hold(ledger, row["handle_digest"]))

    def test_RS_A_F1_the_upgrade_releases_a_slot_the_crash_rule_already_frees(self):
        """Round 1 F1 (draft 3b item 2): released `crashed` before activation, so it never counts again."""
        self.case.claim()
        row = self.rows()[0]
        self.schema_fourteen(backup=True)
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        ledger = self.upgraded_at(now, row["clock_reference_ns"] - 1)  # after a reboot
        self.assertEqual(("released", "crashed", 1), self.slot_hold(ledger, row["handle_digest"]))

    def test_RS_A_F1_the_upgrade_keeps_a_live_slot_held(self):
        self.case.claim()
        row = self.rows()[0]
        self.schema_fourteen(backup=True)
        ledger = self.upgraded_at(row["claimed_at_ms"] + 1, row["clock_reference_ns"] + 1)
        self.assertEqual(("held", None, 0), self.slot_hold(ledger, row["handle_digest"]))

    def test_RS_A_F1_the_upgrade_releases_an_expired_unclaimed_binding(self):
        """Round 1 F1 (draft 4 item 2)."""
        project, launch = self.new_attempt()
        from codex_agents.learning_contracts import keyed_digest
        digest = keyed_digest(self.ledger.state.digest_key(), launch.run_handle)
        expires = self.ledger._connection.execute(
            "SELECT expires_at_ms FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone()[0]
        self.schema_fourteen(backup=True)
        ledger = self.upgraded_at(expires + 1, 1)
        self.assertEqual(("released", "expired"), tuple(ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='binding' AND identity=?", (digest,)).fetchone()))

    def streamed(self, version, name):
        import os
        from codex_agents import project_backup
        path = self.fixture.state.root / f"{name}.sqlite"
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        project_backup.write(self.ledger._connection, path, self.project, version)
        return path

    def without_register(self, path):
        raw = sqlite3.connect(path)
        try:
            raw.execute("DROP TABLE IF EXISTS run_holds")  # its index goes with it
            raw.commit()
        finally:
            raw.close()

    def test_RS_A_F3_a_streamed_backup_from_schema_14_still_verifies(self):
        """Round 1 F3: a file 0.8.37 wrote has schema 14's objects, without the register."""
        from codex_agents import project_backup
        self.case.claim()
        path = self.streamed(14, "old")
        self.without_register(path)
        project_backup.verify(path, project_id=self.project, schema_version=14)

    def test_RS_A_F3_a_schema_15_backup_without_its_register_is_refused(self):
        from codex_agents import project_backup
        self.case.claim()
        path = self.streamed(15, "new")
        self.without_register(path)
        with self.assertRaises(LedgerError):
            project_backup.verify(path, project_id=self.project, schema_version=15)

    def binding_hold(self, handle):
        from codex_agents.learning_contracts import keyed_digest
        digest = keyed_digest(self.ledger.state.digest_key(), handle)
        row = self.ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='binding' AND project_id=? AND identity=?",
            (self.project, digest)).fetchone()
        return None if row is None else tuple(row)

    REQUEST = "a" * 64

    def withdraw(self, project, handle, *, request_digest=REQUEST, generation=1):
        """Round 1 F13: a withdrawal names the request's digest and the binding's generation (a held one is 1)."""
        return self.ledger.withdraw_open(project, run_handle=handle, request_digest=request_digest,
                                         generation=generation)

    def binding_generation(self, handle):
        from codex_agents.learning_contracts import keyed_digest
        return self.ledger._connection.execute(
            "SELECT generation FROM run_holds WHERE kind='binding' AND project_id=? AND identity=?",
            (self.project, keyed_digest(self.ledger.state.digest_key(), handle))).fetchone()[0]

    def test_RS_A_F13_a_withdrawal_keeps_its_request_digest_and_generation_with_its_receipt(self):
        """Round 1 F13 (draft 4 item 2): the receipt is stored with the request's digest and the generation it
        named, and is the digest of that stored evidence."""
        import json
        from codex_agents.learning_contracts import content_digest, keyed_digest
        project, launch = self.new_attempt()
        first = self.withdraw(project, launch.run_handle)
        hold = self.ledger._connection.execute(
            "SELECT receipt, evidence FROM run_holds WHERE kind='binding' AND identity=?",
            (keyed_digest(self.ledger.state.digest_key(), launch.run_handle),)).fetchone()
        evidence = json.loads(hold["evidence"])
        self.assertEqual((self.REQUEST, 1), (evidence["request_digest"], evidence["generation"]))
        self.assertEqual(hold["receipt"], content_digest(evidence))
        self.assertEqual(hold["receipt"], first["receipt"])

    def test_RS_A_F13_an_identical_retry_after_a_restart_gets_the_stored_receipt(self):
        """Round 1 F13: a reply lost, the ledger reopened, the same request again: the same answer, known."""
        project, launch = self.new_attempt()
        first = self.withdraw(project, launch.run_handle)
        self.reopened()
        self.assertEqual({**first, "known": True}, self.withdraw(project, launch.run_handle))

    def test_RS_A_F13_an_identical_retry_after_its_binding_is_pruned_gets_the_stored_receipt(self):
        """Round 1 F13 (draft 4 item 2, kept through pruning): the tombstone answers once the binding is gone."""
        from codex_agents.learning_contracts import keyed_digest
        project, launch = self.new_attempt()
        first = self.withdraw(project, launch.run_handle)
        with self.ledger._account_transaction():
            self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?",
                                            (keyed_digest(self.ledger.state.digest_key(), launch.run_handle),))
        self.assertEqual({**first, "known": True}, self.withdraw(project, launch.run_handle))

    def test_RS_A_F13_a_stale_generation_is_refused_and_changes_nothing(self):
        """Round 1 F13: a different request naming the generation before the withdrawal is stale; one naming
        a generation the held binding does not have is refused too, and the binding stays held."""
        project, launch = self.new_attempt()
        with self.assertRaisesRegex(LedgerError, "stale"):
            self.withdraw(project, launch.run_handle, generation=2)
        self.assertEqual((("held", None), 1), (self.binding_hold(launch.run_handle),
                                               self.binding_generation(launch.run_handle)))
        first = self.withdraw(project, launch.run_handle)
        with self.assertRaisesRegex(LedgerError, "stale"):
            self.withdraw(project, launch.run_handle, request_digest="b" * 64, generation=1)
        self.assertEqual({**first, "known": True}, self.withdraw(project, launch.run_handle))

    def test_RS_A_F13_a_new_withdrawal_without_a_generation_is_refused_generation_required(self):
        """Round 1 F13 (draft 4b item 2): an older client cannot perform a new transition."""
        project, launch = self.new_attempt()
        with self.assertRaisesRegex(LedgerError, "generation_required"):
            self.withdraw(project, launch.run_handle, generation=None)
        self.assertEqual(("held", None), self.binding_hold(launch.run_handle))

    def test_RS_A_F13_an_identical_retry_without_a_generation_gets_the_stored_receipt(self):
        """Round 1 F13 (draft 4b item 2): matched by its request digest, an older client's retry is answered;
        a different one without a generation is refused."""
        project, launch = self.new_attempt()
        first = self.withdraw(project, launch.run_handle)
        self.assertEqual({**first, "known": True}, self.withdraw(project, launch.run_handle, generation=None))
        with self.assertRaisesRegex(LedgerError, "generation_required"):
            self.withdraw(project, launch.run_handle, request_digest="c" * 64, generation=None)

    def test_RS_A_2_a_never_claimed_binding_is_withdrawn_for_good(self):
        project, launch = self.new_attempt()
        # (requirement) round 1 F13, design draft 4 item 2: a withdrawal names its generation and request digest.
        first = self.withdraw(project, launch.run_handle)
        self.assertEqual(("released", "withdrawn"), self.binding_hold(launch.run_handle))
        self.assertEqual("withdrawn", first["state"])
        self.assertEqual({**first, "known": True}, self.withdraw(project, launch.run_handle))
        denied = self.claim((project, launch), "after-withdrawal")  # a claim after it is refused
        self.assertEqual(("denied", "claim_not_open"), (denied["state"], denied["reason_code"]))

    def test_RS_A_2_a_binding_whose_claim_was_admitted_is_never_withdrawn(self):
        project, launch = self.new_attempt()
        self.claim((project, launch), "admitted")
        with self.assertRaises(LedgerError):
            self.withdraw(project, launch.run_handle)  # (requirement) round 1 F13: generation and digest named
        self.assertEqual(("held", None), self.binding_hold(launch.run_handle))

    def test_RS_A_5_withdrawal_for_another_project_or_handle_changes_nothing(self):
        project, launch = self.new_attempt()
        other = self.second_project()
        for target, handle in ((other, launch.run_handle), (project, "hrn_" + "0" * 48)):
            with self.subTest(target=target, handle=handle), self.assertRaises(LedgerError):
                self.withdraw(target, handle)  # (requirement) round 1 F13: generation and digest named
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
            self.withdraw(project, launch.run_handle)  # (requirement) round 1 F13: generation and digest named
        from codex_agents.learning_contracts import keyed_digest
        self.assertEqual(at, self.ledger._connection.execute(
            "SELECT released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (keyed_digest(self.ledger.state.digest_key(), launch.run_handle),)).fetchone()[0])

    def test_RS_A_F5_an_adopted_release_whose_time_history_lacks_keeps_it_unknown(self):
        """Round 1 F5: adoption never writes a creation time as a release time."""
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)  # (requirement) round 1 F13: generation and digest named
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

    def test_RS_A_1_a_hold_whose_record_retention_removes_is_released_and_its_tombstone_kept(self):
        """RS-A-1: a released row stays as a tombstone after its source is pruned; a held one is released
        `removed` with its source, so no held row is ever left without its hold, and the ledger still opens."""
        project, launch = self.new_attempt()
        from codex_agents.learning_contracts import keyed_digest
        digest = keyed_digest(self.ledger.state.digest_key(), launch.run_handle)
        at = self.case.stamp + 777
        with mock.patch.object(self.ledger, "_clock_ms", return_value=at), self.ledger._account_transaction():
            self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?", (digest,))
        self.assertEqual(("released", "removed", at), tuple(self.ledger._connection.execute(
            "SELECT state, release_kind, released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (digest,)).fetchone()))
        self.reopened()

    def test_RS_A_F2_a_valid_ledger_with_every_kind_of_release_still_opens(self):
        self.crashed_release()
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)  # (requirement) round 1 F13: generation and digest named
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

    # O0b RS-A round 2 G1, G2 (DESIGN-0.4.0-RS-A.md, "Code review round 2"): written before the code.

    def refused(self):
        assert_refused_everywhere(self, self.ledger, self.project, self.fixture.state.root, self.reopened)

    def accepted(self):
        return assert_accepted_everywhere(self.ledger, self.project, self.fixture.state.root, self.reopened)

    def expired_binding(self):
        """An unclaimed binding the sweep released `expired`, at its expiry plus one."""
        from codex_agents.learning_contracts import keyed_digest
        project, launch = self.new_attempt()
        digest = keyed_digest(self.ledger.state.digest_key(), launch.run_handle)
        expires = self.ledger._connection.execute(
            "SELECT expires_at_ms FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone()[0]
        self.assertGreaterEqual(self.sweep(expires + 1, 1), 1)  # the fixture's own binding may expire too
        self.assertEqual(("released", "expired", expires + 1), tuple(self.ledger._connection.execute(
            "SELECT state, release_kind, released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (digest,)).fetchone()))
        return digest, expires

    def test_RS_A_G1_an_expired_tombstone_released_before_its_binding_s_expiry_is_refused(self):
        digest, expires = self.expired_binding()
        with self.ledger._account_transaction():
            self.ledger._connection.execute(
                "UPDATE run_holds SET released_at_ms=? WHERE kind='binding' AND identity=?", (expires - 1, digest))
        self.refused()

    def tampered_evidence(self, kind, change):
        with self.ledger._account_transaction():
            evidence = self.ledger._connection.execute(
                "SELECT evidence FROM run_holds WHERE release_kind=?", (kind,)).fetchone()[0]
            self.assertIsNotNone(evidence)
            self.ledger._connection.execute(
                "UPDATE run_holds SET evidence=? WHERE release_kind=?", (change(evidence), kind))

    def test_RS_A_G1_a_crashed_decision_whose_evidence_does_not_hash_to_its_receipt_is_refused(self):
        import json
        self.crashed_release()
        self.tampered_evidence("crashed", lambda raw: canonical_json(
            {**json.loads(raw), "observed_wall_ms": 1}).decode())
        self.refused()

    def test_RS_A_G1_a_withdrawal_whose_evidence_does_not_hash_to_its_receipt_is_refused(self):
        import json
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)
        self.tampered_evidence("withdrawn", lambda raw: canonical_json(
            {**json.loads(raw), "generation": 2}).decode())
        self.refused()

    def test_RS_A_G1_malformed_decision_evidence_is_refused(self):
        self.crashed_release()
        self.tampered_evidence("crashed", lambda raw: raw[:-1])
        self.refused()

    def test_RS_A_G11_evidence_stored_in_another_form_that_still_hashes_to_its_receipt_opens(self):
        """Round 2 G11: the register check's fast path (the stored bytes hash to the receipt) never narrows what
        opens: evidence stored with other spacing, whose decoded form still hashes to its receipt, still opens
        everywhere, as before."""
        import json
        self.crashed_release()
        self.tampered_evidence("crashed", lambda raw: json.dumps(json.loads(raw), indent=1))
        self.accepted()

    # O0b RS-A round 3 (review 040-rsa-code-r3-1): the G11 fast path compares the receipt with the stored
    # bytes' own digest, which is NULL for evidence that is not ASCII text; a missing receipt must never equal
    # that NULL. Retained evidence with no receipt was refused before G11 and is refused still.

    def withdrawn_evidence(self, receipt, evidence):
        """A withdrawal's register row (its source kept), its receipt and evidence then set as given."""
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)
        with self.ledger._account_transaction():
            self.assertEqual(1, self.ledger._connection.execute(
                "UPDATE run_holds SET receipt=?,evidence=? WHERE release_kind='withdrawn'",
                (receipt, evidence)).rowcount)

    def test_RS_A_G11_malformed_non_ascii_evidence_with_no_receipt_is_refused(self):
        self.withdrawn_evidence(None, '{"withdrawn": "é"')
        self.refused()

    def test_RS_A_G11_evidence_stored_as_a_blob_with_no_receipt_is_refused(self):
        self.withdrawn_evidence(None, b'{"withdrawn": true}')
        self.refused()

    def test_RS_A_G11_ascii_evidence_with_no_receipt_is_refused(self):
        self.withdrawn_evidence(None, canonical_json({"withdrawn": True}).decode())
        self.refused()

    def test_RS_A_G11_evidence_whose_own_bytes_hash_to_its_receipt_opens(self):
        """Round 3: the accepted G11 meaning, pinned: retained ASCII evidence whose stored bytes hash to its
        receipt opens everywhere (the writers' canonical form)."""
        raw = canonical_json({"withdrawn": True, "generation": 1})
        self.withdrawn_evidence(hashlib.sha256(raw).hexdigest(), raw.decode())
        self.accepted()

    KEEP = object()

    def pruned_decision(self, kind, *, receipt, evidence):
        """A lost or crashed decision's tombstone whose slot (and its claim, binding and launch rows) record
        retention has removed, then its receipt and evidence set as given (KEEP leaves them)."""
        if kind == "crashed":
            row = self.crashed_release()
        else:
            self.case.claim()
            row = self.rows()[0]
            with self.ledger._account_transaction():
                self.ledger._connection.execute(
                    "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                    "WHERE handle_digest=?", (row["handle_digest"],))
            now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
            self.assertGreaterEqual(self.sweep(now, row["clock_reference_ns"] - 1), 1)
        digest = row["handle_digest"]
        with self.ledger._account_transaction():
            connection = self.ledger._connection
            for table in ("launch_receipts", "launch_observations", "launch_inputs", "host_run_bindings",
                          "active_run_capacity", "launch_claims"):
                connection.execute(f"DELETE FROM {table} WHERE project_id=? AND handle_digest=?",
                                   (self.project, digest))
            if receipt is not self.KEEP:
                connection.execute("UPDATE run_holds SET receipt=? WHERE kind='run_slot' AND identity=?",
                                   (receipt, digest))
            if evidence is not self.KEEP:
                connection.execute("UPDATE run_holds SET evidence=? WHERE kind='run_slot' AND identity=?",
                                   (evidence, digest))
        self.assertEqual(("released", kind), tuple(self.ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='run_slot' AND identity=?", (digest,)).fetchone()))
        self.assertIsNone(self.ledger._connection.execute(
            "SELECT 1 FROM active_run_capacity WHERE handle_digest=?", (digest,)).fetchone())
        return digest

    def test_RS_A_G11_a_pruned_decision_tombstone_with_its_evidence_still_opens(self):
        """Round 3 control for the two below: a decision's tombstone whose source retention removed opens."""
        self.pruned_decision("crashed", receipt=self.KEEP, evidence=self.KEEP)
        self.accepted()

    def test_RS_A_G11_a_pruned_crashed_tombstone_with_malformed_evidence_and_no_receipt_is_refused(self):
        self.pruned_decision("crashed", receipt=None, evidence='{"kind": "crashedé"')
        self.refused()

    def test_RS_A_G10_a_pruned_lost_tombstone_without_its_evidence_is_refused(self):
        """Round 3 (tests missing in round 2): a lost decision's evidence is required after its source goes."""
        self.pruned_decision("lost", receipt=self.KEEP, evidence=None)
        self.refused()

    def test_RS_A_G1_pruned_evidence_unknown_times_and_pruned_sources_still_open(self):
        """Round 2 G1: what the register may legitimately lack (pruned evidence, history's unknown times, a
        tombstone whose source retention removed) is still accepted everywhere. (requirement) Round 2 G10
        (DESIGN-0.4.0-RS-A.md): a lost or crashed decision's evidence is never pruned, so only other rows lose it."""
        self.crashed_release()
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)
        digest, _ = self.expired_binding()
        with self.ledger._account_transaction():
            self.ledger._connection.execute(  # (requirement) round 2 G10: not a decision's evidence
                "UPDATE run_holds SET evidence=NULL WHERE release_kind NOT IN ('lost','crashed')")
            self.ledger._connection.execute(
                "UPDATE run_holds SET released_at_ms=NULL WHERE kind='binding' AND release_kind='withdrawn'")
            self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?", (digest,))
        self.assertEqual(("released", "expired"), tuple(self.ledger._connection.execute(
            "SELECT state, release_kind FROM run_holds WHERE kind='binding' AND identity=?", (digest,)).fetchone()))
        self.accepted()

    def test_RS_A_G1_every_legitimate_release_kind_with_its_evidence_still_opens(self):
        self.crashed_release()
        project, launch = self.new_attempt()
        self.withdraw(project, launch.run_handle)
        self.expired_binding()
        self.accepted()

    # O0b RS-A round 2 G10: a lost or crashed decision's evidence is its only proof, never pruned.

    def test_RS_A_G10_a_crashed_decision_without_its_evidence_is_refused(self):
        self.crashed_release()
        with self.ledger._account_transaction():
            self.ledger._connection.execute("UPDATE run_holds SET evidence=NULL WHERE release_kind='crashed'")
        self.refused()

    def test_RS_A_G10_a_real_crashed_decision_still_opens(self):
        self.crashed_release()
        self.accepted()

    def test_RS_A_G10_a_real_lost_decision_still_opens(self):
        self.case.claim()
        row = self.rows()[0]
        with self.ledger._account_transaction():  # its service settled it lost
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='service_lost' "
                "WHERE handle_digest=?", (row["handle_digest"],))
        now = row["deadline_at_ms"] + learning_store.CRASHED_CLAIM_GRACE_MS + 1
        self.assertGreaterEqual(self.sweep(now, row["clock_reference_ns"] - 1), 1)
        self.assertEqual(("released", "lost", 1), self.slot_hold(self.ledger, row["handle_digest"]))
        self.accepted()

    def recovery_unavailable_then_fallback(self, consumed_at):
        """A binding abandoned recovery_unavailable, whose fallback later wrote consumed_at_ms (as
        authorize_fallback does): the abandonment's own time is not that."""
        from codex_agents.learning_contracts import keyed_digest
        project, launch = self.new_attempt()
        digest = keyed_digest(self.ledger.state.digest_key(), launch.run_handle)
        with self.ledger._account_transaction():
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET binding_state='abandoned',abandoned_reason='recovery_unavailable' "
                "WHERE handle_digest=?", (digest,))
            self.ledger._connection.execute(
                "UPDATE host_run_bindings SET consumed_at_ms=? WHERE handle_digest=?", (consumed_at, digest))
        return digest

    def test_RS_A_G2_an_abandoned_binding_later_consumed_by_fallback_is_adopted_with_an_unknown_time(self):
        digest = self.recovery_unavailable_then_fallback(self.case.stamp + 50)
        self.schema_fourteen(backup=True)
        ledger = self.upgraded()
        self.assertEqual(("released", "recovery_unavailable", None), tuple(ledger._connection.execute(
            "SELECT state, release_kind, released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (digest,)).fetchone()))

    def test_RS_A_G2_an_abandoned_binding_inserted_with_a_consumed_time_keeps_its_release_time_unknown(self):
        """The take trigger records a final row in adoption's terms (a restore, a migration step)."""
        digest = self.recovery_unavailable_then_fallback(self.case.stamp + 50)
        with self.ledger._account_transaction():
            row = dict(self.ledger._connection.execute(
                "SELECT * FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone())
            self.ledger._connection.execute("DELETE FROM host_run_bindings WHERE handle_digest=?", (digest,))
            self.ledger._connection.execute("DELETE FROM run_holds WHERE kind='binding' AND identity=?", (digest,))
            self.ledger._connection.execute(
                f"INSERT INTO host_run_bindings({','.join(row)}) VALUES({','.join('?' * len(row))})",
                tuple(row.values()))
        self.assertEqual(("released", "recovery_unavailable", None), tuple(self.ledger._connection.execute(
            "SELECT state, release_kind, released_at_ms FROM run_holds WHERE kind='binding' AND identity=?",
            (digest,)).fetchone()))

    def test_RS_A_1_each_account_hold_has_its_register_row(self):
        self.case.claim()
        holds = [dict(r) for r in self.ledger._connection.execute(
            "SELECT kind, state FROM run_holds ORDER BY kind")]
        self.assertIn({"kind": "run_slot", "state": "held"}, holds)

    def test_RS_A_G4_the_legacy_open_cap_admits_by_the_count_the_status_reports(self):
        """Round 2 G4: open_host_run's binding cap skips a register-released claimed binding (still pending for
        its settlement), as the status and the signature open's cap do, also after a clock rollback."""
        self.case.claim()
        binding = next(b for b in self.bindings() if b["binding_state"] == "pending")
        row = self.rows()[0]
        self.assertEqual(1, self.sweep(binding["expires_at_ms"] + 1, row["clock_reference_ns"] + 1))
        # The fixture's clock is the claim's time again: rolled back, the binding is not past its expiry on it.
        self.assertLess(self.ledger._clock_ms(), binding["expires_at_ms"])
        live = self.ledger.account_capacity(self.project)["pending_bindings"]["live"]
        with mock.patch.object(learning_store, "MAX_PENDING_HOST_RUNS", live + 1):
            self.new_attempt()  # the cap's last place, by the status's count
            with self.assertRaisesRegex(LedgerError, "pending host run capacity is exhausted"):
                self.new_attempt()  # control: a live binding still counts
        self.assertEqual(live + 1, self.ledger.account_capacity(self.project)["pending_bindings"]["live"])

    def add_released_claimed_history(self, count, expired_at):
        """`count` historical claimed bindings past their expiry, register-released `expired` and still pending
        for their settlement, each with its claim (cloned from the fixture's claimed binding)."""
        connection = self.ledger._connection
        source = dict(connection.execute(
            "SELECT h.* FROM host_run_bindings h JOIN launch_claims c ON c.project_id=h.project_id "
            "AND c.handle_digest=h.handle_digest LIMIT 1").fetchone())
        claim = dict(connection.execute("SELECT * FROM launch_claims WHERE project_id=? AND handle_digest=?",
                                        (source["project_id"], source["handle_digest"])).fetchone())
        bindings, claims = [], []
        for index in range(count):
            digest = hashlib.sha256(b"history-%d" % index).hexdigest()
            bindings.append({**source, "handle_digest": digest, "run_id": "history-%d" % index,
                             "expires_at_ms": expired_at, "binding_state": "pending"})
            claims.append({**claim, "handle_digest": digest, "run_id": "history-%d" % index,
                           "bridge_run_id": "brg_" + digest[:32]})
        connection.execute("BEGIN IMMEDIATE")
        for table, rows in (("host_run_bindings", bindings), ("launch_claims", claims)):
            columns = list(rows[0])
            connection.executemany(
                f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' * len(columns))})",
                [tuple(row[c] for c in columns) for row in rows])
        connection.execute(
            "UPDATE run_holds SET state='released',generation=generation+1,released_at_ms=?,release_kind='expired' "
            "WHERE kind='binding' AND state='held' AND identity IN "
            "(SELECT handle_digest FROM host_run_bindings WHERE run_id LIKE 'history-%')", (expired_at + 1,))
        connection.execute("COMMIT")

    def release_steps(self, now, monotonic):
        """SQLite VM steps of one release batch (a progress handler counting every step) and what it released."""
        steps = [0]

        def count():
            steps[0] += 1
            return 0
        self.ledger._connection.set_progress_handler(count, 1)
        try:
            released = self.sweep(now, monotonic)
        finally:
            self.ledger._connection.set_progress_handler(None, 0)
        return steps[0], released

    def test_RS_A_G5_an_empty_batch_s_work_does_not_grow_with_released_claimed_history(self):
        """Round 2 G5: expiry candidates come from held register rows, so released claimed bindings (pending for
        their settlement) are never re-examined: an empty batch does the same work with 2,000 of them."""
        self.case.claim()
        binding = next(b for b in self.bindings() if b["binding_state"] == "pending")
        row = self.rows()[0]
        now, monotonic = binding["expires_at_ms"] + 1, row["clock_reference_ns"] + 1
        self.assertEqual(1, self.sweep(now, monotonic))
        before, released = self.release_steps(now, monotonic)
        self.assertEqual(0, released)
        self.add_released_claimed_history(2000, binding["expires_at_ms"])
        after, released = self.release_steps(now, monotonic)
        self.assertEqual(0, released)
        self.assertLessEqual(after, before + 200, (before, after))  # a constant, never a step per history row

    def test_RS_A_G5_both_binding_expiry_queries_read_the_register_index(self):
        """Round 2 G5: by query plan, the unclaimed and the claimed binding expiry select their candidates
        through run_holds_due (held register rows), never by a scan of pending source bindings."""
        self.case.claim()
        statements = []
        self.ledger._connection.set_trace_callback(statements.append)
        try:
            self.sweep(1, 1)
        finally:
            self.ledger._connection.set_trace_callback(None)
        expiry = [s for s in statements if s.lstrip().startswith("UPDATE")
                  and "expires_at_ms<=" in s and ("abandoned_reason='expired'" in s or "release_kind='expired'" in s)]
        self.assertEqual(2, len(expiry), statements)
        for statement in expiry:
            plan = " | ".join(r[3] for r in self.ledger._connection.execute("EXPLAIN QUERY PLAN " + statement))
            self.assertIn("run_holds_due", plan, statement)
            self.assertNotIn("host_run_bindings_live", plan, statement)

    def steps_of(self, call):
        """SQLite VM steps of one call (a progress handler counting every step) and its result."""
        steps = [0]

        def count():
            steps[0] += 1
            return 0
        self.ledger._connection.set_progress_handler(count, 1)
        try:
            result = call()
        finally:
            self.ledger._connection.set_progress_handler(None, 0)
        return steps[0], result

    def rolled_back_history(self):
        """A claimed binding released `expired` by the sweep, then the clock back at the claim's time, so the
        binding (and 2,000 historical copies added after the first measurement) are not past expiry on it."""
        self.case.claim()
        binding = next(b for b in self.bindings() if b["binding_state"] == "pending")
        row = self.rows()[0]
        self.assertEqual(1, self.sweep(binding["expires_at_ms"] + 1, row["clock_reference_ns"] + 1))
        self.assertLess(self.ledger._clock_ms(), binding["expires_at_ms"])
        return binding["expires_at_ms"]

    def test_RS_A_G12_the_status_count_after_a_rollback_does_not_grow_with_history(self):
        """Round 2 G12: the status's binding count reads held register rows, so 2,000 released claimed
        bindings back inside their expiry by a clock rollback cost no work and are not counted."""
        expires = self.rolled_back_history()
        before, report = self.steps_of(lambda: self.ledger.account_capacity(self.project))
        self.add_released_claimed_history(2000, expires)
        after, again = self.steps_of(lambda: self.ledger.account_capacity(self.project))
        self.assertEqual(report["pending_bindings"], again["pending_bindings"])
        self.assertLessEqual(after, before + 200, (before, after))

    def test_RS_A_G12_the_legacy_open_cap_after_a_rollback_does_not_grow_with_history(self):
        """Round 2 G12: open_host_run's cap reads held register rows: an open does the same work with 2,000
        released claimed bindings back inside their expiry, and still admits by the status's count."""
        expires = self.rolled_back_history()
        self.new_attempt()  # warm: the first open compiles what the second reuses
        before, _ = self.steps_of(self.new_attempt)
        self.add_released_claimed_history(2000, expires)
        after, _ = self.steps_of(self.new_attempt)
        self.assertLessEqual(after, before + 500, (before, after))
        live = self.ledger.account_capacity(self.project)["pending_bindings"]["live"]
        with mock.patch.object(learning_store, "MAX_PENDING_HOST_RUNS", live + 1):
            self.new_attempt()  # the cap's last place, by the status's count
            with self.assertRaisesRegex(LedgerError, "pending host run capacity is exhausted"):
                self.new_attempt()

    def drained_for_harness(self):
        """The budget harness (scripts/measure_release_batches.py), with one claimed run built and drained."""
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "scripts" / "measure_release_batches.py"
        spec = importlib.util.spec_from_file_location("measure_release_batches_under_test", path)
        harness = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness)
        harness.build(self, 1, 0)
        harness.drain(self, *harness.due_clocks(self))
        return harness

    def test_RS_A_G8_the_benchmark_refuses_history_the_validator_would_refuse(self):
        """Round 3 (review 040-rsa-code-r3-3): the harness checks the history it builds with the ledger's own
        validator before it measures anything, and keeps none of an invalid copy (here: each copy's receipt
        computed wrongly)."""
        harness = self.drained_for_harness()
        count = self.ledger._connection.execute("SELECT COUNT(*) FROM run_holds").fetchone()[0]
        with mock.patch.object(harness, "content_digest", return_value="0" * 64), \
                self.assertRaisesRegex(AssertionError, "not valid ledger history"):
            harness.add_expired_claimed_runs(self, 2, self.case.stamp - 1)
        self.assertFalse(self.ledger._connection.in_transaction)
        self.assertEqual(count, self.ledger._connection.execute("SELECT COUNT(*) FROM run_holds").fetchone()[0])

    def test_RS_A_G8_the_benchmark_s_history_copies_are_valid_ledger_history(self):
        """Round 3 (review 040-rsa-code-r3-3): the budget harness's history builder, on one drained run copied
        twice, writes decisions whose evidence hashes to their receipt and to their slot's release digest; the
        result passes the real validator, both backup formats and a reopen (as its start-up measure needs)."""
        import json
        from codex_agents.learning_contracts import content_digest
        harness = self.drained_for_harness()
        before = {row["handle_digest"] for row in self.rows()}
        harness.add_expired_claimed_runs(self, 2, self.case.stamp - 1)
        copies = self.ledger._connection.execute(
            "SELECT a.handle_digest, a.release_digest, r.receipt, r.evidence, r.release_kind FROM active_run_capacity a "
            "JOIN run_holds r ON r.project_id=a.project_id AND r.kind='run_slot' AND r.identity=a.handle_digest"
        ).fetchall()
        copies = [row for row in copies if row["handle_digest"] not in before]
        self.assertEqual(2, len(copies))
        for row in copies:
            self.assertEqual("crashed", row["release_kind"])
            self.assertEqual(row["receipt"], content_digest(json.loads(row["evidence"])))
            self.assertEqual(row["receipt"], row["release_digest"])
        self.accepted()


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited capacity tests run in test_active_run_capacity."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(RunHoldTests):
        if name in RunHoldTests.__dict__:
            suite.addTest(RunHoldTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
