"""S9 HM-4: temporary SQLite stores, no socket transport or provider calls."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT
from codex_agents import health_ledger as hm
from codex_agents.health_monitor import CheckContext, HealthMonitor
from test_framework import requirements

PROJECT = 'prj_' + 'a' * 32
OTHER = 'prj_' + 'b' * 32


class LedgerHealthTests(unittest.TestCase):
    __why_not_scenario__ = 'Real temporary SQLite stores and owner-thread fixture; no sockets or providers.'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / 'learning.sqlite3'
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE projects(project_id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO projects VALUES(?)', (PROJECT,))
            db.commit()
        class Owner:
            def call(inner, operation, **kwargs):
                return operation()
        self.owner = Owner()
        self.state = SimpleNamespace(root=self.root, database=self.database, _ledger_owner=self.owner)
        self.settings = self.root / 'health-limits.json'
        self.check = hm.LedgerCheck(self.state, settings_path=self.settings)
        self.addCleanup(self.check.close)

    def context(self, now=1000, seconds=.7):
        return CheckContext(time.monotonic() + seconds, now, 60_000)

    @requirements('S9-HM4-1')
    def test_corrupted_copy_reported_without_private_text(self):
        path = self.root / 'PRIVATE-CANARY.sqlite3'
        path.write_bytes(b'private invalid SQLite database')
        found = self.check.check_copy(path, PROJECT, self.context())
        self.assertEqual(['store_corrupt'], [f.reason_code for f in found])
        self.assertTrue(all(f.outcome == 'reported' for f in found))
        self.assertNotIn('PRIVATE', str(found))
        self.assertEqual(b'private invalid SQLite database', path.read_bytes())

    @requirements('S9-HM4-1')
    def test_healthy_copy(self):
        self.assertEqual([], self.check.check_copy(self.database, None, self.context()))

    @requirements('S9-HM4-1')
    def test_quick_check_every_visit(self):
        calls = []
        original = hm.open_reader
        def reader(path):
            db = original(path)
            db.set_trace_callback(calls.append)
            return db
        with mock.patch.object(hm, 'open_reader', side_effect=reader):
            self.check.visit(self.database, None, self.context())
            self.check.visit(self.database, None, self.context())
        self.assertEqual(0, sum('quick_check' in sql for sql in calls))
        task = self.check._scan
        self.assertIsNotNone(task)
        task.advance(time.monotonic() + 1)
        self.assertTrue(task.done.wait(2))
        self.assertEqual('ok', task.result)

    @requirements('S9-HM4-2')
    def test_size_boundary_report_only(self):
        for project in (None, PROJECT):
            for size, warn in ((8 * hm.GIB - 1, False), (8 * hm.GIB, True), (8 * hm.GIB + 1, True)):
                found = self.check.measure(project, size, 1000)
                self.assertEqual(warn, any(f.reason_code == 'store_size' for f in found))
                self.assertTrue(all(f.outcome == 'reported' for f in found))
                if warn:
                    self.assertIn('8 GiB', found[0].text)
                    self.assertIn('New runs are still allowed', found[0].text)

    @requirements('S9-HM4-2')
    def test_growth_rate_boundary_and_clock(self):
        for increase, warn in ((hm.GIB - 1, False), (hm.GIB, False), (hm.GIB + 1, True)):
            check = hm.LedgerCheck(self.state, settings_path=self.settings)
            self.addCleanup(check.close)
            project = 'prj_' + f'{increase:032x}'  # independent persisted history per boundary
            check.measure(project, 100, 1000)
            found = check.measure(project, 100 + increase, 1000 + hm.DAY_MS)
            self.assertEqual(warn, any(f.reason_code == 'store_growth' for f in found))
        self.assertFalse(any(f.reason_code == 'store_growth' for f in self.check.measure(None, 10, 1000)))
        self.assertFalse(any(f.reason_code == 'store_growth' for f in self.check.measure(None, 100000, 999)))

    @requirements('S9-HM4-3')
    def test_daily_completion_boundaries(self):
        for elapsed, expected in ((hm.DAY_MS - 1, []), (hm.DAY_MS, []), (hm.DAY_MS + 1, ['full_check_overdue'])):
            found = self.check.completion_findings(PROJECT, 1000, 1000 + elapsed)
            self.assertEqual(expected, [f.reason_code for f in found])

    @requirements('S9-HM4-3', 'S9-HM4-5')
    def test_incomplete_copy_does_not_complete_full_check(self):
        self.check._last_full[None] = 1000
        with mock.patch.object(hm.Snapshot, 'step', return_value=False):
            found = self.check.visit(self.database, None, self.context(1000 + hm.DAY_MS + 1))
        self.assertIn('full_check_overdue', [f.reason_code for f in found])
        self.assertEqual(1000, self.check._last_full[None])

    @requirements('S9-HM4-4')
    def test_settings_reload_and_status(self):
        monitor = HealthMonitor((self.check,))
        self.check.reload_settings()
        self.assertEqual({'store_size_bytes': 8 * hm.GIB, 'store_growth_bytes_per_day': hm.GIB}, self.check.limits)
        monitor.run_pass(now_ms=1000)
        self.assertIn('8 GiB', monitor.view(PROJECT)['summary'])
        self.settings.write_text(json.dumps({'store_size_bytes': 17 * 1024 ** 2, 'store_growth_bytes_per_day': 11 * 1024 ** 2}))
        monitor.run_pass(now_ms=2000)
        self.assertEqual(17 * 1024 ** 2, self.check.limits['store_size_bytes'])
        self.assertIn('17825792 bytes', monitor.view(PROJECT)['summary'])
        self.assertTrue(any(f.reason_code == 'store_size' for f in self.check.measure(None, 18 * 1024 ** 2, 2000)))
        self.check.measure(PROJECT, 1, 2000)
        self.assertIn('store_growth', [f.reason_code for f in self.check.measure(PROJECT, 13 * 1024 ** 2, 2000 + hm.DAY_MS)])

    @requirements('S9-HM4-4')
    def test_invalid_settings_visible(self):
        self.settings.write_text('{PRIVATE-CANARY')
        found = self.check.run(self.context())
        self.assertIn('health_settings_invalid', [f.reason_code for f in found])
        self.assertNotIn('PRIVATE', str(found))
        self.assertEqual(8 * hm.GIB, self.check.limits['store_size_bytes'])

    @requirements('S9-HM4-5')
    def test_owner_thread_and_total_budget(self):
        from tests.test_ledger_owner_wiring import LedgerOwnerWiringTests
        fixture = LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()
        check = hm.LedgerCheck(fixture.state, settings_path=self.settings)
        self.addCleanup(check.close)
        observed = []
        snapshot_threads = set()
        original = hm.open_reader
        def reader(path):
            if threading.current_thread() is not fixture.thread:
                self.assertIn(threading.current_thread(), snapshot_threads)
            return original(path)
        original_step = hm.Snapshot.step
        def step(snapshot, deadline):
            self.assertIs(threading.current_thread(), fixture.thread)
            snapshot_threads.add(snapshot.thread)
            start = time.monotonic()
            result = original_step(snapshot, deadline)
            observed.append(time.monotonic() - start)
            return result
        started = time.monotonic()
        with mock.patch.object(hm, 'open_reader', side_effect=reader), mock.patch.object(hm.Snapshot, 'step', step):
            check.run(self.context())
        self.assertLess(time.monotonic() - started, .75)
        self.assertTrue(observed)
        self.assertLess(sum(observed), .75)
        self.assertFalse(fixture.owner.call(lambda: fixture.owner.ledger._connection.in_transaction))

    @requirements('S9-HM4-5')
    def test_copy_past_page_limit(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE payload(value BLOB)')
            db.execute('INSERT INTO payload VALUES(zeroblob(?))', ((hm.COPY_PAGES + 5) * 4096,))
            db.commit()
        snapshot = hm.Snapshot(self.database, self.root / 'snapshot.sqlite3')
        self.addCleanup(snapshot.close)
        self.assertFalse(snapshot.step(time.monotonic() + 1))
        for _ in range(20):
            if snapshot.step(time.monotonic() + 1):
                break
        else:
            self.fail('snapshot failed to advance beyond its page limit')
        with closing(sqlite3.connect(snapshot.path)) as db:
            self.assertEqual((hm.COPY_PAGES + 5) * 4096, db.execute('SELECT length(value) FROM payload').fetchone()[0])
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO projects VALUES(?)', (OTHER,))
            db.commit()

    @requirements('S9-HM4-5')
    def test_expired_pass(self):
        with mock.patch.object(self.owner, 'call', side_effect=AssertionError('owner used after deadline')):
            self.assertEqual([], self.check.run(self.context(seconds=-1)))

    @requirements('S9-HM4-5')
    def test_full_scan_resumes_after_quantum(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE many(value TEXT)')
            db.executemany('INSERT INTO many VALUES(?)', [('a' * 100,)] * 10000)
            db.commit()
        task = hm.CopyScan(self.database)
        self.addCleanup(task.close)
        task.advance(time.monotonic() - 1)
        self.assertFalse(task.done.wait(.03))
        for _ in range(50):
            task.advance(time.monotonic() + .01)
            if task.done.wait(.02):
                break
        self.assertTrue(task.done.is_set(), 'full integrity VM never resumed')
        self.assertEqual('ok', task.result)

    @requirements('S9-HM4-6')
    def test_registration_and_project_scope(self):
        monitor = HealthMonitor()
        check = hm.register(monitor, self.state, settings_path=self.settings)
        self.addCleanup(check.close)
        self.assertEqual(['ledger'], [item.name for item in monitor._checks])
        found = self.check.measure(PROJECT, 8 * hm.GIB, 1000)
        self.assertTrue(found)
        self.assertTrue(all(f.visible_to(PROJECT) and not f.visible_to(OTHER) for f in found))

    @requirements('S9-HM4-6', 'S9-HM4-5')
    def test_project_enumeration_past_page(self):
        for project in (PROJECT, OTHER):
            directory = self.root / 'projects' / project
            directory.mkdir(parents=True)
            (directory / 'history.sqlite3').write_bytes(b'corrupt')
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO projects VALUES(?)', (OTHER,))
            db.commit()
        found = []
        for n in range(8):
            found.extend(self.check.run(self.context(1000 + n)))
        self.assertEqual({PROJECT, OTHER}, {f.project_id for f in found if f.reason_code == 'store_corrupt'})

    @requirements('S9-HM4-6')
    def test_warning_survives_other_visit(self):
        self.check._observed[PROJECT] = self.check.measure(PROJECT, 8 * hm.GIB, 1000)
        self.assertIn(PROJECT, [f.project_id for f in self.check.run(self.context()) if f.reason_code == 'store_size'])


    @requirements('S9-HM4-3', 'S9-HM4-2')
    def test_completed_clock_and_growth_survive_restart(self):
        self.check.run(self.context())
        for _ in range(20):
            if None in self.check._last_full:
                break
            self.check.run(self.context())
        self.assertIn(None, self.check._last_full)
        completed = self.check._last_full[None]
        self.check.close()
        restarted = hm.LedgerCheck(self.state, settings_path=self.settings)
        self.addCleanup(restarted.close)
        found = restarted.measure(None, 2 * hm.GIB, completed + hm.DAY_MS + 1)
        self.assertEqual(completed, restarted._loaded_completion[None])
        self.assertIn('store_growth', [f.reason_code for f in found])

    @requirements('S9-HM4-1', 'S9-HM4-6')
    def test_full_corruption_latches_until_full_clean_result(self):
        task = SimpleNamespace(done=threading.Event(), result='corrupt',
                               advance=lambda deadline: None, close=lambda: None,
                               thread=SimpleNamespace(is_alive=lambda: False))
        task.done.set()
        self.check._scan = task
        self.check._job_path = self.database
        self.check._job_project = None
        self.check.run(self.context())
        found = self.check.run(self.context(2000))
        self.assertIn('store_corrupt', [f.reason_code for f in found])

    @requirements('S9-HM4-1')
    def test_failed_full_scan_reported_without_completion(self):
        task = SimpleNamespace(done=threading.Event(), result='unavailable',
                               advance=lambda deadline: None, close=lambda: None,
                               thread=SimpleNamespace(is_alive=lambda: False))
        task.done.set()
        self.check._scan = task
        self.check._job_path = self.database
        self.check._job_project = None
        found = self.check.run(self.context())
        self.assertIn('full_check_unavailable', [f.reason_code for f in found])
        self.assertNotIn(None, self.check._last_full)

    @requirements('S9-HM4-4')
    def test_settings_past_byte_and_integer_limits(self):
        for content in (' ' * (hm.SETTINGS_BYTES + 1),
                        json.dumps({'store_size_bytes': 2 ** 63}),
                        json.dumps({'store_size_bytes': True}),
                        json.dumps({'store_size_bytes': -1}),
                        '{"store_size_bytes":1,"store_size_bytes":2}'):
            self.settings.write_text(content)
            self.check.reload_settings()
            self.assertTrue(self.check._settings_invalid)
            self.assertEqual(hm.DEFAULT_LIMITS, self.check.limits)
        self.settings.write_text(json.dumps({'store_size_bytes': 2 ** 63 - 1,
                                            'store_growth_bytes_per_day': 2 ** 63 - 1}))
        self.check.reload_settings()
        self.assertFalse(self.check._settings_invalid)
        monitor = HealthMonitor((self.check,))
        monitor.run_pass()
        from codex_agents.client_runtime import _status_health_monitor
        self.assertIn('findings', _status_health_monitor(monitor.view(PROJECT)))

    @requirements('S9-HM4-5')
    def test_one_pass_advances_many_copy_pages_with_shared_deadline(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE big(value BLOB)')
            db.execute('INSERT INTO big VALUES(zeroblob(?))', ((hm.COPY_PAGES * 3 + 5) * 4096,))
            db.commit()
        deadlines = []
        original = hm.Snapshot.step
        def step(snapshot, deadline):
            deadlines.append(deadline)
            return original(snapshot, deadline)
        with mock.patch.object(hm.Snapshot, 'step', step):
            started = time.monotonic()
            self.check.run(self.context())
        self.assertGreater(len(deadlines), 1)
        self.assertEqual(1, len(set(deadlines)))
        self.assertLess(time.monotonic() - started, .75)

    @requirements('S9-HM4-6')
    def test_deleted_projects_drop_retained_observations(self):
        self.check._observed[OTHER] = self.check.measure(OTHER, 8 * hm.GIB, 1000)
        for n in range(5):
            found = self.check.run(self.context(1000 + n))
        self.assertNotIn(OTHER, [f.project_id for f in found])


    @requirements('S9-HM4-4')
    def test_default_settings_are_editable_outside_protected_runtime(self):
        (self.root / 'user-settings.json').write_text(json.dumps({'store_size_bytes': 1234 * 1024 ** 2}))
        check = hm.LedgerCheck(self.state)
        self.addCleanup(check.close)
        check.reload_settings()
        self.assertEqual(1234 * 1024 ** 2, check.limits['store_size_bytes'])
        self.assertEqual(self.state.root / 'user-settings.json', check.settings_path)

    @requirements('S9-HM4-5')
    def test_saved_state_past_byte_limit_is_reported_and_rechecked(self):
        path = self.root / 'health-ledger' / 'ledger.json'
        path.parent.mkdir()
        path.write_bytes(b' ' * (hm.STATE_BYTES + 1))
        self.assertIn('health_state_unavailable', [f.reason_code for f in self.check.run(self.context())])

    @requirements('S9-HM4-5')
    def test_import_does_not_open_files_or_databases(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('codex_agents._s9_import_probe', hm.__file__)
        module = importlib.util.module_from_spec(spec)
        with mock.patch('builtins.open', side_effect=AssertionError('file I/O on import')), \
             mock.patch.object(Path, 'open', side_effect=AssertionError('file I/O on import')), \
             mock.patch.object(os, 'open', side_effect=AssertionError('file I/O on import')), \
             mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLite on import')):
            spec.loader.exec_module(module)


    @requirements('S9-HM4-5', 'S9-HM4-6')
    def test_past_observation_cache_limit_still_reports_old_project_status(self):
        for n in range(hm.MAX_TRACKED_STORES + 3):
            project = 'prj_' + f'{n:032x}'
            path = self.root / 'projects' / project
            path.mkdir(parents=True)
            (path / 'history.sqlite3').write_bytes(b'corrupt')
            self.check.visit(path / 'history.sqlite3', project, self.context())
        self.assertLessEqual(len(self.check._loaded), hm.MAX_TRACKED_STORES)
        monitor = HealthMonitor((self.check,))
        monitor.run_pass(now_ms=1001)
        oldest = 'prj_' + '0' * 32
        self.assertIn('store_corrupt', [f['reason_code'] for f in monitor.view(oldest)['findings']])
        self.assertEqual([], [f for f in monitor.view(OTHER)['findings'] if f['scope'] == 'project'])

    @requirements('S9-HM4-2', 'S9-HM4-6')
    def test_warning_reaches_owner_attention(self):
        self.settings.write_text(json.dumps({'store_size_bytes': 1024 ** 2}))
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('CREATE TABLE attention_payload(value BLOB)')
            db.execute('INSERT INTO attention_payload VALUES(zeroblob(?))', (2 * 1024 ** 2,))
            db.commit()
        monitor = HealthMonitor((self.check,))
        monitor.run_pass()
        from codex_agents.client_runtime import _health_attention, _status_health_monitor
        report = {'health_monitor': _status_health_monitor(monitor.view(PROJECT)),
                  'account_health': {'attention': [], 'state': 'ok'}}
        _health_attention(report)
        self.assertEqual('degraded', report['account_health']['state'])
        self.assertTrue(any('New runs are still allowed' in line for line in report['account_health']['attention']))


    @requirements('S9-HM4-4', 'S9-HM4-5')
    def test_settings_symlink_is_reported_without_following(self):
        target = self.root / 'private-settings'
        target.write_text(json.dumps({'store_size_bytes': 1}))
        self.settings.symlink_to(target)
        self.check.reload_settings()
        self.assertTrue(self.check._settings_invalid)
        self.assertEqual(hm.DEFAULT_LIMITS, self.check.limits)

    @requirements('S9-HM4-5')
    def test_wal_copy_allows_writer_between_page_slices(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE wal_payload(value BLOB)')
            db.execute('INSERT INTO wal_payload VALUES(zeroblob(?))', ((hm.COPY_PAGES + 5) * 4096,))
            db.commit()
            snapshot = hm.Snapshot(self.database, self.root / 'wal-copy.sqlite3')
            self.addCleanup(snapshot.close)
            self.assertFalse(snapshot.step(time.monotonic() + 1))
            db.execute('INSERT INTO projects VALUES(?)', (OTHER,))
            db.commit()
            for _ in range(30):
                if snapshot.step(time.monotonic() + 1):
                    break
            else:
                self.fail('WAL snapshot did not finish')
            with closing(sqlite3.connect(snapshot.path)) as copied:
                self.assertEqual(('ok',), copied.execute('PRAGMA integrity_check').fetchone())
                self.assertEqual(1, copied.execute('SELECT count(*) FROM projects').fetchone()[0])
                self.assertEqual(2, db.execute('SELECT count(*) FROM projects').fetchone()[0])


    @requirements('S9-HM4-5')
    def test_busy_queue_uses_shorter_slice_without_starving_copy(self):
        self.owner.queue = SimpleNamespace(snapshot=lambda: {'retained': 2})
        calls = []
        def step(snapshot, deadline):
            calls.append(deadline)
            time.sleep(.01)
            return False
        with mock.patch.object(hm.Snapshot, 'step', step):
            started = time.monotonic()
            self.check.run(self.context())
        self.assertGreater(len(calls), 1)
        self.assertLess(time.monotonic() - started, hm.BUSY_COPY_SECONDS + .15)
        self.assertEqual(1, len(set(calls)))


    @requirements('S9-HM4-4')
    def test_effective_defaults_shown_before_first_pass(self):
        monitor = HealthMonitor((self.check,))
        self.assertIn('8 GiB', monitor.view(PROJECT)['summary'])
        self.assertIn('1 GiB/day', monitor.view(PROJECT)['summary'])


if __name__ == '__main__':
    unittest.main()
