"""S8 HM-3: real temporary stores, no service sockets or provider calls; requirements first."""
from contextlib import closing
import sqlite3
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_result_leases as leases
from tests.test_project_history import PROJECT_A, PROJECT_B
from codex_agents import health_holds as holds
from codex_agents.health_monitor import CheckContext, HealthMonitor


class HoldsTests(unittest.TestCase):
    def setUp(self):
        self.base = leases.ResultLeaseTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.store = self.base.store
        self.path = self.base.fixture.directory / 'history.sqlite3'
        self.check = holds.HoldsCheck(None, sweep_interval_seconds=60)

    def context(self, now=1000):
        return CheckContext(time.monotonic() + 5, now, 60_000)

    def findings(self, now=1000):
        with closing(holds.open_reader(self.path)) as connection:
            return self.check.check_project(connection, PROJECT_A, self.context(now), leases.fixtures.KEY)

    def orphan(self, identity='private-source-path'):
        self.store._connection.execute(
            "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms) "
            "VALUES(?,'reservation',?,'held',1000)", (PROJECT_A, identity))

    def test_project_orphan_is_reported(self):
        self.orphan()
        before = self.store._connection.total_changes
        self.assertIn('hold_source_mismatch', [f.reason_code for f in self.findings()])
        self.assertEqual(before, self.store._connection.total_changes)
        self.assertEqual('held', self.store._connection.execute(
            "SELECT state FROM project_holds WHERE identity='private-source-path'").fetchone()[0])

    def test_account_orphan_is_reported(self):
        from tests.test_learning_store import LearningStoreTests
        fixture = LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.ledger._connection.execute(
            "INSERT INTO run_holds(kind,project_id,identity,state,taken_at_ms) "
            "VALUES('binding',?,'private-account-source','held',1000)", (fixture.project_a_id,))
        with closing(holds.open_reader(fixture.state.database)) as connection:
            found = self.check.check_account(connection, self.context())
        self.assertIn('hold_source_mismatch', [f.reason_code for f in found])
        self.assertTrue(all(f.project_id == fixture.project_a_id for f in found))

    def test_account_due_binding_is_reported_without_release(self):
        from tests.test_learning_store import LearningStoreTests
        fixture = LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.ledger._connection.execute(
            'INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,'
            'route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) '
            "VALUES(?,?,'due-run',?,?,?,?,?,0,1000,'pending')",
            (fixture.project_a_id, 'a' * 64, 'b' * 64, 'c' * 64, 'd' * 64, 'e' * 64, 'fixture'))
        for now, expected in ((61_000, False), (61_001, True)):
            with closing(holds.open_reader(fixture.state.database)) as connection:
                found = self.check.check_account(connection, self.context(now))
            self.assertEqual(expected, 'hold_overdue' in [f.reason_code for f in found])
        self.assertEqual('held', fixture.ledger._connection.execute(
            "SELECT state FROM run_holds WHERE identity=?", ('a' * 64,)).fetchone()[0])

    def test_missing_hold_is_reported(self):
        self.base.reserve()
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='lease'")
        self.assertIn('hold_source_mismatch', [f.reason_code for f in self.findings()])

    def test_overdue_boundary_and_backoff(self):
        self.base.reserve()
        self.store._connection.execute("UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 "
                                       "WHERE kind='lease'")
        for now, due in ((60_999, False), (61_000, False), (61_001, True)):
            with self.subTest(now=now):
                self.assertEqual(due, 'hold_overdue' in [f.reason_code for f in self.findings(now)])
        self.store._connection.execute("UPDATE project_holds SET next_check_ms=90_000 WHERE kind='lease'")
        self.assertNotIn('hold_overdue', [f.reason_code for f in self.findings(61_001)])

    def test_clock_floor(self):
        self.base.reserve()
        self.store._connection.execute("UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 "
                                       "WHERE kind='lease'")
        self.store._connection.execute("UPDATE result_identity SET last_observed_at_ms=100000")
        self.assertIn('hold_overdue', [f.reason_code for f in self.findings(1000)])

    def test_custody_drift(self):
        self.base.reserve()
        target = leases.lease_binding()
        self.store._connection.execute(
            "INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,lease_deadline_ms) "
            "VALUES(?,?,?,'legacy_no_context',0,61000)", (PROJECT_A, target.run_handle, target.attempt))
        self.store._connection.execute("DELETE FROM project_holds WHERE kind='result_custody'")
        self.assertIn('custody_mismatch', [f.reason_code for f in self.findings()])

    def test_healthy_store(self):
        self.base.reserve()
        self.assertEqual([], self.findings())

    def test_past_page_limit(self):
        self.base.reserve()
        template = dict(self.store._connection.execute('SELECT * FROM result_leases').fetchone())
        for number in range(holds.READ_PAGE_ROWS + 1):
            source = dict(template, run_handle='hrn_' + f'{number + 20:048x}',
                          claim_request_id=f'paged-claim-{number}')
            self.store._connection.execute(
                'INSERT INTO result_leases(' + ','.join(source) + ') VALUES('
                + ','.join('?' for _ in source) + ')', tuple(source.values()))
            self.store._connection.execute(
                "INSERT INTO project_holds(project_id,kind,identity,state,lease_case,taken_at_ms) "
                "VALUES(?,'lease',?,'held','unarmed',1000)", (PROJECT_A, source['run_handle'] + ':1'))
        for number in range(holds.READ_PAGE_ROWS + 3):
            self.store._connection.execute(
                "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,release_kind) "
                "VALUES(?,'reservation',?,'released',1000,1000,'completed')", (PROJECT_A, str(number)))
        self.orphan('last-private-row')
        seen = []
        for _ in range(5):
            seen.extend(self.findings())
        self.assertIn('hold_source_mismatch', [f.reason_code for f in seen])
        # Remove the synthetic source copies before exercising authenticated admission.
        original = leases.lease_binding().run_handle
        self.store._connection.execute('DELETE FROM result_leases WHERE run_handle<>?', (original,))
        self.base.reserve()
        self.assertEqual('reserved', self.store.result_lease(leases.lease_binding()).state)

    def test_unreadable_account_is_plain_machine_finding(self):
        state = mock.Mock()
        state.database = self.path.with_name('missing.sqlite3')
        check = holds.HoldsCheck(state, sweep_interval_seconds=60)
        found = check.run(self.context())
        self.assertEqual(['holds_unreadable'], [f.reason_code for f in found])
        self.assertEqual('machine', found[0].scope)
        self.assertFalse(state.database.exists())

    def test_publication_material_is_checked(self):
        self.base.reserve()
        self.store.arm_result_lease(leases.lease_binding())
        self.base.bind()
        self.store._connection.execute("UPDATE result_reservations SET prepared_digest=?", ('0' * 64,))
        self.store._connection.execute(
            'INSERT INTO project_result_publications VALUES(?,?,?)',
            (PROJECT_A, leases.binding().claim_id, b'PRIVATE-PUBLICATION'))
        self.assertIn('custody_mismatch', [f.reason_code for f in self.findings()])

    def test_account_projects_past_page_limit(self):
        from tests.test_learning_store import LearningStoreTests
        fixture = LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        connection = fixture.ledger._connection
        target = 'prj_' + 'f' * 32
        for number in range(holds.READ_PAGE_ROWS + 1):
            connection.execute("INSERT INTO projects(project_id,status,created_at_ms) VALUES(?,'active',1)",
                               ('prj_' + f'{number:032x}',))
        connection.execute("INSERT INTO projects(project_id,status,created_at_ms) VALUES(?,'active',1)", (target,))
        connection.execute(
            "INSERT INTO run_holds(kind,project_id,identity,state,taken_at_ms) "
            "VALUES('binding',?,'past-project-page','held',1000)", (target,))
        found = []
        for _ in range(3):
            with closing(holds.open_reader(fixture.state.database)) as reader:
                found.extend(self.check.check_account(reader, self.context()))
        self.assertTrue(any(f.project_id == target and f.reason_code == 'hold_source_mismatch' for f in found))
        runtime = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60)
        runtime_findings = []
        for _ in range(3):
            runtime_findings.extend(runtime.run(self.context()))
        self.assertTrue(any(f.project_id == target and f.reason_code == 'hold_source_mismatch'
                            for f in runtime_findings))

    def test_findings_survive_other_project_pass_and_say_when_stale(self):
        from tests.test_learning_store import LearningStoreTests
        fixture = LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        check = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60)
        clock = [100.0]
        check._monotonic = lambda: clock[0]
        first = min(fixture.project_a_id, fixture.project_b_id)
        drift = holds._finding(first, 'hold_source_mismatch', 'A held resource has no source.')
        with mock.patch.object(holds, 'READ_PAGE_ROWS', 1), \
                mock.patch.object(check, 'check_account', side_effect=lambda c, context, pid: [drift] if pid == first else []):
            self.assertEqual([drift], check.run(self.context()))
            clock[0] += 61
            later = check.run(self.context())
        self.assertEqual(1, len(later))
        self.assertEqual(first, later[0].project_id)
        self.assertIn('older than one sweep', later[0].text)
        with mock.patch.object(check, 'check_account', return_value=[]):
            for _ in range(3):
                latest = check.run(self.context())
        self.assertEqual([], latest)

    def contexts(self):
        for number in range(holds.READ_PAGE_ROWS + 1):
            run = 'hrn_' + f'{number + 100:048x}'
            self.store._connection.execute(
                "INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,"
                "context_digest,host_class,lease_deadline_ms) VALUES(?,?,1,'retiring',4,?,'host',61000)",
                (PROJECT_A, run, 'a' * 64))
            self.store._connection.execute(
                "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms) "
                "VALUES(?,'result_custody',?,'held',1000)", (PROJECT_A, run + ':1'))

    def test_context_byte_budget_across_pages(self):
        from codex_agents import project_result_contexts
        self.contexts()
        with mock.patch.object(project_result_contexts, 'MAX_LIVE_CONTEXT_BYTES', holds.READ_PAGE_ROWS * 4):
            self.assertEqual([], self.findings())
            self.assertIn('custody_mismatch', [f.reason_code for f in self.findings()])

    def test_context_byte_budget_does_not_combine_changed_snapshots(self):
        from codex_agents import project_result_contexts
        self.contexts()
        with mock.patch.object(project_result_contexts, 'MAX_LIVE_CONTEXT_BYTES', holds.READ_PAGE_ROWS * 4):
            self.assertEqual([], self.findings())
            self.store._connection.execute('UPDATE project_result_contexts SET context_size=1')
            self.assertEqual([], self.findings())

    def test_past_legacy_evidence_limit(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.executescript(
                'CREATE TABLE active_run_capacity(project_id,run_id,bridge_run_id);'
                'CREATE TABLE records(project_id,record_type,source,content,content_digest);'
                'CREATE INDEX records_run ON records(project_id,CASE WHEN json_valid(CAST(content AS TEXT)) '
                "THEN json_extract(CAST(content AS TEXT),'$.run_id') END,record_type);"
                "INSERT INTO active_run_capacity VALUES('project','legacy-run',NULL);")
            connection.create_function('legacy_dispatch_deadline', 4, lambda *args: 1000)
            connection.create_function('legacy_dispatch_run_id', 1, lambda raw: 'legacy-run')
            for _ in range(3):
                connection.execute("INSERT INTO records VALUES('project','dispatch_intent','lifecycle',?,?)",
                                   (b'{"run_id":"legacy-run"}', 'a' * 64))
            sql = 'SELECT ' + holds._legacy_deadline() + ' FROM active_run_capacity a'
            self.assertIsNone(connection.execute(sql).fetchone()[0])
            plan = [row[3] for row in connection.execute('EXPLAIN QUERY PLAN ' + sql)]
            self.assertTrue(any('SEARCH records USING INDEX records_run' in step for step in plan), plan)
            connection.execute('DELETE FROM records WHERE rowid>1')
            self.assertEqual(1000, connection.execute(sql).fetchone()[0])

    def test_query_plans_and_read_only(self):
        self.base.reserve()
        with closing(holds.open_reader(self.path)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM project_holds")
            page = holds.ValidatorPages(connection, {}, self.context(), PROJECT_A)
            holds.validate_project(page, leases.fixtures.KEY)
            self.assertTrue(page.plans)
            for plan in page.plans:
                self.assertFalse(any('SCAN ' in row[3] and 'SCAN CONSTANT' not in row[3]
                                     for row in plan), plan)

    def test_deadline(self):
        self.orphan()
        with closing(holds.open_reader(self.path)) as connection:
            self.assertEqual([], self.check.check_project(
                connection, PROJECT_A, CheckContext(time.monotonic() - 1, 1000, 0), leases.fixtures.KEY))

    def test_scoping_and_private_text(self):
        self.orphan('PRIVATE-CANARY')
        found = self.findings()
        self.assertTrue(found)
        self.assertTrue(all(f.visible_to(PROJECT_A) and not f.visible_to(PROJECT_B) for f in found))
        self.assertNotIn('PRIVATE-CANARY', str(found))
        self.assertTrue(all(f.outcome == 'reported' for f in found))

    def test_registration(self):
        monitor = HealthMonitor.with_builtin_checks(sweep_interval_seconds=60, checks=(self.check,))
        self.assertIn('holds', [c.name for c in monitor._checks])

    def test_faulted_service_reported_then_stopped(self):
        events = []
        host = mock.Mock()
        host.faulted.return_value = True
        host.close.side_effect = lambda: events.append('stop')
        found = holds.stop_faulted_service(host, PROJECT_A, report=lambda f: events.append(f))
        self.assertEqual('service_writer_fault', found.reason_code)
        self.assertEqual([found, 'stop'], events)
        host.settle_lost_runs.assert_not_called()

    def test_service_stop_failure(self):
        host = mock.Mock()
        host.faulted.return_value = True
        host.close.side_effect = RuntimeError('PRIVATE-STOP-PATH')
        reports = []
        found = holds.stop_faulted_service(host, PROJECT_A, report=reports.append)
        self.assertEqual('service_stop_failed', found.reason_code)
        self.assertEqual(['service_writer_fault', 'service_stop_failed'], [f.reason_code for f in reports])
        self.assertNotIn('PRIVATE', str(reports))

    def test_healthy_service(self):
        host = mock.Mock()
        host.faulted.return_value = False
        self.assertIsNone(holds.stop_faulted_service(host, PROJECT_A))
        host.close.assert_not_called()


# Declarative test metadata; no persistence access at import.
HoldsTests.test_account_orphan_is_reported.__requirements__ = ('S8-HM3-1',)
HoldsTests.test_project_orphan_is_reported.__requirements__ = ('S8-HM3-1',)
HoldsTests.test_missing_hold_is_reported.__requirements__ = ('S8-HM3-1',)
HoldsTests.test_overdue_boundary_and_backoff.__requirements__ = ('S8-HM3-2',)
HoldsTests.test_clock_floor.__requirements__ = ('S8-HM3-2',)
HoldsTests.test_faulted_service_reported_then_stopped.__requirements__ = ('S8-HM3-3',)
HoldsTests.test_service_stop_failure.__requirements__ = ('S8-HM3-3',)
HoldsTests.test_healthy_service.__requirements__ = ('S8-HM3-3',)
HoldsTests.test_custody_drift.__requirements__ = ('S8-HM3-4',)
HoldsTests.test_healthy_store.__requirements__ = ('S8-HM3-4',)
HoldsTests.test_past_page_limit.__requirements__ = ('S8-HM3-5',)
HoldsTests.test_query_plans_and_read_only.__requirements__ = ('S8-HM3-5',)
HoldsTests.test_deadline.__requirements__ = ('S8-HM3-5',)
HoldsTests.test_scoping_and_private_text.__requirements__ = ('S8-HM3-6',)
HoldsTests.test_registration.__requirements__ = ('S8-HM3-6',)
HoldsTests.test_unreadable_account_is_plain_machine_finding.__requirements__ = ('S8-HM3-5', 'S8-HM3-6')
HoldsTests.test_publication_material_is_checked.__requirements__ = ('S8-HM3-4',)
HoldsTests.test_account_projects_past_page_limit.__requirements__ = ('S8-HM3-5',)
HoldsTests.test_findings_survive_other_project_pass_and_say_when_stale.__requirements__ = ('S8-HM3-6',)
HoldsTests.test_context_byte_budget_across_pages.__requirements__ = ('S8-HM3-4', 'S8-HM3-5')
HoldsTests.test_context_byte_budget_does_not_combine_changed_snapshots.__requirements__ = ('S8-HM3-4',)
HoldsTests.test_account_due_binding_is_reported_without_release.__requirements__ = ('S8-HM3-2',)
HoldsTests.test_past_legacy_evidence_limit.__requirements__ = ('S8-HM3-5',)
HoldsTests.__why_not_scenario__ = 'Scenario: real temporary stores and the account fixture owner-key helper; no live services or provider calls.'

if __name__ == '__main__':
    unittest.main()
