"""Actual committed exports and fixed workers; native authorization is synthetic."""
from copy import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import threading
import time
import unittest
from unittest import mock

from tests import test_bridge as fixtures  # puts the repository scripts folder on sys.path
from tests import test_project_runtime as runtime_fixtures

from codex_agents import bridge, codex_preparation, codex_worker
from codex_agents import learning_engine
from codex_agents.codex_preparation import (MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES,
    CodexPreparationLane, PreparationContext, PreparationError, PreparationLimits, preparation_payload)
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.control_api import ControlError, ControlScope, control_request, decode_control_response, encode_control_request
from codex_agents.contracts import validate_result
from codex_agents.learning_contracts import canonical_json
from codex_agents.open_scope_contracts import OpenScopeContext, open_inputs_digest
from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationScope
from codex_agents.project_history import ProjectHistory
from codex_agents.project_runtime import ProjectRuntime
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.service_protocol import ServiceTarget
from tests.test_project_results import KEY

from test_framework import requirements  # after the fixtures import, which adds scripts/ to sys.path (a bare child process imports this module)


class CodexPreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.BridgeTestCase(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.scope = OrchestrationScope(self.fixture.project_id, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
        self.route = self.fixture.bridged_route('codex')
        self.assignment = self.fixture.assignment_for(self.route)
        self.prospective = OpenScopeContext(self.scope, 'orc_'+'a'*48, self.assignment, self.fixture.task(),
                                            self.route, fixtures.PARENT_GRANT)
        self.context = PreparationContext(preparation_payload(self.prospective, self.fixture.project,
                                                               self.fixture.bundle, fixtures.CLEAN))
        directory = self.fixture.state.root/'prepared-history'
        directory.mkdir(mode=0o700)
        def factory():
            return ProjectHistory(directory, self.scope.project_id, result_key=KEY,
                orchestration_limits=OrchestrationLimits(2, 32),
                result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))
        self.runtime = ProjectRuntime(self.scope, factory, runtime_fixtures.limits())
        self.addCleanup(self.stop)
        self.allowed = True
        self.lane = self.runtime.bind_preparation(PreparationLimits(1, MAX_PREPARATION_BYTES, MAX_PREPARATION_EXPORT_BYTES),
            authorizer=lambda context: self.allowed and context is self.context)
        self.gates = []

    def stop(self):
        for gate in getattr(self, 'gates', []):
            gate.set()
        self.runtime.request_close()
        end = time.monotonic()+8
        while not self.runtime.threads_stopped():
            self.assertLess(time.monotonic(), end, 'original preparation worker did not stop')
            time.sleep(.002)

    def until(self, predicate):
        end = time.monotonic()+10
        while not predicate():
            self.runtime.run_once()
            self.assertLess(time.monotonic(), end, 'bounded preparation progress expired')
            time.sleep(.002)

    def prepare(self):
        handle = self.lane.submit(self.context)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'ready', 'failed', 'unknown', 'cleaned'})
        self.assertEqual('ready', self.lane.snapshot(handle)['state'], self.lane.snapshot(handle))
        return handle, self.lane.prepared(handle)

    def discard(self, handle):
        self.lane.discard(handle)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'cleaned', 'unknown'})
        self.assertEqual('cleaned', self.lane.snapshot(handle)['state'])
        self.lane.retire(handle)

    def test_real_export_and_routed_role_are_bound_to_original_worker_before_any_ticket(self):
        (self.fixture.project/'source.txt').write_text('PRIVATE-UNCOMMITTED-BYTES')
        handle, prepared = self.prepare()
        self.assertEqual('marker\n', (prepared.workspace/'export/source.txt').read_text())
        self.assertFalse((prepared.workspace/'export/secret.txt').exists())
        manifest = bridge.committed_export_manifest(self.fixture.project, ['source.txt'])
        self.assertEqual((manifest['file_count'], manifest['commit'], manifest['manifest_digest']), prepared.export_info)
        original = self.runtime.pool._job(prepared.worker)
        data, checked = codex_worker._decode_input(original.payload)
        self.assertEqual(self.assignment, checked)
        self.assertEqual(str(prepared.workspace/'export'), data['export'])
        self.assertEqual(bridge._codex_role_prompt(self.fixture.bundle, self.route), data['role_prompt'])
        self.assertEqual(open_inputs_digest(self.assignment, self.prospective.task, self.route, fixtures.PARENT_GRANT),
                         prepared.inputs_digest)
        self.assertEqual({}, self.runtime.pool._registry._issued)
        self.assertEqual('prepared', original.state)
        self.assertIs(prepared, self.lane.prepared(handle))
        workspace = prepared.workspace
        self.discard(handle)
        self.assertFalse(workspace.exists())
        self.assertNotIn(prepared.worker, self.runtime.pool._jobs)

    def test_held_git_export_does_not_block_original_control_loop_and_cancelled_work_stays_charged_until_return(self):
        entered, resume = threading.Event(), threading.Event()
        self.gates.append(resume)
        original = bridge.export_committed_snapshot
        def held(*args, **options):
            entered.set()
            if not resume.wait(8):
                raise RuntimeError('fixture export release expired')
            return original(*args, **options)
        with mock.patch.object(bridge, 'export_committed_snapshot', new=held):
            handle = self.lane.submit(self.context)
            self.until(entered.is_set)
            server, client = socket.socketpair()
            self.addCleanup(client.close)
            self.runtime.attach_control(server, {'authorized': True})
            request = control_request(ControlScope(**self.scope.as_data()), 'project.status',
                ServiceTarget(self.prospective.orchestration_handle, None, None))
            client.sendall(encode_control_request(request)+b'\n')
            client.setblocking(False)
            raw = bytearray()
            def received():
                try:
                    raw.extend(client.recv(16_384))
                except BlockingIOError:
                    pass
                return b'\n' in raw
            start = time.monotonic()
            self.until(received)
            self.assertLess(time.monotonic()-start, 1)
            with self.assertRaisesRegex(ControlError, 'control_not_authorized'):
                decode_control_response(bytes(raw[:-1]), request)
            self.lane.discard(handle)
            with self.assertRaisesRegex(PreparationError, 'preparation_full'):
                self.lane.submit(self.context)
            self.assertEqual({}, self.runtime.pool._registry._issued)
            resume.set()
            self.until(lambda: self.lane.snapshot(handle)['state'] == 'cleaned')
            self.lane.retire(handle)

    def test_default_denial_and_copied_context_cannot_create_scratch_or_worker(self):
        for context in (copy(self.context), PreparationContext(self.context.payload)):
            with self.assertRaisesRegex(PreparationError, 'preparation_not_authorized'):
                self.lane.submit(context)
        self.allowed = False
        with mock.patch.object(bridge, 'private_workspace', side_effect=AssertionError('denied workspace')):
            with self.assertRaisesRegex(PreparationError, 'preparation_not_authorized'):
                self.lane.submit(self.context)
        self.assertEqual({}, self.lane._jobs)
        self.assertEqual({}, self.runtime.pool._jobs)

    def test_changed_commit_or_role_cannot_produce_prepared_worker(self):
        for kind in ('commit', 'role'):
            if kind == 'role':
                role = self.fixture.bundle/'.codex/agents'/f"{self.route['role']}.toml"
                role.write_text(role.read_text()+'\n# changed original role\n')
            self.context = PreparationContext(preparation_payload(self.prospective, self.fixture.project,
                self.fixture.bundle, fixtures.CLEAN, expected_export_commit='f'*40 if kind == 'commit' else None))
            handle = self.lane.submit(self.context)
            self.until(lambda: self.lane.snapshot(handle)['state'] in {'failed', 'unknown'})
            self.assertEqual('failed', self.lane.snapshot(handle)['state'])
            self.assertEqual({}, self.runtime.pool._jobs)
            self.lane.retire(handle)

    def test_a_failed_preparation_keeps_the_fixed_reason_of_its_stage(self):
        # Every failure used to read preparation_unavailable, so a refused run could not
        # say what stopped it (local-routing's 0.3.2 test: dispatch said only run_unavailable).
        cases = {
            'preparation_managed_configuration': mock.patch.object(
                bridge, 'codex_managed_configuration_present', return_value=True),
            'preparation_client_missing': mock.patch.object(
                codex_preparation, 'resolve_client', return_value=(None, None)),
            'preparation_export_failed': None,
        }
        for reason, patch in cases.items():
            with self.subTest(reason=reason):
                self.context = PreparationContext(preparation_payload(
                    self.prospective, self.fixture.project, self.fixture.bundle, fixtures.CLEAN,
                    expected_export_commit='f' * 40 if patch is None else None))
                with patch or mock.patch.object(self, 'allowed', True):
                    handle = self.lane.submit(self.context)
                    self.until(lambda: self.lane.snapshot(handle)['state'] in {'failed', 'unknown'})
                snapshot = self.lane.snapshot(handle)
                self.lane.retire(handle)
                self.assertEqual({'state': 'failed', 'reason_code': reason}, snapshot)

    def test_an_unexpected_error_names_the_step_it_happened_in(self):
        # local-routing's 0.8.18 test: dispatch said preparation_unavailable and nothing more.
        for target, name, reason in (
                (bridge, 'assignment_read_paths', 'preparation_failed_at_read_paths'),
                (codex_preparation, '_worker_input', 'preparation_failed_at_worker_input'),
                (bridge, '_codex_role_prompt', 'preparation_failed_at_role_prompt')):
            with self.subTest(step=name):
                with mock.patch.object(target, name, side_effect=RuntimeError('unexpected')):
                    handle = self.lane.submit(self.context)
                    self.until(lambda: self.lane.snapshot(handle)['state'] in {'failed', 'unknown'})
                snapshot = self.lane.snapshot(handle)
                self.lane.retire(handle)
                self.assertEqual({'state': 'failed', 'reason_code': reason}, snapshot)

    def test_a_refused_worker_handoff_says_so(self):
        for error, reason in ((codex_worker.CodexWorkerError('worker_busy'), 'preparation_worker_busy'),
                              (RuntimeError('unexpected'), 'preparation_worker_failed')):
            with self.subTest(reason=reason):
                with mock.patch.object(self.runtime.pool, 'prepare', side_effect=error):
                    handle = self.lane.submit(self.context)
                    self.until(lambda: self.lane.snapshot(handle)['state'] in {'failed', 'unknown', 'cleaned'})
                snapshot = self.lane.snapshot(handle)
                self.lane.retire(handle)
                self.assertEqual(reason, snapshot['reason_code'], snapshot)

    def test_revocation_after_export_drops_original_scratch_without_worker_handoff(self):
        handle = self.lane.submit(self.context)
        end = time.monotonic()+8
        while self.lane.snapshot(handle)['state'] == 'queued' or self.lane.snapshot(handle)['state'] == 'exporting':
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        workspace = self.lane._jobs[handle].workspace
        self.allowed = False
        self.until(lambda: self.lane.snapshot(handle)['state'] == 'cleaned')
        self.assertFalse(workspace.exists())
        self.assertEqual({}, self.runtime.pool._jobs)
        self.lane.retire(handle)

    def test_failed_physical_cleanup_remains_unknown_and_cannot_recycle_capacity(self):
        handle, prepared = self.prepare()
        with mock.patch.object(codex_preparation, 'remove_private_directory', new=lambda path: None):
            self.lane.discard(handle)
            self.until(lambda: self.lane.snapshot(handle)['state'] == 'unknown')
        self.assertTrue(prepared.workspace.exists())
        with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
            self.lane.retire(handle)
        with self.assertRaisesRegex(PreparationError, 'preparation_full'):
            self.lane.submit(self.context)
        codex_preparation.remove_private_directory(prepared.workspace)  # Fixture-owned orphan cleanup only.

    @requirements('PREPCLEAN-1', why_not_scenario='Real preparation worker, filesystem removal and capacity boundary; no service restart needed.')
    def test_PREPCLEAN_1_twelve_physical_failures_then_observed_removal(self):
        handle, prepared = self.prepare()
        original = codex_preparation.remove_private_directory
        calls = []
        completed = threading.Event()

        def remove(path):
            calls.append(path)
            if len(calls) == 13:
                original(path)
            completed.set()

        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=remove):
            self.lane.discard(handle)
            for number in range(1, 14):
                self.assertTrue(completed.wait(5))
                self.until(lambda: self.lane.snapshot(handle)['state'] in {'unknown', 'cleaned'})
                self.assertEqual(number, len(calls))
                if number <= 12:
                    self.assertTrue(prepared.workspace.is_dir())
                    self.assertEqual('unknown', self.lane.snapshot(handle)['state'])
                    self.assertEqual(1, self.lane.capacity()['jobs'])
                    self.assertEqual(MAX_PREPARATION_BYTES, self.lane.capacity()['retained_bytes_reserved'])
                    self.assertEqual(MAX_PREPARATION_EXPORT_BYTES, self.lane.capacity()['export_bytes_reserved'])
                    with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
                        self.lane.retire(handle)
                    with self.assertRaisesRegex(PreparationError, 'preparation_full'):
                        self.lane.submit(self.context)
                    completed.clear()
                    self.lane.retry_cleanup(handle)
        self.assertFalse(prepared.workspace.exists())
        self.assertEqual('cleaned', self.lane.snapshot(handle)['state'])
        self.lane.retire(handle)
        self.assertEqual(0, self.lane.capacity()['jobs'])
        self.discard(self.prepare()[0])

    @requirements('PREPCLEAN-1', why_not_scenario='Fault at the remover/lstat proof boundary: a missing child is not a missing preparation directory.')
    def test_PREPCLEAN_1_missing_child_is_not_workspace_removal(self):
        handle, prepared = self.prepare()
        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=FileNotFoundError):
            self.assertFalse(codex_preparation._remove(prepared.workspace))
        self.assertTrue(prepared.workspace.is_dir())
        self.discard(handle)

    @requirements('PREPCLEAN-1', 'PREPCLEAN-2', why_not_scenario='Real worker and folder with direct driver/host predicates; runnable when Unix socket binding is denied.')
    def test_PREPCLEAN_real_cleanup_keeps_host_and_slot_until_thirteenth_removal(self):
        from types import SimpleNamespace
        from tests.test_run_driver_retries import driver
        from codex_agents.service_host import ProjectServiceHost
        value = driver()
        value._preparation = self.lane
        value._runs, value._dispatch = {}, SimpleNamespace(_jobs={})
        handle, prepared = self.prepare()
        value._giving_back = [handle]
        original = codex_preparation.remove_private_directory
        attempts = []
        completed = threading.Event()
        def remove(path):
            attempts.append(path)
            if len(attempts) == 13:
                original(path)
            completed.set()
        host = object.__new__(ProjectServiceHost)
        host._owner = mock.Mock()
        host._driver = value
        host._clock_ns = lambda: 2_000_000_000_000
        host._frontend = SimpleNamespace(last_enrolled_ns=0)
        host._runtime = SimpleNamespace(pool=self.runtime.pool, persistence=SimpleNamespace(_jobs={}))
        with mock.patch.object(codex_preparation, 'remove_private_directory', side_effect=remove):
            self.lane.discard(handle)
            for number in range(1, 14):
                self.assertTrue(completed.wait(5))
                self.until(lambda: self.lane.snapshot(handle)['state'] in {'unknown', 'cleaned'})
                self.assertEqual(number, len(attempts))
                if number <= 12:
                    self.assertTrue(prepared.workspace.is_dir())
                    self.assertTrue(host.holding())
                    self.assertEqual(0, host.idle_ns())  # even past the automatic replacement deadline
                    self.assertFalse(host.replacing())
                    self.assertFalse(host.exhausted())
                    self.assertEqual(1, self.lane.capacity()['jobs'])
                    completed.clear()
                    value._clock.now += 60
                    value._retire_given_back()
            self.assertFalse(prepared.workspace.exists())
            value._retire_given_back()
        self.assertEqual([], value._giving_back)
        self.assertEqual(0, self.lane.capacity()['jobs'])
        self.assertFalse(host.holding())
        self.assertEqual(2_000_000_000_000, host.idle_ns())

    @requirements('PREPCLEAN-3', why_not_scenario='Real framed history service encode/decode with live owner diagnostics, including malformed and past-bound counts; no socket privilege required.')
    def test_PREPCLEAN_3_capacity_transport_warning_and_clear(self):
        from codex_agents.history_api import (HistoryRequest, history_request_digest,
            encode_history_request, decode_history_response)
        from codex_agents.history_service import HistoryAuthorization, HistoryService
        from codex_agents.history_contracts import HistoryError
        from tests.test_bridge import cli
        directory = self.fixture.state.root / 'cleanup-status-history'
        directory.mkdir(mode=0o700)
        count = [1]
        def authorize(request, token):
            return HistoryAuthorization(self.scope.project_id, self.scope.runtime_digest,
                history_request_digest(request), 'host', frozenset({'history.capacity_report'}),
                run_slots=(0, 3), preparation_cleanup_pending=count[0])
        service = HistoryService(self.scope.project_id, self.scope.runtime_digest,
            lambda: ProjectHistory(directory, self.scope.project_id, result_key=KEY,
                result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000)), authorizer=authorize)
        self.addCleanup(service.close)
        request = HistoryRequest(1, 'history.capacity_report', 'capacity',
                                 self.scope.project_id, self.scope.runtime_digest, {})
        for pending in (1, 0):
            count[0] = pending
            raw = service.handle(encode_history_request(request), None)
            report = decode_history_response(raw, request)
            self.assertEqual(pending, report['run_slots']['cleanup_pending'])
            status = cli._service_slots(report['run_slots'])
            self.assertEqual(bool(pending), bool(status['warnings']))
            self.assertNotIn(str(directory), str(status))
            for bad in (-1, True, 65, 'one'):
                malformed = json.loads(raw)
                malformed['result']['run_slots']['cleanup_pending'] = bad
                with self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                    decode_history_response(canonical_json(malformed), request)
        # The diagnostic shares the existing run-slot wire bound (64), not a new admission cap.
        for bad in (-1, True, 65, 'one'):
            count[0] = bad
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                authorize(request, None)

    @requirements('PREPCLEAN-3', why_not_scenario='Real framed history service encode/decode of the unlocated-folder count, with malformed and past-bound values.')
    def test_PREPCLEAN_3_unlocated_count_transport(self):
        from codex_agents.history_api import (HistoryRequest, history_request_digest,
            encode_history_request, decode_history_response)
        from codex_agents.history_service import HistoryAuthorization, HistoryService
        from codex_agents.history_contracts import HistoryError
        directory = self.fixture.state.root / 'cleanup-unlocated-history'
        directory.mkdir(mode=0o700)

        def authorize(request, token, unlocated=2):
            return HistoryAuthorization(self.scope.project_id, self.scope.runtime_digest,
                history_request_digest(request), 'host', frozenset({'history.capacity_report'}),
                run_slots=(0, 3), preparation_cleanup_pending=3, preparation_cleanup_unlocated=unlocated)
        service = HistoryService(self.scope.project_id, self.scope.runtime_digest,
            lambda: ProjectHistory(directory, self.scope.project_id, result_key=KEY,
                result_capacity=ResultCapacity(8, 2*MAX_RESULT_BODY_BYTES, 1_800_000, 120_000)), authorizer=authorize)
        self.addCleanup(service.close)
        request = HistoryRequest(1, 'history.capacity_report', 'capacity',
                                 self.scope.project_id, self.scope.runtime_digest, {})
        raw = service.handle(encode_history_request(request), None)
        report = decode_history_response(raw, request)
        self.assertEqual((3, 2), (report['run_slots']['cleanup_pending'], report['run_slots']['cleanup_unlocated']))
        for bad in (-1, True, 65, 'one'):
            malformed = json.loads(raw)
            malformed['result']['run_slots']['cleanup_unlocated'] = bad
            with self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json(malformed), request)
            with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
                authorize(request, None, unlocated=bad)

    @requirements('PREPCLEAN-3', why_not_scenario='Real lane job states: a given-back job knows its folder, or says it never learned it.')
    def test_PREPCLEAN_3_lane_says_whether_a_pending_folder_is_located(self):
        handle, prepared = self.prepare()
        job = self.lane._jobs[handle]
        self.assertEqual((None, False), (self.lane.workspace_of(handle), self.lane.unlocated(handle)))  # not given back
        with self.lane._lock:
            job.state, job.cancelled = 'unknown', True
        self.assertEqual((prepared.workspace, False), (self.lane.workspace_of(handle), self.lane.unlocated(handle)))
        with self.lane._lock:
            job.workspace = None
        self.assertEqual((None, True), (self.lane.workspace_of(handle), self.lane.unlocated(handle)))
        with self.lane._lock:
            job.state, job.workspace = 'exported', prepared.workspace
        self.discard(handle)

    @requirements('PREPCLEAN-4', why_not_scenario='Real folders: a recorded path is removed only when it is one of this host\'s own preparation folders, and only observed absence is proof.')
    def test_PREPCLEAN_4_recorded_folder_removal_checks_ownership_and_proves_by_absence(self):
        handle, prepared = self.prepare()
        folder = prepared.workspace
        self.lane.discard(handle)
        self.until(lambda: self.lane.snapshot(handle)['state'] == 'cleaned')
        self.lane.retire(handle)
        self.assertFalse(folder.exists())
        self.assertTrue(codex_preparation.remove_recorded_workspace(str(folder)))  # already absent: proven
        # A record that is not one of this host's own preparation folders is never touched (nothing of ours to remove).
        outside = self.fixture.state.root / 'prepare-elsewhere'
        outside.mkdir(mode=0o700)
        for foreign in ('relative/prepare-x', str(outside.parent / 'not-a-preparation'), str(outside)):
            with self.subTest(path=foreign):
                self.assertTrue(codex_preparation.remove_recorded_workspace(foreign))
        self.assertTrue(outside.is_dir())
        # A link standing where a folder of ours was is not removed, and is not proof of removal.
        link = folder.parent / 'prepare-link'
        os.symlink(outside, link)
        self.addCleanup(lambda: link.is_symlink() and link.unlink())
        self.assertFalse(codex_preparation.remove_recorded_workspace(str(link)))
        self.assertTrue(link.is_symlink() and outside.is_dir())
        link.unlink()
        # A real one of ours is removed; a refused removal is not proof.
        made = codex_preparation.bridge.private_workspace('prepare-', claude=False, project_root=None)
        with mock.patch.object(codex_preparation, 'remove_private_directory'):
            self.assertFalse(codex_preparation.remove_recorded_workspace(str(made)))
        self.assertTrue(made.is_dir())
        self.assertTrue(codex_preparation.remove_recorded_workspace(str(made)))
        self.assertFalse(made.exists())

    @requirements('HM2A-1', why_not_scenario='Real preparation worker and folder; the marker is the HM-2a owner proof.')
    def test_HM2A_1_a_preparation_folder_names_the_service_that_owns_it(self):
        handle, prepared = self.prepare()
        self.assertFalse((prepared.workspace/'cocolo-owner.json').exists())  # a lane told no owner writes none
        self.discard(handle)
        state_root, instance = self.fixture.state.root, 'f'*32
        self.lane.set_owner(state_root, self.scope.project_id, instance)
        with self.assertRaisesRegex(PreparationError, 'preparation_invalid_input'):
            self.lane.set_owner(state_root, self.scope.project_id, 'not-an-instance')
        handle, prepared = self.prepare()
        marker = prepared.workspace/'cocolo-owner.json'
        info = os.lstat(marker)
        self.assertEqual((0o600, os.geteuid()), (info.st_mode & 0o777, info.st_uid))
        self.assertEqual({'version': 1, 'kind': 'cocolo_preparation', 'state_root': os.path.realpath(state_root),
                          'project_id': self.scope.project_id, 'instance': instance}, json.loads(marker.read_text()))
        self.assertEqual(['control', 'export', 'tmp'], sorted(set(os.listdir(prepared.workspace)) - {marker.name}))
        workspace = prepared.workspace
        self.discard(handle)
        self.assertFalse(workspace.exists())

    @requirements('HM2A-1', why_not_scenario='Real preparation worker and folder; the marker close is made to fail.')
    def test_HM2A_1_a_marker_that_fails_to_close_leaves_a_report_only_folder(self):
        from codex_agents import health_monitor, health_workspaces
        state_root, instance = Path(os.path.realpath(self.fixture.state.root)), 'e'*32
        launches = state_root/'projects'/self.scope.project_id/'launches'
        launches.mkdir(mode=0o700, parents=True, exist_ok=True)
        (launches/(instance + '.lock')).touch(mode=0o600)  # free: with a valid marker the folder would be removable
        self.lane.set_owner(state_root, self.scope.project_id, instance)
        real_open, real_close, markers = os.open, os.close, set()

        def opener(path, flags, *args, **options):
            descriptor = real_open(path, flags, *args, **options)
            if str(path).endswith('cocolo-owner.json') and flags & os.O_CREAT:
                markers.add(descriptor)
            return descriptor

        def closer(descriptor):
            real_close(descriptor)
            if descriptor in markers:
                markers.discard(descriptor)
                raise OSError(5, 'injected close failure')

        with mock.patch.object(os, 'open', opener), mock.patch.object(os, 'close', closer):
            handle, prepared = self.prepare()  # preparation still succeeds
        workspace, marker = prepared.workspace, prepared.workspace/'cocolo-owner.json'
        self.assertFalse(os.path.lexists(marker))  # no valid marker: the folder is report-only
        later = int((time.time() + 2 * health_workspaces.MIN_ORPHAN_AGE_SECONDS) * 1000)
        found = health_workspaces.WorkspaceCheck(container=workspace.parent, live=lambda: set(), pending=lambda: set(),
                                                 state_root=state_root).run(
            health_monitor.CheckContext(deadline=time.monotonic() + 30, now_ms=later, since_last_pass_ms=None))
        self.assertEqual([('owner_unknown', 'folders')], [(item.reason_code, item.subject) for item in found])  # PREP-1
        self.assertTrue(found[0].text.startswith('1 leftover CoCoLo preparation folder is not in use'))
        self.assertTrue(workspace.is_dir())
        self.discard(handle)
        self.assertFalse(workspace.exists())

    def test_a_failed_cleanup_is_asked_again_and_then_frees_its_slot(self):
        handle, prepared = self.prepare()
        with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
            self.lane.retry_cleanup(handle)  # Never for a preparation that was not given back.
        with mock.patch.object(codex_preparation, 'remove_private_directory', new=lambda path: None):
            self.lane.discard(handle)
            self.until(lambda: self.lane.snapshot(handle)['state'] == 'unknown')
            self.lane.retry_cleanup(handle)  # Removal still fails: unknown again, nothing freed.
            self.until(lambda: self.lane.snapshot(handle)['state'] == 'unknown')
        self.assertTrue(prepared.workspace.exists())
        self.lane.retry_cleanup(handle)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'cleaned', 'unknown'})
        self.assertEqual('cleaned', self.lane.snapshot(handle)['state'])
        self.assertFalse(prepared.workspace.exists())
        self.lane.retire(handle)
        self.discard(self.prepare()[0])  # Its slot is free again.

    def test_a_preparation_without_its_recorded_worker_refuses_a_cleanup_retry_plainly(self):
        handle, prepared = self.prepare()
        self.addCleanup(codex_preparation.remove_private_directory, prepared.workspace)
        job = self.lane._job(handle)
        recorded, job.prepared_snapshot = job.prepared_snapshot, None
        with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
            self.lane.retry_cleanup(handle)
        job.prepared_snapshot = recorded
        self.discard(handle)

    def test_a_dead_cleanup_worker_is_never_asked_again(self):
        handle, prepared = self.prepare()
        self.addCleanup(codex_preparation.remove_private_directory, prepared.workspace)
        self.lane._slots[0].incoming.put_nowait(None)
        self.until(lambda: not self.lane._slots[0].thread.is_alive())
        with self.assertRaisesRegex(PreparationError, 'preparation_unavailable'):
            self.lane.discard(handle)
        with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
            self.lane.retry_cleanup(handle)
        self.assertEqual('unknown', self.lane.snapshot(handle)['state'])

    def test_changed_context_and_forged_prepared_metadata_refuse_without_withdrawing_foreign_worker(self):
        handle, prepared = self.prepare()
        object.__setattr__(prepared, 'inputs_digest', 'f'*64)
        with self.assertRaisesRegex(PreparationError, 'preparation_not_authorized'):
            self.lane.prepared(handle)
        self.discard(handle)
        self.context = PreparationContext(self.context.payload)
        def changed(context):
            object.__setattr__(context, 'payload', b'{}')
            return True
        self.lane._authorizer = changed
        with self.assertRaisesRegex(PreparationError, 'preparation_not_authorized'):
            self.lane.submit(self.context)
        self.assertEqual({}, self.runtime.pool._jobs)

    def test_limits_and_wrong_runtime_scope_do_not_create_execution(self):
        with self.assertRaises(PreparationError):
            PreparationLimits(2, MAX_PREPARATION_BYTES-1, MAX_PREPARATION_EXPORT_BYTES)
        prospective = replace(self.prospective, scope=replace(self.scope, runtime_digest='f'*64))
        self.context = PreparationContext(preparation_payload(prospective, self.fixture.project,
                                                             self.fixture.bundle, fixtures.CLEAN))
        handle = self.lane.submit(self.context)
        self.until(lambda: self.lane.snapshot(handle)['state'] == 'failed')
        self.assertEqual({}, self.runtime.pool._jobs)
        self.assertEqual({}, self.runtime.pool._registry._issued)
        self.lane.retire(handle)

    def test_each_byte_budget_limits_intake_even_with_an_idle_prestarted_worker(self):
        for buffers, exports in ((1, 2), (2, 1)):
            with self.subTest(buffer_slots=buffers, export_slots=exports):
                entered, resume = threading.Event(), threading.Event()
                self.gates.append(resume)
                lane = CodexPreparationLane(self.scope, self.runtime.pool,
                    PreparationLimits(2, buffers*MAX_PREPARATION_BYTES, exports*MAX_PREPARATION_EXPORT_BYTES),
                    authorizer=lambda context: context is self.context)
                original = bridge.export_committed_snapshot
                def held(*args, **options):
                    entered.set()
                    if not resume.wait(8):
                        raise RuntimeError('fixture export release expired')
                    return original(*args, **options)
                try:
                    with mock.patch.object(bridge, 'export_committed_snapshot', new=held):
                        handle = lane.submit(self.context)
                        self.assertTrue(entered.wait(5))
                        lane.discard(handle)
                        with self.assertRaisesRegex(PreparationError, 'preparation_full'):
                            lane.submit(self.context)
                        self.assertEqual(1, lane.capacity()['jobs'])
                        self.assertEqual(MAX_PREPARATION_BYTES, lane.capacity()['retained_bytes_reserved'])
                        self.assertEqual(MAX_PREPARATION_EXPORT_BYTES, lane.capacity()['export_bytes_reserved'])
                        resume.set()
                        self.until(lambda: lane.snapshot(handle)['state'] == 'cleaned')
                        with self.assertRaisesRegex(PreparationError, 'preparation_full'):
                            lane.submit(self.context)
                        lane.retire(handle)
                        self.assertEqual(0, lane.capacity()['retained_bytes_reserved'])
                        self.assertEqual(0, lane.capacity()['export_bytes_reserved'])
                        following = lane.submit(self.context)
                        lane.discard(following)
                        self.until(lambda: lane.snapshot(following)['state'] == 'cleaned')
                        lane.retire(following)
                finally:
                    resume.set()
                    lane.request_close()
                    self.until(lane.threads_stopped)

    def test_dead_cleanup_worker_retains_unknown_export_and_cannot_queue_false_cleanup(self):
        handle, prepared = self.prepare()
        self.addCleanup(codex_preparation.remove_private_directory, prepared.workspace)
        self.lane._slots[0].incoming.put_nowait(None)
        self.until(lambda: not self.lane._slots[0].thread.is_alive())
        with self.assertRaisesRegex(PreparationError, 'preparation_unavailable'):
            self.lane.discard(handle)
        self.assertEqual('unknown', self.lane.snapshot(handle)['state'])
        self.assertTrue(prepared.workspace.exists())
        with self.assertRaisesRegex(PreparationError, 'preparation_busy'):
            self.lane.retire(handle)
        self.assertEqual(MAX_PREPARATION_EXPORT_BYTES, self.lane.capacity()['export_bytes_reserved'])
        self.assertTrue(self.lane._slots[0].incoming.empty())

    def test_hard_quarantine_requires_original_pool_shutdown(self):
        with self.assertRaisesRegex(PreparationError, 'preparation_invalid_input'):
            self.lane.request_close(abandon_bound=True)
        self.assertFalse(self.lane._closing)
        handle, prepared = self.prepare()
        self.discard(handle)
        self.assertFalse(prepared.workspace.exists())

    def test_the_result_schema_comes_from_the_running_runtime_never_the_project(self):
        # Unlike a role, the schema is bound to no digest, so like the CLI (which reads it
        # from ORCHESTRATION_ROOT) preparation reads it from the runtime it runs from: a
        # session that can write the project cannot change what a child is told to return.
        (self.fixture.bundle/'.codex/orchestration/schemas/result.schema.json').write_text('not json')
        handle, prepared = self.prepare()
        self.discard(handle)

    def test_oversized_installed_schema_is_refused_before_account_readiness(self):
        self.enterContext(mock.patch.object(codex_preparation, 'RUNTIME_ROOT', self.fixture.bundle))
        schema = self.fixture.bundle/'.codex/orchestration/schemas/result.schema.json'
        schema.write_bytes(b' '*(MAX_RESULT_BODY_BYTES+1))
        handle = self.lane.submit(self.context)
        self.until(lambda: self.lane.snapshot(handle)['state'] in {'ready', 'failed', 'unknown'})
        self.assertEqual('failed', self.lane.snapshot(handle)['state'])
        self.assertEqual({}, self.runtime.pool._jobs)
        self.assertEqual({}, self.runtime.pool._registry._issued)
        self.lane.retire(handle)

    def test_shutdown_after_export_publication_drains_queued_physical_cleanup(self):
        published, resume = threading.Event(), threading.Event()
        self.gates.append(resume)
        original = self.lane._lock
        lane, worker_thread = self.lane, self.lane._slots[0].thread
        class PublicationLock:
            def __enter__(self):
                original.acquire()
            def __exit__(self, *args):
                pause = (threading.current_thread() is worker_thread and not published.is_set()
                         and any(job.state == 'exported' for job in lane._jobs.values()))
                original.release()
                if pause:
                    published.set()
                    if not resume.wait(5):
                        raise RuntimeError('fixture publication release expired')
        lane._lock = PublicationLock()
        handle = lane.submit(self.context)
        self.assertTrue(published.wait(5))
        workspace = lane._jobs[handle].workspace
        self.addCleanup(codex_preparation.remove_private_directory, workspace)
        self.runtime.request_close()
        resume.set()
        end = time.monotonic()+5
        while not self.runtime.threads_stopped():
            self.assertLess(time.monotonic(), end)
            time.sleep(.002)
        self.assertFalse(workspace.exists(), 'thread exit abandoned queued original export cleanup')
        self.assertEqual('cleaned', lane.snapshot(handle)['state'])

    def test_fifo_installed_schema_refuses_without_waiting_for_a_writer(self):
        self.enterContext(mock.patch.object(codex_preparation, 'RUNTIME_ROOT', self.fixture.bundle))
        schema = self.fixture.bundle/'.codex/orchestration/schemas/result.schema.json'
        schema.unlink()
        os.mkfifo(schema, 0o600)
        entered = threading.Event()
        original = learning_engine.os.open
        def opened(path, flags, *args, **options):
            if path == 'result.schema.json':
                entered.set()
            return original(path, flags, *args, **options)
        with mock.patch.object(learning_engine.os, 'open', new=opened):
            handle = self.lane.submit(self.context)
            try:
                self.assertTrue(entered.wait(5))
                end = time.monotonic()+.5
                while self.lane.snapshot(handle)['state'] in {'queued', 'exporting'} and time.monotonic() < end:
                    self.runtime.run_once()
                    time.sleep(.002)
                self.assertEqual('failed', self.lane.snapshot(handle)['state'])
            finally:
                try:
                    writer = original(schema, os.O_WRONLY | os.O_NONBLOCK)
                except OSError:
                    pass
                else:
                    os.close(writer)  # Release only the old buggy fixture read, if blocked.

    def test_malformed_and_nonobject_schema_refuse_before_account_readiness(self):
        self.enterContext(mock.patch.object(codex_preparation, 'RUNTIME_ROOT', self.fixture.bundle))
        schema = self.fixture.bundle/'.codex/orchestration/schemas/result.schema.json'
        for contents in (b'{', b'[]'):
            with self.subTest(contents=contents):
                schema.write_bytes(contents)
                handle = self.lane.submit(self.context)
                self.until(lambda: self.lane.snapshot(handle)['state'] in {'ready', 'failed', 'unknown'})
                try:
                    self.assertEqual('failed', self.lane.snapshot(handle)['state'])
                    self.assertEqual({}, self.runtime.pool._jobs)
                finally:
                    if self.lane.snapshot(handle)['state'] == 'ready':
                        self.discard(handle)
                    else:
                        self.lane.retire(handle)

    def test_valid_near_limit_verifier_result_copy_stays_within_original_body_budget(self):
        body = fixtures.result()
        body['summary'] = ''
        body['summary'] = 'x'*(MAX_RESULT_BODY_BYTES-len(canonical_json(body)))
        self.assertEqual(MAX_RESULT_BODY_BYTES, len(canonical_json(body)))
        validate_result(body, assignment=self.assignment)
        export = self.fixture.state.root/'verifier-copy'
        export.mkdir(mode=0o700)
        relative = bridge.place_worker_result(export, body)
        self.assertLessEqual((export/relative).stat().st_size, MAX_RESULT_BODY_BYTES)
