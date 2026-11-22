"""C2: the run's container is recorded before its launch (PLAN-0.4.0-supervisor-slices.md, C2).

Requirements (RS-C-1 and RS-C-2 reduced, RS-12, D7, gap C7):
- No intent, no claim: in container mode the run's container identity (its name and the labels
  `cocolo.project`, `cocolo.run`, `cocolo.kind=run`) is written durably to the launch journal as
  `intent` before the claim is handed to the account; a failed write stops the run before the claim
  is sent. Without the switch nothing changes: no intent, and the claim goes as today.
- An admitted claim is recorded on its intent, so an admitted claim always has an entry (C7).
- The container is created only after its admitted intent is checked (`record` before create),
  its id is written as `started` once podman returns, and the absolute deadline is set on the
  container itself (podman's `--timeout`, enforced by conmon, checked at start). Journal states:
  intent -> started -> stopping -> gone | stop_failed -> settled.
- A crash between intent and start leaves an entry with everything a fence needs; the process rule
  never reads a container entry as gone (recovery is C4's).
- The deadline fires with the service killed (D7: no unguarded window), and bridge launches run in a
  container.

The real-podman tests run only with podman, its machine and python:3.12-slim present (never pulled);
they create containers named `cocolo-test-*` with a per-test project label and remove exactly those.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from tests.test_container_runtime import (FakeClock, FakeHostFiles, FakePodman, REAL_IMAGE, REAL_PODMAN,
                                          REAL_SKIP_REASON)

from codex_agents import bridge, container_launch, launch_journal, project_driver
from codex_agents import container_runtime as cr
from codex_agents.claim_denial import FenceClaimInput
from codex_agents.launch_journal import JournalError, LaunchJournal, tree_state
from codex_agents.owned_execution import ExecutionCustodyError, OwnedExecutionKey, OwnedExecutionRegistry
from codex_agents.project_driver import RunDriver, _Run

PROJECT = 'prj_' + 'a' * 32
HANDLE = 'hrn_' + 'b' * 48
BRIDGE = 'brg_' + 'c' * 32
OTHER_BRIDGE = 'brg_' + 'd' * 32
IDENTITY = 'e' * 64
CLAIM = 'claim-' + 'f' * 32
LEASE_DIGEST = '1' * 64
EVIDENCE = json.dumps({'lease': {'lease_digest': LEASE_DIGEST}}).encode()
CONTAINER_PROJECT = 'cocolo-test-project'


@dataclass
class Lease:
    """Stands in for the run's ResultLeaseBinding: its scope fields and claim request."""
    project_id: str = PROJECT
    primary_session_id: str = 'session-1'
    runtime_digest: str = '3' * 64
    supervisor_instance_id: str = 'supervisor-1'
    supervisor_generation: int = 1
    orchestration_handle: str = 'orchestration-1'
    run_handle: str = HANDLE
    run_id: str = 'run-1'
    attempt: int = 1
    claim_request_id: str = CLAIM


def lease_dict():
    return {'project_id': PROJECT, 'run_handle': HANDLE, 'attempt': 1, 'claim_request_id': CLAIM}


class TimeoutPodman(FakePodman):
    """The fake podman, reporting the configured `--timeout` the way podman's inspect does."""

    def __init__(self, host):
        super().__init__(host)
        self.reported_timeout = None  # an override: what inspect claims was configured

    def inspect_json(self, container):
        value = super().inspect_json(container)
        configured = [arg[len('--timeout='):] for arg in container.args if arg.startswith('--timeout=')]
        timeout = int(configured[0]) if configured else 0
        value['Config']['Timeout'] = timeout if self.reported_timeout is None else self.reported_timeout
        return value


def private_folder(test):
    folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
    test.addCleanup(folder.cleanup)
    return os.path.realpath(folder.name)


def fake_launcher(test, *, journal=None, attach=None, grace_seconds=1):
    """A launcher over the fake podman, with its journal, podman and clock on the test."""
    root = private_folder(test)
    test.workspace = os.path.join(root, 'workspace')
    os.mkdir(test.workspace)
    test.seccomp = os.path.join(root, 'seccomp.json')
    Path(test.seccomp).write_text('{}')
    test.host = FakeHostFiles()
    test.podman = TimeoutPodman(test.host)
    test.clock = FakeClock()
    runtime = cr.ContainerRuntime(CONTAINER_PROJECT, runner=test.podman, host_files=test.host,
                                  monotonic=test.clock.monotonic, sleep=test.clock.sleep)
    if journal is None:
        journal = LaunchJournal(Path(root) / 'launches')
        test.addCleanup(journal.release)
    test.journal = journal
    config = container_launch.ContainerLaunchConfig(image='python:3.12-slim', seccomp_profile=test.seccomp,
                                                    grace_seconds=grace_seconds)
    return container_launch.ContainerLauncher(runtime, journal, config, attach=attach)


def write_intent(launcher, **changes):
    values = dict(run_handle=HANDLE, attempt=1, host_client='codex-host-v1', lease=lease_dict(),
                  identity_digest=IDENTITY, claim_request_id=CLAIM, storage_evidence=EVIDENCE, workspace=None)
    values.update(changes)
    return launcher.intent(**values)


def deadline_ms(seconds=600):
    return int(time.time() * 1000) + seconds * 1000


# --------------------------------------------------------------------------------------------
# The journal's container entries
# --------------------------------------------------------------------------------------------

class ContainerJournalTests(unittest.TestCase):
    def setUp(self):
        self.launcher = fake_launcher(self)

    def entry(self, token):
        return self.journal.entry(token)

    def test_the_intent_is_on_disk_with_the_container_name_and_its_labels(self):
        token = write_intent(self.launcher)
        self.assertRegex(token, r'^lnc_[0-9a-f]{32}$')
        path = Path(self.journal.directory) / (token + '.json')
        info = os.lstat(path)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(0, info.st_mode & 0o077)
        stored = json.loads(path.read_text())
        identity = self.launcher.runtime.identity(token)
        self.assertEqual(2, stored['version'])
        self.assertEqual('intent', stored['state'])
        self.assertEqual(identity.name, stored['container_name'])
        self.assertEqual({cr.PROJECT_LABEL: CONTAINER_PROJECT, cr.RUN_LABEL: token, cr.KIND_LABEL: 'run'},
                         stored['labels'])
        self.assertEqual(identity.labels, stored['labels'])
        # Not yet admitted: the account has not answered.
        self.assertIsNone(stored['bridge_run_id'])
        self.assertIsNone(stored['container_id'])
        # Everything a later service needs to fence the claim (RS-C-6, C4).
        self.assertEqual((HANDLE, IDENTITY, CLAIM, lease_dict()),
                         (stored['run_handle'], stored['identity_digest'], stored['claim_request_id'],
                          stored['lease']))
        self.assertEqual(EVIDENCE, bytes.fromhex(stored['storage_evidence']))
        # Each run has its own token, so its own container name (RS-12: the name is the token).
        other = write_intent(self.launcher)
        self.assertNotEqual(token, other)
        self.assertNotEqual(identity.name, self.entry(other)['container_name'])

    def test_the_states_are_intent_started_stopping_then_gone_or_stop_failed_then_settled(self):
        self.assertEqual({'intent', 'started', 'stopping', 'gone', 'stop_failed', 'settled'},
                         set(launch_journal.CONTAINER_STATES))
        token = write_intent(self.launcher)
        self.journal.admitted(token, BRIDGE, deadline_ms())
        self.assertEqual(('intent', BRIDGE), (self.entry(token)['state'], self.entry(token)['bridge_run_id']))
        self.journal.started(token, 'a' * 64, '/machine.slice/libpod-x.scope')
        self.assertEqual(('started', 'a' * 64), (self.entry(token)['state'], self.entry(token)['container_id']))
        self.journal.stopping(token)
        self.assertEqual('stopping', self.entry(token)['state'])
        self.journal.stopped(token, False, 'not gone 10 s after the kill')
        self.assertEqual(('stop_failed', 'not gone 10 s after the kill'),
                         (self.entry(token)['state'], self.entry(token)['stop_detail']))
        # A failed stop is tried again.
        self.journal.stopping(token)
        self.journal.stopped(token, True, 'the container no longer exists')
        self.assertEqual('gone', self.entry(token)['state'])
        self.journal.settled(token, 'released')
        self.assertEqual(('settled', 'released'), (self.entry(token)['state'], self.entry(token)['outcome']))

    def test_out_of_order_and_foreign_transitions_are_refused(self):
        token = write_intent(self.launcher)
        with self.assertRaises(JournalError):
            self.journal.started(token, 'a' * 64, '/x.scope')  # never admitted
        with self.assertRaises(JournalError):
            self.journal.stopped(token, True, 'gone')  # not stopping
        self.journal.admitted(token, BRIDGE, deadline_ms())
        self.journal.admitted(token, BRIDGE, self.entry(token)['deadline_at_ms'])  # the same answer again
        with self.assertRaises(JournalError):
            self.journal.admitted(token, OTHER_BRIDGE, deadline_ms())
        self.journal.started(token, 'a' * 64, '/x.scope')
        with self.assertRaises(JournalError):
            self.journal.started(token, 'b' * 64, '/x.scope')
        with self.assertRaises(JournalError):
            self.journal.intent(token, **self.intent_fields(token))  # an entry is written once
        # Another service instance never moves this instance's entry.
        other = LaunchJournal(self.journal.directory)
        self.addCleanup(other.release)
        with self.assertRaises(JournalError):
            other.stopping(token)
        self.assertEqual('started', self.entry(token)['state'])

    def intent_fields(self, token):
        identity = self.launcher.runtime.identity(token)
        return dict(project_id=CONTAINER_PROJECT, container_name=identity.name, labels=identity.labels,
                    run_handle=HANDLE, attempt=1,
                    host_client='codex-host-v1', lease=lease_dict(), identity_digest=IDENTITY,
                    claim_request_id=CLAIM, storage_evidence=EVIDENCE, workspace=None)

    def test_the_record_check_needs_an_admitted_intent_of_this_instance_with_that_name(self):
        token = write_intent(self.launcher)
        name = self.entry(token)['container_name']
        with self.assertRaises(JournalError):
            self.journal.check_intent(token, name)  # not admitted
        self.journal.admitted(token, BRIDGE, deadline_ms())
        self.journal.check_intent(token, name)
        with self.assertRaises(JournalError):
            self.journal.check_intent(token, name + 'x')
        with self.assertRaises(JournalError):
            self.journal.check_intent('lnc_' + '0' * 32, name)
        other = LaunchJournal(self.journal.directory)
        self.addCleanup(other.release)
        with self.assertRaises(JournalError):
            other.check_intent(token, name)
        self.journal.started(token, 'a' * 64, '/x.scope')
        with self.assertRaises(JournalError):
            self.journal.check_intent(token, name)  # started already: never created twice

    def test_the_admitted_entry_is_found_by_its_bridge_run_id(self):
        token = write_intent(self.launcher)
        self.assertIsNone(self.journal.container_entry(BRIDGE))
        self.journal.admitted(token, BRIDGE, deadline_ms())
        self.assertEqual(token, self.journal.container_entry(BRIDGE)['token'])
        self.assertIsNone(self.journal.container_entry(OTHER_BRIDGE))

    def test_the_process_rule_never_reads_a_container_entry_as_gone(self):
        token = write_intent(self.launcher)
        self.assertEqual('unknown', tree_state(self.entry(token)))
        self.journal.admitted(token, BRIDGE, deadline_ms())
        self.assertEqual('unknown', tree_state(self.entry(token)))
        self.journal.started(token, 'a' * 64, '/x.scope')
        self.journal.stopping(token)
        self.journal.stopped(token, True, 'gone')
        self.assertEqual('unknown', tree_state(self.entry(token)))

    def test_listings_and_status_count_container_entries(self):
        token = write_intent(self.launcher)
        self.assertEqual([token], [entry['token'] for entry in self.journal.entries()])
        directory = self.journal.directory
        later = LaunchJournal(directory)
        self.addCleanup(later.release)
        self.journal.release()
        report = launch_journal.unresolved(directory)
        self.assertEqual(1, report['count'])
        self.assertEqual('intent', report['entries'][0]['state'])
        # The stopped instance still has an unsettled run: its lock is never forgotten.
        later.forget_instance(self.journal.instance)
        self.assertIn(self.journal.instance, later.instances())

    def test_removal_and_settled_tombstones_by_token(self):
        token = write_intent(self.launcher)
        self.journal.settled(token, 'released', now_ms=1)
        self.journal.prune(now_ms=1 + launch_journal.TOMBSTONE_MS + 1)
        self.assertIsNone(self.entry(token))
        again = write_intent(self.launcher)
        self.journal.remove(again)
        self.assertIsNone(self.entry(again))
        self.assertEqual([], self.journal.entries())


# --------------------------------------------------------------------------------------------
# The driver: no intent, no claim
# --------------------------------------------------------------------------------------------

def driver(test, *, containers=True):
    value = object.__new__(RunDriver)
    value._clock = lambda: 100.0
    value._giving_back, value._cleanup_retry, value._forgetting = [], {}, {}
    value._preparation = mock.Mock()
    value._runtime = SimpleNamespace(persistence=mock.Mock(), owner=mock.Mock())
    value._runtime.persistence.snapshot.return_value = {'state': 'claim_pending'}
    value._account_call = mock.Mock()
    value._custody = mock.Mock(return_value=b'{}')
    value._reports = []
    launcher = fake_launcher(test)
    value._journal = test.journal
    value._containers = launcher if containers else None
    return value


def claiming_run(**changes):
    run = _Run(key=(HANDLE, 1), scope=SimpleNamespace(project_id=PROJECT), host_client='codex-host-v1',
               prospective=None, run_id='run-1', inputs_digest='d' * 64)
    run.state, run.persistence, run.prepared = 'recording_open', 'lease-job', 'preparation'
    run.identity_digest, run.claim_request_id, run.evidence = IDENTITY, CLAIM, EVIDENCE
    run.lease, run.prepared_inputs = Lease(), SimpleNamespace(workspace=None)
    for name, value in changes.items():
        setattr(run, name, value)
    return run


class IntentBeforeClaimTests(unittest.TestCase):
    def claim_calls(self, value):
        return [call for call in value._account_call.call_args_list if call.args[2] == 'claim_launch']

    def test_the_intent_is_on_disk_before_the_claim_is_handed_to_the_account(self):
        value, run = driver(self), claiming_run()
        seen = []

        def account_call(run_, role, operation, *args, **kwargs):
            # What is durable at the moment the claim is handed over.
            seen.append((operation, [(entry['state'], entry['claim_request_id'], entry['bridge_run_id'])
                                     for entry in LaunchJournal_entries(value._journal.directory)]))

        value._account_call.side_effect = account_call
        value._advance(run)
        self.assertEqual([('claim_launch', [('intent', CLAIM, None)])], seen)
        self.assertTrue(run.claim_sent)
        self.assertEqual('claiming', run.state)
        self.assertEqual(run.launch_token, value._journal.entries()[0]['token'])

    def test_a_failed_intent_write_stops_the_run_before_the_claim_is_sent(self):
        value, run = driver(self), claiming_run()
        with mock.patch.object(LaunchJournal, '_write', side_effect=JournalError('launch entry was not written')):
            value._advance(run)
        self.assertEqual([], value._account_call.call_args_list)  # the account was not asked anything here
        self.assertFalse(run.claim_sent)
        self.assertEqual('run_unavailable', run.reason)
        self.assertNotIn(run.state, {'claiming', 'recording_open'})
        self.assertEqual([], value._journal.entries())
        # Its armed lease is then closed the existing way (a fence), never by a claim.
        for _ in range(3):
            value._advance(run)
        self.assertEqual([], self.claim_calls(value))

    def test_without_the_switch_no_intent_is_written_and_the_claim_goes_as_today(self):
        value, run = driver(self, containers=False), claiming_run()
        value._advance(run)
        self.assertEqual(1, len(self.claim_calls(value)))
        self.assertTrue(run.claim_sent)
        self.assertEqual([], value._journal.entries())
        self.assertIsNone(run.launch_token)

    def test_the_shipped_switch_keeps_todays_process_launch(self):
        self.assertIsNone(container_launch.SETTINGS)
        self.assertIsNone(container_launch.launcher_for(PROJECT, object()))

    def test_an_admitted_claim_is_recorded_on_its_intent(self):
        value, run = driver(self), claiming_run()
        value._advance(run)
        receipt = {'state': 'claimed', 'bridge_run_id': BRIDGE, 'receipt_digest': '2' * 64,
                   'deadline_at_ms': deadline_ms()}
        with mock.patch.object(project_driver, 'AttemptBinding'):
            value._bind_claim(run, receipt)
        entry = value._journal.container_entry(BRIDGE)
        self.assertEqual((run.launch_token, 'intent', BRIDGE, receipt['deadline_at_ms']),
                         (entry['token'], entry['state'], entry['bridge_run_id'], entry['deadline_at_ms']))
        # The process journal's own `claimed` entry is not written in container mode.
        self.assertEqual([run.launch_token], [entry['token'] for entry in value._journal.entries()])

    def test_a_closed_run_removes_its_intent_and_a_released_run_its_entry(self):
        value, run = driver(self), claiming_run()
        value._advance(run)
        token = run.launch_token
        run.receipt = None
        value._close(run)
        self.assertIsNone(value._journal.entry(token))
        released = claiming_run(state='releasing')
        value._advance(claiming_run())  # a second run's intent
        released.launch_token = value._journal.entries()[0]['token']
        value._journal.admitted(released.launch_token, BRIDGE, deadline_ms())
        released.receipt = {'bridge_run_id': BRIDGE}
        value._runtime.persistence.snapshot.return_value = {'state': 'release_acknowledged'}
        value._advance(released)
        self.assertEqual([], value._journal.entries())


def LaunchJournal_entries(directory):
    """Every entry on disk, read by a separate reader (what a later service would see)."""
    found = []
    for path in sorted(Path(directory).glob('*.json')):
        found.append(json.loads(path.read_text()))
    return found


# --------------------------------------------------------------------------------------------
# A crash between intent and start
# --------------------------------------------------------------------------------------------

CRASHING_SERVICE = textwrap.dedent('''
    import json, sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from codex_agents import container_launch, container_runtime as cr
    from codex_agents.launch_journal import LaunchJournal
    def refuse(args, timeout):
        raise AssertionError("podman must not be called")
    journal = LaunchJournal(Path(sys.argv[2]))
    runtime = cr.ContainerRuntime("cocolo-test-project", runner=refuse, host_files=object())
    config = container_launch.ContainerLaunchConfig(image="python:3.12-slim", seccomp_profile=sys.argv[3])
    launcher = container_launch.ContainerLauncher(runtime, journal, config)
    token = launcher.intent(run_handle=sys.argv[4], attempt=1, host_client="codex-host-v1",
                            lease=json.loads(sys.argv[5]), identity_digest=sys.argv[6], claim_request_id=sys.argv[7],
                            storage_evidence=sys.argv[8].encode(), workspace=None)
    if sys.argv[9] == "admitted":
        journal.admitted(token, sys.argv[10], int(time.time() * 1000) + 600000)
    print(token, flush=True)
    time.sleep(600)
''')


class CrashBetweenIntentAndStartTests(unittest.TestCase):
    def crash_after(self, step):
        root = private_folder(self)
        directory = Path(root) / 'launches'
        seccomp = os.path.join(root, 'seccomp.json')
        Path(seccomp).write_text('{}')
        service = subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', CRASHING_SERVICE, str(ORCHESTRATION_ROOT), str(directory), seccomp,
             HANDLE, json.dumps(lease_dict()), IDENTITY, CLAIM, EVIDENCE.decode(), step, BRIDGE],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            token = service.stdout.readline().decode().strip()
            self.assertRegex(token, r'^lnc_[0-9a-f]{32}$', service.stderr.read(4096) if not token else '')
        finally:
            os.killpg(service.pid, signal.SIGKILL)  # the service dies between intent and start
            service.wait(10)
            service.stdout.close()
            service.stderr.close()
        later = LaunchJournal(directory)
        self.addCleanup(later.release)
        return later, token

    def test_a_crash_after_the_intent_leaves_an_entry_recovery_can_fence(self):
        for step in ('intent', 'admitted'):
            with self.subTest(step=step):
                later, token = self.crash_after(step)
                [entry] = later.entries()
                self.assertEqual((token, 'intent'), (entry['token'], entry['state']))
                self.assertTrue(later.instance_stopped(entry['instance']))
                self.assertEqual(None if step == 'intent' else BRIDGE, entry['bridge_run_id'])
                # Everything the account's fence takes is in the entry.
                lease_digest = json.loads(bytes.fromhex(entry['storage_evidence']))['lease']['lease_digest']
                fence = FenceClaimInput(entry['run_handle'], entry['identity_digest'], entry['claim_request_id'],
                                        lease_digest)
                self.assertEqual((HANDLE, IDENTITY, CLAIM, LEASE_DIGEST),
                                 (fence.run_handle, fence.identity_digest, fence.claim_request_id,
                                  fence.lease_digest))
                # The container it would have had is named in the entry, for the label lookup.
                self.assertEqual(token, entry['labels'][cr.RUN_LABEL])
                # The process rule never settles it: it holds its slot until recovery (C4) fences it.
                self.assertEqual('unknown', tree_state(entry))
                value = object.__new__(RunDriver)
                value._journal = later
                value._settle_entry = mock.Mock()
                lost = value._settle_lost(False)
                value._settle_entry.assert_not_called()
                self.assertEqual({HANDLE: ('codex-host-v1', 'held')}, lost)


# --------------------------------------------------------------------------------------------
# Launch through the container primitive (fake podman)
# --------------------------------------------------------------------------------------------

class ContainerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.launcher = fake_launcher(self)
        self.token = write_intent(self.launcher)
        self.name = self.journal.entry(self.token)['container_name']

    def admit(self):
        self.journal.admitted(self.token, BRIDGE, deadline_ms())

    def start(self, seconds=30.0, bridge_run_id=BRIDGE):
        return self.launcher.start(bridge_run_id, ('python3', '-c', 'print(1)'), workspace=self.workspace,
                                   run_tag='0' * 32, deadline_ns=time.monotonic_ns() + int(seconds * 1e9))

    def test_the_container_is_created_only_after_its_admitted_intent_is_checked(self):
        self.admit()
        at_create = []
        self.podman.on_create = lambda args: at_create.append(self.journal.entry(self.token)['state'])
        started = self.start()
        self.assertEqual(['intent'], at_create)  # durable before the container exists
        entry = self.journal.entry(self.token)
        container = self.podman.containers[self.name]
        self.assertEqual(('started', container.id, container.cgroup_path),
                         (entry['state'], entry['container_id'], entry['cgroup_path']))
        self.assertEqual((self.token, self.name, container.id), (started.token, started.name, started.container_id))
        self.assertEqual({cr.PROJECT_LABEL: CONTAINER_PROJECT, cr.RUN_LABEL: self.token, cr.KIND_LABEL: 'run'},
                         container.labels)

    def test_no_admitted_intent_no_container(self):
        with self.assertRaises(container_launch.ContainerLaunchRefused):
            self.start()  # the claim was not admitted: nothing to start
        self.assertEqual([], self.podman.commands('create'))
        self.admit()
        with self.assertRaises(container_launch.ContainerLaunchRefused):
            self.start(bridge_run_id=OTHER_BRIDGE)
        # A record that cannot be confirmed (the entry changed under it) refuses before create.
        with mock.patch.object(LaunchJournal, 'check_intent', side_effect=JournalError('launch entry is not intent')):
            with self.assertRaises(container_launch.ContainerLaunchRefused) as refused:
                self.start()
        self.assertEqual('record_failed', refused.exception.reason)
        self.assertEqual([], self.podman.commands('create'))
        self.assertEqual('intent', self.journal.entry(self.token)['state'])

    def test_the_deadline_is_set_on_the_container_and_checked_at_start(self):
        self.admit()
        self.start(seconds=42.2)
        [create] = self.podman.commands('create')
        timeout = [arg for arg in create if arg.startswith('--timeout=')]
        self.assertEqual(1, len(timeout))
        self.assertIn(int(timeout[0][len('--timeout='):]), (42, 43))
        self.assertIn('--label=cocolo.kind=run', create)

    def test_dec49_the_backstop_is_applied_and_named_when_podman_supports_it(self):
        self.admit()
        started = self.start()
        self.assertTrue(started.timeout_backstop.startswith('applied'), started)

    def test_dec49_a_podman_without_the_run_time_limit_starts_it_without_and_says_why(self):
        self.admit()
        self.podman.versions = ('3.4.2', '3.4.2')
        started = self.start()
        [create] = self.podman.commands('create')
        self.assertFalse([arg for arg in create if arg.startswith('--timeout')], create)
        self.assertTrue(started.timeout_backstop.startswith('skipped'), started)
        self.assertIn('3.4.2', started.timeout_backstop)
        self.assertEqual('started', self.journal.entry(self.token)['state'])  # the run still launched

    def test_dec49_the_switch_stays_off(self):
        self.assertIsNone(container_launch.SETTINGS)

    def test_a_container_whose_deadline_podman_did_not_take_is_refused_and_removed(self):
        self.admit()
        self.podman.reported_timeout = 0
        with self.assertRaises(container_launch.ContainerLaunchRefused) as refused:
            self.start()
        self.assertEqual('start_check_failed', refused.exception.reason)
        self.assertNotIn(self.name, self.podman.containers)
        self.assertEqual('gone', self.journal.entry(self.token)['state'])

    def test_a_refused_start_check_records_gone_once_the_container_is_removed(self):
        self.admit()
        self.podman.effective_overrides = {'pids.max': 'max'}
        with self.assertRaises(container_launch.ContainerLaunchRefused):
            self.start()
        self.assertNotIn(self.name, self.podman.containers)
        self.assertEqual('gone', self.journal.entry(self.token)['state'])

    def test_stop_records_stopping_then_gone_or_stop_failed(self):
        self.admit()
        started = self.start()
        self.assertTrue(self.launcher.stop(started))
        self.assertEqual('gone', self.journal.entry(self.token)['state'])
        other = write_intent(self.launcher)
        self.journal.admitted(other, OTHER_BRIDGE, deadline_ms())
        held = self.start(bridge_run_id=OTHER_BRIDGE)
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.assertFalse(self.launcher.stop(held))
        entry = self.journal.entry(other)
        self.assertEqual('stop_failed', entry['state'])
        self.assertIn('hold its slot', entry['stop_detail'])


# --------------------------------------------------------------------------------------------
# Bridge launches through the registry (fake podman, a local stand-in for the attached output)
# --------------------------------------------------------------------------------------------

class RegistryContainerTests(unittest.TestCase):
    def setUp(self):
        self.frames = json.dumps({'type': 'from-the-container'})
        self.launcher = fake_launcher(self, attach=self.attach)
        self.token = write_intent(self.launcher)
        self.journal.admitted(self.token, BRIDGE, deadline_ms())
        self.registry = OwnedExecutionRegistry()
        self.registry.launch_journal = self.journal
        self.registry.container_launch = self.launcher
        self.ticket = self.registry.issue(OwnedExecutionKey(PROJECT, 'b' * 64, 'run:' + 'c' * 64, BRIDGE),
                                          deadline_ns=time.monotonic_ns() + 60_000_000_000)

    def attach(self, started):
        # Stands in for `podman logs --follow` + `podman wait`: the container's output, then its exit.
        return [sys.executable, '-I', '-B', '-c', f'print({self.frames!r})']

    def run_child(self, arguments=('python3', '-c', 'print(1)'), deadline=10):
        received = []
        outcome = bridge.run_child(list(arguments), environment={'PATH': os.defpath}, cwd=Path(self.workspace),
                                   deadline_seconds=deadline, on_frame=received.append, _execution=self.ticket)
        return outcome, received

    def test_a_bridge_launch_runs_in_its_container_and_is_confirmed_once_the_container_is_gone(self):
        outcome, received = self.run_child()
        self.assertEqual({'returncode': 0, 'abort': None}, outcome)
        self.assertEqual([{'type': 'from-the-container'}], received)
        [create] = self.podman.commands('create')
        self.assertEqual(('python3', '-c', 'print(1)'), tuple(create[-3:]))  # the child's command, inside
        self.assertEqual('gone', self.journal.entry(self.token)['state'])
        self.assertNotIn(self.journal.entry(self.token)['container_name'], self.podman.containers)
        # No process-journal states were written for it.
        self.assertEqual([self.token], [entry['token'] for entry in self.journal.entries()])
        self.assertEqual(BRIDGE, self.registry.key(self.ticket, cleaned=True).bridge_run_id)

    def test_a_container_not_proven_gone_keeps_the_run_held(self):
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        outcome, _ = self.run_child()
        self.assertEqual('stop_failed', self.journal.entry(self.token)['state'])
        with self.assertRaises(ExecutionCustodyError):
            self.registry.key(self.ticket, cleaned=True)

    def test_a_refused_container_launch_never_runs_the_child(self):
        self.podman.images = set()
        outcome, received = self.run_child()
        self.assertEqual({'returncode': None, 'abort': 'launch_failed'}, outcome)
        self.assertEqual([], received)
        self.assertNotEqual('started', self.journal.entry(self.token)['state'])


# --------------------------------------------------------------------------------------------
# Real podman
# --------------------------------------------------------------------------------------------

DEADLINE_SERVICE = textwrap.dedent('''
    import json, sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from codex_agents import container_launch, container_runtime as cr
    from codex_agents.launch_journal import LaunchJournal
    journal = LaunchJournal(Path(sys.argv[2]))
    runtime = cr.ContainerRuntime(sys.argv[3], name_prefix="cocolo-test-", runner=cr.SubprocessPodman(sys.argv[4]))
    config = container_launch.ContainerLaunchConfig(image=sys.argv[5], seccomp_profile=sys.argv[6],
                                                    name_prefix="cocolo-test-")
    launcher = container_launch.ContainerLauncher(runtime, journal, config)
    token = launcher.intent(run_handle="hrn_" + "b" * 48, attempt=1, host_client="codex-host-v1", lease={},
                            identity_digest="e" * 64, claim_request_id="claim-" + "f" * 32,
                            storage_evidence=b"{}", workspace=None)
    journal.admitted(token, "brg_" + "c" * 32, int(time.time() * 1000) + 600000)
    started = launcher.start("brg_" + "c" * 32, ("sleep", "600"), workspace=sys.argv[7], run_tag="0" * 32,
                             deadline_ns=time.monotonic_ns() + int(float(sys.argv[8]) * 1e9))
    print(json.dumps([started.name, time.monotonic()]), flush=True)
    time.sleep(600)
''')


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealContainerLaunchTests(unittest.TestCase):
    """Real containers in the existing podman machine; names and the project label start cocolo-test-."""

    @classmethod
    def setUpClass(cls):
        host = cr.default_host_files(cr.SubprocessPodman(REAL_PODMAN))
        profile = cr.read_default_seccomp_profile(host)
        if isinstance(profile, cr.Refusal):
            raise unittest.SkipTest("the podman host's default seccomp profile is unreadable: " + profile.message)
        cls.folder = os.path.realpath(tempfile.mkdtemp(prefix='cocolo-test-', dir=SHORT_TMP))
        cls.seccomp = os.path.join(cls.folder, 'seccomp.json')
        Path(cls.seccomp).write_text(json.dumps(cr.derive_seccomp_profile(profile)))

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(OSError):
            os.unlink(cls.seccomp)
            os.rmdir(cls.folder)

    def setUp(self):
        self.project = 'cocolo-test-' + secrets.token_hex(4)
        self.addCleanup(self.remove_test_containers)
        self.workspace = private_folder(self)
        self.directory = Path(private_folder(self)) / 'launches'

    def podman(self, *args, timeout=60):
        return subprocess.run([REAL_PODMAN, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def remove_test_containers(self):
        """Remove exactly this test's containers: those carrying its own cocolo-test- project label."""
        listing = self.podman('ps', '--all', f'--filter=label={cr.PROJECT_LABEL}={self.project}', '--format=json')
        for row in json.loads(listing.stdout or '[]'):
            name = row['Names'][0]
            if name.startswith('cocolo-test-') and row['Labels'].get(cr.PROJECT_LABEL) == self.project:
                self.podman('rm', '--force', '--time=0', name)

    def launcher(self):
        runtime = cr.ContainerRuntime(self.project, name_prefix='cocolo-test-', runner=cr.SubprocessPodman(REAL_PODMAN))
        self.journal = LaunchJournal(self.directory)
        self.addCleanup(self.journal.release)
        config = container_launch.ContainerLaunchConfig(image=REAL_IMAGE, seccomp_profile=self.seccomp,
                                                        grace_seconds=1, name_prefix='cocolo-test-')
        return container_launch.ContainerLauncher(runtime, self.journal, config)

    def registry_ticket(self, launcher, seconds):
        token = write_intent(launcher)
        self.journal.admitted(token, BRIDGE, deadline_ms())
        registry = OwnedExecutionRegistry()
        registry.launch_journal = self.journal
        registry.container_launch = launcher
        ticket = registry.issue(OwnedExecutionKey(PROJECT, 'b' * 64, 'run:' + 'c' * 64, BRIDGE),
                                deadline_ns=time.monotonic_ns() + int(seconds * 1e9))
        return token, registry, ticket

    def exists(self, name):
        return self.podman('container', 'exists', name).returncode == 0

    def test_real_bridge_launch_runs_in_a_container(self):
        launcher = self.launcher()
        token, registry, ticket = self.registry_ticket(launcher, 60)
        Path(self.workspace, 'input.txt').write_text('seen')
        script = ('import json, os; print(json.dumps({"uid": os.getuid(), "input": open("/workspace/input.txt").read(),'
                  ' "home": os.environ["HOME"]})); print(json.dumps({"done": True}))')
        received = []
        started = time.monotonic()
        outcome = bridge.run_child(['python3', '-c', script], environment={'PATH': os.defpath},
                                   cwd=Path(self.workspace), deadline_seconds=60, on_frame=received.append,
                                   _execution=ticket)
        elapsed = time.monotonic() - started
        self.assertEqual({'returncode': 0, 'abort': None}, outcome)
        self.assertEqual([{'uid': 1000, 'input': 'seen', 'home': cr.CONTAINER_HOME}, {'done': True}], received)
        entry = self.journal.entry(token)
        self.assertEqual('gone', entry['state'])
        self.assertFalse(self.exists(entry['container_name']))
        self.assertEqual(BRIDGE, registry.key(ticket, cleaned=True).bridge_run_id)
        print(f'\n[C2 real] bridge launch in a container {elapsed:.2f} s', file=sys.stderr)

    def test_real_bridge_deadline_stops_the_container(self):
        launcher = self.launcher()
        token, registry, ticket = self.registry_ticket(launcher, 60)
        outcome = bridge.run_child(['sleep', '600'], environment={'PATH': os.defpath}, cwd=Path(self.workspace),
                                   deadline_seconds=3, on_frame=lambda frame: None, _execution=ticket)
        self.assertEqual('timed_out', outcome['abort'])
        entry = self.journal.entry(token)
        self.assertEqual('gone', entry['state'])
        self.assertFalse(self.exists(entry['container_name']))

    def test_real_deadline_fires_with_the_service_killed(self):
        seconds = 4
        service = subprocess.Popen(
            [sys.executable, '-B', '-c', DEADLINE_SERVICE, str(ORCHESTRATION_ROOT), str(self.directory), self.project,
             REAL_PODMAN, REAL_IMAGE, self.seccomp, self.workspace, str(seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            line = service.stdout.readline().decode()
            self.assertTrue(line, service.stderr.read(4096).decode() if not line else '')
            name, _ = json.loads(line)
            launched_at = time.monotonic()
        finally:
            os.killpg(service.pid, signal.SIGKILL)  # the service is gone; only podman can stop it now
            service.wait(10)
            service.stdout.close()
            service.stderr.close()
        self.assertEqual('running', self.podman('inspect', name, '--format', '{{.State.Status}}').stdout.strip())
        deadline = time.monotonic() + seconds + 20
        while time.monotonic() < deadline:
            status = self.podman('inspect', name, '--format', '{{.State.Status}}').stdout.strip()
            if status != 'running':
                break
            time.sleep(0.5)
        ended_after = time.monotonic() - launched_at
        self.assertNotEqual('running', status)
        self.assertLess(ended_after, seconds + 10)
        # The killed service's entry still names the started container: recovery (C4) finds it.
        later = LaunchJournal(self.directory)
        self.addCleanup(later.release)
        [entry] = later.entries()
        self.assertEqual(('started', name), (entry['state'], entry['container_name']))
        print(f'\n[C2 real] deadline {seconds} s fired {ended_after:.2f} s after launch, service killed',
              file=sys.stderr)


if __name__ == '__main__':
    unittest.main()
