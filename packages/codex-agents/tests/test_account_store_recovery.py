"""Disposable account-store recovery; never touch a registered owner store."""
from pathlib import Path
import os
import subprocess
import sys
import sqlite3
import time
import unittest
from unittest import mock

import tests.test_learning_store as fixture_module
from codex_agents.learning_store import LearningLedger, LearningState, LedgerError, StateError
from tests.support import REPOSITORY_ROOT


class AccountStoreRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.LearningStoreTests("test_registration_and_scoped_reads_are_partitioned")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.state, self.ledger = self.fixture.state, self.fixture.ledger

    def backup(self):
        path = self.ledger.create_sanitized_backup(self.fixture.project_a_id, file_id="recovery_backup",
                                                   expires_at_ms=time.time_ns() // 1_000_000 + 60_000)
        self.assertTrue(self.ledger.verify_sanitized_backup(self.fixture.project_a_id, file_id="recovery_backup")["verified"])
        return path, path.read_bytes()

    def assert_reopen_refuses_and_preserves_backup(self, *, empty=False, same_state=False):
        path, contents = self.backup()
        key = self.state.key_path.read_bytes()
        self.ledger.close()
        if empty:
            self.state.database.write_bytes(b"")
        else:
            self.state.database.unlink()
        reopened = None
        try:
            with self.assertRaises((StateError, LedgerError, sqlite3.Error)):
                state = self.state if same_state else LearningState(self.state.root)
                reopened = LearningLedger(state, client_capabilities=self.fixture.clients)
        finally:
            if reopened is not None:
                reopened.close()
        self.assertTrue(path.is_file(), "reopen destroyed the surviving recovery backup")
        self.assertEqual(contents, path.read_bytes())
        self.assertEqual(key, self.state.key_path.read_bytes())
        if empty:
            self.assertEqual(0, self.state.database.stat().st_size)
        else:
            self.assertFalse(self.state.database.exists(), "reopen silently recreated an initialized database")

    def test_missing_initialized_database_refuses_without_erasing_backup(self):
        self.assert_reopen_refuses_and_preserves_backup()

    def test_empty_initialized_database_refuses_without_erasing_backup(self):
        self.assert_reopen_refuses_and_preserves_backup(empty=True)

    def test_database_lost_after_state_construction_is_not_created_by_connect(self):
        self.assert_reopen_refuses_and_preserves_backup(same_state=True)

    def test_legacy_initialized_store_without_markers_also_refuses_database_loss(self):
        self.state.initialized.unlink()
        self.assert_reopen_refuses_and_preserves_backup()

    def test_missing_digest_key_is_never_replaced_for_existing_database(self):
        self.ledger.close()
        self.state.key_path.unlink()
        with self.assertRaisesRegex(StateError, "authority file is unavailable"):
            LearningState(self.state.root)
        self.assertFalse(self.state.key_path.exists())

    def test_initialized_marker_dominates_leftover_initializing_marker(self):
        self.state._write_marker(self.state.initializing)
        self.assert_reopen_refuses_and_preserves_backup()

    def test_database_replaced_after_availability_check_is_not_initialized(self):
        path, contents = self.backup()
        self.ledger.close()
        prepare = self.state._prepare_database

        def replace(*, create):
            prepare(create=create)
            self.state.database.write_bytes(b"")

        with mock.patch.object(self.state, "_prepare_database", side_effect=replace), \
                self.assertRaisesRegex(StateError, "verified recovery is required"):
            LearningLedger(self.state, client_capabilities=self.fixture.clients)
        self.assertEqual(contents, path.read_bytes())
        self.assertEqual((0, []), self.version_and_tables(self.state))

    def test_unregistered_backup_is_preserved_without_becoming_trusted(self):
        path = self.ledger._write_owner_file("backup", self.fixture.project_a_id, "orphan_backup", b"unverified recovery material")
        self.ledger.close()
        reopened = LearningLedger(self.state, client_capabilities=self.fixture.clients)
        self.addCleanup(reopened.close)
        self.assertTrue(path.is_file(), "startup deleted an unregistered recovery backup")
        self.assertEqual(b"unverified recovery material", path.read_bytes())
        with self.assertRaisesRegex(LedgerError, "backup is unavailable"):
            reopened.verify_sanitized_backup(self.fixture.project_a_id, file_id="orphan_backup")

    def fresh_state(self):
        return LearningState(Path(self.fixture.temporary.name) / "new-state")

    def test_prepared_uninitialized_store_can_be_inspected_without_creating_files(self):
        state = self.fresh_state()
        before = sorted(path.name for path in state.root.iterdir())
        LearningState(state.root, create=False)
        self.assertEqual(before, sorted(path.name for path in state.root.iterdir()))
        ledger = LearningLedger(state)
        self.addCleanup(ledger.close)
        self.assertTrue(state.initialized.is_file())
        self.assertFalse(state.initializing.exists())

    def test_invalid_bootstrap_marker_is_refused_without_changing_empty_database(self):
        state = self.fresh_state()
        state.initializing.write_bytes(b"1\ntrailing")
        with self.assertRaisesRegex(StateError, "initialization marker is invalid"):
            LearningState(state.root)
        self.assertEqual(0, state.database.stat().st_size)
        state.initializing.unlink()
        state.initializing.symlink_to(state.key_path)
        with self.assertRaises(StateError):
            LearningState(state.root)

    def test_empty_legacy_store_with_authority_material_is_not_treated_as_new(self):
        state = self.fresh_state()
        state.initializing.unlink()
        with self.assertRaisesRegex(StateError, "verified recovery is required"):
            LearningState(state.root)
        self.assertEqual(0, state.database.stat().st_size)

    def version_and_tables(self, state):
        connection = sqlite3.connect(state.database.as_uri() + "?mode=ro", uri=True)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0], [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
        finally:
            connection.close()

    def crash_marker_creation(self, root, marker):
        script = """
import os, sys
from pathlib import Path
import tests.support
from codex_agents.learning_store import LearningLedger, LearningState
root, marker = Path(sys.argv[1]), sys.argv[2]
fdopen = os.fdopen
def crash(descriptor, *args, **kwargs):
    info = os.fstat(descriptor)
    for path in (root / marker, *root.glob('.' + marker + '.*.tmp')):
        if path.exists() and path.stat().st_ino == info.st_ino:
            os._exit(73)
    return fdopen(descriptor, *args, **kwargs)
os.fdopen = crash
LearningLedger(LearningState(root))
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(root), marker],
                                cwd=REPOSITORY_ROOT, capture_output=True, timeout=20)
        self.assertEqual(73, result.returncode, result.stderr.decode(errors="replace"))

    @unittest.skipUnless(os.name == "posix", "requires POSIX process exit")
    def test_process_death_before_initialized_marker_contents_can_reopen_committed_store(self):
        state = self.fresh_state()
        self.crash_marker_creation(state.root, "ledger-initialized")
        reopened = LearningLedger(LearningState(state.root))
        self.addCleanup(reopened.close)
        self.assertEqual(8, reopened._connection.execute("PRAGMA user_version").fetchone()[0])

    @unittest.skipUnless(os.name == "posix", "requires POSIX process exit")
    def test_process_death_before_bootstrap_marker_contents_can_resume_empty_root(self):
        root = Path(self.fixture.temporary.name) / "interrupted-bootstrap"
        self.crash_marker_creation(root, "ledger-initializing")
        reopened = LearningLedger(LearningState(root))
        self.addCleanup(reopened.close)
        self.assertEqual(8, reopened._connection.execute("PRAGMA user_version").fetchone()[0])

    def test_interrupted_fresh_initialization_rolls_back_all_schema_and_reopens(self):
        class FixtureStop(BaseException):
            pass

        state = self.fresh_state()
        initialize = LearningLedger._initialize_launch_operations_locked

        def interrupted(ledger):
            initialize(ledger)
            raise FixtureStop()

        with mock.patch.object(LearningLedger, "_initialize_launch_operations_locked", autospec=True, side_effect=interrupted), \
                self.assertRaises(FixtureStop):
            LearningLedger(state)
        self.assertEqual((0, []), self.version_and_tables(state))
        reopened = LearningLedger(LearningState(state.root))
        self.addCleanup(reopened.close)
        self.assertEqual(8, reopened._connection.execute("PRAGMA user_version").fetchone()[0])

    @unittest.skipUnless(os.name == "posix", "requires POSIX process exit")
    def test_process_death_during_initial_schema_recovers_without_partial_tables(self):
        state = self.fresh_state()
        script = """
import os, sys
from pathlib import Path
import tests.support
from codex_agents.learning_store import LearningLedger, LearningState
initialize = LearningLedger._initialize_launch_operations_locked
def die(ledger):
    initialize(ledger)
    os._exit(73)
LearningLedger._initialize_launch_operations_locked = die
LearningLedger(LearningState(Path(sys.argv[1])))
"""
        result = subprocess.run([sys.executable, "-B", "-c", script, str(state.root)],
                                cwd=REPOSITORY_ROOT, capture_output=True, timeout=20)
        self.assertEqual(73, result.returncode, result.stderr.decode(errors="replace"))
        self.assertEqual((0, []), self.version_and_tables(state))
        reopened = LearningLedger(LearningState(state.root))
        self.addCleanup(reopened.close)
        self.assertEqual(8, reopened._connection.execute("PRAGMA user_version").fetchone()[0])

    def test_fresh_schema_mode_failure_rolls_back_before_commit_and_can_resume(self):
        state = self.fresh_state()
        harden = state.harden_database_files
        initialize = LearningLedger._initialize_launch_operations_locked
        active, observed = [], []

        def capture(ledger):
            initialize(ledger)
            active.append(ledger)

        def refused():
            if active and active[0]._connection.in_transaction:
                observed.append(True)
                raise StateError("fixture schema precommit refusal")
            harden()

        created = None
        try:
            with mock.patch.object(LearningLedger, "_initialize_launch_operations_locked", autospec=True, side_effect=capture), \
                    mock.patch.object(state, "harden_database_files", side_effect=refused), \
                    self.assertRaisesRegex(StateError, "fixture schema precommit refusal"):
                created = LearningLedger(state)
        finally:
            if created is not None:
                created.close()
        self.assertEqual([True], observed)
        self.assertEqual((0, []), self.version_and_tables(state))
        reopened = LearningLedger(LearningState(state.root))
        self.addCleanup(reopened.close)
        self.assertEqual(8, reopened._connection.execute("PRAGMA user_version").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
