"""One project's learning history moves from the legacy ledger to its project store."""
from __future__ import annotations

import json
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_cli as cli_fixture
from tests import test_learning_source_image_legacy as legacy
from tests.learning_support import verification_outcome
from tests.test_project_learning_live import fixture_record, full_report, run_cycle, stored

from codex_agents import learning_migration as migration
from codex_agents import learning_source_image as image
from codex_agents import learning_store
from codex_agents import project_learning_live as live
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_store import LEARNING_MOVED_ERROR, LearningState, LedgerError
from codex_agents.project_service import history_directory
from codex_agents.project_history import ProjectHistory
from codex_agents.project_learning_activation import activation, staged_imports
from codex_agents.result_contracts import ResultCapacity


def now_ms():
    return time.time_ns() // 1_000_000


class LearningMigrationTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def migrate(self):
        return migration.migrate_project(self.source, self.target, self.project, now_ms=now_ms())

    def status(self):
        return migration.migration_status(self.source, self.target, self.project)

    def rows(self, sql, *parameters, store=None):
        connection = (self.target if store is None else store)._connection
        return [tuple(row) for row in connection.execute(sql, parameters)]

    def partition(self):
        body = learning_store._snapshot_project_partition(
            self.source._connection, self.project, ledger_schema_version=13)
        body["tables"].pop("stored_files")  # The migration's own sealed backup is registered here.
        body["counts"].pop("stored_files")
        return body

    def reopen(self):
        self.target.close()
        directory = Path(self.fixture.temporary.name) / "image-target-history"
        self.target = ProjectHistory(directory, self.project, result_key=self.key,
            result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000, 120_000), allow_schema_upgrade=True)
        self.addCleanup(self.target.close)

    def test_migration_copies_signed_bytes_retires_the_ledger_and_is_repeatable(self):
        self.assertEqual("pending", self.status()["state"])
        before = self.partition()
        ledger_records = self.rows(
            "SELECT record_id,content FROM records WHERE project_id=? ORDER BY source_sequence",
            self.project, store=self.source)
        signatures = self.rows(
            "SELECT subject_record_id,signature FROM owner_attestations WHERE project_id=? "
            "ORDER BY subject_record_id", self.project, store=self.source)
        report = self.migrate()
        self.assertEqual("complete", report["state"])
        self.assertEqual(self.project, report["project_id"])
        self.assertTrue(report["ledger_retired"])
        self.assertEqual((23, 1), (report["imported_records"], report["imported_qa"]))
        # Every record and every owner signature is in the project store byte for byte.
        self.assertEqual(ledger_records, [(a, bytes(b)) for a, b in self.rows(
            "SELECT record_id,content FROM project_learning_records WHERE import_id=? ORDER BY source_sequence",
            report["import_id"])])
        self.assertEqual(2, len(signatures))
        self.assertEqual([(a, bytes(b)) for a, b in signatures], [(a, bytes(b)) for a, b in self.rows(
            "SELECT subject_record_id,signature FROM project_learning_attestations WHERE import_id=? "
            "ORDER BY subject_record_id", report["import_id"])])
        # The ledger's own rows for the project, account rows included, are untouched.
        self.assertEqual(before, self.partition())
        changes = (self.source._connection.total_changes, self.target._connection.total_changes)
        self.assertEqual(report, self.migrate())
        self.assertEqual(report, self.status())
        self.assertEqual(changes, (self.source._connection.total_changes, self.target._connection.total_changes))
        self.reopen()
        self.assertEqual(report, self.status())

    def test_retired_ledger_refuses_learning_records_but_not_other_work(self):
        outcome = verification_outcome("after-move")
        self.migrate()
        with self.assertRaisesRegex(LedgerError, LEARNING_MOVED_ERROR):
            self.fixture._admit_outcome(self.project, outcome, now_ms=now_ms())
        with self.assertRaisesRegex(LedgerError, LEARNING_MOVED_ERROR):
            self.source.run_learning_cycle(self.project, as_of_ms=now_ms())
        with self.source._lock, self.source._connection:
            for kind in sorted(learning_store.LEARNING_MOVED_TYPES):
                with self.subTest(kind=kind), self.assertRaisesRegex(LedgerError, LEARNING_MOVED_ERROR):
                    self.source._append_checked_locked(
                        self.project, {"record_type": kind, "record_id": "x"}, producer="evaluator", timestamp=1)
        # Another project still learns in the ledger, and this one is still registered.
        other = verification_outcome("other-project")
        self.fixture._admit_outcome(self.fixture.project_b_id, other, now_ms=now_ms())
        self.assertFalse(self.source.learning_moved(self.fixture.project_b_id))
        self.assertEqual(self.project, self.source.project_for_root(self.fixture.project_a))

    def test_live_cycle_reads_the_imported_history_with_the_live_records(self):
        # The legacy fixture's records carry small synthetic times; learn at that time.
        learning = live.ProjectLearning(clock_ms=lambda: 500)
        self.addCleanup(learning.close)
        fixture_record(self.target, verification_outcome("live-one"), "verifier", 400)
        self.assertEqual("ready", run_cycle(learning, self.target, "before")["state"])
        alone = full_report(learning, "before")
        self.assertEqual(1, len(alone["mined_case_ids"]))
        self.assertEqual(0, alone["analysis"]["owner_feedback_excluded"])
        self.migrate()
        view = run_cycle(learning, self.target, "after")
        self.assertEqual("ready", view["state"])
        joined = full_report(learning, "after")
        # The ledger's owner feedback now excludes its run here too, and nothing
        # the ledger already mined is mined again.
        self.assertGreater(joined["analysis"]["owner_feedback_excluded"], 0)
        self.assertEqual([], joined["mined_case_ids"])
        self.assertEqual(1, len(stored(self.target, "evaluation_case")))

    def test_failure_after_staging_changes_nothing_and_the_next_run_finishes(self):
        with mock.patch.object(self.source, "set_learning_moved", side_effect=LedgerError("stopped")), \
                self.assertRaises(LedgerError):
            self.migrate()
        self.assertEqual("pending", self.status()["state"])
        self.assertFalse(self.source.learning_moved(self.project))
        self.assertIsNone(activation(self.target))
        self.assertEqual(1, len(staged_imports(self.target)))
        self.reopen()
        report = self.migrate()
        self.assertEqual("complete", report["state"])
        self.assertEqual((report["import_id"],), staged_imports(self.target))

    def test_failure_after_retirement_is_finished_without_copying_again(self):
        with mock.patch.object(migration, "activate", side_effect=HistoryError("history_unavailable")), \
                self.assertRaises(migration.MigrationError):
            self.migrate()
        self.assertTrue(self.source.learning_moved(self.project))
        self.assertIsNone(activation(self.target))
        self.assertEqual("pending", self.status()["state"])
        self.reopen()
        with mock.patch.object(migration, "stage_source", side_effect=AssertionError("copied again")):
            report = self.migrate()
        self.assertEqual("complete", report["state"])
        self.assertEqual(23, report["imported_records"])

    def test_undrained_ledger_is_refused_and_left_as_it_was(self):
        with mock.patch.object(image, "_drained", side_effect=HistoryError("history_unavailable")), \
                self.assertRaises(migration.MigrationError) as refused:
            self.migrate()
        self.assertEqual("migration_not_drained", refused.exception.reason_code)
        self.assertFalse(self.source.learning_moved(self.project))
        self.assertIsNone(activation(self.target))
        self.assertEqual("complete", self.migrate()["state"])

    def test_rollback_restores_the_ledger_until_new_learning_is_written(self):
        report = self.migrate()
        rolled = migration.rollback_project(self.source, self.target, self.project)
        self.assertEqual("rolled_back", rolled["state"])
        self.assertFalse(rolled["ledger_retired"])
        self.assertIsNone(activation(self.target))
        self.fixture._admit_outcome(self.project, verification_outcome("back-in-ledger"), now_ms=now_ms())
        again = self.migrate()
        self.assertEqual("complete", again["state"])
        self.assertGreater(again["imported_records"], report["imported_records"])
        # Learning written after the cutover may rest on the imported history.
        learning = live.ProjectLearning(clock_ms=now_ms)
        self.addCleanup(learning.close)
        fixture_record(self.target, verification_outcome("live-after"), "verifier", now_ms())
        self.assertEqual("ready", run_cycle(learning, self.target, "one")["state"])
        with self.assertRaises(migration.MigrationError) as refused:
            migration.rollback_project(self.source, self.target, self.project)
        self.assertEqual("migration_reconciliation_required", refused.exception.reason_code)
        self.assertTrue(self.source.learning_moved(self.project))
        self.assertEqual(again, self.status())

    def test_project_without_learning_history_only_retires_the_ledger(self):
        other = self.fixture.project_b_id
        directory = Path(self.fixture.temporary.name) / "empty-target-history"
        directory.mkdir(mode=0o700)
        target = ProjectHistory(directory, other, result_key=self.key,
            result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000, 120_000), allow_schema_upgrade=True)
        self.addCleanup(target.close)
        self.assertEqual("not_needed", migration.migration_status(self.source, target, other)["state"])
        report = migration.migrate_project(self.source, target, other, now_ms=now_ms())
        self.assertEqual(("complete", True, None, 0), (
            report["state"], report["ledger_retired"], report["import_id"], report["imported_records"]))
        self.assertEqual((), staged_imports(target))
        self.assertFalse(self.source.learning_moved(self.project))

    def test_cutover_needs_the_offline_owners(self):
        self.source._collector_active = True
        try:
            with self.assertRaisesRegex(LedgerError, "offline owner"):
                self.source.set_learning_moved(self.project, True)
        finally:
            self.source._collector_active = False
        self.target._allow_schema_upgrade = False
        with self.assertRaises(migration.MigrationError) as refused:
            self.migrate()
        self.assertEqual("migration_not_authorized", refused.exception.reason_code)
        self.assertFalse(self.source.learning_moved(self.project))


class MigrationCliTests(unittest.TestCase):
    """The owner's command against a disposable state root, as a separate process."""

    def test_command_moves_reports_refuses_and_rolls_back(self):
        self.assertEqual("pending", self._run("learning-migrate-project", "--status")["state"])
        # The project's own service holds its store: nothing is changed.
        directory = history_directory(LearningState(self.state_root, create=False), self.project_id)
        directory.parent.mkdir(mode=0o700, exist_ok=True)
        directory.mkdir(mode=0o700, exist_ok=True)
        held = ProjectHistory(directory, self.project_id)
        try:
            refused = self._run("learning-migrate-project", expected=2)
        finally:
            held.close()
        self.assertEqual("migration_project_service_running", refused["reason_code"])
        self.assertIn("nothing was changed", refused["error"])
        self.assertEqual("pending", self._run("learning-migrate-project", "--status")["state"])
        moved = self._run("learning-migrate-project")
        self.assertEqual(("complete", True, self.project_id), (
            moved["state"], moved["ledger_retired"], moved["project_id"]))
        self.assertGreater(moved["imported_records"], 0)
        self.assertEqual(moved, self._run("learning-migrate-project"))
        self.assertEqual("complete", self._run("learning-migrate-project", "--status")["state"])
        # The old commands say where the history went instead of writing beside it.
        refused = self._run("learning-run-cycle", expected=2)
        self.assertIn("project learning has moved to its project store", json.dumps(refused))
        rolled = self._run("learning-migrate-project", "--rollback")
        self.assertEqual(("rolled_back", False), (rolled["state"], rolled["ledger_retired"]))
        self.assertTrue(self._run("learning-run-cycle")["ready"])


for _name in dir(cli_fixture.LearningCliTests):
    if not _name.startswith("test") and not _name.startswith("__") and _name not in {"run", "id"} and (
            _name in {"setUp", "tearDown"} or _name.startswith("_")) and _name in vars(cli_fixture.LearningCliTests):
        setattr(MigrationCliTests, _name, vars(cli_fixture.LearningCliTests)[_name])


class ActivationSchemaTests(unittest.TestCase):
    def test_schema15_store_gains_the_cutover_table_only_by_explicit_upgrade(self):
        from contextlib import closing
        import tempfile

        from codex_agents import project_history as history
        from tests.support import TEMP_ROOT

        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "old15"
        directory.mkdir(mode=0o700)
        database = directory / "history.sqlite3"
        project = "prj_" + "a" * 32
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.executescript(history._SCHEMA_V15)
            connection.execute("INSERT INTO history_identity VALUES(?,7,3)", (project,))
            connection.execute(f"PRAGMA application_id={history._APPLICATION_ID}")
            connection.execute("PRAGMA user_version=15")
        database.chmod(0o600)
        with self.assertRaisesRegex(HistoryError, "^history_incompatible$"):
            ProjectHistory(directory, project)
        store = ProjectHistory(directory, project, allow_schema_upgrade=True)
        try:
            self.assertEqual(16, store._connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual([], store._connection.execute("SELECT * FROM project_learning_activation").fetchall())
            self.assertEqual((7, 3), tuple(store._connection.execute(
                "SELECT next_source_sequence,next_delivery_sequence FROM history_identity").fetchone()))
        finally:
            store.close()
        ProjectHistory(directory, project).close()


class DrainedRuleTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE host_run_bindings(project_id,handle_digest,binding_state);"
            "CREATE TABLE active_run_capacity(project_id,handle_digest,deadline_at_ms,released_at_ms);")
        self.now = 10 * learning_store.CRASHED_CLAIM_GRACE_MS

    def claim(self, name, *, deadline, released=None, binding="pending"):
        self.connection.execute("INSERT INTO host_run_bindings VALUES('p',?,?)", (name, binding))
        self.connection.execute("INSERT INTO active_run_capacity VALUES('p',?,?,?)", (name, deadline, released))

    def test_settled_and_long_crashed_runs_do_not_block(self):
        image._drained(self.connection, self.now)
        self.claim("done", deadline=self.now, released=self.now, binding="terminal")
        self.claim("driven-by-the-project-service", deadline=self.now, released=self.now)
        self.claim("crashed", deadline=self.now - learning_store.CRASHED_CLAIM_GRACE_MS - 1)
        image._drained(self.connection, self.now)

    def test_running_recent_and_unaccounted_runs_block(self):
        for name, arguments in (("running", {"deadline": self.now + 1}),
                                ("recent", {"deadline": self.now - 1}),
                                ("open-ended", {"deadline": None})):
            with self.subTest(name=name):
                self.connection.execute("DELETE FROM host_run_bindings")
                self.connection.execute("DELETE FROM active_run_capacity")
                self.claim(name, **arguments)
                with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                    image._drained(self.connection, self.now)
        self.connection.execute("DELETE FROM active_run_capacity")
        with self.assertRaisesRegex(HistoryError, "history_unavailable"):
            image._drained(self.connection, self.now)  # A pending binding with no claim at all.


if __name__ == "__main__":
    unittest.main()
