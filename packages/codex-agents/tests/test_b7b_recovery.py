"""B7b project-local recovery observations; real store and real sweep control flow."""
import copy
import json
from types import SimpleNamespace
from unittest import mock

from tests import test_deadlines_retention as dr
from tests import test_holds_report as b7a
from codex_agents.history_api import decode_history_response
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json
from test_framework import requirements

WHY = 'Real store, validator and driver sweep; only account/process boundaries and monotonic time are controlled.'
REASONS = ('needs_settlement', 'needs_process_evidence', 'has_holds', 'stop_failed', 'account_unreachable')


class RecoveryTests(dr.LeaseBackedFixture):
    @requirements('RS-B-B7-5', why_not_scenario=WHY)
    def test_attempt_identity_requires_colon_free_run_handle(self):
        from dataclasses import replace
        from tests.test_result_leases import lease_binding
        from codex_agents.project_holds_schema import attempt_identity
        lease = lease_binding()
        self.assertEqual(f'{lease.run_handle}:12', attempt_identity(lease.run_handle, 12))
        with self.assertRaises(HistoryError):
            replace(lease, run_handle=lease.run_handle + ':2')
        with self.assertRaises(AssertionError):
            attempt_identity(lease.run_handle + ':2', 12)

    @requirements('RS-B-B7-4', 'RS-B-B7-6', why_not_scenario=WHY)
    def test_history_only_service_is_stale_and_runtime_mismatch_never_serves_report(self):
        from codex_agents.history_api import encode_history_request
        from codex_agents import project_holds_report
        service = b7a.service_for(self.store)  # No runtime or RunDriver.
        self.addCleanup(service.close)
        request = b7a.command()
        value = decode_history_response(service.handle(encode_history_request(request), b7a.TOKEN), request)
        self.assertEqual({'reasons': dict.fromkeys(REASONS, 0), 'stale': True}, value['recovery_pending'])
        old = b7a.command(runtime='b' * 64)
        with mock.patch.object(project_holds_report, 'holds_report', side_effect=AssertionError('report served')), \
             mock.patch.object(service, '_authorizer', side_effect=AssertionError('authorization reached')):
            raw = service.handle(encode_history_request(old), b7a.TOKEN)
        envelope = json.loads(raw)
        self.assertIsNone(envelope['result'])
        with self.assertRaisesRegex(HistoryError, 'history_not_authorized'):
            decode_history_response(raw, old)

    @requirements('RS-B-B7-6', why_not_scenario=WHY)
    def test_learning_status_notice_only_when_recovery_is_stale(self):
        import cli
        import contextlib
        import io
        from codex_agents.recovery_report import RecoveryNotes
        for stale in (True, False, None):
            notes = RecoveryNotes()
            if stale is False:
                notes.publish(dict.fromkeys(REASONS, 0))
            self.store._recovery_notes = notes
            value = None if stale is None else b7a.report(self.store)
            with mock.patch('sys.argv', ['cli.py', 'learning-status', '--state-root', '/tmp/unused',
                                        '--project-root', '/tmp/unused-project']), \
                 mock.patch.object(cli, '_require_protected_runtime'), mock.patch.object(cli, '_require_own_roots'), \
                 mock.patch.object(cli, '_start_collector_quietly'), \
                 mock.patch.object(cli, 'status_client', return_value={'ready': True}), \
                 mock.patch.object(cli, 'capacity_report_client', return_value=None), \
                 mock.patch.object(cli, 'holds_report_client', return_value=value), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(0, cli.main())
            printed = json.loads(stdout.getvalue())
            self.assertEqual(None if value is None else value['recovery_pending'], printed['recovery_pending'])
            if stale is True:
                self.assertEqual('Recovery was not checked recently.', printed['recovery_pending_notice'])
            else:
                self.assertNotIn('recovery_pending_notice', printed)

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_sweep_resets_account_capture_on_cleanup_and_publication_exceptions(self):
        from codex_agents import project_driver as pd
        from codex_agents.recovery_report import RecoveryNotes
        for failure in ('instances', 'publish'):
            with self.subTest(failure=failure):
                notes = RecoveryNotes()
                driver = object.__new__(pd.RunDriver)
                driver._call = pd._Call()
                driver._runtime = SimpleNamespace(persistence=SimpleNamespace(recovery_notes=notes))
                driver._containers = None
                driver._container_recovery = lambda: None
                driver._settled_retry, driver._settled_checked = {}, frozenset()
                driver._journal = SimpleNamespace(entries=lambda: [], prune=lambda **kw: [], instances=lambda: [])
                target = driver._journal if failure == 'instances' else notes
                with mock.patch.object(target, failure, side_effect=RuntimeError('cut')):
                    with self.assertRaisesRegex(RuntimeError, 'cut'):
                        driver._settle_lost(False)
                self.assertIsNone(driver._call.recovery_unreachable)

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_account_refusal_beside_unreachable_counts_only_transport_failure(self):
        from codex_agents import project_driver as pd
        from codex_agents.account_api import AccountError
        from codex_agents.recovery_report import RecoveryNotes
        for refusal in ('account_not_authorized', 'account_wrong_owner'):
            with self.subTest(refusal=refusal):
                notes = RecoveryNotes()
                driver = object.__new__(pd.RunDriver)
                driver._call = pd._Call()
                driver._runtime = SimpleNamespace(persistence=SimpleNamespace(recovery_notes=notes))
                driver._containers = None
                driver._container_recovery = lambda: None
                driver._settled_retry, driver._settled_checked = {}, frozenset()
                entries = [dict(version=1, state='gone', instance='old', run_handle=handle,
                                host_client='host', claim_request_id=handle, token=handle, bridge_run_id=handle)
                           for handle in ('refused', 'unreachable')]
                driver._journal = SimpleNamespace(instance='current', entries=lambda: entries,
                    instance_stopped=lambda _: True, prune=lambda **kw: [], instances=lambda: [])
                def settle(entry):
                    raise AccountError(refusal if entry['run_handle'] == 'refused' else 'account_unavailable')
                driver._settle_entry = settle
                with mock.patch.object(pd, 'tree_state', return_value='gone'):
                    driver._settle_lost(False)
                self.assertEqual({**dict.fromkeys(REASONS, 0), 'needs_settlement': 1,
                                  'account_unreachable': 1}, notes.snapshot()['reasons'])
                self.assertIsNone(driver._call.recovery_unreachable)

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_held_reservation_reports_settlement_even_without_a_journal_observation(self):
        self.bound()
        value = b7a.report(self.store)['recovery_pending']
        self.assertEqual(1, value['reasons']['needs_settlement'])
        self.assertTrue(value['stale'])

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_empty_restart_and_strict_stale_boundary(self):
        from codex_agents.recovery_report import RecoveryNotes, EXPIRY_SECONDS
        now = [100.0]
        notes = RecoveryNotes(clock=lambda: now[0])
        self.store._recovery_notes = notes
        empty = {'reasons': dict.fromkeys(REASONS, 0), 'stale': True}
        self.assertEqual(empty, b7a.report(self.store)['recovery_pending'])
        notes.publish(dict.fromkeys(REASONS, 0))
        for age, expected in ((0, False), (2 * EXPIRY_SECONDS, False), (2 * EXPIRY_SECONDS + .001, True)):
            now[0] = 100 + age
            self.assertEqual(expected, b7a.report(self.store)['recovery_pending']['stale'])
        self.store._recovery_notes = RecoveryNotes()
        self.assertEqual(empty, b7a.report(self.store)['recovery_pending'])

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_sweep_counts_real_blockers_excludes_active_and_clears_on_success(self):
        from codex_agents.recovery_report import RecoveryNotes
        from codex_agents import project_driver as pd
        from codex_agents.account_api import AccountError
        driver = object.__new__(pd.RunDriver)
        notes = RecoveryNotes()
        driver._runtime = SimpleNamespace(persistence=SimpleNamespace(recovery_notes=notes))
        driver._containers = None
        driver._settled_retry, driver._settled_checked = {}, frozenset()
        entries = [dict(version=1, state='gone', instance='old', run_handle='PRIVATE-account', host_client='host'),
                   dict(version=1, state='gone', instance='old', run_handle='PRIVATE-local', host_client='host'),
                   dict(version=1, state='started', instance='old', run_handle='PRIVATE-process', host_client='host'),
                   dict(version=2, state='stop_failed', instance='old', run_handle='PRIVATE-stop', host_client='host'),
                   dict(version=1, state='started', instance='current', run_handle='PRIVATE-live', host_client='host')]
        for n, entry in enumerate(entries):
            entry.update(claim_request_id=str(n), token=str(n), bridge_run_id=str(n), workspace='PRIVATE')
        journal = SimpleNamespace(instance='current', entries=lambda: entries, instance_stopped=lambda _: True,
                                  prune=lambda **_: [], instances=lambda: [], settled=lambda *_: None)
        driver._journal = journal
        driver._container_recovery = lambda: None
        driver._remove_workspace = lambda _: True
        def settle(entry):
            if entry['run_handle'] == 'PRIVATE-account':
                raise AccountError('account_unavailable')
            raise HistoryError('history_conflict')
        driver._settle_entry = settle
        self.store._recovery_notes = notes
        with mock.patch.object(pd, 'tree_state', side_effect=lambda entry: 'gone' if entry['state'] == 'gone' else 'unknown'):
            driver._settle_lost(False)
        value = b7a.report(self.store)['recovery_pending']
        self.assertEqual({'needs_settlement': 1, 'needs_process_evidence': 1, 'has_holds': 0,
                          'stop_failed': 1, 'account_unreachable': 1}, value['reasons'])
        self.assertFalse(value['stale'])
        self.assertNotIn('PRIVATE', json.dumps(value))
        driver._settle_entry = mock.Mock(side_effect=AccountError('account_not_authorized'))
        with mock.patch.object(pd, 'tree_state', return_value='gone'):
            driver._settle_lost(False)
        self.assertEqual(0, b7a.report(self.store)['recovery_pending']['reasons']['account_unreachable'])
        entries.clear()
        driver._settle_lost(False)
        self.assertEqual(dict.fromkeys(REASONS, 0), b7a.report(self.store)['recovery_pending']['reasons'])
        entries.append(dict(version=3, state='intent', instance='old', run_handle='PRIVATE-intent',
                            host_client='host', claim_request_id='intent'))
        for outcome in ('fenced', 'removed'):
            driver._settle_intent = lambda *args: outcome
            driver._settle_lost(False)
            self.assertEqual(0, b7a.report(self.store)['recovery_pending']['reasons']['needs_settlement'])
        entries.clear()
        entries.append(dict(version=2, state='stop_failed', instance='current', run_handle='PRIVATE-own-stop',
                            host_client='host', claim_request_id='own-stop', token='own-stop'))
        driver._settle_lost(False)
        self.assertEqual({'needs_settlement': 0, 'needs_process_evidence': 0, 'has_holds': 0,
                          'stop_failed': 1, 'account_unreachable': 0},
                         b7a.report(self.store)['recovery_pending']['reasons'])

    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_failed_sweep_keeps_observation_age(self):
        from codex_agents.recovery_report import RecoveryNotes
        from codex_agents import project_driver as pd
        now = [0.0]
        notes = RecoveryNotes(clock=lambda: now[0])
        notes.publish(dict.fromkeys(REASONS, 0))
        now[0] = 21.0
        driver = object.__new__(pd.RunDriver)
        driver._runtime = SimpleNamespace(persistence=SimpleNamespace(recovery_notes=notes))
        driver._containers = None
        driver._container_recovery = lambda: None
        driver._journal = SimpleNamespace(entries=mock.Mock(side_effect=OSError('PRIVATE')))
        with self.assertRaises(OSError):
            driver._settle_lost(False)
        self.store._recovery_notes = notes
        self.assertTrue(b7a.report(self.store)['recovery_pending']['stale'])

    @requirements('RS-B-B7-6', why_not_scenario=WHY)
    def test_response_requires_closed_reasons_types_and_stale(self):
        service = b7a.service_for(self.store)
        self.addCleanup(service.close)
        request = b7a.command()
        from codex_agents.history_api import encode_history_request
        envelope = json.loads(service.handle(encode_history_request(request), b7a.TOKEN))
        self.assertIn('recovery_pending', envelope['result'])
        base = envelope['result']
        self.assertEqual(base, decode_history_response(canonical_json(envelope), request))
        variants = []
        for key in REASONS:
            for bad in (-1, True, None, 'PRIVATE'):
                value = copy.deepcopy(base)
                value['recovery_pending']['reasons'][key] = bad
                variants.append(value)
        for path in (('recovery_pending',), ('recovery_pending', 'stale'),
                     ('recovery_pending', 'reasons', 'stop_failed')):
            value = copy.deepcopy(base)
            target = value
            for key in path[:-1]:
                target = target[key]
            del target[path[-1]]
            variants.append(value)
        for key, bad in (('stale', 1), ('private', 'PRIVATE')):
            value = copy.deepcopy(base)
            value['recovery_pending'][key] = bad
            variants.append(value)
        value = copy.deepcopy(base)
        value['recovery_pending']['reasons']['private'] = 1
        variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaisesRegex(HistoryError, 'history_invalid_response'):
                decode_history_response(canonical_json({**envelope, 'result': value}), request)


class RecoveryScopeTests(__import__('unittest').TestCase):
    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_open_orchestrations_count_once_and_other_projects_never_leak(self):
        from tests import test_b6_unattended_close as b6
        from codex_agents.project_history import ProjectHistory
        from codex_agents.recovery_report import RecoveryNotes
        fixture = b6.LeaseTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        handle = fixture.open()['orchestration_handle']
        fixture.lease(handle)
        notes = RecoveryNotes()
        counts = dict.fromkeys(REASONS, 0)
        notes.publish({**counts, 'account_unreachable': 3})
        fixture.store._recovery_notes = notes
        before = list(fixture.store._connection.iterdump())
        with mock.patch('builtins.open', side_effect=AssertionError('content read')), \
             mock.patch('os.open', side_effect=AssertionError('content read')):
            value = b7a.report(fixture.store)['recovery_pending']
        self.assertEqual({**counts, 'has_holds': 1, 'account_unreachable': 3}, value['reasons'])
        self.assertEqual(before, list(fixture.store._connection.iterdump()))
        directory = fixture.directory.parent / 'PRIVATE-OTHER'
        directory.mkdir(mode=0o700)
        other = ProjectHistory(directory, b7a.PROJECT_B)
        self.addCleanup(other.close)
        self.assertEqual({'reasons': counts, 'stale': True}, b7a.report(other)['recovery_pending'])
        fixture.store.release_unsent_result_lease(fixture.lease_binding)
        self.assertEqual(0, b7a.report(fixture.store)['recovery_pending']['reasons']['has_holds'])
        for private in (handle, b7a.PROJECT_A, b7a.PROJECT_B, 'PRIVATE'):
            self.assertNotIn(private, json.dumps(value))


class AccountObservationTests(__import__('unittest').TestCase):
    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_fence_and_settlement_failures_survive_helpers_that_swallow_them(self):
        from codex_agents import project_driver as pd
        from codex_agents.account_api import AccountError
        from tests import test_result_leases as leases
        from codex_agents.hosts import CODEX_HOST_CLIENT
        driver = object.__new__(pd.RunDriver)
        driver._scope = SimpleNamespace(project_id=b7a.PROJECT_A)
        driver._account = __import__('pathlib').Path('/tmp/unused-account.sock')
        driver._call = pd._Call()
        driver._call.recovery_unreachable = set()
        driver._arbitrate_publication = lambda *args: None
        driver._settlement_step = lambda *args, **kw: {'state': 'releasing'}
        lease = leases.lease_binding()
        entry = dict(version=1, state='gone', lease=lease.as_data(), host_client=CODEX_HOST_CLIENT,
                     run_handle=lease.run_handle, identity_digest='c' * 64, claim_request_id=lease.claim_request_id,
                     bridge_run_id='brg_' + 'd' * 32, token='d' * 32,
                     storage_evidence=canonical_json({'lease': {'lease_digest': 'a' * 64}}).hex())
        for method, operation in (('_settle_entry', 'settle_lost_run'), ('_fence_entry', 'fence_claim')):
            driver._call.recovery_unreachable.clear()
            with mock.patch.object(pd, 'AccountClient') as client:
                getattr(client.return_value, operation).side_effect = AccountError('account_unavailable')
                with self.assertRaises(AccountError):
                    getattr(driver, method)(entry)
            self.assertEqual({lease.run_handle}, driver._call.recovery_unreachable)


class OwnRecoveryTests(__import__('unittest').TestCase):
    @requirements('RS-B-B7-4', why_not_scenario=WHY)
    def test_own_failed_run_account_backoff_is_counted_without_refreshing_sweep_age(self):
        from codex_agents import project_driver as pd
        from codex_agents.account_api import AccountError
        from codex_agents.recovery_report import RecoveryNotes
        from concurrent.futures import Future
        notes = RecoveryNotes()
        future = Future()
        future.set_exception(AccountError('account_unavailable'))
        run = SimpleNamespace(key=('PRIVATE-own', 1), own_settled=False, launch_token='token', receipt={},
                              state='failed', future=None, own_settle=future, own_settle_failures=0,
                              own_settle_at=None, own_reply=None)
        driver = object.__new__(pd.RunDriver)
        driver._runtime = SimpleNamespace(persistence=SimpleNamespace(recovery_notes=notes))
        driver._runs, driver._clock = {'run': run}, lambda: 100.0
        driver._settle_own_ended()
        value = notes.snapshot()
        self.assertEqual(0, value['reasons']['needs_settlement'])
        self.assertEqual(1, value['reasons']['account_unreachable'])
        self.assertTrue(value['stale'])
        notes.own_gone('PRIVATE-own')  # A retry starting does not clear the last failed answer.
        self.assertEqual(1, notes.snapshot()['reasons']['account_unreachable'])
        notes.own_gone('PRIVATE-own', account_unreachable=False)
        self.assertEqual(0, notes.snapshot()['reasons']['account_unreachable'])
        notes.own_gone('PRIVATE-own', account_unreachable=True)
        notes.publish(dict.fromkeys(REASONS, 0))
        self.assertEqual(1, notes.snapshot()['reasons']['account_unreachable'])
        notes.publish({**dict.fromkeys(REASONS, 0), 'needs_settlement': 1, 'account_unreachable': 1},
                      runs={'needs_settlement': {'PRIVATE-own'}, 'account_unreachable': {'PRIVATE-own'}})
        self.assertEqual(0, notes.snapshot()['reasons']['needs_settlement'])
        self.assertEqual(1, notes.snapshot()['reasons']['account_unreachable'])
        self.assertNotIn('PRIVATE', json.dumps(notes.snapshot()))
        notes.publish(dict.fromkeys(REASONS, 0))
        driver._runs.clear()
        driver._settle_own_ended()
        self.assertEqual(0, notes.snapshot()['reasons']['account_unreachable'])
