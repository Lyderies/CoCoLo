"""In-process collector, account and project services for tests that drive the real CLI.

The collector, the account services and each project host run as threads of the
test process, so the fake provider clients the bridge tests trust are the ones
they resolve. Everything else is the product path: the in-process CLI, the
hosts' enrollment, the run driver and the original owners.
"""
import contextlib
import io
import json
import sys
import threading
import time
from unittest import mock

import tests.support  # noqa: F401 -- the source CLI import path
import cli

from codex_agents import client_runtime, collector_runtime, learning_store, project_driver
from codex_agents.account_host import AccountServiceHost
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.collector_paths import CollectorPaths
from codex_agents.learning_store import CollectorServer, LearningLedger
from codex_agents.project_service import serve_project
from codex_agents.service_enrollment import capability_path


class LiveLedger:
    """The running collector's ledger, for a test's direct reads between CLI calls."""

    def __init__(self, harness):
        object.__setattr__(self, '_harness', harness)

    def __getattr__(self, name):
        harness = object.__getattribute__(self, '_harness')
        harness.wait_for(lambda: harness.collector_ledger is not None)
        return getattr(harness.collector_ledger, name)

    def close(self):
        pass  # The collector thread owns and closes it.


class ServiceHarness:
    """Mixin for a BridgeTestCase: call `start_services()` once setup has used its own ledger."""
    liveness = None  # The installed provisional policy unless a test narrows it.
    collector_ledger = None
    threads = ()

    def start_services(self, *, project_host=True):
        self.ledger.close()
        self.ledger = LiveLedger(self)
        self.paths = CollectorPaths(self.state.root)
        self.stop, self.restart = threading.Event(), threading.Event()
        self.restart.set()
        self.failures = []
        # Legacy routing still stops the collector for maintenance. Its restart is
        # this thread's in-process collector, so the trusted fake clients still apply.
        for module in (collector_runtime, learning_store):
            patcher = mock.patch.object(module, '_start_collector_locked', self.start_collector)
            patcher.start()
            self.addCleanup(patcher.stop)
        # How each result-persistence lane started, for the failure message (0.4.0 O5): on GitHub's
        # runners a project service sometimes refused to start with history_service_unavailable, which
        # the lane reports both for a writer not ready in time and for a fault while opening the store.
        from codex_agents import result_persistence
        self.lane_starts = []
        original_init, original_open = (result_persistence.ResultPersistenceLane.__init__,
                                        result_persistence.ResultPersistenceLane._history)

        def init(lane, *arguments, **options):
            began = time.monotonic()
            try:
                return original_init(lane, *arguments, **options)
            except BaseException as error:
                self.lane_starts.append(('lane refused', round(time.monotonic() - began, 2),
                                         'fault' if getattr(lane, '_fault', False) else 'not ready', repr(error)))
                raise

        def opened(lane, *arguments, **options):
            try:
                return original_open(lane, *arguments, **options)
            except BaseException as error:
                self.lane_starts.append(('store open failed', type(error).__name__, getattr(error, 'reason_code', '')))
                raise
        for name, value in (('__init__', init), ('_history', opened)):
            patcher = mock.patch.object(result_persistence.ResultPersistenceLane, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Where a run stopped, for the failure message; the driver itself keeps only a reason code.
        self.stopped_runs, original_fail = [], project_driver.RunDriver._fail

        def record(driver, run, reason):
            self.stopped_runs.append((run.state, reason))
            return original_fail(driver, run, reason)

        patcher = mock.patch.object(project_driver.RunDriver, '_fail', record)
        patcher.start()
        self.addCleanup(patcher.stop)
        original_exchange = client_runtime._run_driver

        def exchange(*args, **options):
            try:
                reply = original_exchange(*args, **options)
            except Exception as error:
                self.stopped_runs.append(('client', args[1], repr(error)))
                raise
            self.stopped_runs.append(('client', args[1], reply))
            return reply

        patcher = mock.patch.object(client_runtime, '_run_driver', exchange)
        patcher.start()
        self.addCleanup(patcher.stop)
        from codex_agents import dispatch_api
        original_call = dispatch_api.DispatchClient.call

        def call(client, operation, *args, **options):
            try:
                view = original_call(client, operation, *args, **options)
            except Exception as error:
                self.stopped_runs.append((operation, repr(error)))
                raise
            self.stopped_runs.append((operation, view.get('state')))
            return view

        patcher = mock.patch.object(dispatch_api.DispatchClient, 'call', call)
        patcher.start()
        self.addCleanup(patcher.stop)
        from codex_agents import prepared_dispatch
        original_advance = prepared_dispatch.PreparedDispatchService._advance

        def advance(service, job):
            before = job.state
            try:
                return original_advance(service, job)
            except Exception as error:
                self.stopped_runs.append(('dispatch-drive', before, repr(error)))
                raise

        patcher = mock.patch.object(prepared_dispatch.PreparedDispatchService, '_advance', advance)
        patcher.start()
        self.addCleanup(patcher.stop)
        original_driver_advance = project_driver.RunDriver._advance

        def driver_advance(driver, run):
            before = run.state
            try:
                return original_driver_advance(driver, run)
            finally:
                if run.state != before:
                    self.stopped_runs.append(('driver', before, run.state))

        patcher = mock.patch.object(project_driver.RunDriver, '_advance', driver_advance)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.threads = [threading.Thread(target=self.collector, name='test-collector')]
        self.threads[0].start()
        self.wait_for(lambda: capability_path(self.paths, None, 'account').exists())
        if project_host:
            self.start_project_host(self.project)

    def tearDown(self):
        # Before the base class removes the state these services own.
        try:
            if self.threads:
                self.stop_services()
        finally:
            super().tearDown()

    def wait_for(self, condition, seconds=20):
        end = time.monotonic() + seconds
        while not condition():
            if self.failures:
                self.fail(self.failures[0])
            self.assertLess(time.monotonic(), end, 'service did not become ready')
            time.sleep(.01)

    def start_collector(self, state, cli_path, *, project_root, timeout=1.0):
        self.restart.set()
        end = time.monotonic() + 10
        while True:
            try:
                return CollectorClient(self.paths.socket).health()
            except CollectorError:
                if time.monotonic() >= end:
                    raise
                time.sleep(.01)

    def collector(self):
        """collector_host.py's loop, restarted in this thread whenever maintenance asks."""
        try:
            while not self.stop.is_set():
                if not self.restart.wait(.05):
                    continue
                self.restart.clear()
                ledger = LearningLedger(self.state, client_capabilities=self.clients)
                server, account = CollectorServer(ledger), None
                self.collector_ledger = ledger
                try:
                    server.start()
                    account = AccountServiceHost(ledger, self.paths)
                    while not self.stop.is_set() and not server.wait(.002):
                        account.serve_once()
                finally:
                    try:
                        if account is not None:
                            account.close()
                            end = time.monotonic() + 10
                            while not account.drained() and time.monotonic() < end:
                                time.sleep(.01)
                            if not account.drained():
                                self.failures.append('account services did not drain')
                    finally:
                        self.collector_ledger = None
                        server.stop()
                        ledger.close()
        except BaseException as error:  # Surface a service failure in the test thread.
            self.failures.append(repr(error))

    def start_project_host(self, project, stop=None):
        stopped = (stop or self.stop).is_set

        def serve():
            try:
                report = serve_project(self.state.root, project, stopped=stopped, bundle_root=self.bundle,
                                       **({} if self.liveness is None else {'liveness': self.liveness}))
                if not report.get('ready'):
                    self.failures.append('project host refused: %r; lane starts: %r' % (report, self.lane_starts))
            except BaseException as error:
                self.failures.append(repr(error))

        thread = threading.Thread(target=serve, name='test-project-host-' + project.name)
        self.threads.append(thread)
        thread.start()
        self.wait_for(lambda: runtime_hint_path(self.paths, project).exists())

    def cli_for(self, project, command, *arguments, expected=0):
        stdout = io.StringIO()
        argv = ['cli.py', command, '--state-root', str(self.state.root), '--project-root', str(project), *arguments]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle), \
                contextlib.redirect_stdout(stdout):
            code = cli.main()
        self.assertEqual(expected, code, stdout.getvalue())
        return json.loads(stdout.getvalue())

    def stop_services(self):
        self.stop.set()
        for thread in reversed(self.threads):
            thread.join(20)
            self.assertFalse(thread.is_alive(), thread.name + ' did not stop')
        self.assertEqual([], self.failures)
        workers = {'account-observation', 'signature-fixed-worker', 'codex-agents-collector', 'account-ingress'}
        self.assertEqual([], [t.name for t in threading.enumerate() if t.name in workers and t.is_alive()])

