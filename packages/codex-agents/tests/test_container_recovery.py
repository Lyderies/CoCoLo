"""C4: recovery after a restart, by name and label (PLAN-0.4.0-supervisor-slices.md, C4).

Requirements (RS-C-6 reduced, RS-C-9, RS-3 for processes, the "workspace removal is checked" case), container
mode only (behind C2's switch; the process mode is unchanged):
- C4-1 At service start and on each sweep, every container entry of this project left by an earlier service
  instance (its lock free: it can launch nothing more) is checked with the OS by name and label (the
  project's listing `podman ps --all --filter label=cocolo.project=<project>`, then the container by its
  name, its labels checked): a container that exists is stopped through `container_runtime.stop` (the one
  stop action, reason `service_lost`) and the run is then settled as lost (`settle_lost_run`) with its stop
  evidence kept in the entry; its workspace is removed.
- C4-2 A container that does not exist: an entry whose claim is recorded as admitted (`started`, `stopping`,
  `gone`, `stop_failed`, or an admitted `intent`) is settled as lost; an `intent` whose admission was never
  recorded is fenced (`FenceClaimInput`), so the account closes the claim request for good; if the account
  admitted it after all, it is settled as lost with the fence's receipt. Each run is settled once.
- C4-3 A stop that does not prove the container gone leaves the entry `stop_failed` and its slot held; it is
  tried again with the launcher's back-off and settled once the OS says gone.
- C4-4 Orphans: a container carrying this project's labels whose run has no journal entry is stopped and
  reported (status); other projects' containers, unlabelled ones, and labelled ones whose name is not that
  run's are never touched; the live service's own containers are never orphans.
- C4-5 Podman unreachable: nothing is settled and nothing is written to the entries (they stay held); the
  check is tried again with back-off, and status says why the entries wait. Podman going away part way
  through recovery leaves the run held (`stop_failed`), never settled.
- C4-6 Never touched: an earlier instance that still holds its lock (it may still launch); a v1 (process)
  entry, settled by today's rule and never asked of podman; a container under the entry's name whose labels
  are not this run's.
- C4-7 A machine or VM restart is just "exited": every entry's container gone or exited, each settled once.
- C4-8 Status shows a container entry's launch token in its own field (`token`), never as `bridge_run_id`.
- C4-9 `ContainerRuntime` refuses a per-run network whose `cocolo.run` / `cocolo.project` labels are not the
  run's, before anything is created.

The real-podman tests run only with podman, its machine and python:3.12-slim present (never pulled); they create
containers named `cocolo-test-*` with a per-test project label and remove exactly those, by name. They never
touch the machine and never prune.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
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
from tests.test_container_launch_journal import (BRIDGE, CONTAINER_PROJECT, HANDLE, OTHER_BRIDGE, TimeoutPodman,
                                                 deadline_ms, private_folder, write_intent)
from tests.test_container_runtime import FakeClock, FakeHostFiles, REAL_IMAGE, REAL_PODMAN, REAL_SKIP_REASON

from codex_agents import container_launch, launch_journal
from codex_agents import container_runtime as cr
from codex_agents.launch_journal import LaunchJournal
from codex_agents.project_driver import RunDriver

THIRD_BRIDGE = 'brg_' + 'e' * 32
OTHER_PROJECT = 'cocolo-test-other'


def settled_reply(entry):
    return {'state': 'settled', 'run_handle': entry['run_handle'], 'bridge_run_id': entry['bridge_run_id'],
            'receipt_digest': '9' * 64, 'known': False}


def claimed_receipt(bridge_run_id):
    return {'receipt': {'state': 'claimed', 'bridge_run_id': bridge_run_id, 'deadline_at_ms': deadline_ms(),
                        'receipt_digest': '8' * 64, 'run_id': 'run-1', 'reason_code': None,
                        'recorded_at_ms': 1, 'known': True}, 'closure': None}


def denied_receipt():
    return {'receipt': {'state': 'denied', 'bridge_run_id': None, 'deadline_at_ms': None, 'receipt_digest': '7' * 64,
                        'run_id': 'run-1', 'reason_code': 'claim_not_open', 'recorded_at_ms': 1, 'known': False},
            'closure': {'closure': 'stands in for the account closure'}}


class RecoveryCase(unittest.TestCase):
    """An earlier service (its journal and launcher) over a fake podman; then the later service's driver."""

    def setUp(self):
        root = private_folder(self)
        self.workspace = os.path.join(root, 'workspace')
        os.mkdir(self.workspace)
        self.seccomp = os.path.join(root, 'seccomp.json')
        Path(self.seccomp).write_text('{}')
        self.host = FakeHostFiles()
        self.podman = TimeoutPodman(self.host)
        self.clock = FakeClock()
        self.directory = Path(root) / 'launches'
        self.config = container_launch.ContainerLaunchConfig(image='python:3.12-slim', seccomp_profile=self.seccomp,
                                                             grace_seconds=1)
        self.earlier_journal = LaunchJournal(self.directory)
        self.addCleanup(self.earlier_journal.release)
        self.earlier = self.launcher(self.earlier_journal)

    def runtime(self, project=CONTAINER_PROJECT):
        return cr.ContainerRuntime(project, runner=self.podman, host_files=self.host, monotonic=self.clock.monotonic,
                                   sleep=self.clock.sleep)

    def launcher(self, journal, project=CONTAINER_PROJECT):
        return container_launch.ContainerLauncher(self.runtime(project), journal, self.config,
                                                  clock=self.clock.monotonic)

    # -- the earlier service's runs, left at each journal state
    def intent(self, *, admitted=None):
        token = write_intent(self.earlier)
        if admitted is not None:
            self.earlier_journal.admitted(token, admitted, deadline_ms())
        return token

    def started(self, bridge_run_id=BRIDGE):
        token = self.intent(admitted=bridge_run_id)
        self.earlier.start(bridge_run_id, ('python3', '-c', 'pass'), workspace=self.workspace, run_tag='0' * 32,
                           deadline_ns=time.monotonic_ns() + 30_000_000_000)
        return token

    def entry(self, token):
        return self.earlier_journal._read(self.directory / (token + '.json'))

    def name(self, token):
        return self.entry(token)['container_name']

    def started_container(self, token):
        value = self.entry(token)
        return container_launch.StartedContainer(token, value['container_name'], value['container_id'] or '',
                                                 value['cgroup_path'] or '')

    def vanish(self, token):
        """The container went (its watchdog stopped it, say): removed, its cgroup gone."""
        container = self.podman.containers.pop(self.name(token))
        self.podman._end(container, 137)

    def stop_commands(self, name):
        return [call for call in self.podman.calls if call[0] in ('stop', 'rm') and name in call]

    # -- the later service
    def crash_earlier(self):
        """The earlier service dies: the kernel frees its instance lock."""
        self.earlier_journal.release()

    def later(self, *, fence=None):
        """The project's next service: its own journal in the same folder and a driver over it."""
        self.later_journal = LaunchJournal(self.directory)
        self.addCleanup(self.later_journal.release)
        value = object.__new__(RunDriver)
        value._clock = self.clock.monotonic
        value._giving_back, value._cleanup_retry, value._forgetting = [], {}, {}
        value._preparation = mock.Mock()
        value._runtime = SimpleNamespace(persistence=mock.Mock(), owner=mock.Mock())
        value._account_call = mock.Mock()
        value._reports = []
        value._journal = self.later_journal
        value._containers = self.launcher(self.later_journal)
        value._settle_entry = mock.Mock(side_effect=settled_reply)
        value._fence_entry = mock.Mock(return_value=denied_receipt() if fence is None else fence)
        value._remove_workspace = mock.Mock()
        self.driver = value
        return value

    def sweep(self, times=1):
        lost = None
        for _ in range(times):
            lost = self.driver._settle_lost(False)
        return lost

    def settled_tokens(self):
        return [call.args[0]['token'] for call in self.driver._settle_entry.call_args_list]

    def holds(self):
        return launch_journal.container_holds(self.directory)


# --------------------------------------------------------------------------------------------
# C4-1 and C4-2: each state an earlier service can leave
# --------------------------------------------------------------------------------------------

class RecoveryByStateTests(RecoveryCase):
    def test_a_running_container_is_stopped_through_the_one_stop_action_then_settled_lost(self):
        token = self.started()
        name = self.name(token)
        self.crash_earlier()
        self.later()
        lost = self.sweep()
        self.assertNotIn(name, self.podman.containers)
        self.assertEqual(('stop', '--time=1', name), [c for c in self.podman.calls if c[0] == 'stop'][0])
        entry = self.entry(token)
        self.assertEqual(('settled', 'settled', 'service_lost'), (entry['state'], entry['outcome'],
                                                                  entry['stop_reason']))
        self.assertTrue(entry['stop_detail'])  # the OS's answer is kept as the stop evidence
        self.assertEqual([token], self.settled_tokens())
        [settled] = self.driver._settle_entry.call_args.args
        self.assertEqual(BRIDGE, settled['bridge_run_id'])
        self.driver._fence_entry.assert_not_called()  # admitted: settle_lost_run's own receipt settles it
        self.assertEqual({HANDLE: ('codex-host-v1', 'released')}, lost)
        # Settled once, whatever the sweeps after.
        self.sweep(3)
        self.assertEqual([token], self.settled_tokens())

    def test_the_settled_runs_workspace_is_removed(self):
        token = self.started()
        self.crash_earlier()
        self.later()
        self.sweep()
        self.assertEqual('settled', self.entry(token)['state'])
        self.driver._remove_workspace.assert_called_once_with(self.entry(token)['workspace'])

    def test_a_started_entry_whose_container_is_gone_is_settled_lost(self):
        token = self.started()
        self.vanish(token)  # the detached watchdog stopped it, unrecorded
        self.crash_earlier()
        self.later()
        self.sweep()
        entry = self.entry(token)
        self.assertEqual(('settled', 'settled'), (entry['state'], entry['outcome']))
        self.assertIn('no longer exists', entry['stop_detail'])
        self.assertEqual([token], self.settled_tokens())

    def test_an_intent_never_admitted_is_fenced_so_the_account_closes_its_claim(self):
        token = self.intent()
        self.crash_earlier()
        self.later()
        lost = self.sweep()
        self.driver._fence_entry.assert_called_once()
        [fenced] = self.driver._fence_entry.call_args.args
        self.assertEqual((token, None), (fenced['token'], fenced['bridge_run_id']))
        self.driver._settle_entry.assert_not_called()  # never admitted: nothing to settle as lost
        entry = self.entry(token)
        self.assertEqual(('settled', 'fenced'), (entry['state'], entry['outcome']))
        self.assertEqual({HANDLE: ('codex-host-v1', 'released')}, lost)
        self.assertEqual([], [c for c in self.podman.calls if c[0] in ('stop', 'rm')])  # nothing existed
        self.sweep(2)
        self.driver._fence_entry.assert_called_once()

    def test_an_intent_the_account_admitted_after_all_is_settled_lost_with_the_fences_receipt(self):
        token = self.intent()
        self.crash_earlier()
        self.later(fence=claimed_receipt(OTHER_BRIDGE))
        self.sweep()
        [settled] = self.driver._settle_entry.call_args.args
        self.assertEqual((token, OTHER_BRIDGE), (settled['token'], settled['bridge_run_id']))
        self.assertEqual(('settled', 'settled'), (self.entry(token)['state'], self.entry(token)['outcome']))

    def test_an_admitted_intent_with_no_container_is_settled_lost(self):
        token = self.intent(admitted=BRIDGE)
        self.crash_earlier()
        self.later()
        self.sweep()
        self.assertEqual([token], self.settled_tokens())
        self.assertEqual('settled', self.entry(token)['state'])

    def test_an_admitted_intent_whose_container_was_created_is_stopped_then_settled(self):
        token = self.intent(admitted=BRIDGE)
        name = self.name(token)
        # Killed between `podman create` (and start) and the `started` write.
        self.podman.add_container(name, self.entry(token)['labels'])
        self.crash_earlier()
        self.later()
        self.sweep()
        self.assertNotIn(name, self.podman.containers)
        self.assertTrue(self.stop_commands(name))
        self.assertEqual(('settled', 'service_lost'), (self.entry(token)['state'], self.entry(token)['stop_reason']))
        self.assertEqual([token], self.settled_tokens())

    def test_a_service_killed_at_each_journal_state_is_settled_once(self):
        tokens = {}
        tokens['intent'] = self.intent()
        tokens['admitted'] = self.intent(admitted='brg_' + '1' * 32)
        tokens['started'] = self.started('brg_' + '2' * 32)
        tokens['stopping'] = self.started('brg_' + '3' * 32)
        self.earlier_journal.stopping(tokens['stopping'], 'work_silence')  # killed mid-stop
        tokens['gone'] = self.started('brg_' + '4' * 32)
        self.assertTrue(self.earlier.stop(self.started_container(tokens['gone']), 'run_ended'))
        tokens['stop_failed'] = self.started('brg_' + '5' * 32)
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.assertFalse(self.earlier.stop(self.started_container(tokens['stop_failed']), 'cancel_requested'))
        self.podman.stop_does_nothing = self.podman.rm_fails = False
        self.crash_earlier()
        self.later()
        self.sweep(3)
        for state, token in tokens.items():
            with self.subTest(state=state):
                self.assertEqual('settled', self.entry(token)['state'])
        self.assertEqual({}, {n: c for n, c in self.podman.containers.items()})
        self.assertEqual(sorted(t for s, t in tokens.items() if s != 'intent'), sorted(self.settled_tokens()))
        self.assertEqual(1, self.driver._fence_entry.call_count)
        # The first reason each was stopped for is kept.
        self.assertEqual('work_silence', self.entry(tokens['stopping'])['stop_reason'])
        self.assertEqual('cancel_requested', self.entry(tokens['stop_failed'])['stop_reason'])
        self.assertEqual('run_ended', self.entry(tokens['gone'])['stop_reason'])


# --------------------------------------------------------------------------------------------
# C4-3: a stop that fails stays held
# --------------------------------------------------------------------------------------------

class StopFailedRecoveryTests(RecoveryCase):
    def test_a_container_that_will_not_stop_stays_stop_failed_and_held_until_a_retry_proves_it_gone(self):
        token = self.started()
        name = self.name(token)
        self.crash_earlier()
        self.later()
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        lost = self.sweep()
        entry = self.entry(token)
        self.assertEqual(('stop_failed', 'service_lost'), (entry['state'], entry['stop_reason']))
        self.assertEqual({HANDLE: ('codex-host-v1', 'held')}, lost)
        self.driver._settle_entry.assert_not_called()
        self.assertEqual(1, self.holds()['counts']['stop_failed'])
        # Not tried again before its back-off.
        stops = len(self.stop_commands(name))
        self.sweep(3)
        self.assertEqual(stops, len(self.stop_commands(name)))
        self.driver._settle_entry.assert_not_called()
        # The container can be stopped now: the retried stop proves it gone, and the run is settled once.
        self.podman.stop_does_nothing = self.podman.rm_fails = False
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        self.sweep(3)
        self.assertNotIn(name, self.podman.containers)
        self.assertEqual(('settled', 'settled'), (self.entry(token)['state'], self.entry(token)['outcome']))
        self.assertEqual([token], self.settled_tokens())

    def test_an_account_that_does_not_answer_keeps_the_run_held_and_is_asked_again(self):
        token = self.started()
        self.crash_earlier()
        self.later()
        self.driver._settle_entry.side_effect = RuntimeError('account unreachable')
        lost = self.sweep()
        self.assertEqual('gone', self.entry(token)['state'])
        self.assertEqual({HANDLE: ('codex-host-v1', 'held')}, lost)
        self.sweep(2)
        self.assertEqual(1, self.driver._settle_entry.call_count)  # back-off
        self.driver._settle_entry.side_effect = settled_reply
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        self.sweep(2)
        self.assertEqual(2, self.driver._settle_entry.call_count)
        self.assertEqual('settled', self.entry(token)['state'])

    def test_a_refused_fence_keeps_the_intent_held_and_is_asked_again(self):
        token = self.intent()
        self.crash_earlier()
        self.later()
        self.driver._fence_entry.side_effect = RuntimeError('account unreachable')
        self.sweep(2)
        self.assertNotEqual('settled', self.entry(token)['state'])
        self.assertEqual(1, self.driver._fence_entry.call_count)
        self.driver._fence_entry.side_effect = None
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        self.sweep()
        self.assertEqual(('settled', 'fenced'), (self.entry(token)['state'], self.entry(token)['outcome']))


# --------------------------------------------------------------------------------------------
# C4-4: orphans
# --------------------------------------------------------------------------------------------

class OrphanRecoveryTests(RecoveryCase):
    def labelled(self, token, project=CONTAINER_PROJECT, name=None):
        identity = self.runtime(project).identity(token)
        return self.podman.add_container(name or identity.name, identity.labels)

    def test_an_orphan_with_this_projects_labels_is_stopped_and_reported_and_nothing_else_is_touched(self):
        orphan = self.labelled(launch_journal.new_token())
        other_project = self.labelled(launch_journal.new_token(), OTHER_PROJECT)
        unlabelled = self.podman.add_container('someone-elses-container', {})
        misnamed = self.labelled(launch_journal.new_token(), name='cocolo-run-' + 'f' * 32)
        self.crash_earlier()
        self.later()
        own_launcher = self.driver._containers  # the live service's own run: never an orphan
        own_token = write_intent(own_launcher)
        self.later_journal.admitted(own_token, BRIDGE, deadline_ms())
        own_launcher.start(BRIDGE, ('python3', '-c', 'pass'), workspace=self.workspace, run_tag='0' * 32,
                           deadline_ns=time.monotonic_ns() + 30_000_000_000)
        own_name = self.later_journal.entry(own_token)['container_name']
        self.sweep()
        self.assertNotIn(orphan.name, self.podman.containers)
        for kept in (other_project.name, unlabelled.name, misnamed.name, own_name):
            self.assertIn(kept, self.podman.containers)
            self.assertEqual([], self.stop_commands(kept))
        self.assertEqual('started', self.later_journal.entry(own_token)['state'])
        recovery = self.holds()['recovery']
        outcomes = {item['container']: item['outcome'] for item in recovery['orphans']}
        self.assertEqual('removed', outcomes[orphan.name])
        self.assertEqual('skipped', outcomes[misnamed.name])
        self.assertNotIn(other_project.name, outcomes)

    def test_an_orphan_that_will_not_stop_is_reported_to_the_owner(self):
        orphan = self.labelled(launch_journal.new_token())
        self.crash_earlier()
        self.later()
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.sweep()
        self.assertIn(orphan.name, self.podman.containers)
        holds = self.holds()
        self.assertEqual('stop_failed', {i['container']: i['outcome'] for i in holds['recovery']['orphans']}[
            orphan.name])
        self.assertTrue(any(orphan.name in line for line in holds['attention']), holds['attention'])

    def test_an_entrys_own_container_is_never_an_orphan(self):
        token = self.started()
        self.podman.stop_does_nothing = self.podman.rm_fails = True
        self.crash_earlier()
        self.later()
        self.sweep()
        self.assertNotIn(self.name(token), {i['container'] for i in self.holds()['recovery']['orphans']})


# --------------------------------------------------------------------------------------------
# C4-5: podman unreachable
# --------------------------------------------------------------------------------------------

class UnreachablePodmanRecoveryTests(RecoveryCase):
    def test_podman_unreachable_settles_nothing_writes_nothing_and_is_retried(self):
        tokens = [self.started(), self.intent()]
        before = {token: self.entry(token) for token in tokens}
        self.crash_earlier()
        self.later()
        self.podman.unreachable = True
        lost = self.sweep()
        self.assertEqual(before, {token: self.entry(token) for token in tokens})  # not even adopted
        self.driver._settle_entry.assert_not_called()
        self.driver._fence_entry.assert_not_called()
        self.assertEqual({HANDLE: ('codex-host-v1', 'held')}, lost)
        holds = self.holds()
        self.assertEqual('unavailable', holds['recovery']['podman'])
        self.assertTrue(any('podman' in line and 'held' in line for line in holds['attention']), holds['attention'])
        # Retried with back-off, not on every sweep.
        calls = len(self.podman.calls)
        self.sweep(3)
        self.assertEqual(calls, len(self.podman.calls))
        self.podman.unreachable = False
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        self.sweep(2)
        for token in tokens:
            self.assertEqual('settled', self.entry(token)['state'])
        self.assertEqual('ok', self.holds()['recovery']['podman'])

    def test_podman_going_away_part_way_through_recovery_settles_nothing(self):
        token = self.started()
        name = self.name(token)
        self.crash_earlier()
        self.later()
        original = self.podman.__call__

        def going_away(args, timeout):
            result = original(args, timeout)
            if args[0] == 'ps':
                self.podman.unreachable = True  # stopped right after the listing
            return result

        self.driver._containers.runtime.runner = going_away
        self.sweep()
        self.assertIn(name, self.podman.containers)
        self.assertEqual('running', self.podman.containers[name].status)
        self.assertEqual('stop_failed', self.entry(token)['state'])
        self.driver._settle_entry.assert_not_called()


# --------------------------------------------------------------------------------------------
# C4-6 and C4-7: never touched; a machine restart
# --------------------------------------------------------------------------------------------

class NeverTouchedTests(RecoveryCase):
    def test_an_earlier_instance_that_still_holds_its_lock_is_left_alone(self):
        token = self.started()
        self.later()  # the earlier one did not die
        self.sweep(2)
        self.assertEqual('started', self.entry(token)['state'])
        self.assertEqual(self.earlier_journal.instance, self.entry(token)['instance'])
        self.assertEqual([], self.stop_commands(self.name(token)))
        self.driver._settle_entry.assert_not_called()

    def test_a_v1_entry_is_settled_by_todays_rule_and_never_asked_of_podman(self):
        self.earlier_journal.claimed(THIRD_BRIDGE, run_handle='hrn_' + 'a' * 48, attempt=1,
                                     host_client='codex-host-v1', lease={'project_id': 'prj_' + 'b' * 32},
                                     identity_digest='c' * 64, claim_request_id='claim-' + 'd' * 32,
                                     storage_evidence=b'{"evidence":1}', workspace=None)
        self.crash_earlier()
        self.later()
        self.sweep()
        self.assertEqual('settled', self.entry(THIRD_BRIDGE)['state'])
        self.assertEqual([THIRD_BRIDGE], [c.args[0]['bridge_run_id'] for c in self.driver._settle_entry.call_args_list])
        self.assertEqual([], [c for c in self.podman.calls if THIRD_BRIDGE in ' '.join(c)])
        self.assertEqual([], [c for c in self.podman.calls if c[0] in ('stop', 'rm', 'kill')])

    def test_a_container_under_the_entrys_name_with_other_labels_is_never_stopped(self):
        token = self.intent(admitted=BRIDGE)
        name = self.name(token)
        foreign = self.podman.add_container(name, {cr.PROJECT_LABEL: OTHER_PROJECT, cr.RUN_LABEL: token,
                                                   cr.KIND_LABEL: cr.RUN_KIND})
        self.crash_earlier()
        self.later()
        self.sweep(2)
        self.assertIs(foreign, self.podman.containers[name])
        self.assertEqual('running', foreign.status)
        self.assertEqual([], self.stop_commands(name))
        self.assertNotEqual('settled', self.entry(token)['state'])
        self.driver._settle_entry.assert_not_called()


class MachineRestartTests(RecoveryCase):
    def test_after_a_vm_restart_every_entry_is_exited_or_gone_and_settled_once(self):
        tokens = [self.started('brg_' + str(n) * 32) for n in range(1, 5)]
        self.vanish(tokens[0])
        for token in tokens[1:]:
            container = self.podman.containers[self.name(token)]
            self.podman._end(container, 137)  # the VM restarted: exited, every cgroup gone
        self.crash_earlier()
        self.later()
        self.sweep(4)
        for token in tokens:
            self.assertEqual('settled', self.entry(token)['state'])
        self.assertEqual({}, self.podman.containers)
        self.assertEqual(sorted(tokens), sorted(self.settled_tokens()))


# --------------------------------------------------------------------------------------------
# C4-8 status and C4-9 the network's labels
# --------------------------------------------------------------------------------------------

class StatusTokenTests(RecoveryCase):
    def test_status_names_a_container_entry_by_its_token_in_its_own_field(self):
        intent = self.intent()
        admitted = self.intent(admitted=BRIDGE)
        self.earlier_journal.claimed(THIRD_BRIDGE, run_handle='hrn_' + 'a' * 48, attempt=1,
                                     host_client='codex-host-v1', lease={'project_id': 'prj_' + 'b' * 32},
                                     identity_digest='c' * 64, claim_request_id='claim-' + 'd' * 32,
                                     storage_evidence=b'{"evidence":1}', workspace=None)
        self.earlier_journal.spawning(THIRD_BRIDGE)
        self.crash_earlier()
        report = launch_journal.unresolved(self.directory)
        found = {(item['token'], item['bridge_run_id'], item['state']) for item in report['entries']}
        self.assertEqual({(intent, None, 'intent'), (admitted, BRIDGE, 'intent'), (None, THIRD_BRIDGE, 'spawning')},
                         found)


class NetworkLabelTests(unittest.TestCase):
    def setUp(self):
        root = private_folder(self)
        self.host = FakeHostFiles()
        self.podman = TimeoutPodman(self.host)
        self.clock = FakeClock()
        self.runtime = cr.ContainerRuntime(CONTAINER_PROJECT, runner=self.podman, host_files=self.host,
                                           monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.seccomp = os.path.join(root, 'seccomp.json')
        Path(self.seccomp).write_text('{}')
        self.workspace = root
        self.podman.networks['cocolo-run-net'] = True

    def launch(self, run_id='run:one'):
        spec = cr.ContainerSpec(run_id=run_id, image='python:3.12-slim', command=('true',), workspace=self.workspace,
                                seccomp_profile=self.seccomp, network='cocolo-run-net')
        return self.runtime.launch(spec, lambda identity: None)

    def test_a_network_whose_labels_are_not_the_runs_is_refused_before_anything_exists(self):
        for labels in ({}, {cr.RUN_LABEL: 'run:other', cr.PROJECT_LABEL: CONTAINER_PROJECT},
                       {cr.RUN_LABEL: 'run:one', cr.PROJECT_LABEL: OTHER_PROJECT},
                       {cr.RUN_LABEL: 'run:one'}):
            with self.subTest(labels=labels):
                self.podman.network_labels['cocolo-run-net'] = labels
                result = self.launch()
                self.assertIsInstance(result, cr.Refusal)
                self.assertEqual('network_not_this_run', result.reason)
                self.assertEqual([], self.podman.commands('create'))

    def test_a_network_labelled_for_the_run_is_used(self):
        self.podman.network_labels['cocolo-run-net'] = {cr.RUN_LABEL: 'run:one', cr.PROJECT_LABEL: CONTAINER_PROJECT}
        launched = self.launch()
        self.assertTrue(launched.ok, launched)
        self.assertIn('--network=cocolo-run-net', self.podman.commands('create')[0])


# --------------------------------------------------------------------------------------------
# Real podman
# --------------------------------------------------------------------------------------------

# The earlier service: writes the run's admitted intent, starts its container (no watchdog: it is lost with
# the service), prints the token, then waits to be killed.
EARLIER_SERVICE = textwrap.dedent('''
    import json, sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from codex_agents import container_launch, container_runtime as cr
    from codex_agents.launch_journal import LaunchJournal
    journal = LaunchJournal(Path(sys.argv[2]))
    runtime = cr.ContainerRuntime(sys.argv[3], name_prefix="cocolo-test-", runner=cr.SubprocessPodman(sys.argv[4]))
    config = container_launch.ContainerLaunchConfig(image=sys.argv[5], seccomp_profile=sys.argv[6],
                                                    name_prefix="cocolo-test-", grace_seconds=1)
    launcher = container_launch.ContainerLauncher(runtime, journal, config)
    token = launcher.intent(run_handle="hrn_" + "b" * 48, attempt=1, host_client="codex-host-v1",
                            lease={"project_id": "prj_" + "a" * 32}, identity_digest="e" * 64,
                            claim_request_id="claim-" + "f" * 32, storage_evidence=b"{}", workspace=None)
    journal.admitted(token, "brg_" + "c" * 32, int(time.time() * 1000) + 600000)
    launcher.start("brg_" + "c" * 32, ["sleep", "600"], workspace=sys.argv[7], run_tag="0" * 32,
                   deadline_ns=time.monotonic_ns() + 600 * 10**9)
    print(json.dumps(token), flush=True)
    time.sleep(600)
''')


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealRecoveryTests(unittest.TestCase):
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

    def status(self, name):
        return self.podman('container', 'inspect', name, '--format', '{{.State.Status}}').stdout.strip()

    def later(self, runner=None, *, fence=None):
        """The next service: a new journal on the folder and a driver whose launcher uses the real podman."""
        journal = LaunchJournal(self.directory)
        self.addCleanup(journal.release)
        runtime = cr.ContainerRuntime(self.project, name_prefix='cocolo-test-',
                                      runner=runner or cr.SubprocessPodman(REAL_PODMAN))
        config = container_launch.ContainerLaunchConfig(image=REAL_IMAGE, seccomp_profile=self.seccomp,
                                                        name_prefix='cocolo-test-', grace_seconds=1)
        self.clock = FakeClock()
        launcher = container_launch.ContainerLauncher(runtime, journal, config, clock=self.clock.monotonic)
        self.addCleanup(launcher.close)
        value = object.__new__(RunDriver)
        value._clock = self.clock.monotonic
        value._journal, value._containers = journal, launcher
        value._settle_entry = mock.Mock(side_effect=settled_reply)
        value._fence_entry = mock.Mock(return_value=denied_receipt() if fence is None else fence)
        value._remove_workspace = mock.Mock()
        self.journal = journal
        return value

    def killed_service(self):
        """An earlier service with a running container, killed with SIGKILL: (token, container name)."""
        service = subprocess.Popen(
            [sys.executable, '-B', '-c', EARLIER_SERVICE, str(ORCHESTRATION_ROOT), str(self.directory), self.project,
             REAL_PODMAN, REAL_IMAGE, self.seccomp, self.workspace],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            line = service.stdout.readline().decode()
            self.assertTrue(line, service.stderr.read(4096).decode() if not line else '')
            token = json.loads(line)
        finally:
            os.killpg(service.pid, signal.SIGKILL)
            service.wait(10)
            service.stdout.close()
            service.stderr.close()
        entry = json.loads((self.directory / (token + '.json')).read_text())
        self.assertEqual('started', entry['state'])
        self.assertEqual('running', self.status(entry['container_name']))
        return token, entry['container_name']

    def test_real_a_service_killed_with_a_running_container_is_recovered_stopped_and_settled_lost(self):
        token, name = self.killed_service()
        driver = self.later()
        began = time.monotonic()
        lost = driver._settle_lost(False)
        seconds = time.monotonic() - began
        self.assertEqual(1, self.podman('container', 'exists', name).returncode)  # stopped and removed
        entry = self.journal.entry(token)
        self.assertEqual(('settled', 'settled', 'service_lost'), (entry['state'], entry['outcome'],
                                                                  entry['stop_reason']))
        self.assertTrue(entry['stop_detail'])
        driver._settle_entry.assert_called_once()
        self.assertEqual('brg_' + 'c' * 32, driver._settle_entry.call_args.args[0]['bridge_run_id'])
        self.assertEqual('released', lost['hrn_' + 'b' * 48][1])  # the slot the account held is freed
        print(f'\n[C4 real] killed service\'s container stopped and its run settled in {seconds:.2f} s',
              file=sys.stderr)

    def test_real_an_intent_with_no_container_is_settled(self):
        journal = LaunchJournal(self.directory)
        runtime = cr.ContainerRuntime(self.project, name_prefix='cocolo-test-', runner=cr.SubprocessPodman(REAL_PODMAN))
        config = container_launch.ContainerLaunchConfig(image=REAL_IMAGE, seccomp_profile=self.seccomp,
                                                        name_prefix='cocolo-test-')
        token = write_intent(container_launch.ContainerLauncher(runtime, journal, config))
        journal.release()  # killed before its claim was answered
        driver = self.later()
        driver._settle_lost(False)
        entry = self.journal.entry(token)
        self.assertEqual(('settled', 'fenced'), (entry['state'], entry['outcome']))
        self.assertIn('no longer exists', entry['stop_detail'])
        driver._fence_entry.assert_called_once()
        driver._settle_entry.assert_not_called()

    def test_real_podman_unreachable_settles_nothing(self):
        token, name = self.killed_service()
        before = self.directory.joinpath(token + '.json').read_text()
        unreachable = cr.SubprocessPodman(REAL_PODMAN, global_args=('--url', 'unix:///nonexistent/cocolo-test.sock'))
        driver = self.later(unreachable)
        driver._settle_lost(False)
        self.assertEqual(before, self.directory.joinpath(token + '.json').read_text())
        self.assertEqual('running', self.status(name))
        driver._settle_entry.assert_not_called()

    def test_real_podman_stopped_mid_recovery_settles_nothing(self):
        token, name = self.killed_service()

        class GoingAway(cr.SubprocessPodman):
            """Real podman; right after the project's listing it answers as a stopped machine does."""
            armed, gone = True, False

            def __call__(self, args, timeout):
                if self.gone:
                    args = ['--url', 'unix:///nonexistent/cocolo-test.sock', *args]
                result = super().__call__(args, timeout)
                if self.armed and args[0] == 'ps':
                    self.gone = True
                return result

        runner = GoingAway(REAL_PODMAN)
        driver = self.later(runner)
        driver._settle_lost(False)
        self.assertEqual('running', self.status(name))
        self.assertEqual('stop_failed', self.journal.entry(token)['state'])
        driver._settle_entry.assert_not_called()
        # Podman answers again: the retried stop proves it gone, and the run is settled once.
        runner.armed = runner.gone = False
        self.clock.now += container_launch.STOP_RETRY_MAX_SECONDS
        driver._settle_lost(False)
        driver._settle_lost(False)
        self.assertEqual(1, self.podman('container', 'exists', name).returncode)
        self.assertEqual('settled', self.journal.entry(token)['state'])
        driver._settle_entry.assert_called_once()


if __name__ == '__main__':
    unittest.main()
