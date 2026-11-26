"""S8 HM-3: real temporary stores, no service sockets or provider calls; requirements first."""
from contextlib import closing
import sqlite3
import time
import threading
from pathlib import Path
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

    def account_fixture(self):
        from tests.test_learning_store import LearningStoreTests
        from codex_agents.ledger_owner import LedgerOwner
        fixture = LearningStoreTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        owner = LedgerOwner(ledger=fixture.ledger)
        return fixture, owner

    def due_binding(self, fixture, identity, *, due=1000, project=None):
        fixture.ledger._connection.execute(
            'INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,'
            'route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) '
            "VALUES(?,?,?,?,?,?,?,?,0,?,'pending')",
            (project or fixture.project_a_id, identity, 'run-' + identity, 'b' * 64, 'c' * 64, 'd' * 64, 'e' * 64, 'fixture', due))

    def test_project_overdue_stays_present_past_live_page(self):
        self.base.reserve()
        self.store._connection.execute("UPDATE project_holds SET due_at_ms=1000,next_check_ms=1000 WHERE kind='lease'")
        for number in range(holds.READ_PAGE_ROWS + 5):
            self.orphan(f'other-{number}')
        for turn in range(5):
            with self.subTest(turn=turn):
                self.assertIn('hold_overdue', [f.reason_code for f in self.findings(61_001)])

    def test_account_overdue_stays_present_and_project_scoped_past_live_page(self):
        fixture, owner = self.account_fixture()
        self.due_binding(fixture, 'a' * 64)
        for number in range(holds.READ_PAGE_ROWS + 5):
            self.due_binding(fixture, f'{number + 1:064x}', due=900_000, project=fixture.project_b_id)
        for turn in range(5):
            found = self.check.check_account(owner.ledger._connection, self.context(61_001))
            self.assertEqual([fixture.project_a_id], [f.project_id for f in found if f.reason_code == 'hold_overdue'], turn)

    def test_account_overdue_uses_saved_project_clock_floor_without_advancing(self):
        fixture, owner = self.account_fixture()
        self.due_binding(fixture, 'a' * 64)
        directory = fixture.state.root / 'projects' / fixture.project_a_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.parent.chmod(0o700)
        directory.chmod(0o700)
        # A real project history at its registered identity, with a persisted clock ahead of wall time.
        from codex_agents.project_history import ProjectHistory
        with closing(ProjectHistory(directory, fixture.project_a_id, result_key=fixture.state.digest_key(),
                                    result_capacity=self.store._result_capacity)) as project:
            project._connection.execute('UPDATE result_identity SET last_observed_at_ms=100000')
        check = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60)
        found = check.run(self.context(1000))
        self.assertIn('hold_overdue', [f.reason_code for f in found if f.project_id == fixture.project_a_id])
        with closing(holds.open_reader(directory / 'history.sqlite3')) as project:
            self.assertEqual(100000, project.execute('SELECT last_observed_at_ms FROM result_identity').fetchone()[0])

    def test_runtime_account_sql_runs_only_on_owner_without_opening_reader(self):
        from tests.test_ledger_owner_wiring import LedgerOwnerWiringTests
        fixture = LedgerOwnerWiringTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.start_owner()
        check = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60)
        fixture.sql.clear()
        original = holds.open_reader
        def project_reader(path):
            self.assertNotEqual(fixture.state.database, Path(path), 'check opened an independent ledger reader')
            return original(path)
        with mock.patch.object(holds, 'open_reader', side_effect=project_reader):
            self.assertEqual([], check.run(self.context()))
        self.assertTrue(fixture.sql)
        self.assertTrue(all(thread is fixture.thread for thread, sql in fixture.sql))
        self.assertFalse(fixture.owner.ledger._connection.in_transaction)
        self.assertEqual({}, fixture.owner.queue.snapshot()['refusals'])

    def test_durable_service_status_reaches_monitor_and_clears_on_recovery(self):
        fixture, owner = self.account_fixture()
        directory = fixture.state.root / 'projects' / fixture.project_a_id
        directory.mkdir(parents=True, mode=0o700)
        # The service writes only a fixed status code; a fresh check must pick it up.
        for reason in ('service_writer_fault', 'service_stop_failed'):
            finding = holds._finding(fixture.project_a_id, reason, holds._SERVICE_TEXT[reason], 'service')
            holds.save_service_finding(directory, finding)
            check = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60)
            monitor = HealthMonitor((check,))
            worker = threading.Thread(target=monitor.run_pass)
            worker.start()
            end = time.monotonic() + 5
            while worker.is_alive() and time.monotonic() < end:
                owner.pump()
                worker.join(.01)
            self.assertFalse(worker.is_alive())
            view = monitor.view(fixture.project_a_id)
            self.assertIn(reason, [f['reason_code'] for f in view['findings']])
            self.assertIn('finding(s) to look at', view['summary'])
            self.assertEqual([], monitor.view(fixture.project_b_id)['findings'])
        holds.clear_service_finding(directory)
        self.assertEqual([], check.run(self.context()))

    def test_durable_service_fault_survives_unreadable_history(self):
        fixture, owner = self.account_fixture()
        directory = fixture.state.root / 'projects' / fixture.project_a_id
        directory.mkdir(parents=True, mode=0o700)
        holds.save_service_finding(directory, holds._finding(
            fixture.project_a_id, 'service_writer_fault', 'The service writer stopped.', 'service'))
        (directory / 'history.sqlite3').write_bytes(b'not a database')
        found = holds.HoldsCheck(fixture.state, sweep_interval_seconds=60).run(self.context())
        self.assertIn('service_writer_fault', [f.reason_code for f in found])
        self.assertIn('holds_unreadable', [f.reason_code for f in found])

    def test_report_io_failure_still_attempts_service_close(self):
        host = mock.Mock()
        host.faulted.return_value = True
        def unavailable(finding):
            raise OSError('PRIVATE-PATH')
        with self.assertRaises(OSError):
            holds.stop_faulted_service(host, PROJECT_A, report=unavailable)
        host.close.assert_called_once_with()

    def test_project_id_is_bound_in_validator_queries(self):
        self.base.reserve()
        class Recording:
            def __init__(inner, connection):
                inner.connection = connection
                inner.calls = []
            def __getattr__(inner, name):
                return getattr(inner.connection, name)
            def execute(inner, sql, parameters=()):
                inner.calls.append((sql, parameters))
                return inner.connection.execute(sql, parameters)
        with closing(holds.open_reader(self.path)) as connection:
            recording = Recording(connection)
            holds.validate_project(holds.ValidatorPages(recording, {}, self.context(), PROJECT_A), leases.fixtures.KEY)
            self.assertTrue(recording.calls)
            self.assertFalse(any(PROJECT_A in sql for sql, args in recording.calls))
            self.assertTrue(any(PROJECT_A in args for sql, args in recording.calls))

    def test_registration_does_not_edit_primary_owned_files_or_builtin_api(self):
        # S8 does not wire its holds check into learning_store.py; the primary does that after S5.
        # Other accepted steps change this file, so scan for the specific registration or import.
        import re
        from tests.support import ORCHESTRATION_ROOT
        source = (ORCHESTRATION_ROOT / 'codex_agents' / 'learning_store.py').read_text(encoding='utf-8')
        pattern = re.compile(r'health_holds|HoldsCheck')
        self.assertIsNone(pattern.search(source), 'learning_store.py must not register or import the S8 holds check yet')

    def test_unrelated_lifetime_caps_bytes_are_preserved(self):
        # Every cap entry present at the S8 base (ce81bd96) is still present with an unchanged value;
        # other accepted steps add entries, so this is a superset check on the parsed JSON.
        import json
        import subprocess
        repository = Path(__file__).resolve().parents[3]
        path = Path(__file__).with_name('lifetime_caps.json')
        original = json.loads(subprocess.check_output(['git', 'show', 'ce81bd96:' + str(path.relative_to(repository))], cwd=repository))
        current = json.loads(path.read_text(encoding='utf-8'))
        # S14a (335) deliberately re-audited these two sites (checks 5 -> 6, new ends text); every other value is unchanged.
        updated_by_other_steps = {'account_service.py::_capture_admission_rows_locked', 'admission_input_decode.py::_preflight'}
        for site, entry in original['sites'].items():
            self.assertIn(site, current['sites'], site)
            if site not in updated_by_other_steps:
                self.assertEqual(entry, current['sites'][site], site)

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
        from codex_agents.ledger_owner import LedgerOwner
        LedgerOwner(ledger=fixture.ledger)
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
        from codex_agents.ledger_owner import LedgerOwner
        LedgerOwner(ledger=fixture.ledger)
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
        monitor = HealthMonitor.with_builtin_checks(sweep_interval_seconds=60)
        monitor.register(self.check)
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

for _name, _ids in {
    'test_project_overdue_stays_present_past_live_page': ('S8-HM3-2',),
    'test_account_overdue_stays_present_and_project_scoped_past_live_page': ('S8-HM3-2',),
    'test_account_overdue_uses_saved_project_clock_floor_without_advancing': ('S8-HM3-2',),
    'test_runtime_account_sql_runs_only_on_owner_without_opening_reader': ('S8-HM3-5',),
    'test_project_id_is_bound_in_validator_queries': ('S8-HM3-5',),
    'test_registration_does_not_edit_primary_owned_files_or_builtin_api': ('S8-HM3-6',),
    'test_unrelated_lifetime_caps_bytes_are_preserved': ('S8-HM3-5',),
}.items():
    getattr(HoldsTests, _name).__requirements__ = _ids

HoldsTests.test_durable_service_status_reaches_monitor_and_clears_on_recovery.__requirements__ = ('S8-HM3-3',)

HoldsTests.test_durable_service_fault_survives_unreadable_history.__requirements__ = ('S8-HM3-3',)
HoldsTests.test_report_io_failure_still_attempts_service_close.__requirements__ = ('S8-HM3-3',)

if __name__ == '__main__':
    unittest.main()
