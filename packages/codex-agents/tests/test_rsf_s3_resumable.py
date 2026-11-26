"""RSF-S3: real process death at SQLite commit boundaries on disposable stores (the kill-boundary tests are split into test_rsf_s3_resumable_*; shared fixtures in rsf_s3_support)."""
from contextlib import closing
import json
import shutil
import sqlite3
from unittest import mock

from codex_agents import learning_store, project_history, store_fence
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_store import LedgerError
from tests.rsf_s3_support import ACCOUNT_CLASSES, NOW, PROJECT_CLASSES, ResumableSupport, database, open_owner, worker  # noqa: F401
import tests.support  # noqa: F401
from test_framework import requirements


class ResumableMigrationTests(ResumableSupport):
    __why_not_scenario__ = 'Isolated persistence owners and exact subprocess deaths; no installed services.'

    @requirements('RSF-S3-3')
    def test_migration_context_is_reset_after_adoption_and_validation_exceptions(self):
        for kind in ('account', 'project'):
            source = self.account_fixture() if kind == 'account' else self.project_fixture()
            for stage in ('adoption', 'validation'):
                with self.subTest(kind=kind, stage=stage):
                    root = self.clone(source, kind + '-context-' + stage)
                    captured = []
                    module = learning_store if kind == 'account' else project_history.project_holds_schema
                    name = (('install_run_holds_schema' if kind == 'account' else 'adopt_project_holds')
                            if stage == 'adoption' else
                            ('check_run_holds_consistency' if kind == 'account' else 'register_consistent'))
                    original = getattr(module, name)
                    def fail(connection, *args, **kwargs):
                        if getattr(connection, '_hold_migration', None) is not None:
                            captured.append(connection)
                            if stage == 'validation':
                                return False
                            raise ValueError('adoption failure')
                        return original(connection, *args, **kwargs)
                    with mock.patch.object(module, name, fail), \
                            self.assertRaises((ValueError, LedgerError, HistoryError)):
                        self.reopen(root, kind)
                    self.assertTrue(captured)
                    for connection in captured:
                        self.assertIsNone(connection._hold_migration)
                    kept = []
                    if kind == 'project' and stage == 'validation':
                        self.assertIn('hold_migration_progress', self.snapshot(root, kind))
                        self.assertEqual(1, len(list((root / 'history').glob('.history.sqlite3.upgrade-23.*.attempt'))))
                        kept = [(path, path.read_bytes()) for path in
                                (root / 'history').glob('history.sqlite3.before-23.*')]
                        self.assertEqual(1, len(kept))
                    self.reopen(root, kind)
                    for path, before_bytes in kept:
                        self.assertEqual(before_bytes, path.read_bytes())

    @requirements('RSF-S3-3')
    def test_pending_refusal_gives_plain_next_step_and_update_command(self):
        for kind, target in (('account', ACCOUNT_CLASSES[0]), ('project', PROJECT_CLASSES[0])):
            with self.subTest(kind=kind):
                root = self.account_fixture() if kind == 'account' else self.project_fixture()
                self.kill_at(root, kind, 'after', target, 64)
                before = self.snapshot(root, kind)
                with self.assertRaises((LedgerError, HistoryError)) as refused:
                    open_owner(root, kind, authorized=False)
                self.assertIn('pending hold migration', str(refused.exception).lower())
                self.assertIn('Resume the drained upgrade', str(refused.exception))
                self.assertIn('Run: ./setup.sh --update', str(refused.exception))
                self.assertEqual(before, self.snapshot(root, kind))

    @requirements('RSF-S3-3')
    def test_project_resume_refuses_missing_kept_before_copy_without_writes(self):
        source = self.project_fixture()
        self.kill_at(source, 'project', 'after', PROJECT_CLASSES[0], 64)
        for missing in ('copy', 'marker', 'regular_file'):
            with self.subTest(missing=missing):
                root = self.clone(source, 'missing-' + missing)
                directory = root / 'history'
                marker, = directory.glob('.history.sqlite3.upgrade-23.*.attempt')
                copy = directory / json.loads(marker.read_text())['copy']
                recovery_bytes = copy.read_bytes()
                # A different copy cannot stand in for the original attempt's.
                decoy = directory / 'history.sqlite3.before-23.1'
                decoy.write_bytes(recovery_bytes)
                decoy.chmod(0o600)
                if missing == 'marker':
                    marker.unlink()
                else:
                    copy.unlink()
                    if missing == 'regular_file':
                        copy.symlink_to(decoy.name)
                before = self.snapshot(root, 'project')
                with closing(sqlite3.connect(database(root, 'project'))) as db:
                    epoch = db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0]
                with self.assertRaises(HistoryError) as refused:
                    self.reopen(root, 'project')
                self.assertIn('kept project before-copy', str(refused.exception))
                self.assertIn('Restore', str(refused.exception))
                self.assertIn('Run: ./setup.sh --update', str(refused.exception))
                self.assertEqual(before, self.snapshot(root, 'project'))
                with closing(sqlite3.connect(database(root, 'project'))) as db:
                    self.assertEqual(epoch, db.execute('SELECT epoch FROM runtime_takeover').fetchone()[0])
                if missing == 'marker':
                    original_marker, = (source / 'history').glob('.history.sqlite3.upgrade-23.*.attempt')
                    shutil.copy2(original_marker, marker)
                else:
                    if copy.is_symlink():
                        copy.unlink()
                    copy.write_bytes(recovery_bytes)
                    copy.chmod(0o600)
                self.reopen(root, 'project')
                self.assertNotIn('hold_migration_progress', self.snapshot(root, 'project'))

    @requirements('RSF-S3-1', 'RSF-S3-4')
    def test_resumed_classification_keeps_original_wall_and_monotonic_evidence(self):
        source = self.account_fixture(lost=True)
        uninterrupted = self.clone(source, 'clock-uninterrupted')
        self.reopen(uninterrupted, 'account')
        expected = self.snapshot(uninterrupted, 'account')
        self.kill_at(source, 'account', 'after', 'run_slot', 64)
        self.reopen(source, 'account', now=NOW + 50, monotonic=99)
        self.assertEqual(expected, self.snapshot(source, 'account'))
        self.reopen(source, 'account', now=NOW + 100, monotonic=199)
        self.assertEqual(expected, self.snapshot(source, 'account'))

    @requirements('RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4')
    def test_revoked_batch_and_failed_validation_keep_progress_unpublished(self):
        for kind in ('account', 'project'):
            source = self.account_fixture() if kind == 'account' else self.project_fixture()
            target = ACCOUNT_CLASSES[0] if kind == 'account' else PROJECT_CLASSES[0]
            self.kill_at(source, kind, 'after', target, 64)
            before = self.snapshot(source, kind)
            real = store_fence.StoreFence.check
            def revoke(fence, connection, *, full=True):
                if connection.in_transaction and sqlite3.Connection.execute(connection,
                        'SELECT MAX(cursor) FROM hold_migration_progress').fetchone()[0] > 64:
                    sqlite3.Connection.execute(connection, 'UPDATE runtime_takeover SET epoch=epoch+1')
                return real(fence, connection, full=full)
            with mock.patch.object(store_fence.StoreFence, 'check', revoke), \
                    self.assertRaises((LedgerError, HistoryError)):
                self.reopen(source, kind)
            self.assertEqual(before, self.snapshot(source, kind))
            validator = ('codex_agents.learning_store.check_run_holds_consistency' if kind == 'account'
                         else 'codex_agents.project_holds_schema.register_consistent')
            with mock.patch(validator, return_value=False), self.assertRaises((LedgerError, HistoryError)):
                self.reopen(source, kind)
            self.assertIn('hold_migration_progress', self.snapshot(source, kind))
            self.reopen(source, kind)
            self.assertNotIn('hold_migration_progress', self.snapshot(source, kind))
