"""Slice SB: the shared project-store schema step 17 -> 18 (DESIGN-0.4.0-RS-BD-shared.md SH-1 and SH-7).

RS-B X5 = RS-D M3' (one step: the four tables, both validators pass, a settlement row written and read back, a
fault inside leaves 17, a 17 binary refused), RS-D M2 (repeated, interrupted, failed backup, unauthorized, old
writer running, a 17 binary on an 18 store) and RS-B R3 (the validators refuse at open and at restore, changing
nothing). The register and custody rows are written here directly, as the later slices' writers (B1, D1 ...) will:
no behaviour writes the new tables yet, and nothing is adopted. Integration stage: real stores in temporary folders.
"""
from contextlib import closing, contextmanager
import fcntl
import os
import sqlite3
import stat
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_leases as leases
from tests.project_schema_fixtures import SCHEMA_18_TABLES, legacy_project_holds_layout
from tests.test_project_history import PROJECT_A, binding
from tests.test_project_orchestration import ProjectOrchestrationTests

from codex_agents import project_history
from codex_agents.history_contracts import HistoryError
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity

KEY = leases.fixtures.KEY


class StopFixture(BaseException):
    """A stop inside the step's transaction, after its writes and before its commit."""


class Cut(ProjectHistory):
    @contextmanager
    def _transaction(self):
        with super()._transaction():
            yield
            if self._connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='project_holds'").fetchone()[0]:
                raise StopFixture()


def attempt(number):
    return binding(number).run_handle, binding(number).attempt


class SchemaStepTests(unittest.TestCase):
    """An 18 store (or a 17 one) holding every source kind: an unarmed lease (1), a bound lease with its reserved
    reservation (2), a committed legacy reservation (3) and a prepared, uncommitted legacy reservation (4)."""

    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.fixture = self.base.fixture
        self.fixture.capacity = ResultCapacity(16, 8 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.reopen()
        self.directory = self.fixture.directory
        self.database = self.directory / 'history.sqlite3'
        self.base.reserve(1)
        self.base.reserve(2)
        self.store.arm_result_lease(leases.lease_binding(2))
        self.base.bind(2)
        self.fixture.reserve(3)
        self.fixture.commit(3)
        self.fixture.reserve(4)
        self.material = self.prepared(4)

    @property
    def store(self):
        return self.fixture.store

    def reopen(self, **changes):
        self.store.close()
        self.fixture.store = self.fixture.open(**changes)
        return self.store

    def prepared(self, number):
        """Prepare a publication whose commit fails: the reservation keeps its prepared digest, and the exact
        material it covers is captured as the store hashed it (RS-D draft 3 §3)."""
        hashed = []
        real = project_history.keyed_digest

        def recording(key, value):
            hashed.append(value)
            return real(key, value)
        with mock.patch.object(project_history, 'keyed_digest', recording), \
                mock.patch.object(self.store, '_insert_terminal', side_effect=RuntimeError('fixture cut')):
            with self.assertRaisesRegex(RuntimeError, 'fixture cut'):
                self.fixture.commit(number)
        digest = self.store._connection.execute('SELECT prepared_digest FROM result_reservations WHERE claim_id=?',
                                                (binding(number).claim_id,)).fetchone()[0]
        (material,) = [value for value in hashed if type(value) is bytes and real(KEY, value) == digest]
        return material

    # Snapshots ---------------------------------------------------------------------------------------------------

    def rows(self, path=None):
        with closing(sqlite3.connect(path or self.database)) as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: sorted((tuple(row) for row in db.execute('SELECT * FROM ' + name)), key=repr)
                    for name in names}

    def version(self, path=None):
        with closing(sqlite3.connect(path or self.database)) as db:
            return db.execute('PRAGMA user_version').fetchone()[0]

    def copies(self):
        """HS-10: the copies made so far for any step, never a temporary one."""
        return sorted(path for path in self.directory.glob('history.sqlite3.before-*')
                      if not path.name.endswith('.partial'))

    def format_17_store(self):
        legacy_project_holds_layout(self.store)
        self.store._connection.execute('PRAGMA user_version=17')
        self.store.close()
        return self.rows()

    def upgrade(self, store_class=ProjectHistory):
        self.fixture.store = store_class(
            self.directory, PROJECT_A, result_key=KEY, result_capacity=self.fixture.capacity,
            result_clock_ms=lambda: self.fixture.now, allow_legacy_orchestration_reference=True,
            allow_schema_upgrade=True)
        return self.store

    def assert_left_at_17(self, before):
        self.assertEqual(17, self.version())
        self.assertEqual(before, self.rows())  # the four tables absent, every other row as it was
        self.assertEqual([], self.copies())  # HS-18: the copy made for a failed step goes too
        self.assertFalse(list(self.directory.glob('*.partial')))

    # X5 = M3': the one step ---------------------------------------------------------------------------------------

    def test_X5_M3_one_step_creates_the_four_tables_adopts_nothing_and_both_validators_pass(self):
        from codex_agents import project_holds_schema
        before = self.format_17_store()
        db = self.upgrade()._connection
        self.assertEqual(18, db.execute('PRAGMA user_version').fetchone()[0])
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(set(SCHEMA_18_TABLES), tables)
        for name in SCHEMA_18_TABLES:
            self.assertEqual(project_holds_schema.PROJECT_HOLDS_COLUMNS[name],
                             tuple(row[1] for row in db.execute(f'PRAGMA table_info({name})')))
            if name not in {'project_holds', 'project_result_contexts'}:
                self.assertEqual(0, db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0])  # nothing adopted yet
        # (requirement) B1: the step adopts every lease and reservation (and nothing else) into the register.
        # (requirement) D1 (RS-D M1): and each reservation not yet expired (2, 3, 4) as a `legacy_no_context` context
        # with its held custody row; no context was ever stored for them, so none is invented.
        self.assertEqual([('lease', 2), ('reservation', 3), ('result_custody', 3)], [tuple(row) for row in db.execute(
            'SELECT kind,COUNT(*) FROM project_holds GROUP BY kind ORDER BY kind')])
        self.assertEqual([('legacy_no_context', 3)], [tuple(row) for row in db.execute(
            'SELECT state,COUNT(*) FROM project_result_contexts GROUP BY state')])
        indexes = {row[0]: row[1] for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL AND tbl_name IN "
            "('project_holds','project_result_contexts')")}
        for name in ('project_holds_due', 'project_holds_kind_due', 'project_result_contexts_live',
                     'project_result_contexts_outcome'):
            self.assertIn(' WHERE ', indexes[name])  # partial indexes: no tombstone is visited
        plan = ' '.join(str(row[-1]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT kind,identity FROM project_holds WHERE state<>'released' AND next_check_ms<=?",
            (0,)))
        self.assertIn('project_holds_due', plan)
        after = self.rows()
        self.assertEqual(before, {name: rows for name, rows in after.items() if name not in SCHEMA_18_TABLES})
        self.assertTrue(project_holds_schema.register_consistent(db))
        self.assertTrue(project_holds_schema.custody_consistent(db, KEY))

    def test_X5_M3_a_settlement_row_is_written_and_read_back(self):
        self.format_17_store()
        self.upgrade()
        run_handle, number = attempt(2)
        row = (PROJECT_A, run_handle, number, binding(2).claim_id, 'lost', b'{"closure":"fixture"}', 1500)
        columns = 'project_id,run_handle,attempt,claim_id,release_kind,closure,settled_at_ms'
        with self.store._transaction():
            self.store._connection.execute(f'INSERT INTO project_account_settlements({columns}) VALUES(?,?,?,?,?,?,?)',
                                           row)
        self.reopen()  # an ordinary open runs both validators over it
        self.assertEqual([row], [tuple(r) for r in self.store._connection.execute(
            f'SELECT {columns} FROM project_account_settlements')])

    def test_X5_M3_the_validators_run_inside_the_step_before_it_commits(self):
        from codex_agents import project_holds_schema
        self.format_17_store()
        seen = []
        real = project_holds_schema.register_consistent

        def watching(connection, *args):
            seen.append((connection.in_transaction, connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
                SCHEMA_18_TABLES).fetchone()[0]))
            return real(connection, *args)
        with mock.patch.object(project_holds_schema, 'register_consistent', watching):
            self.upgrade()
        self.assertEqual((True, 4), seen[0])  # inside the step's own transaction, the four tables present

    def test_X5_M3_a_failing_register_validator_inside_the_step_leaves_17(self):
        from codex_agents import project_holds_schema
        before = self.format_17_store()
        with mock.patch.object(project_holds_schema, 'register_consistent', return_value=False):
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.upgrade()
        self.assert_left_at_17(before)

    def test_X5_M3_a_failing_custody_validator_inside_the_step_leaves_17(self):
        from codex_agents import project_holds_schema
        before = self.format_17_store()
        with mock.patch.object(project_holds_schema, 'custody_consistent', return_value=False):
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.upgrade()
        self.assert_left_at_17(before)

    def test_X5_M3_a_broken_table_definition_leaves_17(self):
        before = self.format_17_store()
        with mock.patch.object(project_history, 'PROJECT_HOLDS_SCHEMA',
                               'CREATE TABLE project_holds(x); CREATE TABL broken(x)'):
            with self.assertRaises(HistoryError):
                self.upgrade()
        self.assert_left_at_17(before)

    def test_X5_M2_a_17_binary_refuses_an_18_store_and_changes_nothing(self):
        from codex_agents import project_holds_schema
        self.store.close()
        before = self.rows()
        older = {name: columns for name, columns in project_history._COLUMNS.items()
                 if name not in project_holds_schema.PROJECT_HOLDS_COLUMNS}
        with mock.patch.object(project_history, '_SCHEMA_VERSION', 17), \
                mock.patch.object(project_history, '_SCHEMA', project_history._SCHEMA_V17), \
                mock.patch.object(project_history, '_COLUMNS', older):
            for allowed in (False, True):
                with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                    self.fixture.open(allow_schema_upgrade=allowed)
        self.assertEqual(18, self.version())
        self.assertEqual(before, self.rows())
        self.assertEqual([], self.copies())

    # M2: the step's guards ---------------------------------------------------------------------------------------

    def test_M2_a_repeated_step_is_a_no_op(self):
        self.format_17_store()
        self.upgrade()
        (copy,) = self.copies()
        self.store.close()
        upgraded = self.rows()
        self.upgrade()
        self.assertEqual([copy], self.copies())  # no second copy: the store is already 18
        self.store.close()
        self.assertEqual(upgraded, self.rows())

    def test_M2_an_interrupted_step_rolls_back_to_17_and_a_later_attempt_upgrades(self):
        before = self.format_17_store()
        with self.assertRaises(StopFixture):
            self.upgrade(Cut)
        self.assert_left_at_17(before)
        self.upgrade()
        self.assertEqual(18, self.version())
        self.assertEqual(1, len(self.copies()))

    def test_M2_a_failed_backup_refuses_the_step(self):
        before = self.format_17_store()
        real = sqlite3.connect

        def connect(path, *args, **kwargs):
            if '.partial' in str(path):
                raise OSError('disk full')
            return real(path, *args, **kwargs)
        with mock.patch.object(project_history.sqlite3, 'connect', connect):
            with self.assertRaisesRegex(HistoryError, '^history_unavailable$'):
                self.upgrade()
        self.assert_left_at_17(before)

    def test_M2_an_unauthorized_open_refuses_a_17_store_and_writes_nothing(self):
        before = self.format_17_store()
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.fixture.open()
        self.assert_left_at_17(before)

    def test_M2_an_old_writer_still_running_excludes_the_step(self):
        before = self.format_17_store()
        lock = os.open(self.directory / 'history.lock', os.O_RDWR)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the old service still holds the writer lock
        with self.assertRaisesRegex(HistoryError, '^history_writer_busy$'):
            self.upgrade()
        self.assert_left_at_17(before)
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.upgrade()
        self.assertEqual(18, self.version())

    def test_M2_the_copy_keeps_the_17_store_as_it_was(self):
        before = self.format_17_store()
        self.upgrade()
        (copy,) = self.copies()
        self.assertTrue(copy.name.startswith('history.sqlite3.before-18.'))
        self.assertEqual(0o600, stat.S_IMODE(os.stat(copy).st_mode))
        self.assertEqual(17, self.version(copy))
        self.assertEqual(before, self.rows(copy))

    # Backups and restore carry the new tables ---------------------------------------------------------------------

    def hold(self, kind, identity, state='held', release_kind=None, evidence=None):
        # (requirement) B1: the store's own takes already wrote the lease and reservation rows; a row written here
        # replaces its (kind, identity) row, as a later slice's writer would leave it.
        return ('INSERT OR REPLACE INTO project_holds(project_id,kind,identity,state,instance,lease_case,taken_at_ms,'
                'release_kind,evidence) VALUES(?,?,?,?,?,?,?,?,?)',
                (PROJECT_A, kind, identity, state, 'instance-1' if kind in ('driver_entry', 'preparation') else None,
                 'unarmed' if kind == 'lease' else None, 1000, release_kind, evidence))

    def context(self, number, state, **values):
        run_handle, number = attempt(number)
        fields = {'project_id': PROJECT_A, 'run_handle': run_handle, 'attempt': number, 'state': state,
                  'context_digest': 'c' * 64, 'context_size': 10, 'host_class': 'codex-host-v1',
                  'lease_deadline_ms': 61_000, **values}
        return (f'INSERT INTO project_result_contexts({",".join(fields)}) VALUES({",".join("?" for _ in fields)})',
                tuple(fields.values()))

    def settlement(self, number, release_kind):
        run_handle, number_ = attempt(number)
        return ('INSERT INTO project_account_settlements(project_id,run_handle,attempt,claim_id,release_kind,closure,'
                'settled_at_ms) VALUES(?,?,?,?,?,?,?)',
                (PROJECT_A, run_handle, number_, binding(number).claim_id, release_kind, b'{"closure":1}', 1500))

    def publication(self, number, material):
        return ('INSERT INTO project_result_publications(project_id,claim_id,material) VALUES(?,?,?)',
                (PROJECT_A, binding(number).claim_id, material))

    def identity(self, number):
        from codex_agents.project_holds_schema import attempt_identity
        return attempt_identity(*attempt(number))

    def write(self, *statements, path=None):
        if not self.store._closed:
            self.store.close()
        with closing(sqlite3.connect(path or self.database)) as db, db:
            for sql, values in statements:
                db.execute(sql, values)

    def assert_accepted(self, *statements):
        from codex_agents import project_holds_schema
        self.write(*statements)
        db = self.reopen()._connection
        self.assertTrue(project_holds_schema.register_consistent(db))
        self.assertTrue(project_holds_schema.custody_consistent(db, KEY))

    def assert_refused(self, *statements):
        """R3 (U): the open is refused `history_incompatible`, with or without the upgrade path, and nothing in
        the store changes."""
        self.write(*statements)
        before = self.rows()
        for allowed in (False, True):
            with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
                self.fixture.open(allow_schema_upgrade=allowed)
        self.assertEqual(before, self.rows())
        self.assertEqual([], self.copies())

    def test_SB_a_copy_of_an_18_store_carries_the_new_tables_and_a_restore_opens_validated(self):
        from codex_agents import project_holds_schema
        self.write(self.hold('lease', self.identity(1)),
                   self.hold('lease', project_holds_schema.attempt_identity('hrn_' + 'e' * 48, 1), 'released',
                             'withdrawn_unsent'),
                   self.settlement(2, 'lost'))
        self.reopen()
        copy = self.store._keep_copy_before_upgrade(18)
        self.store.close()
        kept = self.rows()
        self.assertEqual({name: kept[name] for name in SCHEMA_18_TABLES},
                         {name: rows for name, rows in self.rows(copy).items() if name in SCHEMA_18_TABLES})
        # the tombstone, with the five rows B1's takes wrote (lease 1's replaced by the held row): (requirement) B1
        self.assertEqual(1 + 5, len(kept['project_holds']))
        os.replace(copy, self.database)  # the restore
        self.reopen()
        self.store.close()
        self.assertEqual(kept, self.rows())

    def test_R3_a_restored_copy_that_breaks_the_register_is_refused(self):
        self.reopen()
        copy = self.store._keep_copy_before_upgrade(18)
        self.write(self.hold('reservation', 'claim-none'), path=copy)  # a held row with no source
        os.replace(copy, self.database)
        self.assert_refused()

    # R3: the register validator (RS-B) ----------------------------------------------------------------------------

    def test_R3_held_rows_with_their_sources_and_released_tombstones_are_accepted(self):
        from codex_agents import project_holds_schema
        self.assert_accepted(
            self.hold('lease', self.identity(1)), self.hold('lease', self.identity(2), 'released', 'bound'),
            self.hold('reservation', binding(2).claim_id), self.hold('reservation', binding(3).claim_id),
            self.hold('lease', project_holds_schema.attempt_identity('hrn_' + 'e' * 48, 1), 'released', 'denied'),
            self.hold('reservation', 'claim-purged', 'released', 'completed'))

    def test_R3_a_held_lease_row_with_no_lease_is_refused(self):
        from codex_agents import project_holds_schema
        self.assert_refused(self.hold('lease', project_holds_schema.attempt_identity('hrn_' + 'e' * 48, 1)))

    def test_R3_a_held_lease_row_whose_identity_is_not_exact_is_refused(self):
        run_handle, number = attempt(1)
        self.assert_refused(self.hold('lease', f'{run_handle}:0{number}'))

    def test_R3_a_held_reservation_row_with_no_reservation_is_refused(self):
        self.assert_refused(self.hold('reservation', 'claim-none'))

    def test_R3_a_completed_reservation_with_no_release_ack_is_refused(self):
        self.assert_refused(self.hold('reservation', binding(3).claim_id, 'released', 'completed'))

    def test_R3_a_reserved_reservation_released_completed_is_refused(self):
        self.assert_refused(self.hold('reservation', binding(2).claim_id, 'released', 'completed'))

    def test_R3_a_reservation_released_by_settlement_needs_its_settlement_row(self):
        self.assert_refused(self.hold('reservation', binding(2).claim_id, 'released', 'lost'))

    def test_R3_a_reservation_released_with_another_settlement_kind_is_refused(self):
        self.assert_refused(self.hold('reservation', binding(2).claim_id, 'released', 'crashed'),
                            self.settlement(2, 'lost'))

    def test_R3_a_reservation_released_by_its_settlement_is_accepted(self):
        self.assert_accepted(self.hold('reservation', binding(2).claim_id, 'released', 'lost'),
                             self.settlement(2, 'lost'))

    def test_R3_a_held_source_with_no_register_row_is_refused(self):
        # B1: every lease and reservation now has its row from its take (or the step's adoption); without it the
        # open is refused, never silently adopted.
        self.assert_refused(("DELETE FROM project_holds WHERE kind='lease' AND identity=?", (self.identity(1),)))

    # SH-7: the custody validator (RS-D) and the joint check -------------------------------------------------------

    def test_SH7_a_context_with_its_custody_and_a_held_obligation_is_accepted(self):
        self.assert_accepted(self.context(1, 'leased'), self.hold('result_custody', self.identity(1)),
                             self.hold('lease', self.identity(1)),
                             self.context(2, 'bound', claim_id=binding(2).claim_id, claim_digest=binding(2).claim_digest),
                             self.hold('result_custody', self.identity(2)),
                             self.hold('reservation', binding(2).claim_id),
                             self.context(3, 'committed'), self.hold('result_custody', self.identity(3)))

    def test_SH7_a_context_with_no_custody_row_is_refused(self):
        self.assert_refused(self.context(1, 'leased'), self.hold('lease', self.identity(1)))

    def test_SH7_a_held_custody_row_with_no_context_is_refused(self):
        self.assert_refused(self.hold('result_custody', self.identity(1)))

    def test_SH7_an_ended_context_with_held_custody_is_refused(self):
        self.assert_refused(self.context(1, 'ended'), self.hold('result_custody', self.identity(1)))

    def withdrawn(self, number=6):
        """(requirement) B1: an attempt whose lease was really released `withdrawn_unsent` (no reservation): a
        lease row can no longer be released by hand while its lease is held (R3)."""
        self.base.reserve(number)
        self.store.release_unsent_result_lease(leases.lease_binding(number))
        return number

    def test_SH7_a_leased_context_with_no_held_obligation_is_refused(self):
        number = self.withdrawn()
        self.assert_refused(self.context(number, 'leased'), self.hold('result_custody', self.identity(number)))

    def test_SH7_a_committed_context_with_a_reserved_reservation_is_refused(self):
        self.assert_refused(self.context(2, 'committed'), self.hold('result_custody', self.identity(2)))

    def test_SH7_a_publication_of_its_prepared_material_is_accepted(self):
        self.assert_accepted(self.publication(4, self.material))

    def test_SH7_a_publication_whose_material_does_not_hash_to_the_prepared_digest_is_refused(self):
        self.assert_refused(self.publication(4, self.material + b' '))

    def test_SH7_a_publication_for_an_unprepared_or_committed_reservation_is_refused(self):
        for number in (2, 3):
            with self.subTest(number=number):
                self.assert_refused(self.publication(number, self.material))
                self.write(('DELETE FROM project_result_publications', ()))

    def test_SH7_joint_custody_released_with_its_lease_release_is_accepted(self):
        number = self.withdrawn()
        self.assert_accepted(self.context(number, 'retiring'),
                             self.hold('result_custody', self.identity(number), 'released', 'lost_with_reservation',
                                       evidence='{"cause":"lease"}'))

    def test_SH7_joint_custody_released_lost_with_reservation_and_no_release_cause_is_refused(self):
        self.assert_refused(self.context(1, 'retiring'),
                            self.hold('result_custody', self.identity(1), 'released', 'lost_with_reservation',
                                      evidence='{"cause":"lease"}'),
                            self.hold('lease', self.identity(1)))

    def test_SH7_joint_custody_released_lost_with_reservation_without_evidence_is_refused(self):
        number = self.withdrawn()
        self.assert_refused(self.context(number, 'retiring'),
                            self.hold('result_custody', self.identity(number), 'released', 'lost_with_reservation'))


class OrchestrationHoldTests(ProjectOrchestrationTests):
    """R3 for the orchestration kind: a held row has its orchestration."""

    def holds(self, handle):
        self.store.close()
        with closing(sqlite3.connect(self.directory / 'history.sqlite3')) as db, db:
            db.execute("INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms) "
                       "VALUES(?,'orchestration',?,'held',1000)", (PROJECT_A, handle))

    def test_R3_a_held_orchestration_row_with_its_orchestration_is_accepted(self):
        handle = self.opened()['orchestration_handle']
        self.holds(handle)
        self.store = self.open_history()

    def test_R3_a_held_orchestration_row_with_no_orchestration_is_refused(self):
        self.opened()
        self.holds('orc_' + 'f' * 48)
        with self.assertRaisesRegex(HistoryError, '^history_incompatible$'):
            self.open_history()


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited orchestration tests run in their own module."""
    suite = unittest.TestSuite()
    for case in (SchemaStepTests, OrchestrationHoldTests):
        for name in loader.getTestCaseNames(case):
            if name in case.__dict__:
                suite.addTest(case(name))
    return suite


if __name__ == '__main__':
    unittest.main()
