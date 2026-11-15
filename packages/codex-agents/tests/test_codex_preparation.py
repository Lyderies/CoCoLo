"""Actual committed exports and fixed workers; native authorization is synthetic."""
from copy import copy
from dataclasses import replace
import os
import socket
import threading
import time
import unittest
from unittest import mock

from tests import test_bridge as fixtures
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

    def test_oversized_installed_schema_is_refused_before_account_readiness(self):
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
