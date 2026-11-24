"""RS-E E2a-5..9: the authenticated wake publish, its generations, the project service's crash-safe publishes and
the collector's reconcile.

Wire tests run the product collector and project host processes, started by the unmodified CLI, on a short TMPDIR.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests import test_learning_store as store_tests
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from tests.learning_support import generate_test_owner_key
from tests.test_rsf_s1_ingress import UPDATE_COMMAND, UPGRADE_MESSAGE

from codex_agents import learning_store, project_holds_schema, project_wake
from codex_agents.account_api import (
    ACCOUNT_PROTOCOL_VERSION, AccountError, AccountRequest, _checked_result, decode_account_response,
    encode_account_request,
)
from codex_agents.account_service import AccountOwnerViews, AccountService
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
from codex_agents.collector_paths import CollectorPaths
from codex_agents.hosts import CODEX_HOST_CLIENT
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.reply_shapes import REPLY_FIELDS, reply_fields_match
from codex_agents.service_enrollment import (
    account_endpoint_path, enrollment_preamble, issue_wake_capability, proof_directory, read_capability,
    read_wake_capability,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements  # noqa: E402

SANDBOXED = sys.platform.startswith('linux') or (sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())


def wake_payload(epoch=None, seq=1, state='exact', due=None):
    return {'epoch': epoch, 'seq': seq, 'state': state, 'next_due_at_ms': due}


class StoreBase(unittest.TestCase):
    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger

    def row(self, project_id):
        found = self.ledger._connection.execute('SELECT * FROM project_wake WHERE project_id=?', (project_id,)).fetchone()
        return None if found is None else dict(found)


class WakeLedgerTests(StoreBase):
    @requirements('E2a-6', why_not_scenario='Generation rules on a real ledger; the wire is WakeWireTests.')
    def test_E2a_6_ledger_generation_rules(self):
        project = self.fixture.project_a_id
        first = self.ledger.publish_wake(project, epoch=None, seq=1, state='pending', next_due_at_ms=None, now_ms=10)
        self.assertEqual({'epoch': 1, 'seq': 1, 'outcome': 'accepted'}, first)
        second = self.ledger.publish_wake(project, epoch=None, seq=1, state='pending', next_due_at_ms=None, now_ms=11)
        self.assertEqual(2, second['epoch'])
        exact = self.ledger.publish_wake(project, epoch=2, seq=2, state='exact', next_due_at_ms=500, now_ms=12)
        self.assertEqual('accepted', exact['outcome'])
        before = self.row(project)
        self.assertEqual((2, 2, 'exact', 500, 500), (before['epoch'], before['seq'], before['state'],
                                                     before['next_due_at_ms'], before['due_at_ms']))
        self.assertEqual('duplicate', self.ledger.publish_wake(project, epoch=2, seq=2, state='exact',
                                                               next_due_at_ms=500, now_ms=13)['outcome'])
        self.assertEqual(before, self.row(project))
        for epoch, seq, due in ((2, 2, 600), (2, 1, 500), (1, 9, 500), (3, 1, 500), (0, 1, 500)):
            with self.subTest(epoch=epoch, seq=seq, due=due):
                self.assertEqual('stale', self.ledger.publish_wake(project, epoch=epoch, seq=seq, state='exact',
                                                                   next_due_at_ms=due, now_ms=14)['outcome'])
        after = self.row(project)
        self.assertEqual(5, after['refused'])
        self.assertEqual({k: v for k, v in before.items() if k not in {'refused', 'updated_at_ms'}},
                         {k: v for k, v in after.items() if k not in {'refused', 'updated_at_ms'}})
        # Durable: a reopened ledger allocates past every earlier epoch.
        self.ledger.close()
        self.ledger = self.fixture.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.assertEqual(3, self.ledger.publish_wake(project, epoch=None, seq=1, state='pending', next_due_at_ms=None,
                                                     now_ms=20)['epoch'])
        with self.assertRaises(learning_store.ProjectNotRegisteredError):
            self.ledger.publish_wake('prj_' + '0' * 32, epoch=None, seq=1, state='exact', next_due_at_ms=None)


class WakeProtocolTests(StoreBase):
    @requirements('E2a-6', why_not_scenario='Reply-shape table and the RS-F S1 gate on the real AccountService handler.')
    def test_E2a_6_reply_shape_row_and_closed_reply(self):
        self.assertEqual({'optional': [], 'required': ['epoch', 'outcome', 'seq']}, REPLY_FIELDS['account.publish_wake'])
        request = AccountRequest(1, 'account.publish_wake', 'w', self.fixture.project_a_id, COLLECTOR_RUNTIME_DIGEST,
                                 wake_payload(epoch=None, seq=3))
        good = {'epoch': 4, 'seq': 3, 'outcome': 'accepted'}
        self.assertTrue(reply_fields_match('account.publish_wake', good))
        self.assertEqual(good, _checked_result(good, request))
        for bad in ({**good, 'extra': 1}, {**good, 'seq': 2}, {**good, 'outcome': 'stale'}, {**good, 'epoch': 0},
                    {**good, 'epoch': True}):
            with self.subTest(bad=bad), self.assertRaises(AccountError):
                _checked_result(bad, request)
        pinned = AccountRequest(1, 'account.publish_wake', 'w', self.fixture.project_a_id, COLLECTOR_RUNTIME_DIGEST,
                                wake_payload(epoch=4, seq=3))
        with self.assertRaises(AccountError):
            _checked_result({**good, 'epoch': 5}, pinned)
        for payload in ({}, {**wake_payload(), 'extra': 1}, wake_payload(seq=0), wake_payload(state='running'),
                        wake_payload(due=-1), wake_payload(epoch=0), wake_payload(seq=True)):
            with self.subTest(payload=payload), self.assertRaises(AccountError):
                AccountRequest(1, 'account.publish_wake', 'w', self.fixture.project_a_id, COLLECTOR_RUNTIME_DIGEST,
                               payload)

    @requirements('E2a-6', why_not_scenario='Reply-shape table and the RS-F S1 gate on the real AccountService handler.')
    def test_E2a_6_runtime_gate_refuses_an_old_caller_before_authorization(self):
        authorize = mock.Mock(side_effect=AssertionError('runtime gate must precede authorizer'))
        service = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(service.close)
        for fields in ({}, {'runtime_digest': '0' * 64}):
            with self.subTest(fields=fields):
                raw = canonical_json({'protocol_version': ACCOUNT_PROTOCOL_VERSION, 'operation': 'account.publish_wake',
                                      'request_id': 'old', 'project_id': self.fixture.project_a_id,
                                      'payload': wake_payload(), **fields})
                before = self.ledger._connection.total_changes
                response = json.loads(service.handle(raw))
                self.assertEqual(before, self.ledger._connection.total_changes)
                self.assertEqual(('runtime_upgrade_required', UPGRADE_MESSAGE, UPDATE_COMMAND),
                                 (response['reason_code'], response['message'], response['update_command']))
        authorize.assert_not_called()


class WakeReconcileTests(StoreBase):
    @requirements('E2a-9', why_not_scenario='The real collector server and ledger in process; nothing is spawned.')
    def test_E2a_9_unknown_projects_are_marked_due_at_collector_start_and_clean_stops_are_not(self):
        a, b = self.fixture.project_a_id, self.fixture.project_b_id
        epoch = self.ledger.publish_wake(b, epoch=None, seq=1, state='clean_stop', next_due_at_ms=None)['epoch']
        started = time.time_ns() // 1_000_000
        with mock.patch('subprocess.Popen', side_effect=AssertionError('nothing starts')), \
                mock.patch('os.posix_spawn', side_effect=AssertionError('nothing starts')):
            server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
            server.start()
            self.addCleanup(server.stop)
            with self.ledger._lock:
                row_a, row_b = self.row(a), self.row(b)
        self.assertIsNotNone(row_a)  # no row: unknown, marked due before ready
        self.assertIsNone(row_a['state'])
        self.assertGreaterEqual(row_a['due_at_ms'], started)
        self.assertEqual((epoch, 'clean_stop', None), (row_b['epoch'], row_b['state'], row_b['due_at_ms']))

    @requirements('E2a-9', why_not_scenario='The real collector server and ledger in process; nothing is spawned.')
    def test_E2a_9_a_newer_publication_is_not_overwritten_and_serving_projects_are_skipped(self):
        a, b = self.fixture.project_a_id, self.fixture.project_b_id
        self.ledger.publish_wake(a, epoch=None, seq=1, state='exact', next_due_at_ms=10**15)
        self.ledger.publish_wake(b, epoch=None, seq=1, state='pending', next_due_at_ms=None)
        candidates = self.ledger.wake_reconcile_candidates(now_ms=1_000, limit=64)
        self.assertEqual({a, b}, {row[0] for row in candidates})
        self.ledger.publish_wake(a, epoch=1, seq=2, state='exact', next_due_at_ms=10**15)  # lands in between
        self.assertEqual(1, self.ledger.mark_wakes_due(candidates, now_ms=1_000))
        self.assertEqual((2, 10**15), (self.row(a)['seq'], self.row(a)['due_at_ms']))
        self.assertEqual(1_000, self.row(b)['due_at_ms'])
        self.assertEqual(1, len(self.ledger.wake_reconcile_candidates(now_ms=1_000, limit=1)))
        # A served project is never marked.
        server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        with mock.patch.object(learning_store, 'project_host_serving', return_value=True) as serving:
            server._reconcile_wakes()
        self.assertTrue(serving.called)
        self.assertEqual(10**15, self.row(a)['due_at_ms'])


class FakeSender:
    def __init__(self):
        self.sent, self.fail, self.epoch = [], False, 0
        self.lock = threading.Lock()

    def __call__(self, payload, timeout):
        with self.lock:
            self.sent.append(dict(payload))
            if self.fail:
                raise AccountError('account_unavailable')
            if payload['epoch'] is None:
                self.epoch += 1
            return {'epoch': payload['epoch'] or self.epoch, 'seq': payload['seq'], 'outcome': 'accepted'}


class WakePublisherTests(unittest.TestCase):
    def publisher(self, sender, **options):
        publisher = project_wake.WakePublisher(None, None, 'prj_' + 'a' * 32, send=sender, **options)
        self.addCleanup(publisher.close)
        return publisher

    @requirements('E2a-7', why_not_scenario='The publisher ordering rules with a recording sender; the wire is WakeServiceTests.')
    def test_E2a_7_earlier_deadlines_need_a_pending_ack_and_exact_never_overtakes_it(self):
        sender = FakeSender()
        publisher = self.publisher(sender, start_sender=False)
        publisher.before_due(None)  # first write of this instance: pending first
        self.assertEqual([('pending', None, None)], [(s['state'], s['epoch'], s['next_due_at_ms']) for s in sender.sent])
        publisher.after_settle(1_000)
        publisher.drain()
        self.assertEqual(('exact', 1, 1_000), (sender.sent[-1]['state'], sender.sent[-1]['epoch'],
                                               sender.sent[-1]['next_due_at_ms']))
        count = len(sender.sent)
        publisher.before_due(2_000)  # later than published: no pending needed
        publisher.before_due(None)
        self.assertEqual(count, len(sender.sent))
        publisher.after_settle(1_000)  # an exact computed now ...
        publisher.before_due(500)  # ... is overtaken by an earlier deadline's pending, sent at once
        self.assertEqual(('pending', 500), (sender.sent[-1]['state'], sender.sent[-1]['next_due_at_ms']))
        publisher.drain()
        self.assertEqual('pending', sender.sent[-1]['state'])  # the stale exact was dropped
        publisher.after_settle(500)
        publisher.drain()
        self.assertEqual(('exact', 500), (sender.sent[-1]['state'], sender.sent[-1]['next_due_at_ms']))
        seqs = [s['seq'] for s in sender.sent]
        self.assertEqual(sorted(set(seqs)), seqs)  # strictly increasing

    @requirements('E2a-7', why_not_scenario='The publisher ordering rules with a recording sender; the wire is WakeServiceTests.')
    def test_E2a_7_unreachable_collector_lets_the_write_proceed_flagged(self):
        sender = FakeSender()
        sender.fail = True
        publisher = self.publisher(sender, start_sender=False)
        # Review round 1 (assertions.md): a first write with no acknowledged publication and no marker the
        # collector's reconcile can see is refused (the collector may still hold a clean stop).
        with self.assertRaises(project_wake.WakeRefused):
            publisher.before_due(100)
        base = Path(tempfile.mkdtemp(dir=SHORT_TMP))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(base)], check=False))
        publisher = project_wake.WakePublisher(CollectorPaths(LearningState(base / 'state').root), None,
                                               'prj_' + 'a' * 32, send=sender, start_sender=False)
        self.addCleanup(publisher.close)
        publisher.before_due(100)  # with the marker kept, the write proceeds
        self.assertTrue(publisher.marker.exists())
        self.assertTrue(publisher.unpublished)
        publisher.after_settle(100)
        publisher.drain()
        self.assertTrue(publisher.unpublished)
        sender.fail = False
        publisher.drain()  # the retry publishes it
        self.assertFalse(publisher.unpublished)
        self.assertFalse(publisher.marker.exists())  # the acknowledgement removes the marker
        self.assertEqual(('exact', 100), (sender.sent[-1]['state'], sender.sent[-1]['next_due_at_ms']))

    @requirements('E2a-8', why_not_scenario='The publisher ordering rules with a recording sender; the wire is WakeServiceTests.')
    def test_E2a_8_clean_stop_is_bounded_and_a_writer_fault_publishes_nothing(self):
        base = Path(tempfile.mkdtemp(dir=SHORT_TMP))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(base)], check=False))
        state = LearningState(base / 'state')
        paths = CollectorPaths(state.root)
        root = base / 'project'
        root.mkdir()
        issue_wake_capability(paths, root)
        endpoint = account_endpoint_path(paths)
        endpoint.parent.mkdir(parents=True, mode=0o700)
        proof_directory(endpoint).mkdir(mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        listener.listen(4)
        self.addCleanup(listener.close)
        held = []
        threading.Thread(target=lambda: held.append(listener.accept()), daemon=True).start()  # never answers
        self.addCleanup(lambda: [pair[0].close() for pair in held])
        publisher = project_wake.WakePublisher(paths, root, 'prj_' + 'b' * 32, start_sender=False)
        self.addCleanup(publisher.close)
        began = time.monotonic()
        self.assertFalse(publisher.finish(faulted=False, deadline=2.0))
        self.assertLess(time.monotonic() - began, 3.0)
        sender = FakeSender()
        faulted = self.publisher(sender, start_sender=False)
        faulted.after_settle(5)
        self.assertFalse(faulted.finish(faulted=True))
        self.assertEqual([], sender.sent)


@unittest.skipUnless(SANDBOXED, 'account signature workers need the reviewed macOS sandbox')
class WireBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.projects = []
        ledger = LearningLedger(self.state)
        try:
            for name in ('first', 'second'):
                root = (self.base / name).resolve()
                (root / '.git').mkdir(parents=True)
                self.projects.append((root, ledger.register_project(root)))
        finally:
            ledger.close()
        self.paths = CollectorPaths(self.state.root)
        self.addCleanup(self.stop_services)

    def processes(self, entry):
        listed = subprocess.run(['pgrep', '-f', entry + ' --state-root ' + str(self.state.root)],
                                stdout=subprocess.PIPE, text=True, check=False)
        return sorted(int(value) for value in listed.stdout.split())

    def stop_services(self, entries=('project_host.py', 'collector_host.py')):
        for entry in entries:
            for pid in self.processes(entry):
                os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while any(self.processes(entry) for entry in entries):
            self.assertLess(time.monotonic(), end, 'service processes did not stop')
            time.sleep(.05)

    def cli(self, command, *args, project=None, expected=0):
        env = {k: v for k, v in os.environ.items() if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(name, None)
        env['CODEX_THREAD_ID'] = 'wake-primary'
        env['TMPDIR'] = SHORT_TMP
        done = subprocess.run([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), command,
            '--state-root', str(self.state.root), '--project-root', str(project or self.projects[0][0]), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=60, check=False)
        self.assertEqual(expected, done.returncode, (done.stdout + done.stderr).decode(errors='replace'))
        return json.loads(done.stdout)

    def start(self):
        self.assertEqual('active', self.cli('learning-open-orchestration', '--request-id', 'wake-open')['status'])

    def wake_row(self, project_id):
        connection = sqlite3.connect(self.state.database.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            found = connection.execute('SELECT * FROM project_wake WHERE project_id=?', (project_id,)).fetchone()
            return None if found is None else dict(found)
        finally:
            connection.close()

    def wait_row(self, project_id, predicate, seconds=15):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            row = self.wake_row(project_id)
            if row is not None and predicate(row):
                return row
            time.sleep(.05)
        self.fail(f'wake row never matched: {self.wake_row(project_id)}')

    def raw(self, key, subject, project_id, payload, *, frame_payload=None, line=None, request_id=None):
        request = AccountRequest(1, 'account.publish_wake', request_id or 'w-' + os.urandom(8).hex(), project_id,
                                 COLLECTOR_RUNTIME_DIGEST, payload)
        frame = encode_account_request(request)
        endpoint = account_endpoint_path(self.paths)
        if line is None:
            signed = frame if frame_payload is None else encode_account_request(
                AccountRequest(1, 'account.publish_wake', request.request_id, project_id, COLLECTOR_RUNTIME_DIGEST,
                               frame_payload))
            line = enrollment_preamble(key, 'project', CODEX_HOST_CLIENT, 1, signed, proof_directory(endpoint),
                                       subject=str(subject))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(endpoint))
            client.sendall(line + b'\n' + frame + b'\n')
            received = bytearray()
            try:
                while b'\n' not in received:
                    chunk = client.recv(65_536)
                    if not chunk:
                        break
                    received.extend(chunk)
            except OSError:
                pass
        return request, bytes(received).split(b'\n', 1)[0], line

    def refusal(self, request, reply):
        if not reply:
            return 'closed'
        with self.assertRaises(AccountError) as refused:
            decode_account_response(reply, request)
        return refused.exception.reason_code


class WakeWireTests(WireBase):
    @requirements('E2a-5', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_5_a_credential_is_bound_to_one_project_and_denials_are_uniform(self):
        self.start()
        (root_a, id_a), (root_b, id_b) = self.projects
        key_a, key_b = read_wake_capability(self.paths, root_a), read_wake_capability(self.paths, root_b)
        self.assertNotEqual(key_a, key_b)
        request, reply, _ = self.raw(key_b, root_b, id_b, wake_payload(state='pending'))
        self.assertEqual('accepted', decode_account_response(reply, request)['outcome'])
        _, _, line = self.raw(key_b, root_b, id_b, wake_payload(epoch=None, seq=7))  # accepted; replayed below
        before = self.wake_row(id_b)
        unknown_root = self.base / 'unknown'
        (unknown_root / '.git').mkdir(parents=True)
        # Enrollment: A's key for B's root, for an unregistered root, and a replayed preamble end alike, with the
        # frontend's one fixed refusal.
        enrolled = [self.raw(key_a, root_b, id_b, wake_payload())[1],
                    self.raw(key_a, unknown_root, id_b, wake_payload())[1]]
        enrolled.append(self.raw(key_b, root_b, id_b, wake_payload(epoch=None, seq=7), line=line)[1])
        self.assertEqual(1, len(set(enrolled)), enrolled)
        self.assertIn(b'service_refused_enrollment', enrolled[0])
        # Bound credential, body naming another project (registered or not) or a forged body: the same refusal.
        denied = [self.refusal(*self.raw(key_b, root_b, id_a, wake_payload())[:2]),
                  self.refusal(*self.raw(key_b, root_b, 'prj_' + 'f' * 32, wake_payload())[:2]),
                  self.refusal(*self.raw(key_b, root_b, id_b, wake_payload(due=1),
                                         frame_payload=wake_payload(due=2))[:2])]
        self.assertEqual(['account_not_authorized'] * 3, denied)
        after = self.wake_row(id_b)
        self.assertEqual({k: v for k, v in before.items() if k != 'updated_at_ms'},
                         {k: v for k, v in after.items() if k != 'updated_at_ms'})

    @requirements('E2a-5', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_5_the_wake_key_publishes_nothing_else_and_the_account_key_cannot_publish(self):
        self.start()
        root_b, id_b = self.projects[1]
        endpoint = account_endpoint_path(self.paths)
        key_b = read_wake_capability(self.paths, root_b)
        account_key = read_capability(self.paths, None, 'account')
        for key, role, operation in ((key_b, 'project', 'account.owner_key'), (key_b, 'project', 'account.project'),
                                     (account_key, 'project', 'account.publish_wake'),
                                     (account_key, 'account', 'account.publish_wake')):
            with self.subTest(role=role, operation=operation, wake_key=key is key_b):
                payload = wake_payload() if operation == 'account.publish_wake' else {}
                request = AccountRequest(1, operation, 'x-' + os.urandom(6).hex(), id_b, COLLECTOR_RUNTIME_DIGEST, payload)
                frame = encode_account_request(request)
                line = enrollment_preamble(key, role, CODEX_HOST_CLIENT, 1, frame, proof_directory(endpoint),
                                           subject=str(root_b))
                _, reply, _ = self.raw(None, None, id_b, payload, line=line, request_id=request.request_id) \
                    if operation == 'account.publish_wake' else (None, self.send(endpoint, line, frame), None)
                self.assertEqual('account_not_authorized', self.refusal(request, reply))
        row = self.wake_row(id_b)  # only the collector's reconcile mark: nothing was published
        self.assertEqual((0, None, None), (row['allocated_epoch'], row['epoch'], row['state']))

    @staticmethod
    def send(endpoint, line, frame):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(endpoint))
            client.sendall(line + b'\n' + frame + b'\n')
            received = bytearray()
            while b'\n' not in received:
                chunk = client.recv(65_536)
                if not chunk:
                    break
                received.extend(chunk)
        return bytes(received).split(b'\n', 1)[0]

    @requirements('E2a-6', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_6_epochs_are_durable_duplicates_are_no_ops_and_stale_is_refused(self):
        self.start()
        root_b, id_b = self.projects[1]
        key_b = read_wake_capability(self.paths, root_b)

        def publish(payload):
            request, reply, _ = self.raw(read_wake_capability(self.paths, root_b), root_b, id_b, payload)
            try:
                return decode_account_response(reply, request)
            except AccountError as error:
                return error.reason_code
        first, second = publish(wake_payload(state='pending')), publish(wake_payload(state='pending'))
        self.assertLess(first['epoch'], second['epoch'])
        epoch = second['epoch']
        self.assertEqual('accepted', publish(wake_payload(epoch, 2, 'exact', 900))['outcome'])
        self.assertEqual('duplicate', publish(wake_payload(epoch, 2, 'exact', 900))['outcome'])
        for stale in (wake_payload(epoch, 2, 'exact', 901), wake_payload(epoch, 1, 'exact', 900),
                      wake_payload(first['epoch'], 9, 'exact', 900), wake_payload(epoch + 5, 1, 'exact', 900)):
            with self.subTest(stale=stale):
                self.assertEqual('wake_stale', publish(stale))
        row = self.wake_row(id_b)
        self.assertEqual((epoch, 2, 'exact', 900, 4), (row['epoch'], row['seq'], row['state'], row['next_due_at_ms'],
                                                       row['refused']))
        # The epoch is durable across a collector restart (the keys are reissued).
        self.stop_services()
        self.start()
        self.assertNotEqual(key_b, read_wake_capability(self.paths, root_b))
        self.assertGreater(publish(wake_payload(state='pending'))['epoch'], epoch)


KILLER = r'''
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from codex_agents import project_holds_schema, project_service, project_wake
from codex_agents.collector_paths import CollectorPaths
state, root, project_id, mode = CollectorPaths(Path(sys.argv[2])), Path(sys.argv[3]), sys.argv[4], sys.argv[5]
history = project_service.open_offline_history(state, project_id)
publisher = project_wake.WakePublisher(state, root, project_id, start_sender=False)
project_holds_schema.install_wake_guard(project_id, publisher)
publisher.open(history._connection)
publisher.drain()
assert publisher.acknowledged[0] == 'exact', publisher.acknowledged
if mode == 'after_ack':
    real = publisher.before_due
    def before_due(due):
        real(due)
        assert publisher.acknowledged == ('pending', due), publisher.acknowledged
        os.kill(os.getpid(), signal.SIGKILL)
    publisher.before_due = before_due
with history._transaction():
    project_holds_schema.take_hold(history._connection, project_id, 'orchestration', 'orc_kill', now_ms=1,
                                   due_at_ms=123_456)
# after commit: the exact is never sent (no sender thread), then the process dies.
os.kill(os.getpid(), signal.SIGKILL)
'''


class WakeServiceTests(WireBase):
    @requirements('E2a-7', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_7_a_live_service_publishes_pending_then_exact(self):
        self.start()
        root_a, id_a = self.projects[0]
        row = self.wait_row(id_a, lambda row: row['state'] == 'exact')
        self.assertGreaterEqual(row['epoch'], 1)
        self.assertGreaterEqual(row['seq'], 2)  # pending first, then exact

    @requirements('E2a-8', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_8_a_normal_stop_publishes_clean_stop(self):
        self.start()
        id_a = self.projects[0][1]
        epoch = self.wait_row(id_a, lambda row: row['state'] == 'exact')['epoch']
        self.stop_services(('project_host.py',))
        row = self.wait_row(id_a, lambda row: row['state'] == 'clean_stop')
        self.assertEqual(epoch, row['epoch'])

    @requirements('E2a-7', why_not_scenario='Real collector and project host processes started by the unmodified CLI.')
    def test_E2a_7_killed_after_the_pending_ack_or_after_commit_leaves_pending(self):
        self.start()  # the collector, for project A; B has no service
        root_b, id_b = self.projects[1]
        for mode in ('after_ack', 'after_commit'):
            with self.subTest(mode=mode):
                done = subprocess.run([sys.executable, '-I', '-B', '-c', KILLER, str(ORCHESTRATION_ROOT),
                                       str(self.state.root), str(root_b), id_b, mode],
                                      capture_output=True, text=True, timeout=60, env={'PATH': os.defpath,
                                                                                       'TMPDIR': SHORT_TMP})
                self.assertEqual(-signal.SIGKILL, done.returncode, done.stderr)
                row = self.wake_row(id_b)
                self.assertEqual(('pending', 123_456), (row['state'], row['next_due_at_ms']), row)
                # Release the hold so the next round takes it again from a clean register.
                connection = sqlite3.connect(str(self.state.root / 'projects' / id_b / 'history.sqlite3'))
                with connection:
                    connection.execute("DELETE FROM project_holds WHERE identity='orc_kill'")
                connection.close()



class NeverAnswers:
    """A real account endpoint under a state root that accepts and never answers (or answers `reply` unframed)."""

    def __init__(self, case, reply=None):
        self.base = Path(tempfile.mkdtemp(dir=SHORT_TMP))
        case.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.base)], check=False))
        self.state = LearningState(self.base / 'state')
        self.paths = CollectorPaths(self.state.root)
        self.root = self.base / 'project'
        self.root.mkdir()
        issue_wake_capability(self.paths, self.root)
        endpoint = account_endpoint_path(self.paths)
        endpoint.parent.mkdir(parents=True, mode=0o700)
        proof_directory(endpoint).mkdir(mode=0o700)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(endpoint))
        listener.listen(4)
        case.addCleanup(listener.close)
        held = []

        def accept():
            connection, _ = listener.accept()
            held.append(connection)
            if reply is not None:
                with __import__('contextlib').suppress(OSError):
                    connection.sendall(reply)
        threading.Thread(target=accept, daemon=True).start()
        case.addCleanup(lambda: [connection.close() for connection in held])


class RecordingGuard:
    def __init__(self):
        self.calls, self.dirty = [], False

    def before_due(self, due):
        self.dirty = True
        self.calls.append(('before', due))

    def touch(self):
        self.dirty = True

    def after_settle(self, due):
        self.dirty = False
        self.calls.append(('settled', due))


class WakeRoundOneTests(StoreBase):
    """Review round 1: the first write after a clean stop, exact and stop ordering, the withdrawal deadline, the
    epoch of one instance, the verifier's privacy and the reconcile's budgets."""

    def marked_publisher(self, sender, project=None):
        project = project or self.fixture.project_a_id
        publisher = project_wake.WakePublisher(CollectorPaths(self.fixture.state_root), self.fixture.project_a,
                                               project, send=sender, start_sender=False)
        self.addCleanup(publisher.close)
        return publisher

    @requirements('E2a-7', why_not_scenario='The real ledger and collector reconcile in process with a failing sender.')
    def test_E2a_7_a_first_write_unpublished_after_a_clean_stop_is_still_reconciled(self):
        project = self.fixture.project_a_id
        self.ledger.publish_wake(project, epoch=None, seq=1, state='clean_stop', next_due_at_ms=None)
        sender = FakeSender()
        sender.fail = True
        self.marked_publisher(sender).before_due(5_000)  # the write proceeds; the host dies before any publish
        server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        before = time.time_ns() // 1_000_000
        with mock.patch.object(learning_store, 'project_host_serving', return_value=False):
            server._reconcile_wakes()
        row = self.row(project)
        self.assertIsNotNone(row['due_at_ms'], row)
        self.assertGreaterEqual(row['due_at_ms'], before)

    @requirements('E2a-7', why_not_scenario='The publisher ordering rules with a recording sender.')
    def test_E2a_7_a_write_while_pending_invalidates_the_queued_exact(self):
        sender = FakeSender()
        publisher = project_wake.WakePublisher(None, None, 'prj_' + 'a' * 32, send=sender, start_sender=False)
        self.addCleanup(publisher.close)
        publisher.before_due(None)  # pending acknowledged
        publisher.after_settle(1_000)  # an exact queued for the transaction that ended ...
        publisher.before_due(500)  # ... and a new write, while the collector's state is still pending
        publisher.drain()
        self.assertNotIn(('exact', 1_000), [(s['state'], s['next_due_at_ms']) for s in sender.sent])
        publisher.after_settle(500)
        publisher.drain()
        self.assertEqual(('exact', 500), (sender.sent[-1]['state'], sender.sent[-1]['next_due_at_ms']))

    @requirements('E2a-8', why_not_scenario='The publisher stop rules with a recording sender and a real endpoint.')
    def test_E2a_8_clean_stop_needs_settlement_and_a_final_due_read(self):
        sender = FakeSender()
        publisher = project_wake.WakePublisher(None, None, 'prj_' + 'a' * 32, send=sender, start_sender=False)
        self.addCleanup(publisher.close)
        publisher.before_due(None)
        publisher.after_settle(None)
        publisher.drain()
        publisher.before_due(100)  # a write whose transaction-end read then failed: no after_settle
        self.assertFalse(publisher.finish(faulted=False))  # nothing verified: no clean stop
        self.assertNotIn('clean_stop', [s['state'] for s in sender.sent])
        read = project_wake.WakePublisher(None, None, 'prj_' + 'a' * 32, send=sender, start_sender=False)
        self.addCleanup(read.close)
        self.assertFalse(read.finish(faulted=False, final_due=mock.Mock(side_effect=sqlite3.OperationalError())))
        self.assertNotIn('clean_stop', [s['state'] for s in sender.sent])
        done = project_wake.WakePublisher(None, None, 'prj_' + 'a' * 32, send=sender, start_sender=False)
        self.addCleanup(done.close)
        self.assertTrue(done.finish(faulted=False, final_due=lambda: 100))
        self.assertEqual(('clean_stop', 100), (sender.sent[-1]['state'], sender.sent[-1]['next_due_at_ms']))
        # Bounded: a collector that accepts and never answers does not hold the stop past its deadline.
        silent = NeverAnswers(self)
        bounded = project_wake.WakePublisher(silent.paths, silent.root, 'prj_' + 'b' * 32, start_sender=False)
        self.addCleanup(bounded.close)
        began = time.monotonic()
        self.assertFalse(bounded.finish(faulted=False, deadline=1.0, final_due=lambda: None))
        self.assertLess(time.monotonic() - began, 2.0)

    @requirements('E2a-7', why_not_scenario='A real project store with the withdrawal transitions; the guard records.')
    def test_E2a_7_a_releasing_open_binding_counts_by_its_withdrawal_retry(self):
        from tests import test_open_withdrawal as withdrawals
        from tests import test_result_leases as leases
        base = leases.ResultLeaseTests(methodName='runTest')
        base.setUp()
        self.addCleanup(base.doCleanups)
        store, guard = base.store, RecordingGuard()
        project_holds_schema.install_wake_guard(store.project_id, guard)
        self.addCleanup(project_holds_schema.install_wake_guard, store.project_id, None)
        binding = withdrawals.binding(1)
        store.record_open_binding(binding.run_handle, 1, binding.run_id, withdrawals.INPUTS, 'codex',
                                  instance='svc-' + 'a' * 32, now_ms=1000)
        guard.calls.clear()
        store.mark_open_withdrawing(binding.run_handle, 1, 'cancelled_before_claim', project_id=withdrawals.PROJECT,
                                    runtime_digest=COLLECTOR_RUNTIME_DIGEST)
        row = store._connection.execute("SELECT identity,next_check_ms FROM project_holds WHERE kind='open_binding'"
                                        ).fetchone()
        retry = int(row[1])
        self.assertEqual(retry, project_holds_schema.next_hold_due(store._connection))
        self.assertIn(('before', retry), guard.calls)  # the earlier deadline was published before the write
        self.assertEqual(('settled', retry), guard.calls[-1])
        store.defer_open_withdrawal(row[0], now_ms=5_000, base_ms=100, max_ms=1_000)
        self.assertEqual(5_100, project_holds_schema.next_hold_due(store._connection))

    @requirements('E2a-6', why_not_scenario='The real account owner view and ledger, with a reply lost once.')
    def test_E2a_6_a_lost_first_acknowledgement_does_not_allocate_a_second_epoch(self):
        from codex_agents.wake_contracts import decode_wake_publish
        views, project, lost = AccountOwnerViews(self.ledger), self.fixture.project_a_id, [True]

        def send(payload, timeout):
            result = views.publish_wake(project, decode_wake_publish(payload))
            if lost[0]:
                lost[0] = False
                raise AccountError('account_unavailable')  # applied; its reply never arrived
            return result
        publisher = self.marked_publisher(send)
        publisher.before_due(None)
        publisher.before_due(None)  # the retry of this same instance
        row = self.row(project)
        self.assertEqual((1, 1), (row['allocated_epoch'], row['epoch']), row)
        # Another instance still gets a new epoch.
        other = self.marked_publisher(lambda payload, timeout: views.publish_wake(project, decode_wake_publish(payload)))
        other.before_due(None)
        self.assertEqual(2, self.row(project)['epoch'])

    @requirements('E2a-5', why_not_scenario='The real account host verifier in process; the wire is WakeWireTests.')
    def test_E2a_5_the_verifier_probes_no_caller_named_path_and_binds_the_project_id(self):
        from codex_agents.account_host import AccountServiceHost
        from codex_agents.service_enrollment import EnrollmentError
        paths = CollectorPaths(self.fixture.state_root)
        host = AccountServiceHost(self.ledger, paths)
        self.addCleanup(host.close)
        verifier = host._frontend._verifier
        proofs = proof_directory(account_endpoint_path(paths))
        probed = (Path(self.fixture.temporary.name) / 'probe-me').resolve()
        known = self.fixture.project_a.resolve()
        lines = [enrollment_preamble(os.urandom(32), 'project', CODEX_HOST_CLIENT, 1, b'frame', proofs,
                                     subject=str(subject)) for subject in (probed, known)]
        touched = []

        def watch(real):
            def inner(path, *args, **kwargs):
                touched.append(os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike)) else str(path))
                return real(path, *args, **kwargs)
            return inner
        with mock.patch('os.lstat', watch(os.lstat)), mock.patch('os.stat', watch(os.stat)), \
                mock.patch('os.open', watch(os.open)):
            for line in lines:
                with self.assertRaises(EnrollmentError):
                    verifier.verify(line)
        self.assertEqual([], [path for path in touched if str(probed) in path or str(known) in path])
        good = enrollment_preamble(read_wake_capability(paths, known), 'project', CODEX_HOST_CLIENT, 1, b'frame',
                                   proofs, subject=str(known))
        self.assertEqual(self.fixture.project_a_id, verifier.verify(good).bound_project)

    def register(self, count):
        projects = []
        for number in range(count):
            root = Path(self.fixture.temporary.name) / f'many-{len(projects)}-{os.urandom(4).hex()}'
            root.mkdir()
            projects.append(self.ledger.register_project(root, now_ms=1))
        return projects

    def held_work(self, server):
        """The most SQLite steps any one ledger hold of one reconcile pass took."""
        counts, held, transaction = [], self.ledger._held, self.ledger._account_transaction
        steps = [0]

        def counted(real):
            @__import__('contextlib').contextmanager
            def inner(*args, **kwargs):
                steps[0] = 0
                self.ledger._connection.set_progress_handler(lambda: steps.__setitem__(0, steps[0] + 1) or 0, 1)
                try:
                    with real(*args, **kwargs) as value:
                        yield value
                finally:
                    self.ledger._connection.set_progress_handler(None, 1)
                    counts.append(steps[0])
            return inner
        server._wake_cursor = ''
        with mock.patch.object(self.ledger, '_held', counted(held)), \
                mock.patch.object(self.ledger, '_account_transaction', counted(transaction)), \
                mock.patch.object(learning_store, 'project_host_serving', return_value=True):
            server._reconcile_wakes()
        return max(counts)

    @requirements('E2a-9', why_not_scenario='The real collector reconcile and ledger in process; nothing is spawned.')
    def test_E2a_9_one_locked_read_examines_at_most_one_page_of_projects(self):
        for project in [self.fixture.project_a_id, self.fixture.project_b_id] + self.register(62):
            self.ledger.publish_wake(project, epoch=None, seq=1, state='clean_stop', next_due_at_ms=None)
        server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        page = self.held_work(server)
        for project in self.register(4 * 64):
            self.ledger.publish_wake(project, epoch=None, seq=1, state='clean_stop', next_due_at_ms=None)
        self.assertLess(self.held_work(server), 2 * page)

    @requirements('E2a-9', why_not_scenario='The real collector reconcile and ledger in process; nothing is spawned.')
    def test_E2a_9_reconcile_reaches_every_project_past_one_page(self):
        projects = [self.fixture.project_a_id, self.fixture.project_b_id] + self.register(3 * 64)
        server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        with mock.patch.object(learning_store, 'project_host_serving', return_value=False):
            for _ in range(8):
                server._reconcile_wakes()
        unmarked = [project for project in projects if (self.row(project) or {}).get('due_at_ms') is None]
        self.assertEqual([], unmarked)

    @requirements('E2a-9', why_not_scenario='The real collector start in process; nothing is spawned.')
    def test_E2a_9_the_startup_pass_is_bounded_and_the_rest_follows(self):
        projects = [self.fixture.project_a_id, self.fixture.project_b_id] + self.register(10)

        def slow(state_root, root):
            time.sleep(0.25)
            return False
        with mock.patch.object(learning_store, 'project_host_serving', slow):
            server = CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
            began = time.monotonic()
            server.start()
            self.addCleanup(server.stop)
            self.assertLess(time.monotonic() - began, 2.0)
            end = time.monotonic() + 15
            while time.monotonic() < end:
                with self.ledger._lock:
                    if all((self.row(project) or {}).get('due_at_ms') is not None for project in projects):
                        break
                time.sleep(0.1)
        with self.ledger._lock:
            self.assertEqual([], [p for p in projects if (self.row(p) or {}).get('due_at_ms') is None])

    @requirements('E2a-7', why_not_scenario='A real endpoint that answers past the reply limit.')
    def test_E2a_7_a_reply_past_the_limit_is_refused_and_flagged(self):
        oversized = NeverAnswers(self, reply=b'x' * (project_wake.MAX_REPLY_BYTES + 10))
        with self.assertRaises(project_wake.WakeUnavailable):
            project_wake.send_wake(oversized.paths, oversized.root, 'prj_' + 'c' * 32,
                                   wake_payload(state='pending'), 2.0)


if __name__ == '__main__':
    unittest.main()
