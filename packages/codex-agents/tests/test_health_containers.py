"""HM-1: every COCOLO run container belongs to a live run (PLAN-0.4.0-supervisor-slices.md, "HM-1 Containers").

The check runs in the collector's health pass (HM-0), machine wide: the collector's state root is the one every
project service of this machine keeps its launch journals in (`<state root>/projects/<project>/launches`), so the
collector reads them there, read only (no lock taken, nothing written).

Requirements:
- HM-1-1 An orphan (a container labelled `cocolo.kind=run` of a project of this state root, named as its run's
  container, whose run has no journal entry) is stopped and removed through the one stop action
  (`ContainerRuntime.stop` with the run's labels) and reported `fixed` to that project.
- HM-1-2 A stop that does not prove the container gone is reported `stop_failed` (to be looked at) and is tried
  again at the next pass; once gone it is reported fixed.
- HM-1-3 Never touched: a container without the COCOLO run label; one of a project this state root does not have
  (another COCOLO install's, another project label); one carrying a project's label whose name is not its run's
  container (reported, left alone).
- HM-1-4 A live run's container is never touched: any journal entry counts as live, whatever its state, whichever
  instance wrote it (this one, another live one, a stopped one whose entry C4's recovery owns), and an entry
  file that cannot be read. The live set is read after the listing, so a run launched between the two is kept.
- HM-1-5 A live set that cannot be read completely (the journal folder missing, not owner-only, unlistable):
  nothing of that project is stopped, and it is reported to that project.
- HM-1-6 Podman unreachable or missing: reported; nothing is changed.
- HM-1-7 Containers off (`container_launch.SETTINGS` None) and no container entry in any project's journal: the
  check makes no podman call at all. A container entry left anywhere turns the check on.
- HM-1-8 Bounded: the check's own run returns within its share however long a stop takes (stops run on its own
  worker, whose findings come at a later pass; never two workers at once); at most MAX_STOPS_PER_PASS stops per
  pass, the rest wait for the next pass and are reported waiting.
- HM-1-9 The collector's health monitor carries the check, over its own state root.

The real-podman test runs only with podman, its machine and python:3.12-slim present (never pulled); it creates
containers named `cocolo-test-*` and removes exactly those, by name; a guard refuses any stop or removal of a
container it did not create. It never touches the machine and never prunes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import threading
import time
import unittest

import tests.support  # noqa: F401
from tests.support import SHORT_TMP
from tests.test_container_launch_journal import PROJECT, write_intent
from tests.test_container_runtime import FakeClock, FakeHostFiles, FakePodman, REAL_IMAGE, REAL_PODMAN, REAL_SKIP_REASON
from tests import test_learning_store as store_tests

from codex_agents import container_launch, health_containers as hc, health_monitor as hm, learning_store
from codex_agents import container_runtime as cr
from codex_agents.launch_journal import LaunchJournal

OTHER = 'prj_' + 'b' * 32
UNKNOWN = 'prj_' + 'c' * 32


def context(seconds=5.0):
    return hm.CheckContext(deadline=time.monotonic() + seconds, now_ms=time.time_ns() // 1_000_000,
                           since_last_pass_ms=None)


class ContainerCheckCase(unittest.TestCase):
    """A state root with projects, their journals, and a fake podman shared by every project's containers."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        os.chmod(self.root, 0o700)
        self.seccomp = self.root / 'seccomp.json'
        self.seccomp.write_text('{}')
        self.host = FakeHostFiles()
        self.podman = FakePodman(self.host)
        self.clock = FakeClock()
        self.config = container_launch.ContainerLaunchConfig(image='python:3.12-slim', seccomp_profile=str(self.seccomp),
                                                             grace_seconds=1)
        self.settings = self.config
        self.journals = {}

    def check(self, **options):
        values = dict(settings=lambda: self.settings, runner=self.podman, host_files=self.host,
                      monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        values.update(options)
        return hc.ContainerCheck(self.root, **values)

    def launches(self, project):
        folder = self.root / 'projects' / project
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root / 'projects', 0o700)
        os.chmod(folder, 0o700)
        return folder / 'launches'

    def journal(self, project=PROJECT):
        """A live service instance of the project: its journal (its lock held) in the project's folder."""
        journal = LaunchJournal(self.launches(project))
        self.addCleanup(journal.release)
        return journal

    def runtime(self, project=PROJECT):
        return cr.ContainerRuntime(project, runner=self.podman, host_files=self.host, monotonic=self.clock.monotonic,
                                   sleep=self.clock.sleep)

    def live_run(self, journal, project=PROJECT, status='running'):
        """A run whose intent is in that journal, and its container."""
        launcher = container_launch.ContainerLauncher(self.runtime(project), journal, self.config,
                                                      clock=self.clock.monotonic)
        token = write_intent(launcher)
        identity = self.runtime(project).identity(token)
        self.podman.add_container(identity.name, identity.labels, status=status)
        return identity

    def orphan(self, project=PROJECT, status='running'):
        """A run container of the project whose run has no journal entry."""
        identity = self.runtime(project).identity('lnc_' + secrets.token_hex(16))
        self.podman.add_container(identity.name, identity.labels, status=status)
        return identity

    def touched(self):
        """Every container a stopping or removing command was aimed at."""
        return {call[-1] for call in self.podman.calls if call[0] in ('stop', 'rm', 'kill', 'exec')}

    def container_ref(self, identity):
        return {identity.name, self.podman.containers[identity.name].id} if identity.name in self.podman.containers \
            else {identity.name}

    @staticmethod
    def by_reason(findings):
        found = {}
        for item in findings:
            found.setdefault(item.reason_code, []).append(item)
        return found


# --------------------------------------------------------------------------------------------
# HM-1-1 and HM-1-2: orphans
# --------------------------------------------------------------------------------------------

class OrphanTests(ContainerCheckCase):
    def test_HM_1_1_an_orphan_is_stopped_and_removed_and_reported_fixed_to_its_project(self):
        self.journal()
        orphan = self.orphan()
        exited = self.orphan(status='exited')
        findings = self.check().run(context())
        self.assertNotIn(orphan.name, self.podman.containers)
        self.assertNotIn(exited.name, self.podman.containers)
        fixed = self.by_reason(findings)['orphan_removed']
        self.assertEqual({orphan.name, exited.name}, {item.subject for item in fixed})
        for item in fixed:
            self.assertEqual(('containers', 'project', PROJECT, 'fixed'),
                             (item.check, item.scope, item.project_id, item.outcome))
        self.assertEqual(['orphan_removed'], sorted({item.reason_code for item in findings}))
        # through the one stop action: SIGTERM with the configured grace, then removal
        stops = [call for call in self.podman.commands('stop')]
        self.assertTrue(stops and all('--time=1' in call for call in stops), stops)
        # the machine listing asks only for run containers
        self.assertEqual(('ps', '--all', f'--filter=label={cr.KIND_LABEL}={cr.RUN_KIND}', '--format=json'),
                         self.podman.commands('ps')[0])

    def test_HM_1_1_a_healthy_machine_yields_no_findings(self):
        journal = self.journal()
        self.live_run(journal)
        self.assertEqual([], list(self.check().run(context())))

    def test_HM_1_2_a_stop_that_does_not_prove_it_gone_is_reported_stop_failed_and_tried_again_next_pass(self):
        self.journal()
        orphan = self.orphan()
        self.podman.stop_does_nothing = True
        self.podman.rm_fails = True
        check = self.check()
        first = check.run(context())
        failed = self.by_reason(first)['orphan_stop_failed']
        self.assertEqual([(orphan.name, 'project', PROJECT, 'reported')],
                         [(item.subject, item.scope, item.project_id, item.outcome) for item in failed])
        self.assertIn(orphan.name, self.podman.containers)
        stops_before = len(self.podman.commands('stop'))
        second = check.run(context())
        self.assertGreater(len(self.podman.commands('stop')), stops_before)  # tried again
        self.assertIn('orphan_stop_failed', self.by_reason(second))
        self.podman.stop_does_nothing = False
        self.podman.rm_fails = False
        third = check.run(context())
        self.assertNotIn(orphan.name, self.podman.containers)
        self.assertEqual(['orphan_removed'], [item.reason_code for item in third])

    def test_HM_1_2_through_the_monitor_a_failed_stop_reaches_the_projects_view_and_alerts(self):
        self.journal()
        self.orphan()
        self.podman.stop_does_nothing = True
        self.podman.rm_fails = True
        runner = hm.HealthMonitor((self.check(),), pass_budget_seconds=5.0, check_share_seconds=2.0)
        runner.run_pass()
        view = runner.view(PROJECT)
        self.assertEqual([('containers', 'findings')], [(c['check'], c['state']) for c in view['checks']])
        self.assertEqual(['orphan_stop_failed'], [f['reason_code'] for f in view['findings']])
        self.assertEqual([], runner.view(OTHER)['findings'])  # another project never sees it


# --------------------------------------------------------------------------------------------
# HM-1-3: never touched
# --------------------------------------------------------------------------------------------

class NeverTouchedTests(ContainerCheckCase):
    def test_HM_1_3_unlabelled_other_label_and_unknown_project_containers_are_never_touched(self):
        self.journal()
        self.journal(OTHER)
        unlabelled = self.podman.add_container('owners-database', {})
        project_only = self.podman.add_container('owners-tool', {cr.PROJECT_LABEL: PROJECT, cr.RUN_LABEL: 'lnc_x'})
        unknown = self.orphan(UNKNOWN)  # a project this state root does not have: another install's
        foreign_label = cr.ContainerRuntime('cocolo-test-elsewhere', runner=self.podman).identity('lnc_' + 'd' * 32)
        self.podman.add_container(foreign_label.name, foreign_label.labels)
        odd = self.podman.add_container('odd-name', {cr.PROJECT_LABEL: PROJECT, cr.RUN_LABEL: 'lnc_' + 'e' * 32,
                                                     cr.KIND_LABEL: cr.RUN_KIND})
        # a row without the labels (a filter podman did not apply) is still never touched
        self.podman.extra_ps_rows = [{'Id': 'f' * 64, 'Names': ['owners-other'], 'Labels': {}, 'State': 'running'}]
        findings = self.check().run(context())
        for name, container in (('owners-database', unlabelled), ('owners-tool', project_only),
                                (unknown.name, None), (foreign_label.name, None), ('odd-name', odd)):
            self.assertIn(name, self.podman.containers)
            refs = {name, self.podman.containers[name].id}
            self.assertFalse(refs & self.touched(), (name, self.podman.calls))
        self.assertNotIn('owners-other', self.touched())
        self.assertNotIn('f' * 64, self.touched())
        found = self.by_reason(findings)
        # the unknown projects' containers: reported once, to the machine, never fixed
        self.assertEqual(1, len(found['foreign_containers']))
        foreign = found['foreign_containers'][0]
        self.assertEqual(('machine', 'reported'), (foreign.scope, foreign.outcome))
        self.assertIn('2', foreign.text)
        # the misnamed one with this project's labels: reported to the project, left alone
        self.assertEqual([('project', PROJECT, 'reported')],
                         [(item.scope, item.project_id, item.outcome) for item in found['container_not_a_run']])
        self.assertNotIn('orphan_removed', found)

    def test_HM_1_3_another_projects_orphan_is_its_own_and_reported_only_to_it(self):
        self.journal()
        self.journal(OTHER)
        mine = self.orphan(PROJECT)
        theirs = self.orphan(OTHER)
        findings = self.check().run(context())
        self.assertEqual({PROJECT: [mine.name], OTHER: [theirs.name]},
                         {project: [item.subject for item in findings if item.project_id == project]
                          for project in (PROJECT, OTHER)})


# --------------------------------------------------------------------------------------------
# HM-1-4: a live run's container is never touched
# --------------------------------------------------------------------------------------------

class LiveRunTests(ContainerCheckCase):
    def test_HM_1_4_this_instances_and_another_live_instances_runs_are_never_touched(self):
        first = self.journal()
        second = self.journal()  # another live instance of the same project (its own lock held)
        mine = self.live_run(first)
        theirs = self.live_run(second)
        exited = self.live_run(first, status='exited')  # ended, its entry not yet gone: still its run's
        findings = self.check().run(context())
        for identity in (mine, theirs, exited):
            self.assertIn(identity.name, self.podman.containers)
            self.assertFalse(self.container_ref(identity) & self.touched(), self.podman.calls)
        self.assertEqual([], list(findings))

    def test_HM_1_4_a_stopped_instances_entry_a_settled_one_and_an_unreadable_one_count_as_live(self):
        stopped = LaunchJournal(self.launches(PROJECT))
        left = self.live_run(stopped)  # its service died: C4's recovery owns it, not the health check
        stopped.release()
        current = self.journal()
        settled = self.live_run(current, status='exited')
        current.settled(settled.run_id, 'settled')
        unreadable = self.orphan()
        (self.launches(PROJECT) / (unreadable.run_id + '.json')).write_bytes(b'{not json')
        os.chmod(self.launches(PROJECT) / (unreadable.run_id + '.json'), 0o600)
        findings = self.check().run(context())
        for identity in (left, settled, unreadable):
            self.assertIn(identity.name, self.podman.containers)
            self.assertFalse(self.container_ref(identity) & self.touched(), self.podman.calls)
        self.assertEqual([], list(findings))

    def test_HM_1_4_the_live_set_is_read_after_the_listing_so_a_run_launched_meanwhile_is_kept(self):
        journal = self.journal()
        launcher = container_launch.ContainerLauncher(self.runtime(), journal, self.config, clock=self.clock.monotonic)
        late = []
        listing = self.podman.__call__

        def runner(args, timeout):
            result = listing(args, timeout)
            if args[0] == 'ps' and not late:
                # A launch between the listing and the live set: its intent first, then its container.
                token = write_intent(launcher)
                identity = self.runtime().identity(token)
                self.podman.add_container(identity.name, identity.labels)
                late.append(identity)
                rows = json.loads(result.stdout)
                rows.append({'Id': self.podman.containers[identity.name].id, 'Names': [identity.name],
                             'Labels': identity.labels, 'State': 'running'})
                return cr.PodmanResult(0, json.dumps(rows), '')
            return result

        self.check(runner=runner).run(context())
        self.assertIn(late[0].name, self.podman.containers)
        self.assertFalse(self.container_ref(late[0]) & self.touched())


# --------------------------------------------------------------------------------------------
# HM-1-5 and HM-1-6: nothing is stopped on what cannot be read
# --------------------------------------------------------------------------------------------

class UnreadableTests(ContainerCheckCase):
    def test_HM_1_5_a_journal_folder_that_is_not_owner_only_stops_nothing_of_that_project_and_is_reported(self):
        self.journal()
        self.journal(OTHER)
        orphan = self.orphan(PROJECT)
        other = self.orphan(OTHER)
        os.chmod(self.launches(PROJECT), 0o755)
        self.addCleanup(os.chmod, self.launches(PROJECT), 0o700)
        findings = self.check().run(context())
        self.assertIn(orphan.name, self.podman.containers)
        self.assertFalse(self.container_ref(orphan) & self.touched())
        unreadable = self.by_reason(findings)['live_set_unreadable']
        self.assertEqual([('project', PROJECT, 'reported')],
                         [(item.scope, item.project_id, item.outcome) for item in unreadable])
        self.assertNotIn(other.name, self.podman.containers)  # the other project is still looked after

    def test_HM_1_5_a_project_folder_without_a_journal_folder_stops_nothing_and_is_reported(self):
        (self.root / 'projects' / PROJECT).mkdir(parents=True, mode=0o700)
        orphan = self.orphan(PROJECT)
        findings = self.check().run(context())
        self.assertIn(orphan.name, self.podman.containers)
        self.assertFalse(self.container_ref(orphan) & self.touched())
        self.assertIn('live_set_unreadable', self.by_reason(findings))

    def test_HM_1_6_podman_unreachable_is_reported_and_nothing_is_changed(self):
        self.journal()
        orphan = self.orphan()
        self.podman.unreachable = True
        findings = self.check().run(context())
        self.assertEqual(['ps'], [call[0] for call in self.podman.calls])
        self.assertIn(orphan.name, self.podman.containers)
        found = self.by_reason(findings)['podman_unavailable']
        self.assertEqual([('machine', 'reported')], [(item.scope, item.outcome) for item in found])
        self.assertNotIn('Traceback', found[0].text)

    def test_HM_1_6_podman_missing_or_hanging_is_reported(self):
        self.journal()
        for flag in ('missing', 'time_out'):
            podman = FakePodman(self.host)
            setattr(podman, flag, True)
            findings = self.check(runner=podman).run(context())
            self.assertEqual(['podman_unavailable'], [item.reason_code for item in findings], flag)


# --------------------------------------------------------------------------------------------
# HM-1-7: containers off
# --------------------------------------------------------------------------------------------

class ContainersOffTests(ContainerCheckCase):
    def test_HM_1_7_with_containers_off_and_no_container_entry_no_podman_call_is_made(self):
        self.settings = None
        journal = self.journal()  # a project service ran (process mode): its journal has no container entry
        journal.claimed('brg_' + '1' * 32, run_handle='hrn_' + '2' * 48, attempt=1, host_client='codex-host-v1',
                        lease={'project_id': PROJECT}, identity_digest='3' * 64, claim_request_id='claim-4',
                        storage_evidence=b'{}', workspace=None)
        self.orphan()  # even a container that looks like an orphan: containers are off here
        calls = []
        check = self.check(runner=lambda args, timeout: calls.append(args) or self.podman(args, timeout))
        self.assertEqual([], list(check.run(context())))
        self.assertEqual([], list(check.run(context())))
        self.assertEqual([], calls)
        # no state root projects folder at all: still nothing
        empty = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(empty.cleanup)
        bare = hc.ContainerCheck(Path(os.path.realpath(empty.name)), settings=lambda: None,
                                 runner=lambda args, timeout: calls.append(args) or self.podman(args, timeout))
        self.assertEqual([], list(bare.run(context())))
        self.assertEqual([], calls)

    def test_HM_1_7_with_containers_off_a_container_entry_left_anywhere_turns_the_check_on(self):
        self.settings = None
        journal = self.journal()
        self.live_run(journal)
        orphan = self.orphan()
        findings = self.check().run(context())
        self.assertTrue(self.podman.commands('ps'))
        self.assertNotIn(orphan.name, self.podman.containers)
        self.assertIn('orphan_removed', self.by_reason(findings))


# --------------------------------------------------------------------------------------------
# HM-1-8: bounded
# --------------------------------------------------------------------------------------------

class BoundTests(ContainerCheckCase):
    def test_HM_1_8_the_check_returns_within_its_share_while_a_stop_is_slow_and_reports_it_later(self):
        self.journal()
        orphan = self.orphan()
        release, entered = threading.Event(), threading.Event()
        lists = []

        def runner(args, timeout):
            if args[0] == 'ps':
                lists.append(args)
            if args[0] == 'stop':
                entered.set()
                release.wait(30)
            return self.podman(args, timeout)

        self.addCleanup(release.set)
        check = self.check(runner=runner)
        monitor = hm.HealthMonitor((check,), pass_budget_seconds=1.0, check_share_seconds=0.5)
        started = time.monotonic()
        monitor.run_pass()
        self.assertLess(time.monotonic() - started, 0.9)
        self.assertTrue(entered.wait(5))
        reasons = [f['reason_code'] for f in monitor.view(PROJECT)['findings']]
        self.assertNotIn('check_timeout', reasons)
        monitor.run_pass()  # the stop still runs: no second listing, no second worker
        self.assertEqual(1, len(lists))
        self.assertNotIn('check_timeout', [f['reason_code'] for f in monitor.view(PROJECT)['findings']])
        release.set()
        deadline = time.monotonic() + 5
        while orphan.name in self.podman.containers and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        monitor.run_pass()  # the finished stop's findings come now
        view = monitor.view(PROJECT)
        self.assertIn('orphan_removed', [f['reason_code'] for f in view['findings']])

    def test_HM_1_8_more_orphans_than_one_pass_stops_wait_for_the_next_pass_and_are_all_removed(self):
        self.journal()
        orphans = [self.orphan() for _ in range(hc.MAX_STOPS_PER_PASS + 2)]
        check = self.check()
        first = self.by_reason(check.run(context()))
        self.assertEqual(hc.MAX_STOPS_PER_PASS, len(first['orphan_removed']))
        waiting = first['orphans_waiting']
        self.assertEqual(1, len(waiting))
        self.assertIn('2', waiting[0].text)
        self.assertEqual(2, sum(identity.name in self.podman.containers for identity in orphans))
        second = self.by_reason(check.run(context()))
        self.assertEqual(2, len(second['orphan_removed']))
        self.assertNotIn('orphans_waiting', second)
        self.assertFalse(any(identity.name in self.podman.containers for identity in orphans))


# --------------------------------------------------------------------------------------------
# HM-1-9: wired into the collector
# --------------------------------------------------------------------------------------------

class CollectorWiringTests(unittest.TestCase):
    def test_HM_1_9_the_collectors_health_monitor_carries_the_containers_check_over_its_state_root(self):
        store = store_tests.LearningStoreTests(methodName='runTest')
        store.setUp()
        self.addCleanup(store.tearDown)
        server = learning_store.CollectorServer(store.ledger)
        names = [check.name for check in server.health_monitor._checks]
        self.assertEqual(['self_check', 'containers', 'resources'], names)  # S10 wiring adds the resources check
        check = server.health_monitor._checks[1]
        self.assertIsInstance(check, hc.ContainerCheck)
        self.assertEqual(store.ledger.state.root / 'projects', check.projects)
        # containers are off as shipped and nothing launched one: a pass is quiet and asks nothing of podman
        self.assertIsNone(container_launch.SETTINGS)
        server._run_health_pass()
        view = server.health_monitor.view(store.project_a_id)
        self.assertEqual([], view['findings'])
        self.assertEqual({'self_check': 'ok', 'containers': 'ok', 'resources': 'ok'},
                         {c['check']: c['state'] for c in view['checks']})


# --------------------------------------------------------------------------------------------
# Real podman
# --------------------------------------------------------------------------------------------

class Guarded(cr.SubprocessPodman):
    """The real podman client, refusing to stop, remove, kill or exec into any container this test did not make."""

    def __init__(self, path, allowed):
        super().__init__(path)
        self.allowed = allowed

    def __call__(self, args, timeout):
        args = list(args)
        if args[:1] in (['stop'], ['rm'], ['kill'], ['restart'], ['pause']) and args[-1] not in self.allowed:
            raise AssertionError(f'refused to touch a container this test did not create: {args!r}')
        if args[:1] not in (['ps'], ['container'], ['stop'], ['rm']) and args[:2] != ['machine', 'ssh']:
            raise AssertionError(f'refused a podman command the check has no use for: {args!r}')
        if args[:1] == ['container'] and args[1:2] != ['inspect']:
            raise AssertionError(f'refused a podman command the check has no use for: {args!r}')
        return super().__call__(args, timeout)


@unittest.skipIf(REAL_PODMAN is None, REAL_SKIP_REASON)
class RealPodmanTests(unittest.TestCase):
    def podman(self, *args, timeout=60):
        return subprocess.run([REAL_PODMAN, *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=SHORT_TMP, prefix='cocolo-test-')
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        os.chmod(self.root, 0o700)
        self.project = 'prj_' + secrets.token_hex(16)
        project = self.root / 'projects' / self.project
        project.mkdir(parents=True, mode=0o700)
        os.chmod(self.root / 'projects', 0o700)
        self.journal = LaunchJournal(project / 'launches')
        self.addCleanup(self.journal.release)
        self.config = container_launch.ContainerLaunchConfig(image=REAL_IMAGE, seccomp_profile='/nonexistent',
                                                             name_prefix='cocolo-test-', grace_seconds=1)
        self.runtime = cr.ContainerRuntime(self.project, name_prefix='cocolo-test-',
                                           runner=cr.SubprocessPodman(REAL_PODMAN))
        self.created = set()

    def container(self, token):
        identity = self.runtime.identity(token)
        self.assertTrue(identity.name.startswith('cocolo-test-'))
        self.created.add(identity.name)
        self.addCleanup(self.podman, 'rm', '--force', '--time=0', identity.name)
        labels = [f'--label={key}={value}' for key, value in identity.labels.items()]
        made = self.podman('run', '--detach', '--pull=never', f'--name={identity.name}', *labels, REAL_IMAGE,
                           'sleep', '600')
        self.assertEqual(0, made.returncode, made.stderr)
        self.created.add(made.stdout.strip())
        return identity

    def status(self, name):
        found = self.podman('container', 'inspect', name, '--format', '{{.State.Status}}')
        return found.stdout.strip() if found.returncode == 0 else 'absent'

    def test_real_an_orphan_cocolo_test_container_is_stopped_and_removed_and_a_live_one_is_kept(self):
        launcher = container_launch.ContainerLauncher(self.runtime, self.journal, self.config)
        live = self.container(write_intent(launcher))
        orphan = self.container('lnc_' + secrets.token_hex(16))
        self.assertEqual('running', self.status(live.name))
        self.assertEqual('running', self.status(orphan.name))
        check = hc.ContainerCheck(self.root, settings=lambda: self.config,
                                  runner=Guarded(REAL_PODMAN, self.created))
        findings = check.run(context(90.0))
        self.assertEqual('absent', self.status(orphan.name))
        self.assertEqual('running', self.status(live.name))
        mine = [item for item in findings if item.project_id == self.project]
        self.assertEqual([('orphan_removed', orphan.name, 'fixed')],
                         [(item.reason_code, item.subject, item.outcome) for item in mine])


if __name__ == '__main__':
    unittest.main()
