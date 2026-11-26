"""S9 review regressions with a real independent WAL writer, without sockets."""
from contextlib import closing
import ctypes.util
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from tests import test_health_ledger as fixtures
from codex_agents import health_ledger as hm
from test_framework import requirements

PROJECT = fixtures.PROJECT


class LedgerFixTests(unittest.TestCase):
    __why_not_scenario__ = "Real SQLite stores/independent writer children; synthetic account owner, no installed service or provider."
    setUp = fixtures.LedgerHealthTests.setUp
    context = fixtures.LedgerHealthTests.context

    def _paused_owner_copy(self):
        from tests.test_ledger_owner_wiring import LedgerOwnerWiringTests
        fixture = LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()
        check = hm.LedgerCheck(fixture.state, settings_path=self.settings)
        self.addCleanup(check.close)
        def prepare():
            db = fixture.owner.ledger._connection
            # A real outstanding WAL frame and more than one backup grant.
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE health_payload(value BLOB)')
            db.execute('INSERT INTO health_payload VALUES(zeroblob(?))', (1024 ** 2,))
            db.commit()
            with mock.patch.object(check, '_copy_slice', side_effect=lambda deadline: check._job.step(deadline)):
                check.visit(fixture.state.database, None, self.context())
            self.assertIsNotNone(check._job)
            self.assertFalse(check._job.done.is_set())
        fixture.owner.call(prepare, contain_errors=True)
        return fixture, check, check._job

    @requirements('S9-HM4-5')
    def test_project_deletion_cancels_snapshot_and_later_check_completes(self):
        fixture, check, snapshot = self._paused_owner_copy()
        started = time.monotonic()
        deletion = fixture.owner.client.delete_project(fixture.project)
        self.assertTrue(deletion.startswith('del_'))
        self.assertLess(time.monotonic() - started, .75)
        self.assertTrue(snapshot.stopped)
        self.assertTrue(snapshot.done.is_set(), 'read transaction must close before deletion')
        self.assertNotIn(None, check._last_full, 'cancellation is not integrity completion')
        with mock.patch.object(hm, 'Snapshot', wraps=hm.Snapshot) as copies:
            for n in range(30):
                check.run(self.context(1000 + n))
                if None in check._last_full:
                    break
            self.assertGreaterEqual(copies.call_count, 1, 'must restart rather than scan the cancelled copy')
        self.assertIn(None, check._last_full)
        self.assertEqual('ok', check._full_result[None])
        self.assertEqual([], fixture.errors)

    @requirements('S9-HM4-5')
    def test_any_exclusive_maintenance_releases_health_snapshot_on_owner(self):
        fixture, check, snapshot = self._paused_owner_copy()
        callers = []
        def maintenance():
            callers.append(threading.current_thread())
            ledger = fixture.owner.ledger
            ledger._begin_exclusive_maintenance()
            try:
                self.assertTrue(snapshot.done.is_set())
                ledger._connection.rollback()
            finally:
                ledger._finish_exclusive_maintenance()
        started = time.monotonic()
        fixture.owner.call(maintenance, contain_errors=True)
        self.assertLess(time.monotonic() - started, .75)
        self.assertEqual([fixture.thread], callers)
        self.assertTrue(snapshot.stopped)
        self.assertNotIn(None, check._last_full)

    @requirements('S9-HM4-5')
    def test_maintenance_releases_snapshot_while_cleanup_is_pending(self):
        fixture, check, snapshot = self._paused_owner_copy()
        cleanup_started, allow_cleanup = threading.Event(), threading.Event()
        original_close = snapshot.close
        def delayed_close():
            cleanup_started.set()
            allow_cleanup.wait(2)
            original_close()
        def maintenance():
            ledger = fixture.owner.ledger
            ledger._begin_exclusive_maintenance()
            try:
                self.assertTrue(snapshot.done.is_set())
                ledger._connection.rollback()
            finally:
                ledger._finish_exclusive_maintenance()
        with mock.patch.object(snapshot, 'close', side_effect=delayed_close):
            try:
                fixture.owner.call(check._clear_job, contain_errors=True)
                self.assertTrue(cleanup_started.wait(1))
                started = time.monotonic()
                fixture.owner.call(maintenance, contain_errors=True)
                self.assertLess(time.monotonic() - started, .75)
            finally:
                allow_cleanup.set()
                check._cleanup_thread.join(2)
        self.assertTrue(snapshot.stopped)
        self.assertNotIn(None, check._last_full)

    @requirements('S9-HM4-2')
    def test_sustained_growth_alarms_immediately_after_each_daily_renewal(self):
        self.check.measure(None, 100, 1000)
        for day in range(1, 4):
            for offset in (0, 1, 4 * 60000, hm.GROWTH_WINDOW_MS - 1, hm.GROWTH_WINDOW_MS):
                elapsed = day * hm.DAY_MS + offset
                size = 100 + 2 * hm.GIB * elapsed // hm.DAY_MS
                found = self.check.measure(None, size, 1000 + elapsed)
                self.assertIn('store_growth', [f.reason_code for f in found], (day, offset))

    @requirements('S9-HM4-1')
    def test_copy_full_and_io_errors_report_space_or_io_without_damage(self):
        for code in (sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR, sqlite3.SQLITE_IOERR_WRITE):
            with self.subTest(code=code):
                error = sqlite3.OperationalError('PRIVATE scratch path')
                error.sqlite_errorcode = code
                with mock.patch.object(hm.Snapshot, 'step', side_effect=error):
                    found = self.check.visit(self.database, None, self.context())
                self.assertEqual(['store_unreadable'], [f.reason_code for f in found])
                self.assertIn('space or IO', found[0].text)
                self.assertNotIn('damaged', found[0].text)
                self.assertNotIn('PRIVATE', str(found))
                self.assertNotIn(None, self.check._last_full)
                self.check.close()  # wait for scratch cleanup before the next injection

    @requirements('S9-HM4-5')
    def test_copy_never_loads_another_sqlite_library(self):
        spec = importlib.util.spec_from_file_location('codex_agents._s9_library_probe', hm.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.object(ctypes.util, 'find_library', side_effect=AssertionError('second SQLite lookup')), \
             mock.patch('ctypes.CDLL', side_effect=AssertionError('second SQLite load')):
            spec.loader.exec_module(module)
            snapshot = module.Snapshot(self.database, self.root / 'same-library.sqlite3')
            try:
                for _ in range(30):
                    if snapshot.step(time.monotonic() + .1):
                        break
                else:
                    self.fail('copy did not finish')
            finally:
                snapshot.close()

    @requirements('S9-HM4-1', 'S9-HM4-5')
    def test_300_mib_snapshot_and_scan_finish_under_process_writer(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE payload(value BLOB)')
            db.execute('INSERT INTO payload VALUES(zeroblob(?))', (300 * 1024 ** 2,))
            db.execute('CREATE TABLE ticks(value INTEGER)')
            db.execute('INSERT INTO ticks VALUES(0)')
            db.commit()
        writer = subprocess.Popen([sys.executable, '-B', '-c', '''
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1], timeout=2) as db:
    db.execute('PRAGMA wal_autocheckpoint=0')
    print('ready', flush=True)
    while True:
        db.execute('UPDATE ticks SET value=value+1')
        db.commit()
        time.sleep(.002)
''', str(self.database)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if writer.poll() is None:
                writer.terminate()
            writer.communicate(timeout=5)
        self.addCleanup(stop)
        self.assertEqual('ready', writer.stdout.readline().strip())
        holds = []
        self.owner.queue = type('Queue', (), {'snapshot': lambda _: {'retained': 2}})()
        for n in range(80):
            before = time.monotonic()
            found = self.check.run(self.context(1000 + n, seconds=.2))
            holds.append(time.monotonic() - before)
            if None in self.check._last_full:
                break
        self.assertIn(None, self.check._last_full, '300 MiB copy/scan failed to complete in 80 busy passes')
        self.assertLess(max(holds), .75)
        self.assertNotIn('quick_check_pending', [f.reason_code for f in found])
        self.assertEqual('ok', self.check._full_result[None])
        self.assertIsNone(writer.poll())
        stop()
        with closing(sqlite3.connect(self.database)) as db:
            ticks = db.execute('SELECT value FROM ticks').fetchone()[0]
            self.assertGreater(ticks, 10)
            print(f'300 MiB: {n + 1}/80 busy passes; max pass {max(holds):.3f}s; writer commits {ticks}')
            self.assertEqual(('ok',), db.execute('PRAGMA integrity_check').fetchone())

    @requirements('S9-HM4-5')
    def test_snapshot_close_preserves_owner_wal_write_lock(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            snapshot = hm.Snapshot(self.database, self.root / 'locked-copy.sqlite3')
            snapshot.step(time.monotonic() + .1)
            snapshot.close()
            probe = subprocess.run([sys.executable, '-B', '-c', '''
import sqlite3, sys
try:
    with sqlite3.connect(sys.argv[1], timeout=0) as db:
        db.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError:
    sys.exit(0)
sys.exit(1)
''', str(self.database)], capture_output=True, timeout=5)
            self.assertEqual(0, probe.returncode, probe.stderr)
            db.rollback()
            self.assertEqual(('ok',), db.execute('PRAGMA integrity_check').fetchone())

    @requirements('S9-HM4-2')
    def test_short_bursts_after_renewal_and_truncate_are_quiet(self):
        for project, start, end in ((None, 100, 100), (PROJECT, 10000000, 1)):
            self.check.measure(project, start, 1000)
            self.check.measure(project, end, 1000 + hm.DAY_MS)
            found = self.check.measure(project, end + 3600000, 1000 + hm.DAY_MS + 4 * 60000)
            self.assertNotIn('store_growth', [f.reason_code for f in found])
            found = self.check.measure(project, end + hm.GIB + 1, 1000 + 2 * hm.DAY_MS)
            self.assertIn('store_growth', [f.reason_code for f in found])

    @requirements('S9-HM4-2', 'S9-HM4-3')
    def test_future_baseline_is_reset_after_clock_correction(self):
        self.check.measure(None, 100, 5 * hm.DAY_MS)
        self.check.measure(None, 200, hm.DAY_MS)
        self.assertEqual((hm.DAY_MS, 200), self.check._baseline[None])
        found = self.check.measure(None, 200 + hm.GIB + 1, 2 * hm.DAY_MS)
        self.assertIn('store_growth', [f.reason_code for f in found])

    @requirements('S9-HM4-4')
    def test_shared_settings_and_initial_status(self):
        from codex_agents import user_settings
        path = self.root / 'user-settings.json'
        path.write_text(json.dumps({'store_size_bytes': 9 * hm.GIB}))
        check = hm.LedgerCheck(self.state)
        self.addCleanup(check.close)
        self.assertEqual(path, check.settings_path)
        self.assertIn('9 GiB', check.status_summary())
        settings = user_settings.load(self.root)
        self.assertEqual(9 * hm.GIB, settings.store_size_bytes)
        self.assertEqual(2 * hm.GIB, settings.disk_free_bytes)
        self.assertEqual(5, settings.disk_free_percent)
        self.assertEqual(5400000, settings.no_merge_warning_ms)
        self.assertIsNone(settings.warning)

    @requirements('S9-HM4-4')
    def test_one_byte_limits_rejected(self):
        for key in hm.DEFAULT_LIMITS:
            self.settings.write_text(json.dumps({key: 1}))
            self.check.reload_settings()
            self.assertTrue(self.check._settings_invalid)
            self.assertEqual(hm.DEFAULT_LIMITS, self.check.limits)

    @requirements('S9-HM4-5')
    def test_copy_deletion_and_scan_join_are_off_owner(self):
        owner = threading.current_thread()
        deleted = threading.Event()
        joined = threading.Event()
        def cleanup():
            if owner is threading.current_thread():
                raise AssertionError('copy deletion on owner')
            deleted.set()
        def close():
            if owner is threading.current_thread():
                raise AssertionError('scan join on owner')
            joined.set()
        self.check._temporary = type('Temp', (), {'cleanup': lambda _: cleanup()})()
        self.check._scan = type('Scan', (), {'close': lambda _: close(),
            'thread': type('Thread', (), {'is_alive': lambda _: False})()})()
        self.check._clear_job()
        self.assertTrue(deleted.wait(2))
        self.assertTrue(joined.is_set())

    @requirements('S9-HM4-4')
    def test_shared_loader_rejects_malformed_deep_json_with_plain_warning(self):
        from codex_agents import user_settings
        path = self.root / 'user-settings.json'
        path.write_text('[' * 1100 + '0' + ']' * 1100)
        settings = user_settings.load(self.root)
        self.assertIsNotNone(settings.warning)
        self.assertEqual(user_settings.UserSettings().store_size_bytes, settings.store_size_bytes)
        self.assertNotIn('[', settings.warning)

    @requirements('S9-HM4-4')
    def test_shared_loader_turns_recursion_error_into_plain_warning(self):
        # Python 3.11 raises RecursionError (not ValueError) on deeply nested JSON; newer Pythons do not.
        from unittest import mock
        from codex_agents import user_settings
        path = self.root / 'user-settings.json'
        path.write_text('[' * 1100 + '0' + ']' * 1100)
        with mock.patch.object(user_settings.json, 'loads', side_effect=RecursionError('deep')):
            settings = user_settings.load(self.root)
        self.assertIsNotNone(settings.warning)
        self.assertEqual(user_settings.UserSettings().store_size_bytes, settings.store_size_bytes)
        self.assertNotIn('[', settings.warning)

    @requirements('S9-HM4-4')
    def test_shared_settings_byte_cap_rejects_valid_json_one_byte_over(self):
        from codex_agents import user_settings
        path = self.root / 'user-settings.json'
        limit = user_settings.SETTINGS_BYTES
        for size, accepted in ((limit, True), (limit + 1, False)):
            # Valid JSON of exactly `size` bytes: only the byte cap can refuse the larger one.
            body = '{"store_size_bytes":1048576}' + ' ' * (size - 28)
            self.assertEqual(size, len(body.encode()))
            json.loads(body)
            path.write_text(body)
            settings = user_settings.load(self.root)
            self.assertEqual(accepted, settings.warning is None, size)
            if accepted:
                self.assertEqual(1048576, settings.store_size_bytes)

    @requirements('S9-HM4-4', 'S9-HM4-5')
    def test_shared_settings_typed_bounds_and_no_import_io(self):
        import importlib
        from codex_agents import user_settings
        path = self.root / 'user-settings.json'
        for key in ('store_size_bytes', 'store_growth_bytes_per_day', 'disk_free_bytes'):
            for value, valid in ((1, False), (1024 ** 2 - 1, False), (1024 ** 2, True),
                                 (2 ** 63 - 1, True), (2 ** 63, False), (True, False)):
                path.write_text(json.dumps({key: value}))
                settings = user_settings.load(self.root)
                self.assertEqual(valid, settings.warning is None, (key, value))
        for key, values in (('disk_free_percent', ((0, False), (1, True), (99, True), (100, False))),
                            ('no_merge_warning_ms', ((1, False), (59999, False), (60000, True)))):
            for value, valid in values:
                path.write_text(json.dumps({key: value}))
                self.assertEqual(valid, user_settings.load(self.root).warning is None)
        for raw in ('{"unknown":1}', '{"store_size_bytes":1048576,"store_size_bytes":2097152}',
                    ' ' * (user_settings.SETTINGS_BYTES + 1)):
            path.write_text(raw)
            self.assertIsNotNone(user_settings.load(self.root).warning)
        with mock.patch('builtins.open', side_effect=AssertionError('import file IO')), \
             mock.patch.object(hm.os, 'open', side_effect=AssertionError('import file IO')), \
             mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('import SQLite')):
            importlib.reload(user_settings)


class JsonLoaderDeepNestingTests(unittest.TestCase):
    """S9-HM4-4 as a class: deeply nested JSON is malformed JSON on every supported Python.

    Python 3.11 raises RecursionError from json.loads on deep nesting (newer Pythons do not), and
    RecursionError is not a ValueError, so a loader that handles malformed input but names only
    ValueError/JSONDecodeError lets it escape as a crash."""

    ROOTS = ('native-package/.codex/orchestration', 'scripts', '../claude-agents/src')
    COVERS = {'ValueError', 'JSONDecodeError'}
    CATCHES = {'RecursionError', 'RuntimeError', 'Exception', 'BaseException'}

    def sites(self):
        import ast
        from pathlib import Path
        package = Path(__file__).resolve().parents[1]
        found = []
        for root in self.ROOTS:
            for path in sorted((package / root).rglob('*.py')):
                tree = ast.parse(path.read_text())
                parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
                for node in ast.walk(tree):
                    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr in ('loads', 'load') and ast.unparse(node.func.value) == 'json'):
                        continue
                    child = node
                    while child in parent:
                        outer = parent[child]
                        if isinstance(outer, ast.Try) and child in outer.body:
                            names = [{ast.unparse(item).split('.')[-1] for item in
                                      (h.type.elts if isinstance(h.type, ast.Tuple) else [h.type])}
                                     if h.type is not None else {'Exception'} for h in outer.handlers]
                            if any(n & self.COVERS for n in names):
                                if not any(n & self.CATCHES for n in names):
                                    found.append('%s:%d' % (path.name, node.lineno))
                                break
                        child = outer
        return found

    @requirements('S9-HM4-4', why_not_scenario='A static scan of product sources: no process boundary is involved.')
    def test_no_loader_that_handles_malformed_json_lets_recursion_error_escape(self):
        self.assertEqual([], self.sites())

    @requirements('S9-HM4-4', why_not_scenario='The parser raises its refusal in-process on a patched decoder; no service or CLI boundary is involved.')
    def test_shared_bounded_parser_refuses_recursion_error_as_invalid_json(self):
        from codex_agents import learning_contracts
        with mock.patch.object(learning_contracts.json, 'loads', side_effect=RecursionError('deep')):
            with self.assertRaises(learning_contracts.ContractError):
                learning_contracts.parse_bounded_json_value(b'[[0]]')

    @requirements('S9-HM4-4', why_not_scenario='The reader refuses in-process on a patched decoder; no service or CLI boundary is involved.')
    def test_claude_settings_reader_refuses_recursion_error_as_unreadable(self):
        import tempfile
        from pathlib import Path
        from codex_agents import claude_host
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            path.write_text('{}')
            with mock.patch.object(claude_host.json, 'loads', side_effect=RecursionError('deep')):
                with self.assertRaises(claude_host.ClaudeHostDenied):
                    claude_host._settings(path)
