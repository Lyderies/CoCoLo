"""HM-0: the health monitor's pass runner, finding record and status line (PLAN-0.4.0-supervisor-slices.md,
"Health monitor: one check per slice").

Requirements (owner, 2026-11-21: a health monitor as code, no model use):
- HM-0-1 One pass is bounded in time and in checks, so it can never take the sweep thread for long.
- HM-0-2 A check that raises is reported (its own finding, the exception's type name only) and never stops the
  pass or the sweep.
- HM-0-3 A check that runs over its time share is cut and reported; it is never started twice at once.
- HM-0-4 A finding is shown only to its own project (plus machine findings); another project's status never
  shows it.
- HM-0-5 A healthy state yields no findings.
- HM-0-6 The pass never holds the ledger lock (the ledger writer's lock).
- HM-0-7 A finding is a closed record that carries no content: only the check, scope, a name or digest, whether
  it was fixed or reported, a reason code and a plain sentence.
- HM-0-8 What the owner must see (a reported finding, a failed fix) is in the status line and in
  account_health.attention; a fixed one is shown but is not an alert.
"""
import json
import threading
import time
import unittest
from unittest import mock

from tests import test_cli_status_client_boundary as status_tests
from tests import test_learning_store as store_tests

from codex_agents import health_monitor as hm
from codex_agents import learning_store
from codex_agents import health_workspaces

PROJECT_A = 'prj_' + 'a' * 32
PROJECT_B = 'prj_' + 'b' * 32


class Fn(hm.Check):
    """A check from a function of its context; the function may return findings or raise."""

    def __init__(self, name, function=None):
        self.name = name
        self.function = function or (lambda context: [])
        self.calls = 0

    def run(self, context):
        self.calls += 1
        return self.function(context)


def finding(check='fake_check', *, scope='machine', project_id=None, subject='thing_1', outcome='reported',
            reason_code='thing_drifted', text='The thing drifted and could not be fixed.'):
    return hm.Finding(check=check, scope=scope, project_id=project_id, subject=subject, outcome=outcome,
                      reason_code=reason_code, text=text)


def monitor(*checks, **limits):
    limits = {'pass_budget_seconds': 5.0, 'check_share_seconds': 1.0, 'max_checks': 16} | limits
    return hm.HealthMonitor(checks, **limits)


class PassBoundsTests(unittest.TestCase):
    def test_HM_0_1_one_pass_stays_within_its_time_budget_with_many_checks(self):
        order = []
        checks = [Fn(f'slow_check_{n:02d}', lambda context, n=n: order.append(n) or time.sleep(0.1) or [])
                  for n in range(40)]
        runner = monitor(*checks, pass_budget_seconds=0.6, check_share_seconds=0.3, max_checks=64)
        started = time.monotonic()
        runner.run_pass()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)  # 40 checks of 0.1 s would take 4 s
        self.assertTrue(2 <= len(order) <= 6, order)
        view = runner.view(None)
        spent = [f for f in view['findings'] if f['reason_code'] == 'pass_budget_spent']
        self.assertEqual(1, len(spent), view)  # one finding for all the checks not run, with their number
        self.assertEqual(('health_monitor', 'machine', 'reported'),
                         (spent[0]['check'], spent[0]['scope'], spent[0]['outcome']))
        self.assertIn(str(40 - len(order)), spent[0]['text'])
        # the checks left out come first at the next pass (nothing starves)
        first_pass = list(order)
        order.clear()
        runner.run_pass()
        self.assertEqual(first_pass[-1] + 1, order[0])

    def test_HM_0_1_one_pass_runs_at_most_max_checks_and_every_check_gets_its_turn(self):
        checks = [Fn(f'quick_check_{n:02d}') for n in range(12)]
        runner = monitor(*checks, max_checks=5)
        runner.run_pass()
        self.assertEqual(5, sum(check.calls for check in checks))
        limited = [f for f in runner.view(None)['findings'] if f['reason_code'] == 'pass_check_limit']
        self.assertEqual(1, len(limited))
        self.assertIn('7', limited[0]['text'])
        runner.run_pass()
        runner.run_pass()
        self.assertTrue(all(check.calls >= 1 for check in checks), [check.calls for check in checks])
        self.assertLessEqual(max(check.calls for check in checks), 2)

    def test_HM_0_1_registering_more_checks_than_the_pass_limit_is_allowed_and_a_duplicate_name_is_refused(self):
        runner = monitor(Fn('first_check'))
        with self.assertRaises(ValueError):
            runner.register(Fn('first_check'))
        with self.assertRaises(ValueError):
            runner.register(Fn('Not A Name'))

    def test_HM_0_1_a_check_gets_the_pass_deadline_so_it_can_stop_itself(self):
        seen = []
        runner = monitor(Fn('looks_at_time', lambda context: seen.append(context.deadline - time.monotonic()) or []),
                         check_share_seconds=0.5)
        runner.run_pass()
        self.assertTrue(0 < seen[0] <= 0.5, seen)


class FailureTests(unittest.TestCase):
    def test_HM_0_2_a_check_that_raises_is_reported_by_type_name_only_and_the_pass_goes_on(self):
        def boom(context):
            raise ValueError('secret-content private-detail')
        after = Fn('after_the_failure')
        runner = monitor(Fn('first_ok'), Fn('breaks', boom), after)
        runner.run_pass()
        self.assertEqual(1, after.calls)
        view = runner.view(None)
        raised = [f for f in view['findings'] if f['check'] == 'breaks']
        self.assertEqual(1, len(raised), view)
        self.assertEqual(('check_raised', 'reported', 'ValueError'),
                         (raised[0]['reason_code'], raised[0]['outcome'], raised[0]['subject']))
        self.assertIn('ValueError', raised[0]['text'])
        self.assertNotIn('secret-content', json.dumps(view))
        self.assertNotIn('private-detail', json.dumps(view))
        states = {c['check']: c['state'] for c in view['checks']}
        self.assertEqual({'first_ok': 'ok', 'breaks': 'raised', 'after_the_failure': 'ok'}, states)
        runner.run_pass()  # it runs again at the next pass
        self.assertEqual(2, after.calls)

    def test_HM_0_2_a_check_that_returns_something_invalid_is_reported_not_trusted(self):
        for bad in ('text', [object()], [{'check': 'x'}], None, 5):
            with self.subTest(bad=bad):
                runner = monitor(Fn('returns_junk', lambda context, bad=bad: bad))
                runner.run_pass()
                view = runner.view(None)
                self.assertEqual(['check_invalid_result'], [f['reason_code'] for f in view['findings']], view)

    def test_HM_0_2_a_finding_that_names_another_check_is_refused(self):
        runner = monitor(Fn('honest_check', lambda context: [finding('someone_else')]))
        runner.run_pass()
        self.assertEqual(['check_invalid_result'], [f['reason_code'] for f in runner.view(None)['findings']])

    def test_HM_0_2_too_many_findings_from_one_check_are_cut_and_said_so(self):
        many = [finding('chatty_check', subject=f'thing_{n}') for n in range(hm.MAX_FINDINGS_PER_CHECK + 9)]
        runner = monitor(Fn('chatty_check', lambda context: many))
        runner.run_pass()
        findings = runner.view(None)['findings']
        self.assertEqual(hm.MAX_FINDINGS_PER_CHECK + 1, len(findings))
        self.assertIn('findings_truncated', [f['reason_code'] for f in findings])

    def test_HM_0_3_a_check_over_its_time_share_is_cut_reported_and_never_run_twice_at_once(self):
        release = threading.Event()
        self.addCleanup(release.set)
        started = []

        def stuck(context):
            started.append(1)
            release.wait(10)
            return []
        later = Fn('runs_after_the_stuck_one')
        runner = monitor(Fn('stuck_check', stuck), later, check_share_seconds=0.2, pass_budget_seconds=3)
        began = time.monotonic()
        runner.run_pass()
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertEqual(1, later.calls)  # the pass went on
        view = runner.view(None)
        cut = [f for f in view['findings'] if f['check'] == 'stuck_check']
        self.assertEqual(['check_timeout'], [f['reason_code'] for f in cut], view)
        self.assertEqual({'stuck_check': 'timed_out', 'runs_after_the_stuck_one': 'ok'},
                         {c['check']: c['state'] for c in view['checks']})
        runner.run_pass()  # still running from the last pass: not started again
        self.assertEqual(1, len(started))
        view = runner.view(None)
        self.assertEqual(['check_still_running'], [f['reason_code'] for f in view['findings']
                                                    if f['check'] == 'stuck_check'])
        release.set()
        for _ in range(100):  # the cut run ends; a later pass finds the check free and it runs again
            runner.run_pass()
            if not [f for f in runner.view(None)['findings'] if f['check'] == 'stuck_check']:
                break
            time.sleep(0.05)
        self.assertEqual([], [f for f in runner.view(None)['findings'] if f['check'] == 'stuck_check'])

    def test_HM_0_3_a_stop_ends_the_pass_between_checks(self):
        stop = threading.Event()
        first = Fn('stops_the_pass', lambda context: stop.set() or [])
        second = Fn('never_started')
        runner = monitor(first, second)
        runner.run_pass(should_stop=stop.is_set)
        self.assertEqual((1, 0), (first.calls, second.calls))


class RecordTests(unittest.TestCase):
    def test_HM_0_7_a_finding_is_a_closed_record_with_no_content(self):
        good = finding()
        self.assertEqual({'check', 'scope', 'subject', 'outcome', 'reason_code', 'text'}, set(good.shown()))
        for changes in ({'check': 'Bad Name'}, {'scope': 'global'}, {'outcome': 'ignored'},
                        {'subject': 'a path/with/slashes'}, {'subject': 'has space'}, {'subject': 'x' * 129},
                        {'reason_code': 'Not-A-Code'}, {'text': ''}, {'text': 'two\nlines'}, {'text': 'x' * 241},
                        {'scope': 'project'},  # a project finding needs its project
                        {'project_id': PROJECT_A},  # a machine finding has none
                        {'scope': 'project', 'project_id': 'not-a-project'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                finding(**changes)
        hm_project = finding(scope='project', project_id=PROJECT_A)
        self.assertNotIn('project_id', hm_project.shown())  # the shown form never names a project

    def test_HM_0_7_a_finding_survives_no_extra_fields(self):
        with self.assertRaises(TypeError):
            hm.Finding(check='a_check', scope='machine', project_id=None, subject='x', outcome='fixed',
                       reason_code='fixed_it', text='Fixed.', detail='extra')


class ScopeTests(unittest.TestCase):
    def test_HM_0_4_a_project_finding_is_shown_only_to_its_project_and_machine_findings_to_all(self):
        runner = monitor(Fn('scoped_check', lambda context: [
            finding('scoped_check', scope='project', project_id=PROJECT_A, subject='mine_a', reason_code='a_drift',
                    text='Project A has drift.'),
            finding('scoped_check', scope='project', project_id=PROJECT_B, subject='mine_b', reason_code='b_drift',
                    text='Project B has drift.'),
            finding('scoped_check', scope='machine', subject='machine_1', reason_code='machine_drift',
                    text='The machine has drift.')]))
        runner.run_pass()
        shown = {name: runner.view(name) for name in (PROJECT_A, PROJECT_B, None, 'prj_' + 'c' * 32)}
        self.assertEqual({'a_drift', 'machine_drift'}, {f['reason_code'] for f in shown[PROJECT_A]['findings']})
        self.assertEqual({'b_drift', 'machine_drift'}, {f['reason_code'] for f in shown[PROJECT_B]['findings']})
        self.assertEqual({'machine_drift'}, {f['reason_code'] for f in shown[None]['findings']})
        self.assertEqual({'machine_drift'}, {f['reason_code'] for f in shown['prj_' + 'c' * 32]['findings']})
        for name, other in ((PROJECT_A, 'mine_b'), (PROJECT_B, 'mine_a'), (None, 'mine_a'), (None, 'mine_b')):
            self.assertNotIn(other, json.dumps(shown[name]))
        # the counts a project sees are its own too
        self.assertEqual([2], [c['findings'] for c in shown[PROJECT_A]['checks']])
        self.assertEqual([1], [c['findings'] for c in shown[None]['checks']])

    def test_HM_0_4_the_collectors_health_answers_each_project_only_its_own_findings(self):
        store = store_tests.LearningStoreTests(methodName='runTest')
        store.setUp()
        self.addCleanup(store.tearDown)
        self.enterContext(mock.patch.object(health_workspaces.WorkspaceCheck, '_container_path', lambda check: None))  # not the machine's real scratch folder
        server = learning_store.CollectorServer(store.ledger)
        server.start()
        self.addCleanup(server.stop)
        server.health_monitor.register(Fn('scoped_check', lambda context: [
            finding('scoped_check', scope='project', project_id=store.project_a_id, subject='only_a',
                    reason_code='a_drift', text='Project A has drift.')]))
        server._run_health_pass()
        from codex_agents.collector_client import CollectorClient
        client = CollectorClient(store.state.socket, timeout=2)
        seen_a = client.health(store.project_a)['health_monitor']
        seen_b = client.health(store.project_b)['health_monitor']
        seen_none = client.health()['health_monitor']
        self.assertEqual(['a_drift'], [f['reason_code'] for f in seen_a['findings']])
        self.assertEqual([], seen_b['findings'])
        self.assertEqual([], seen_none['findings'])
        self.assertNotIn('only_a', json.dumps([seen_b, seen_none]))


class HealthyTests(unittest.TestCase):
    def test_HM_0_5_a_healthy_state_yields_no_findings_and_says_so(self):
        runner = monitor(Fn('quiet_check'), Fn('other_quiet_check'))
        self.assertIsNone(runner.view(None)['last_pass_at_ms'])  # before the first pass
        runner.run_pass(now_ms=1_700_000_000_000)
        for project in (None, PROJECT_A):
            view = runner.view(project)
            self.assertEqual([], view['findings'])
            self.assertEqual(1_700_000_000_000, view['last_pass_at_ms'])
            self.assertEqual({'quiet_check': 'ok', 'other_quiet_check': 'ok'},
                             {c['check']: c['state'] for c in view['checks']})
            self.assertEqual([0, 0], [c['findings'] for c in view['checks']])
            self.assertIn('no findings', view['summary'])

    def test_HM_0_5_the_built_in_self_check_is_quiet_on_a_healthy_collector_and_reports_a_late_pass(self):
        runner = hm.HealthMonitor.with_builtin_checks(sweep_interval_seconds=60.0)
        runner.run_pass(now_ms=1_000_000, mono_ms=1_000_000)
        runner.run_pass(now_ms=1_000_000 + 60_000, mono_ms=1_000_000 + 60_000)
        self.assertEqual([], runner.view(None)['findings'])
        self.assertEqual(['self_check'], [c['check'] for c in runner.view(None)['checks']])
        # a machine that slept (wall clock far ahead, the monotonic clock not) is not a late pass
        runner.run_pass(now_ms=1_000_000 + 60_000 + 9_000_000, mono_ms=1_000_000 + 120_000)
        self.assertEqual([], runner.view(None)['findings'])
        # the sweep thread was away for five intervals
        runner.run_pass(now_ms=1_000_000 + 60_000 + 9_000_000 + 300_000, mono_ms=1_000_000 + 120_000 + 300_000)
        late = runner.view(None)['findings']
        self.assertEqual(['pass_late'], [f['reason_code'] for f in late])
        self.assertEqual('reported', late[0]['outcome'])

    def test_HM_0_5_a_fixed_finding_is_shown_as_fixed_and_a_later_healthy_pass_does_not_report_it_again(self):
        once = [True]

        def fixes_once(context):
            if once:
                once.clear()
                return [finding('fixing_check', outcome='fixed', reason_code='thing_removed',
                                text='A leftover thing was removed.')]
            return []
        runner = monitor(Fn('fixing_check', fixes_once))
        runner.run_pass()
        self.assertEqual(['fixed'], [f['outcome'] for f in runner.view(None)['findings']])
        runner.run_pass()
        self.assertEqual(['fixed'], [f['outcome'] for f in runner.view(None)['findings']])  # kept in view
        self.assertEqual(1, len(runner.view(None)['findings']))  # but once, not once per pass


def collector_with_loop(test, ledger, *, interval=0.15):
    """The collector's real sweep loop on its own thread, its sweeps counted instead of run."""
    server = learning_store.CollectorServer(ledger)
    cycles = []
    test.enterContext(mock.patch.object(learning_store, 'WORKER_RESULT_SWEEP_SECONDS', interval))
    test.enterContext(mock.patch.object(ledger, 'sweep_worker_results',
                                        side_effect=lambda: cycles.append(time.monotonic())))
    for name in ('sweep_event_retention', 'sweep_account_retention'):
        test.enterContext(mock.patch.object(ledger, name, return_value=0))
    test.enterContext(mock.patch.object(ledger, 'release_due_account_holds', return_value=0))
    thread = threading.Thread(target=server._sweep_loop, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 5)
    test.addCleanup(server._sweeps_stop.set)
    return server, cycles


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(health_workspaces.WorkspaceCheck, '_container_path', lambda check: None))  # not the machine's real scratch folder
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger

    def test_HM_0_6_the_pass_runs_on_the_sweep_thread_and_never_holds_the_ledger_lock(self):
        server, cycles = collector_with_loop(self, self.ledger)
        probes, ran_on = [], []

        def probe(context):
            ran_on.append(threading.current_thread().name)
            got = []

            def try_lock():  # another thread, as the ledger's other users are
                got.append(self.ledger._lock.acquire(blocking=False))
                if got[0]:
                    self.ledger._lock.release()
            thread = threading.Thread(target=try_lock)
            thread.start()
            thread.join(2)
            probes.append(got[0])
            return []
        server.health_monitor.register(Fn('lock_probe', probe))
        self.assertTrue(wait_for(lambda: len(probes) >= 2), probes)
        self.assertEqual([True] * len(probes), probes)  # the lock was free at every pass
        self.assertTrue(wait_for(lambda: len(cycles) >= 2))
        self.assertLessEqual(len(probes), len(cycles) + 1)  # one pass per full sweep cycle, not more

    def test_HM_0_6_a_pass_waits_for_no_ledger_lock_even_while_another_thread_holds_it(self):
        server, cycles = collector_with_loop(self, self.ledger)
        ran = []
        server.health_monitor.register(Fn('lock_free_check', lambda context: ran.append(1) or []))
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(10)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        before = len(ran)
        # the sweeps skip a held ledger (busy); the pass needs no lock, so it still runs on its interval
        self.assertTrue(wait_for(lambda: len(ran) >= before + 2), 'no pass while the ledger lock was held')
        release.set()

    def test_HM_0_2_a_check_that_raises_never_stops_the_pass_or_the_sweep(self):
        server, cycles = collector_with_loop(self, self.ledger)
        counter = Fn('counts_passes')
        server.health_monitor.register(Fn('always_breaks', lambda context: 1 / 0))
        server.health_monitor.register(counter)
        self.assertTrue(wait_for(lambda: counter.calls >= 3 and len(cycles) >= 3), (counter.calls, len(cycles)))
        self.assertIsNone(server.sweep_error)  # the check's failure is a finding, not a sweep error
        broken = [f for f in server.health_monitor.view(None)['findings'] if f['check'] == 'always_breaks']
        self.assertEqual(['check_raised'], [f['reason_code'] for f in broken])

    def test_HM_0_2_a_failing_pass_runner_is_recorded_by_type_and_the_next_sweep_still_runs(self):
        server, cycles = collector_with_loop(self, self.ledger)
        with mock.patch.object(server.health_monitor, 'run_pass', side_effect=RuntimeError('boom')) as runs:
            self.assertTrue(wait_for(lambda: runs.call_count >= 2 and len(cycles) >= 3))
        self.assertEqual('RuntimeError', server.sweep_error)

    def test_HM_0_1_the_pass_is_given_the_sweeps_stop_so_a_stop_is_not_delayed(self):
        server = learning_store.CollectorServer(self.ledger)
        second = Fn('never_started_after_stop')
        server.health_monitor.register(Fn('stops_it', lambda context: server._sweeps_stop.set() or []))
        server.health_monitor.register(second)
        server._run_health_pass()
        self.assertEqual(0, second.calls)

    def test_HM_0_5_a_collector_with_only_its_own_checks_is_healthy(self):
        # HM-1 adds the containers check to the collector (test_health_containers HM-1-9); with containers off
        # as shipped it is quiet, so the collector's own checks give a healthy, finding-free view.
        server = learning_store.CollectorServer(self.ledger)
        server._run_health_pass()
        view = server.health_monitor.view(self.store.project_a_id)
        self.assertEqual([], view['findings'])
        self.assertEqual(['self_check', 'containers', 'workspaces', 'resources'], [c['check'] for c in view['checks']])


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(health_workspaces.WorkspaceCheck, '_container_path', lambda check: None))  # not the machine's real scratch folder
        self.harness = status_tests.StatusCliClientBoundaryTests(methodName='runTest')
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.monitor = self.harness.collector.health_monitor
        clean = {name: self.harness.lane() for name in ('signature', 'claims', 'observations', 'ingress', 'billing',
                                                        'probe_worker')}
        self.harness.collector.account_lanes = lambda: clean  # the account's own health is ok

    def status(self):
        code, stdout, stderr = self.harness.command()
        self.assertIn(code, (0, 1), stdout + stderr)
        return json.loads(stdout)

    def test_HM_0_5_a_healthy_status_shows_the_pass_and_no_findings_and_is_ok(self):
        self.harness.collector._run_health_pass()
        report = self.status()
        health = report['health_monitor']
        self.assertEqual([], health['findings'])
        self.assertIsInstance(health['last_pass_at_ms'], int)
        self.assertEqual('ok', report['account_health']['state'])
        self.assertEqual([], report['account_health']['attention'])
        self.assertTrue(report['ready'])

    def test_HM_0_8_a_reported_finding_is_in_the_status_line_and_is_an_owner_alert(self):
        self.monitor.register(Fn('thing_check', lambda context: [
            finding('thing_check', scope='project', project_id=self.harness.project_id, subject='thing_7',
                    outcome='reported', reason_code='thing_stuck', text='Thing 7 is stuck and was not touched.'),
            finding('thing_check', outcome='fixed', reason_code='thing_removed',
                    text='A leftover thing was removed.')]))
        self.harness.collector._run_health_pass()
        report = self.status()
        health = report['health_monitor']
        by_outcome = {f['outcome']: f for f in health['findings']}
        self.assertEqual('thing_stuck', by_outcome['reported']['reason_code'])
        self.assertEqual('Thing 7 is stuck and was not touched.', by_outcome['reported']['text'])
        self.assertEqual('thing_7', by_outcome['reported']['subject'])
        self.assertEqual('fixed', by_outcome['fixed']['outcome'])
        account = report['account_health']
        self.assertEqual('degraded', account['state'])
        self.assertTrue(any('Thing 7 is stuck' in line for line in account['attention']), account)
        self.assertFalse(any('leftover thing' in line for line in account['attention']), account)  # fixed: no alert
        self.assertTrue(report['ready'])  # health findings never change ready

    def test_HM_0_8_a_failed_check_is_an_owner_alert_too(self):
        self.monitor.register(Fn('breaks', lambda context: 1 / 0))
        self.harness.collector._run_health_pass()
        account = self.status()['account_health']
        self.assertEqual('degraded', account['state'])
        self.assertTrue(any('ZeroDivisionError' in line for line in account['attention']), account)

    def test_HM_0_4_another_projects_findings_are_not_in_this_projects_status(self):
        self.monitor.register(Fn('thing_check', lambda context: [
            finding('thing_check', scope='project', project_id='prj_' + 'f' * 32, subject='their_thing',
                    reason_code='their_drift', text='Their thing drifted.')]))
        self.harness.collector._run_health_pass()
        report = self.status()
        self.assertEqual([], report['health_monitor']['findings'])
        self.assertNotIn('their_thing', json.dumps(report))
        self.assertEqual('ok', report['account_health']['state'])

    def test_HM_0_8_a_malformed_health_reading_never_costs_the_status_its_ready(self):
        self.harness.collector._run_health_pass()
        for bad in ({'findings': 'x'}, 7, {'last_pass_at_ms': 'soon', 'findings': [], 'checks': [], 'summary': 's',
                                           'pass_ms': 1, 'more': 0}):
            with self.subTest(bad=bad), mock.patch.object(self.monitor, 'view', return_value=bad):
                report = self.status()
                self.assertTrue(report['ready'])
                self.assertEqual({'state': 'unknown', 'reason_code': 'health_monitor_invalid'},
                                 report['health_monitor'])

    def test_HM_0_8_an_older_collector_without_the_section_leaves_it_unavailable(self):
        with mock.patch.object(self.monitor, 'view', side_effect=RuntimeError):
            report = self.status()
        self.assertTrue(report['ready'])
        self.assertIsNone(report['health_monitor'])


if __name__ == '__main__':
    unittest.main()
