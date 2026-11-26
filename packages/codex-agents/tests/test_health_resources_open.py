"""S10 HM-6 on the real open path: the CLI, the account owner and the project host's run driver.

Review r1 (reviews/040-s10-r1): the low-disk refusal happened after account admission had already written the
binding, provenance and receipt, and the CLI then reported the refused run as started. Only the disk probe is
replaced here; account admission, the driver and the CLI are the product.
"""
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest import mock

from test_framework import requirements

from tests.test_bridge import PARENT_GRANT, cli
from tests.test_project_driver import DriverHarness

from codex_agents import account_api, health_resources as hr, project_driver, user_settings

GIB = 1024 ** 3
# The account rows an open writes (binding, provenance, receipt, request journal, hold); none may appear on refusal.
WHY = ('Real CLI, account admission and project run driver in process; only the disk probe is replaced, and there is '
       'no provider child, so no lifecycle scenario applies.')
ACCOUNT_TABLES = ('host_run_bindings', 'run_provenance', 'account_open_receipts', 'account_open_requests', 'run_holds')


class ResourceOpenTests(DriverHarness):

    def setUp(self):
        self.drivers = []
        original = project_driver.RunDriver.__init__

        def capture(driver, *args, **options):
            original(driver, *args, **options)
            self.drivers.append(driver)

        patcher = mock.patch.object(project_driver.RunDriver, '__init__', capture)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.low = False  # When True the workspace disk reads low (1 GiB free of 100 GiB).
        healthy = (100 * GIB, 50 * GIB, 50 * GIB)
        low = (100 * GIB, 99 * GIB, GIB)

        def usage(path):
            inside = Path(path).resolve() == Path(self.project).resolve()
            return low if (self.low and inside) else healthy

        patcher = mock.patch.object(hr, 'disk_usage', usage)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def prepare_open(self, name='task-1'):
        task = self.write(name + '.json', {**self.task(), 'id': name})
        route = self.cli('learning-route', '--task', task)
        assigned = self.assignment_for(route, task_id=name, assignment_id=name + '-assignment')
        context = ('--task', task, '--assignment', self.write(name + '-assignment.json', assigned),
                   '--parent-grant', self.write(name + '-grant.json', PARENT_GRANT))
        route_path = self.write(name + '-route.json', route)
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        return (*context, '--route', route_path, '--orchestration-handle', orchestration)

    def open(self, arguments):
        """(exit code, report) of one learning-open-host-run, whatever its exit."""
        stdout = io.StringIO()
        argv = ['cli.py', 'learning-open-host-run', '--state-root', str(self.state.root),
                '--project-root', str(self.project), *arguments]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle), \
                contextlib.redirect_stdout(stdout):
            code = cli.main()
        return code, json.loads(stdout.getvalue())

    def counts(self):
        connection = self.ledger._connection
        return {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ACCOUNT_TABLES}

    def runs(self):
        return {key: run for driver in self.drivers for key, run in driver._runs.items()}

    def assert_refused_plainly(self, code, report):
        self.assertEqual(2, code, report)
        self.assertIs(False, report['ready'])
        self.assertEqual('resource_low', report['reason_code'])
        self.assertIn(str(Path(self.project).resolve()), report['error'])
        self.assertIn('user-settings.json', report['error'])
        self.assertIn('disk_free_bytes', report['next_step'])
        for key in ('admission', 'status', 'run_handle', 'observation_started_at_ms', 'project_host_registered'):
            self.assertNotIn(key, report)
        self.assertNotEqual('opened', report.get('state'))
        for bad in ('Traceback', 'history_', 'None'):
            self.assertNotIn(bad, report['error'])

    @requirements('S10-HM6-4', why_not_scenario=WHY)
    def test_low_disk_open_is_refused_before_any_account_or_run_write(self):
        arguments = self.prepare_open()
        before = self.counts()
        self.low = True
        code, report = self.open(arguments)
        self.assert_refused_plainly(code, report)
        self.assertEqual(before, self.counts())
        self.assertEqual({}, self.runs())
        # With space back, the same open goes ahead: nothing half-made blocks it.
        self.low = False
        code, opened = self.open(arguments)
        self.assertEqual(0, code, opened)
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))

    @requirements('S10-HM6-4', why_not_scenario=WHY)
    def test_identical_retry_of_a_registered_open_is_not_probed_again(self):
        arguments = self.prepare_open()
        code, first = self.open(arguments)
        self.assertEqual((0, True), (code, first['project_host_registered']), first)
        counts = self.counts()
        self.low = True
        probe = mock.Mock(side_effect=AssertionError('probed'))
        with mock.patch.object(hr, 'disk_usage', probe):
            code, retried = self.open(arguments)
        self.assertEqual(0, code, retried)
        probe.assert_not_called()
        self.assertEqual((first['run_handle'], True, True),
                         (retried['run_handle'], retried['known'], retried['project_host_registered']))
        self.assertEqual(counts, self.counts())

    @requirements('S10-HM6-4', why_not_scenario=WHY)
    def test_disk_filling_between_admission_and_registration_never_reports_success(self):
        """The registration's own check stays as a backstop; when it refuses, the CLI says so and exits non-zero."""
        arguments = self.prepare_open()
        original = account_api.AccountClient.open_host_run

        def admitted_then_low(client, *args, **options):
            reply = original(client, *args, **options)
            self.low = True  # The disk fills after the account admitted the run, before the host registers it.
            return reply

        with mock.patch.object(account_api.AccountClient, 'open_host_run', admitted_then_low):
            code, report = self.open(arguments)
        self.assert_refused_plainly(code, report)
        self.assertEqual({}, self.runs())

    @requirements('S10-HM6-5', why_not_scenario=WHY)
    def test_out_of_range_settings_use_defaults_and_the_open_says_so(self):
        arguments = self.prepare_open()
        (Path(self.state.root) / user_settings.FILENAME).write_text(json.dumps({'disk_free_percent': 0}))
        code, opened = self.open(arguments)
        self.assertEqual(0, code, opened)
        self.assertEqual(user_settings.WARNING, opened['settings_warning'])
        arguments = self.prepare_open('task-2')
        self.low = True  # 1 GiB free is below the default 5 GiB: the defaults are in force.
        code, refused = self.open(arguments)
        self.assert_refused_plainly(code, refused)
        self.assertEqual(user_settings.WARNING, refused['settings_warning'])
