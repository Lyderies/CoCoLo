"""S10 HM-6: disk-space refusal and memory pressure. Injected probes; no real disk is filled."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from codex_agents import health_resources as hr
from codex_agents import project_driver, user_settings
from codex_agents.health_monitor import CheckContext
from codex_agents.history_contracts import HistoryError
from test_framework import requirements

GIB = 1024 ** 3
WORKSPACE = Path('/work/space')
STATE = Path('/state/folder')


def usage_for(table):
    """A fake disk_usage: path -> (total, free), or an exception to raise."""
    def usage(path):
        value = table[Path(path)]
        if isinstance(value, Exception):
            raise value
        total, free = value
        return (total, total - free, free)
    return usage


def both(workspace, state):
    """Workspace folder -> workspace reading; any other folder (the state folder) -> state reading."""
    table = usage_for({WORKSPACE: workspace, STATE: state})
    return lambda path: table(WORKSPACE if Path(path) == WORKSPACE else STATE)


class ResourceTests(unittest.TestCase):
    __why_not_scenario__ = 'Injected disk and memory probes; no real disk, sockets or providers.'

    def refusal(self, workspace, state, settings=None):
        return hr.refusal(WORKSPACE, STATE, settings or user_settings.UserSettings(), usage=both(workspace, state))

    @requirements('S10-HM6-1')
    def test_workspace_threshold_below_equal_above(self):
        total = 100 * GIB  # 5% = 5 GiB is the larger
        need = 5 * GIB
        healthy = (total, 50 * GIB)
        self.assertIsNotNone(self.refusal((total, need - 1), healthy))
        self.assertIsNone(self.refusal((total, need), healthy))
        self.assertIsNone(self.refusal((total, need + 1), healthy))
        text = self.refusal((total, need - 1), healthy).text
        self.assertIn(str(WORKSPACE), text)
        self.assertNotIn(str(STATE), text)

    @requirements('S10-HM6-2')
    def test_state_folder_threshold_below_equal_above(self):
        total = 100 * GIB
        need = 5 * GIB
        healthy = (total, 50 * GIB)
        self.assertIsNotNone(self.refusal(healthy, (total, need - 1)))
        self.assertIsNone(self.refusal(healthy, (total, need)))
        self.assertIsNone(self.refusal(healthy, (total, need + 1)))
        text = self.refusal(healthy, (total, need - 1)).text
        self.assertIn(str(STATE), text)
        self.assertNotIn(str(WORKSPACE), text)

    @requirements('S10-HM6-2')
    def test_both_low_names_both(self):
        text = self.refusal((100 * GIB, GIB), (100 * GIB, GIB)).text
        self.assertIn(str(WORKSPACE), text)
        self.assertIn(str(STATE), text)

    @requirements('S10-HM6-3')
    def test_larger_of_two_gib_or_five_percent(self):
        settings = user_settings.UserSettings()
        small, large = 10 * GIB, 200 * GIB  # 5% is 0.5 GiB and 10 GiB
        self.assertEqual(2 * GIB, hr.needed_bytes(small, settings))
        self.assertEqual(10 * GIB, hr.needed_bytes(large, settings))
        # The switch-over is at 40 GiB, where 5% is exactly 2 GiB.
        self.assertEqual(2 * GIB, hr.needed_bytes(40 * GIB, settings))
        self.assertEqual(2 * GIB + 1, hr.needed_bytes(40 * GIB + 20, settings))
        healthy = (100 * GIB, 50 * GIB)
        self.assertIsNotNone(self.refusal((small, 2 * GIB - 1), healthy))
        self.assertIsNone(self.refusal((small, 2 * GIB), healthy))
        self.assertIsNotNone(self.refusal((large, 10 * GIB - 1), healthy))
        self.assertIsNone(self.refusal((large, 10 * GIB), healthy))

    @requirements('S10-HM6-4')
    def test_refusal_is_plain_and_complete(self):
        text = self.refusal((100 * GIB, 3 * GIB), (100 * GIB, 50 * GIB)).text
        for part in (str(WORKSPACE), '3.00 GiB', '5.00 GiB', 'disk_free_bytes', 'disk_free_percent',
                     'user-settings.json'):
            self.assertIn(part, text)
        for bad in ('Traceback', 'Error', 'history_', 'Exception', 'None'):
            self.assertNotIn(bad, text)

    @requirements('S10-HM6-4')
    def test_just_below_amounts_are_distinguishable(self):
        text = self.refusal((10 * GIB, 2 * GIB - 1), (10 * GIB, 5 * GIB)).text
        self.assertIn(str(2 * GIB - 1), text)
        self.assertIn(str(2 * GIB), text)

    def driver(self, state_root):
        driver = object.__new__(project_driver.RunDriver)
        driver._runs, driver._memory, driver._root = {}, None, WORKSPACE
        driver._state = mock.Mock(root=Path(state_root))
        return driver

    def register(self, driver, key=('hrn_' + 'a' * 48, 1), host='codex'):
        """One run.register request through the driver's answer path; returns the connection's reply."""
        request = {'run_handle': key[0], 'attempt': key[1], 'operation': 'run.register', 'inputs_digest': 'd'}
        replies = []
        driver._reply = lambda connection, value: replies.append(value)
        driver._register = mock.Mock(side_effect=lambda *args: driver._runs.setdefault(key, mock.Mock()))
        driver._view = lambda run: {'state': 'registered'}
        driver._answer(object(), mock.Mock(host_client=host), request, {})
        return replies, driver._register

    @requirements('S10-HM6-4')
    def test_driver_refuses_before_any_state_exists(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as state:
            driver = self.driver(state)
            low = both((100 * GIB, GIB), (100 * GIB, 50 * GIB))
            with mock.patch.object(hr, 'disk_usage', low):
                replies, register = self.register(driver)
            self.assertEqual([{'state': 'refused', 'reason_code': 'history_service_unavailable'}], replies)
            register.assert_not_called()
            self.assertEqual({}, driver._runs)
            self.assertEqual([], list(Path(state).iterdir()))
            with mock.patch.object(hr, 'disk_usage', low):
                with self.assertRaises(HistoryError) as caught:
                    driver._resource_preflight(('hrn_' + 'b' * 48, 1))
            self.assertEqual('history_service_unavailable', caught.exception.reason_code)
            self.assertIn(str(WORKSPACE), str(caught.exception))
            self.assertNotIn('Traceback', str(caught.exception))

    @requirements('S10-HM6-4')
    def test_driver_retry_of_registered_run_is_not_rechecked(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as state:
            driver = self.driver(state)
            key = ('hrn_' + 'a' * 48, 1)
            driver._runs[key] = mock.Mock()
            probe = mock.Mock(side_effect=AssertionError('probed'))
            with mock.patch.object(hr, 'disk_usage', probe):
                replies, register = self.register(driver, key)
            probe.assert_not_called()
            register.assert_called_once()
            self.assertEqual([{'state': 'registered'}], replies)

    @requirements('S10-HM6-5')
    def test_settings_file_overrides_limits(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as state:
            driver = self.driver(state)
            usage = both((100 * GIB, 3 * GIB), (100 * GIB, 50 * GIB))
            path = Path(state) / user_settings.FILENAME
            # 3 GiB free is below the default 5% (5 GiB) but above an override of 1 GiB / 1%.
            path.write_text(json.dumps({'disk_free_bytes': GIB, 'disk_free_percent': 1}))
            with mock.patch.object(hr, 'disk_usage', usage):
                driver._resource_preflight(('hrn_' + 'c' * 48, 1))
            path.write_text(json.dumps({'disk_free_bytes': 4 * GIB}))
            with mock.patch.object(hr, 'disk_usage', usage):
                with self.assertRaises(HistoryError):
                    driver._resource_preflight(('hrn_' + 'c' * 48, 1))

    @requirements('S10-HM6-5')
    def test_out_of_range_values_are_refused(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as state:
            path = Path(state) / user_settings.FILENAME
            for content in ({'disk_free_percent': 0}, {'disk_free_percent': 100}, {'disk_free_bytes': 1024},
                            {'disk_free_bytes': -1}, {'disk_free_bytes': 2.5 * GIB},
                            {'memory_available_percent': 0}, {'memory_available_percent': 100}):
                path.write_text(json.dumps(content))
                loaded = user_settings.load(state)
                self.assertIsNotNone(loaded.warning, content)
                self.assertEqual(2 * GIB, loaded.disk_free_bytes)
                self.assertEqual(5, loaded.disk_free_percent)
                self.assertEqual(10, loaded.memory_available_percent)
            path.write_text(json.dumps({'disk_free_percent': 99, 'memory_available_percent': 1}))
            self.assertIsNone(user_settings.load(state).warning)

    @requirements('S10-HM6-6')
    def test_memory_pressure_is_reported_but_never_refuses(self):
        settings = user_settings.UserSettings()
        healthy = both((100 * GIB, 50 * GIB), (100 * GIB, 50 * GIB))
        low = lambda: (16 * GIB, GIB // 2)  # 3% available, below 10%
        check = hr.ResourceCheck(WORKSPACE, STATE, usage=healthy, memory=low, settings=lambda: settings)
        found = list(check.run(CheckContext(1e12, 1000, 60_000)))
        self.assertEqual(['memory_pressure'], [f.reason_code for f in found])
        self.assertEqual('reported', found[0].outcome)
        self.assertIn('memory', found[0].text.lower())
        self.assertIsNone(hr.refusal(WORKSPACE, STATE, settings, usage=healthy))
        ok = lambda: (16 * GIB, 8 * GIB)
        check = hr.ResourceCheck(WORKSPACE, STATE, usage=healthy, memory=ok, settings=lambda: settings)
        self.assertEqual([], list(check.run(CheckContext(1e12, 1000, 60_000))))
        unknown = hr.ResourceCheck(WORKSPACE, STATE, usage=healthy, memory=lambda: None, settings=lambda: settings)
        self.assertEqual([], list(unknown.run(CheckContext(1e12, 1000, 60_000))))

    @requirements('S10-HM6-7')
    def test_probe_errors_fail_safe_with_plain_reason(self):
        healthy = (100 * GIB, 50 * GIB)
        for bad in (OSError(13, 'Permission denied /secret/x'), FileNotFoundError('gone'),
                    (0, 0, 0), (100, 50), 'nonsense', (100 * GIB, 0, -1)):
            usage = both(healthy, bad) if isinstance(bad, Exception) else (
                lambda path, bad=bad: bad if Path(path) == STATE else (100 * GIB, 50 * GIB, 50 * GIB))
            refusal = hr.refusal(WORKSPACE, STATE, user_settings.UserSettings(), usage=usage)
            self.assertIsNotNone(refusal, bad)
            self.assertIn(str(STATE), refusal.text)
            self.assertIn('could not', refusal.text)
            for word in ('Traceback', 'Permission denied', 'errno', 'Errno'):
                self.assertNotIn(word, refusal.text)

    @requirements('S10-HM6-8')
    def test_check_reports_low_and_unreadable_folders_only(self):
        settings = user_settings.UserSettings()
        context = CheckContext(1e12, 1000, 60_000)
        quiet = hr.ResourceCheck(WORKSPACE, STATE, memory=lambda: None, settings=lambda: settings,
                                 usage=both((100 * GIB, 50 * GIB), (100 * GIB, 50 * GIB)))
        self.assertEqual([], list(quiet.run(context)))
        loud = hr.ResourceCheck(WORKSPACE, STATE, memory=lambda: None, settings=lambda: settings,
                                usage=both((100 * GIB, GIB), OSError('boom')))
        found = list(loud.run(context))
        self.assertEqual(['disk_low_workspace', 'disk_unreadable_state'], sorted(f.reason_code for f in found))
        self.assertTrue(all(f.check == 'resources' and f.outcome == 'reported' and f.scope == 'machine'
                            for f in found))
        self.assertIn('user-settings.json', found[0].text + found[1].text)

    @requirements('S10-HM6-8')
    def test_import_does_no_io(self):
        code = ('import builtins, shutil, os\n'
                'def boom(*a, **k): raise SystemExit("io at import")\n'
                'shutil.disk_usage = boom\n'
                'import sys; sys.path.insert(0, %r)\n'
                'import codex_agents.user_settings, codex_agents.health_monitor\n'
                'os.stat = boom\n'
                'import codex_agents.health_resources as m\n'
                'assert m.disk_usage is boom or callable(m.disk_usage)\n') % str(ORCHESTRATION_ROOT)
        result = subprocess.run([sys.executable, '-B', '-I', '-c', code], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
