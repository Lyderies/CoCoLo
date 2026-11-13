"""One project's learning history moves from the legacy ledger to its project store."""
from __future__ import annotations

import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_cli as cli_fixture
from tests import test_learning_source_image_legacy as legacy
from tests.learning_support import routing_fields, verification_outcome
from tests.test_project_learning_live import fixture_record, full_report, run_cycle, stored

from codex_agents import learning_migration as migration
from codex_agents import learning_source_image as image
from codex_agents import learning_store
from codex_agents import project_learning_live as live
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_store import LEARNING_MOVED_ERROR, LearningLedger, LearningState, LedgerError
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
        # Nor does its frozen copy vouch for routing evidence any more.
        cited = {"id": "task-1", "objective": "Use earlier reconnaissance", "family": "research",
                 "complexity": "extreme", "risk": "low", "meaningful": True,
                 **routing_fields(operation="end_to_end"), "deterministic_validation": True,
                 "independent_parts": 1, "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
                 "routing_evidence_refs": ["outcome:" + "a" * 64]}
        with self.assertRaisesRegex(LedgerError, LEARNING_MOVED_ERROR):
            self.source.trusted_routing_evidence_refs(self.project, cited)
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
        # The ledger itself says the copy is not switched on yet, so the running
        # services report it and the next start finishes it.
        self.assertTrue(self.source.learning_activation_pending(self.project))
        self.reopen()
        with mock.patch.object(migration, "stage_source", side_effect=AssertionError("copied again")):
            report = self.migrate()
        self.assertEqual("complete", report["state"])
        self.assertEqual(23, report["imported_records"])
        self.assertFalse(self.source.learning_activation_pending(self.project))
        self.assertEqual("complete", self.status()["state"])

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

    def test_owner_feedback_and_case_review_reach_the_imported_history(self):
        from codex_agents.learning_engine import build_feedback
        from codex_agents.project_learning_owner import (
            OwnerRecordRefused, corpus_records, record_feedback, rejected_run_ids, review_case,
        )

        self.migrate()
        already = rejected_run_ids(self.target)
        outcome = next(item for item in corpus_records(self.target, "outcome", 10_000)
                       if item["run_id"] not in already)
        record = build_feedback(digest_key=self.key, run_id=outcome["run_id"], label="rejected",
                                reason_code="incorrect", observed_at_ms=5)
        # The signature was checked by the service adapter; here it is kept with the record.
        self.assertTrue(record_feedback(self.target, record, b"signature", now_ms=400)["stored"])
        self.assertFalse(record_feedback(self.target, record, b"signature", now_ms=401)["stored"])
        self.assertEqual(already | {outcome["run_id"]}, rejected_run_ids(self.target))
        for changed, signature, reason in (
                (record, b"another signature", "owner_record_conflict"),
                (build_feedback(digest_key=self.key, run_id="run-nobody-ran", label="rejected",
                                reason_code="incorrect", observed_at_ms=5), b"signature", "feedback_run_unknown")):
            with self.subTest(reason=reason), self.assertRaises(OwnerRecordRefused) as refused:
                record_feedback(self.target, changed, signature, now_ms=402)
            self.assertEqual(reason, refused.exception.owner_reason)
        self.assertEqual([(record["record_id"], b"signature")], [(a, bytes(b)) for a, b in self.rows(
            "SELECT subject_record_id,signature FROM project_owner_attestations")])
        with self.assertRaises(HistoryError):
            record_feedback(self.target, outcome, b"signature", now_ms=403)  # Not a feedback record.
        # A development case the ledger mined is reviewed here, and the review is a live record.
        cases = corpus_records(self.target, "evaluation_case", 20_000)
        superseded = {case["supersedes_case_id"] for case in cases}
        sources = {item["record_id"]: item for item in corpus_records(self.target, "outcome", 10_000)}
        development = [case for case in cases if case["corpus_state"] == "development"
                       and case["record_id"] not in superseded
                       and sources[case["source_outcome_id"]]["run_id"] not in rejected_run_ids(self.target)]
        if development:
            arguments = {"review_digest": "d" * 64, "similarity_group_digest": "e" * 64,
                         "destination": "promotion_holdout", "freeze": True}
            reviewed = review_case(self.target, development[0]["record_id"], now_ms=404, **arguments)
            self.assertEqual((True, development[0]["record_id"]), (
                reviewed["stored"], reviewed["record"]["supersedes_case_id"]))
            self.assertFalse(review_case(self.target, development[0]["record_id"], now_ms=405, **arguments)["stored"])
            self.assertEqual([(reviewed["record"]["record_id"],)], self.rows(
                "SELECT record_id FROM records WHERE record_type='evaluation_case'"))
        with self.assertRaises(OwnerRecordRefused) as refused:
            review_case(self.target, "no-such-case", review_digest="d" * 64,
                        similarity_group_digest="e" * 64, destination="canary", freeze=False, now_ms=406)
        self.assertEqual("case_unknown", refused.exception.owner_reason)

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
        self.assertFalse(self.source.learning_activation_pending(other))
        # With nothing learned yet it can go straight back; once the project store
        # holds learning of its own, going back would leave that behind.
        self.assertEqual("rolled_back", migration.rollback_project(self.source, target, other)["state"])
        self.assertEqual("complete", migration.migrate_project(self.source, target, other, now_ms=now_ms())["state"])
        learning = live.ProjectLearning(clock_ms=now_ms)
        self.addCleanup(learning.close)
        fixture_record(target, verification_outcome("first-live"), "verifier", now_ms())
        self.assertEqual(1, run_cycle(learning, target, "one")["summary"]["mined_case_count"])
        with self.assertRaises(migration.MigrationError) as refused:
            migration.rollback_project(self.source, target, other)
        self.assertEqual("migration_reconciliation_required", refused.exception.reason_code)
        self.assertTrue(self.source.learning_moved(other))

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
        # Straight back: nothing was learned on the project store yet.
        rolled = self._run("learning-migrate-project", "--rollback")
        self.assertEqual(("rolled_back", False), (rolled["state"], rolled["ledger_retired"]))
        ledger_cycle = self._run("learning-run-cycle")
        self.assertTrue(ledger_cycle["ready"])
        self.assertEqual("complete", self._run("learning-migrate-project")["state"])
        # The owner's cycle now runs on the project's own service, over the imported
        # history; the command stops that idle service itself when it has to.
        store_cycle = self._run("learning-run-cycle")
        self.assertTrue(store_cycle["ready"])
        self.assertEqual(0, store_cycle["expired_records"])
        # (The fixture's outcomes carry a 1970 timestamp, long past retention.)
        self.assertEqual(set(ledger_cycle["analysis"]), set(store_cycle["analysis"]))
        refused = self._run("learning-mine-case", "--source-outcome-id", "x", "--task", "missing.json",
                            "--criteria-digest", "a" * 64, "--lineage-digest", "b" * 64, expected=2)
        self.assertFalse(refused["ready"])
        self.assertEqual("complete", self._run("learning-migrate-project")["state"])

    def test_first_start_of_the_project_service_moves_the_history_by_itself(self):
        self.assertEqual("pending", self._run("learning-migrate-project", "--status")["state"])
        opened = self._run("learning-open-orchestration")
        self.assertRegex(opened["orchestration_handle"], "^orc_[0-9a-f]{48}$")
        self.assertEqual("complete", self._run("learning-status")["learning_migration"])
        # Its service is running and the history is in its store: the owner's cycle reads it there.
        self.assertTrue(self._run("learning-run-cycle")["ready"])
        ledger_records = self._stopped_ledger_count("records")
        moved = self._run("learning-migrate-project")
        self.assertEqual(("complete", True), (moved["state"], moved["ledger_retired"]))
        self.assertGreater(moved["imported_records"], 0)
        self.assertLessEqual(moved["imported_records"], ledger_records)

    def _stopped_ledger_count(self, table):
        self._stop_collector()
        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            return ledger._connection.execute(
                "SELECT COUNT(*) FROM " + table + " WHERE project_id=?", (self.project_id,)).fetchone()[0]
        finally:
            ledger.close()


for _name in dir(cli_fixture.LearningCliTests):
    if not _name.startswith("test") and not _name.startswith("__") and _name not in {"run", "id"} and (
            _name in {"setUp", "tearDown"} or _name.startswith("_")) and _name in vars(cli_fixture.LearningCliTests):
        setattr(MigrationCliTests, _name, vars(cli_fixture.LearningCliTests)[_name])


class EmptyRetirementTests(unittest.TestCase):
    """A project with nothing to copy is retired by the running collector, but not from under a legacy run."""

    def setUp(self):
        from tests import test_learning_store as store_fixture

        self.fixture = store_fixture.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger, self.project = self.fixture.ledger, self.fixture.project_a_id

    def test_a_run_an_earlier_version_opened_keeps_the_ledger_until_it_settles(self):
        self.fixture._admit(self.project)
        self.assertFalse(self.ledger.has_learning_history(self.project))
        orchestration, opened = self.fixture._pending_upgrade_run()
        # Its outcome would still be recorded in the ledger.
        self.assertFalse(self.ledger.mark_learning_moved_if_empty(self.project))
        self.assertFalse(self.ledger.learning_moved(self.project))
        self.ledger.set_orchestration_paused(
            self.project, orchestration_handle=orchestration["orchestration_handle"], paused=True)
        self.ledger.classify_host_run_abandoned(
            self.project, run_handle=opened["run_handle"], reason="recovery_unavailable")
        self.assertTrue(self.ledger.mark_learning_moved_if_empty(self.project))
        self.assertTrue(self.ledger.mark_learning_moved_if_empty(self.project))  # Already moved.
        self.assertFalse(self.ledger.learning_activation_pending(self.project))
        # Another project is untouched, and one with history is never retired this way.
        other = self.fixture.project_b_id
        self.assertFalse(self.ledger.learning_moved(other))
        self.fixture._admit_outcome(other, verification_outcome("has-history"), now_ms=now_ms())
        self.assertFalse(self.ledger.mark_learning_moved_if_empty(other))


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
            "CREATE TABLE active_run_capacity(project_id,handle_digest,deadline_at_ms,released_at_ms,"
            "clock_reference_ns,execution_deadline_ns);")
        self.now = 10 * learning_store.CRASHED_CLAIM_GRACE_MS
        self.monotonic = self.now * 1_000_000  # Both clocks at the same instant, for simplicity.

    def claim(self, name, *, deadline, released=None, binding="pending", monotonic_deadline="same"):
        self.connection.execute("INSERT INTO host_run_bindings VALUES('p',?,?)", (name, binding))
        if monotonic_deadline == "same":
            monotonic_deadline = None if deadline is None else deadline * 1_000_000
        self.connection.execute("INSERT INTO active_run_capacity VALUES('p',?,?,?,?,?)", (
            name, deadline, released, None if monotonic_deadline is None else 1, monotonic_deadline))

    def drained(self):
        image._drained(self.connection, self.now, self.monotonic)

    def test_settled_and_long_crashed_runs_do_not_block(self):
        self.drained()
        self.claim("done", deadline=self.now, released=self.now, binding="terminal")
        self.claim("driven-by-the-project-service", deadline=self.now, released=self.now)
        self.claim("crashed", deadline=self.now - learning_store.CRASHED_CLAIM_GRACE_MS - 1)
        self.drained()

    def test_running_recent_and_unaccounted_runs_block(self):
        long_ago = self.now - learning_store.CRASHED_CLAIM_GRACE_MS - 1
        for name, arguments in (("running", {"deadline": self.now + 1}),
                                ("recent", {"deadline": self.now - 1}),
                                ("open-ended", {"deadline": None}),
                                # Past on the wall clock only: it may have been set forward.
                                ("wall-clock-only", {"deadline": long_ago, "monotonic_deadline": self.monotonic}),
                                ("no-monotonic-deadline", {"deadline": long_ago, "monotonic_deadline": None})):
            with self.subTest(name=name):
                self.connection.execute("DELETE FROM host_run_bindings")
                self.connection.execute("DELETE FROM active_run_capacity")
                self.claim(name, **arguments)
                with self.assertRaisesRegex(HistoryError, "history_unavailable"):
                    self.drained()
        self.connection.execute("DELETE FROM active_run_capacity")
        with self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.drained()  # A pending binding with no claim at all.


if __name__ == "__main__":
    unittest.main()
