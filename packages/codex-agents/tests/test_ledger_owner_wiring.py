"""E1b wiring through real ledger/collector boundaries; event handoffs only."""
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT
from test_framework import requirements
from codex_agents.account_service import AccountOwnerViews
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, LedgerError
from codex_agents.ledger_owner_queue import DEFAULT_LEDGER_QUEUE_LIMITS


class LedgerOwnerWiringTests(unittest.TestCase):
    __why_not_scenario__ = "Canonical real SQLite and collector thread boundary; no model launch is needed."

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        (self.root / '.git').mkdir(parents=True)
        self.state = LearningState(Path(self.temp.name) / 'state')

    def start_owner(self, *, sockets=False):
        from codex_agents.ledger_owner import LedgerOwner
        ready, stopped = threading.Event(), threading.Event()
        errors, exits, sql, order = [], [], [], []
        original_connect = sqlite3.connect
        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(lambda statement: sql.append((threading.current_thread(), statement)))
            return connection
        def run():
            owner = None
            try:
                owner = LedgerOwner(self.state)
                self.owner = owner
                self.project = owner.ledger.register_project(self.root)
                self.views = AccountOwnerViews(owner.ledger)
                self.server = CollectorServer(owner.client)
                self.server.publish_health(self.root)
                if sockets:
                    self.server.start()
                ready.set()
                while not stopped.is_set():
                    owner.queue.wait_for_work(.05)
                    owner.pump()
            except BaseException as error:
                errors.append(error)
                ready.set()
            finally:
                if owner is not None:
                    try:
                        if hasattr(self, 'server'):
                            self.server.stop()
                        def record():
                            from codex_agents.collector_exit import write_exit
                            write_exit(self.state.root, started_at_ms=None, cause='error' if errors else 'requested',
                                       error=errors[-1] if errors else None)
                            order.append('exit')
                            exits.append(('exit', len(sql)))
                        owner.close(record_exit=record)
                    except BaseException as error:
                        errors.append(error)
        patch = mock.patch('codex_agents.learning_store.sqlite3.connect', side_effect=connect)
        patch.start()
        self.addCleanup(patch.stop)
        thread = threading.Thread(target=run, name='test-ledger-owner')
        thread.start()
        self.assertTrue(ready.wait(3), 'owner startup acknowledgement')
        if errors:
            thread.join(3)
            raise errors[0]
        def finish():
            if thread.is_alive():
                from codex_agents.sql_read_budget import SQLReadError
                try:
                    self.owner.call(stopped.set, caller='stop')
                except SQLReadError:
                    pass  # A failed owner has already closed admission.
                finally:
                    thread.join(3)
            self.assertFalse(thread.is_alive(), 'owner stopped acknowledgement')
        self.addCleanup(finish)
        self.finish, self.errors, self.exits, self.sql, self.thread = finish, errors, exits, sql, thread
        self.order = order

    @requirements('E1b-1', 'E1b-7')
    def test_all_sql_from_open_through_close_has_one_owner(self):
        self.start_owner(sockets=True)
        self.assertEqual(self.project, self.owner.client.project_for_root(self.root))
        self.assertTrue(self.owner.call(lambda: self.views.project(self.project), project_id=self.project))
        self.owner.call(self.server._sweep_cycle, caller='sweeps')
        self.owner.call(self.owner.ledger.machine_idle, caller='restart')
        self.finish()
        self.assertEqual([], self.errors)
        self.assertGreater(len(self.sql), 50)
        self.assertEqual({self.thread}, {thread for thread, _ in self.sql})
        self.assertIs(self.thread, self.owner.queue._owner)
        self.assertEqual(self.exits[0][1], len(self.sql))

    @requirements('E1b-1', 'E1b-6', 'E1b-7')
    def test_sql_trace_without_transport_covers_wakes_sweeps_restart_and_close(self):
        self.start_owner()
        self.assertEqual(self.project, self.owner.client.project_for_root(self.root))
        self.assertTrue(self.owner.call(lambda: self.views.project(self.project), project_id=self.project))
        self.server._reconcile_wakes()
        self.server._holds_due_idle()
        self.server._sweep_cycle()
        self.owner.client.claim_due_starts(now_ms=1, limit=2)
        self.owner.client.machine_idle()
        self.finish()
        self.assertEqual([], self.errors)
        self.assertGreater(len(self.sql), 50)
        self.assertEqual({self.thread}, {thread for thread, _ in self.sql})
        self.assertIs(self.thread, self.owner.queue._owner)
        self.assertEqual(self.exits[0][1], len(self.sql))

    @requirements('E1b-2', 'E1b-7')
    def test_transaction_rechecks_are_one_request_and_revocation_rolls_back(self):
        self.start_owner()
        checkpoints, other = [], []
        def transaction():
            with self.owner.ledger._account_transaction():
                checkpoints.append('begin')
                self.owner.ledger._connection.execute("UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,))
                self.assertEqual([], other)
                checkpoints.append('recheck')
                raise LedgerError('authority revoked')
        handle = self.owner.offer(transaction, project_id=self.project)
        following = self.owner.offer(lambda: other.append('after'), project_id=self.project)
        self.assertTrue(handle.handled.wait(2))
        with self.assertRaisesRegex(LedgerError, 'authority revoked'):
            self.owner.result(handle)
        self.assertTrue(following.handled.wait(2))
        self.assertEqual(['begin', 'recheck'], checkpoints)
        self.assertEqual(['after'], other)
        self.assertEqual(self.project, self.owner.client.project_for_root(self.root))

    @requirements('E1b-5')
    def test_handler_refusal_preserves_answer_and_next_request_runs(self):
        self.start_owner()
        with mock.patch.object(self.owner.ledger, 'project_for_root', side_effect=LedgerError('injected')):
            reply = self.server._dispatch({'protocol': 1, 'op': 'project', 'project_root': str(self.root)})
        self.assertEqual({'ok': False, 'code': 'operation_rejected'}, reply)
        self.assertEqual({'ok': True, 'project_id': self.project}, self.server._dispatch(
            {'protocol': 1, 'op': 'project', 'project_root': str(self.root)}))
        self.assertEqual([], self.errors)

    @requirements('E1b-5')
    def test_sweep_failure_keeps_existing_containment_and_owner_serves_next(self):
        self.start_owner()
        with mock.patch.object(self.owner.ledger, 'release_due_account_holds', side_effect=RuntimeError('injected sweep')):
            with self.assertRaisesRegex(RuntimeError, 'injected sweep'):
                self.server._sweep_cycle(holds_only=True)
        self.assertEqual(self.project, self.owner.client.project_for_root(self.root))
        self.assertEqual([], self.errors)

    @requirements('E1b-5')
    def test_fatal_sql_error_reaches_owner(self):
        self.start_owner()
        def broken():
            self.owner.ledger._connection.execute('SELECT missing_column FROM projects')
        handle = self.owner.offer(broken)
        self.assertTrue(handle.handled.wait(2))
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(1, len(self.errors))
        self.assertIsInstance(self.errors[0], sqlite3.OperationalError)
        self.assertEqual(1, len(self.exits))
        from codex_agents.collector_exit import read_exit
        record = read_exit(self.state.root)
        self.assertEqual(('error', 'OperationalError'), (record['cause'], record['error']))

    @requirements('E1b-6', 'E1b-7')
    def test_paused_owner_leaves_project_health_responsive(self):
        self.start_owner()
        entered, release, answered = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        handle = self.owner.offer(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(2))
        replies = []
        def health():
            replies.append(self.server._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(self.root)}))
            answered.set()
        front = threading.Thread(target=health)
        front.start()
        try:
            self.assertTrue(answered.wait(.25), 'published health waits behind paused owner')
            self.assertTrue(replies[0]['ok'])
            self.assertIn('ingestion', replies[0])
            self.assertFalse(handle.handled.is_set())
        finally:
            release.set()
            front.join(2)
        self.assertTrue(handle.handled.wait(2))

    @requirements('E1b-2', 'E1b-5', 'E1b-6')
    def test_health_for_unregistered_project_keeps_its_unregistered_answer(self):
        self.start_owner()
        other = self.root.parent / 'unregistered'
        (other / '.git').mkdir(parents=True)
        reply = self.server._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(other)})
        self.assertEqual({'ok': False, 'code': 'project_unregistered'}, reply)
        self.assertNotIn(str(other.resolve()), self.server._published_health)
        # The refusal is passing: the next request is served and the registered project still answers.
        again = self.server._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(self.root)})
        self.assertTrue(again['ok'])
        self.assertNotIn('ledger_busy', again)

    @requirements('E1b-2', 'E1b-5', 'E1b-6')
    def test_health_for_registered_project_not_yet_published_answers_in_full(self):
        self.start_owner()
        cold = self.root.parent / 'cold'
        (cold / '.git').mkdir(parents=True)
        self.owner.client.register_project(cold)
        self.assertNotIn(str(cold.resolve()), self.server._published_health)
        reply = self.server._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(cold)})
        self.assertTrue(reply['ok'])
        self.assertNotIn('ledger_busy', reply)
        for section in ('provider_attestations', 'collector_health', 'account_capacity', 'account_slots', 'ingestion'):
            self.assertIn(section, reply)
        self.assertEqual('active', reply['ingestion']['registered_project_state'])

    @requirements('E1b-2', 'E1b-5', 'E1b-6')
    def test_health_for_registered_idle_project_keeps_its_answer_shape(self):
        self.start_owner()
        reply = self.server._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(self.root)})
        self.assertTrue(reply['ok'])
        self.assertNotIn('ledger_busy', reply)
        self.assertEqual('active', reply['ingestion']['registered_project_state'])
        self.assertEqual(0, reply['ingestion']['rejected_hook_event_count'])
        self.assertIsNone(reply['ingestion']['last_rejected_hook_event_at_ms'])

    @requirements('E1b-6', 'E1b-7')
    def test_exit_is_recorded_after_final_handled_request(self):
        self.start_owner()
        waiting = threading.Event()
        last, failures = [], []
        def collector_finishing():
            try:
                waiting.set()
                self.server._stop.wait(2)
                handle = self.owner.offer(lambda: self.order.append('handled'))
                last.append(handle)
                self.owner.result(handle)
            except BaseException as error:
                failures.append(error)
        # Actual CollectorServer.stop must pump the final handoff while joining
        # its producer, then close admission before the exit record is written.
        producer = threading.Thread(target=collector_finishing)
        self.server._thread = producer
        producer.start()
        self.assertTrue(waiting.wait(2))
        self.finish()
        self.assertFalse(producer.is_alive())
        self.assertEqual([], failures)
        self.assertEqual([], self.errors)
        self.assertTrue(last[0].handled.is_set())
        self.assertEqual(['handled', 'exit'], self.order)
        self.assertEqual(1, len(self.exits))
        self.assertTrue(self.owner.queue.drain(0))
        from codex_agents.collector_exit import read_exit
        self.assertEqual('requested', read_exit(self.state.root)['cause'])

    @requirements('E1b-8', 'E1b-2')
    def test_preparation_does_not_occupy_owner_and_commit_rechecks_authority(self):
        import hashlib
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        self.start_owner()
        preparing, prepared = threading.Event(), threading.Event()
        self.addCleanup(prepared.set)
        result = []
        def inspect_files(*args):
            preparing.set()
            self.assertTrue(prepared.wait(2))
            return None
        digest = 'a'*64
        folder = hashlib.sha256(str(self.root).encode()).hexdigest()[:12]
        request = {'protocol': 1, 'op': 'record_launch', 'project_root': str(self.root),
                   'runtime_digest': COLLECTOR_RUNTIME_DIGEST, 'owner': 'enrolled-proof',
                   'launch_copy': str(self.root.parent / 'agent-orchestration' / folder / digest),
                   'launch_digest': digest}
        # Enrollment itself is independent of the slow protected-copy inspection.
        with mock.patch.object(self.server, '_owner_request', return_value=True), mock.patch(
                'codex_agents.host_commands.launch_target_problem', side_effect=inspect_files):
            worker = threading.Thread(target=lambda: result.append(self.server._dispatch(request)))
            worker.start()
            try:
                self.assertTrue(preparing.wait(2))
                self.assertEqual(0, self.owner.queue.snapshot()['retained'])
                self.assertEqual(self.project, self.owner.client.project_for_root(self.root))
                self.owner.call(lambda: self.owner.ledger._connection.execute(
                    "UPDATE projects SET status='deleted' WHERE project_id=?", (self.project,)).close())
            finally:
                prepared.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual([{'ok': False, 'code': 'project_unregistered'}], result)

    @requirements('E1b-8', 'E1b-2')
    def test_signature_preparation_yields_owner_and_changed_generation_refuses_commit(self):
        from tests.learning_support import generate_test_owner_key
        from codex_agents.providers import billing_attestation_record
        from codex_agents.learning_engine import OwnerGrantAuthority
        _, public = generate_test_owner_key(self.root.parent)
        self.state.install_owner_public_key(public)
        replacement = self.root.parent / 'replacement'
        replacement.mkdir()
        _, replacement_public = generate_test_owner_key(replacement)
        self.start_owner()
        record = billing_attestation_record({'version': 1, 'provider': 'codex',
            'account_fingerprint': 'a'*64, 'no_paid_overage': True, 'no_usage_credits': True, 'issued_at': 1})
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        failures = []
        def verify(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return True
        def commit():
            try:
                self.owner.client.commit_owner_record(self.project, record=record, signature=b'prepared')
            except BaseException as error:
                failures.append(error)
        with mock.patch.object(OwnerGrantAuthority, 'verify_owner_record', side_effect=verify):
            worker = threading.Thread(target=commit)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.project, self.owner.client.project_for_root(self.root))
                self.owner.client.replace_owner_key(replacement_public)
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], LedgerError)
        self.assertEqual('owner record signature is invalid', str(failures[0]))
        self.assertEqual(0, self.owner.call(lambda: self.owner.ledger._connection.execute(
            "SELECT COUNT(*) FROM records WHERE record_type='billing_attestation'").fetchone()[0]))

    @requirements('E1b-5', 'E1b-2')
    def test_real_account_handler_keeps_failure_answer_and_processes_next_request(self):
        from tests.test_account_api import AccountApiTests, TOKEN
        from codex_agents.account_api import encode_account_request
        from codex_agents.ledger_owner import LedgerOwner
        fixture = AccountApiTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        request = fixture.request('account.project')
        raw = encode_account_request(request)
        with mock.patch.object(fixture.views, 'project', side_effect=LedgerError('injected')):
            previous = fixture.service.handle(raw, TOKEN)
        owner = LedgerOwner(ledger=fixture.ledger)
        self.addCleanup(owner.queue.close)
        with mock.patch.object(fixture.views, 'project', side_effect=LedgerError('injected')):
            self.assertEqual(previous, fixture.service.handle(raw, TOKEN))
        following = owner.offer(lambda: 'handled', project_id=fixture.projects[0])
        result = fixture.call(request)
        self.assertEqual(fixture.projects[0], result['project_id'])
        self.assertTrue(following.handled.is_set())
        self.assertEqual('handled', owner.result(following))

    @requirements('E1b-3', 'E1b-4', 'E1b-5')
    def test_full_queue_returns_existing_account_busy_and_retry_succeeds(self):
        from tests.test_account_api import AccountApiTests
        from codex_agents.account_api import AccountError
        from codex_agents.ledger_owner import LedgerOwner
        fixture = AccountApiTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner = LedgerOwner(ledger=fixture.ledger)
        self.addCleanup(owner.queue.close)
        handles = [owner.offer(lambda: None, project_id=project) for project in fixture.projects for _ in range(6)]
        self.assertEqual(12, owner.queue.snapshot()['retained'])
        with self.assertRaisesRegex(AccountError, 'account_busy'):
            fixture.call(fixture.request('account.project'))
        self.assertFalse(any(handle.handled.is_set() for handle in handles))
        for handle in handles:
            owner.pump()
        self.assertTrue(all(handle.handled.is_set() for handle in handles))
        self.assertEqual(fixture.projects[0], fixture.call(fixture.request('account.project'))['project_id'])
        self.assertEqual({'queue_full': 1}, owner.queue.snapshot()['refusals']['account'])

    @requirements('E1b-2', 'E1b-8')
    def test_real_original_open_rechecks_and_rolls_back_within_one_request(self):
        from tests.test_account_original_open import OriginalAccountOpenPendingTests
        from codex_agents.account_open_contracts import OpenError
        from codex_agents.ledger_owner import LedgerOwner
        fixture = OriginalAccountOpenPendingTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        owner = LedgerOwner(ledger=fixture.ledger)
        self.addCleanup(owner.queue.close)
        checks, following = [], []
        def recheck():
            checks.append(fixture.ledger._connection.in_transaction)
            if len(checks) == 1:
                following.append(owner.offer(lambda: 'next'))
            else:
                self.assertFalse(following[0].handled.is_set())
                raise OpenError('open_not_authorized')
        def operation():
            try:
                fixture.preflight(before_commit=recheck)
            except OpenError as error:
                return error.reason_code
        handle = owner.offer(operation)
        owner.pump()
        self.assertTrue(handle.handled.is_set())
        self.assertEqual('open_not_authorized', owner.result(handle))
        self.assertEqual([False, True], checks)
        self.assertEqual([], fixture.pending())
        self.assertFalse(following[0].handled.is_set())
        owner.pump()
        self.assertEqual('next', owner.result(following[0]))

    @requirements('E1b-3', 'E1b-4')
    def test_measured_queue_defaults(self):
        self.assertEqual((12, 8, 6, 800_000_000), (
            DEFAULT_LEDGER_QUEUE_LIMITS.requests, DEFAULT_LEDGER_QUEUE_LIMITS.projects,
            DEFAULT_LEDGER_QUEUE_LIMITS.per_project, DEFAULT_LEDGER_QUEUE_LIMITS.max_wait_ns))

    @requirements('E1b-6')
    def test_published_health_evicts_past_project_limit(self):
        self.start_owner()
        roots = []
        for index in range(9):
            root = self.root.parent / str(index)
            (root / '.git').mkdir(parents=True)
            self.owner.client.register_project(root)
            self.owner.call(lambda root=root: self.server.publish_health(root))
            roots.append(root)
        self.assertLessEqual(len(self.server._published_health), 8)
        self.assertNotIn(str(roots[0].resolve()), self.server._published_health)
        self.assertIn(str(roots[-1].resolve()), self.server._published_health)


# Review fix round (040-e1bw-r1): each check below ran red on 34caee73.
def _controlled_queue(test):
    """The owner's queue reads a test clock: its deadline checks see time pass only when a request moves it."""
    import functools
    import time
    from codex_agents import ledger_owner, ledger_owner_queue
    offset = [0]
    factory = functools.partial(ledger_owner_queue.LedgerOwnerQueue, clock_ns=lambda: time.monotonic_ns() + offset[0])
    patch = mock.patch.object(ledger_owner, 'LedgerOwnerQueue', factory)
    patch.start()
    test.addCleanup(patch.stop)
    return offset


LATE_NS = 2 * DEFAULT_LEDGER_QUEUE_LIMITS.max_wait_ns


class LedgerOwnerReviewFixTests(unittest.TestCase):
    """HIGH-1/2, MED-2, LOW-1/2/3 of review 040-e1bw-r1, through the real owner."""
    __why_not_scenario__ = LedgerOwnerWiringTests.__why_not_scenario__
    # The same disposable state and owner thread as the checks above, without running those checks twice.
    setUp = LedgerOwnerWiringTests.setUp
    start_owner = LedgerOwnerWiringTests.start_owner

    def health(self, root=None, server=None):
        return (server or self.server)._dispatch({'protocol': 1, 'op': 'health', 'project_root': str(root or self.root)})

    # HIGH-1 (E1b-4): late only refuses work that has not started; work that ran keeps its answer.
    @requirements('E1b-4', 'E1b-5')
    def test_review_owner_thread_request_that_runs_late_keeps_its_answer(self):
        from codex_agents.ledger_owner import LedgerOwner
        from codex_agents.sql_read_budget import SQLReadError
        offset = _controlled_queue(self)
        owner = LedgerOwner(self.state)
        self.addCleanup(owner.close)
        project = owner.ledger.register_project(self.root)
        def late():
            offset[0] += LATE_NS  # The request itself runs past its deadline.
            owner.ledger.health_increment(project, 'conflict')
            return 'committed'
        self.assertEqual('committed', owner.call(late, project_id=project, caller='account'))
        offset[0] = 0
        self.assertEqual(1, owner.ledger.health_snapshot(project)['conflict']['count'])
        # Work whose deadline passed before it started is still refused, and nothing ran.
        ran = []
        ahead = owner.offer(lambda: offset.__setitem__(0, offset[0] + LATE_NS), project_id=project)
        with self.assertRaises(SQLReadError):
            owner.call(lambda: ran.append(1), project_id=project, caller='account')
        self.assertTrue(ahead.handled.is_set())
        self.assertEqual([], ran)

    def claim_jobs_fixture(self):
        from tests.test_account_claim_jobs import AccountClaimJobTests
        fixture = AccountClaimJobTests(methodName='test_C5_a_submitted_claim_is_busy_in_the_lane_notes')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)  # Runs first: drains the disposable workers.
        return fixture

    @requirements('E1b-4', 'E1b-5')
    def test_review_late_deferred_claim_publication_is_delivered_not_lost(self):
        from codex_agents.account_api import decode_account_response
        from codex_agents.ledger_owner import LedgerOwner
        from codex_agents.sql_read_budget import SQLReadError
        import time
        offset = _controlled_queue(self)
        fixture = self.claim_jobs_fixture()
        owner = LedgerOwner(ledger=fixture.ledger)
        self.addCleanup(owner.queue.close)
        ticket = fixture.begin()
        self.assertIs(object, type(ticket))
        real_take = fixture.jobs.take
        def take(ticket, **options):
            reply = real_take(ticket, **options)
            if reply is not None:
                offset[0] += LATE_NS  # Consumed; the publication now finishes past its deadline.
            return reply
        started, raw = time.monotonic(), None
        with mock.patch.object(fixture.jobs, 'take', side_effect=take):
            while raw is None:
                fixture.service.drive_claims()
                try:
                    raw = fixture.service.take_claim_reply(ticket)
                except SQLReadError:
                    self.fail('a late publication answered busy after consuming its completion (reply lost)')
                finally:
                    offset[0] = 0
                self.assertLess(time.monotonic() - started, 10, 'deferred claim did not finish')
                if raw is None:
                    time.sleep(.002)
        self.assertEqual('claimed', decode_account_response(raw, fixture.request())['state'])
        self.assertEqual(0, fixture.jobs.snapshot()['tickets'])

    @requirements('E1b-4', 'E1b-5')
    def test_review_late_deferred_begin_keeps_its_ticket(self):
        from codex_agents.account_api import decode_account_response
        from codex_agents.ledger_owner import LedgerOwner
        offset = _controlled_queue(self)
        fixture = self.claim_jobs_fixture()
        owner = LedgerOwner(ledger=fixture.ledger)
        self.addCleanup(owner.queue.close)
        real_submit = fixture.jobs.submit
        def submit(*args, **options):
            ticket = real_submit(*args, **options)
            offset[0] += LATE_NS  # Registered; the begin request now finishes past its deadline.
            return ticket
        with mock.patch.object(fixture.jobs, 'submit', side_effect=submit):
            ticket = fixture.begin()
        offset[0] = 0
        self.assertIs(object, type(ticket), 'a late begin answered busy while its ticket stayed registered')
        self.assertEqual(1, fixture.jobs.snapshot()['tickets'])
        self.assertEqual('claimed', decode_account_response(fixture.pump(ticket), fixture.request())['state'])
        self.assertEqual(0, fixture.jobs.snapshot()['tickets'])

    # HIGH-2 (E1b-6): values that depend on the time are as of the read, as before the wiring.
    @requirements('E1b-6')
    def test_review_published_health_ages_an_idle_project_at_read_time(self):
        import time
        from codex_agents import learning_store
        self.start_owner()
        accepted = time.time_ns() // 1_000_000
        self.owner.call(lambda: self.owner.ledger._connection.execute(
            "INSERT INTO hook_ingestion VALUES(?,?,?,?)", (self.project, 'turn_started', 1, accepted)).close(),
            project_id=self.project)
        self.assertEqual('fresh', self.health()['ingestion']['freshness']['state'])
        later = accepted + learning_store.INGESTION_FRESH_MS + 1
        expected = self.owner.call(lambda: self.owner.ledger.ingestion_snapshot(self.project, now_ms=later))['freshness']
        with mock.patch.object(learning_store.time, 'time_ns', return_value=later * 1_000_000):
            reply = self.health()
        self.assertEqual('idle', expected['state'])
        self.assertEqual(expected, reply['ingestion']['freshness'])

    @requirements('E1b-6')
    def test_review_published_health_counts_a_run_slot_that_passed_its_deadline_since_publication(self):
        import tests.test_account_claim_operation as claims
        from codex_agents import learning_store
        from codex_agents.ledger_owner import LedgerOwner
        case = claims.AccountClaimOperationTests('test_handler_claims_with_owner_deadline_and_returns_known_state')
        case.setUp()
        self.addCleanup(case.doCleanups)
        owner = LedgerOwner(ledger=case.ledger)
        self.addCleanup(owner.queue.close)
        self.assertEqual('claimed', case.claim()['state'])
        row = case.ledger._connection.execute(
            'SELECT deadline_at_ms,execution_deadline_ns FROM active_run_capacity').fetchone()
        root = dict(case.ledger.active_project_roots())[case.project]
        server = CollectorServer(owner.client)
        def read():
            replies = []
            front = threading.Thread(target=lambda: replies.append(self.health(root, server)))
            front.start()
            while front.is_alive():
                owner.queue.wait_for_work(.01)
                owner.pump()
            front.join()
            return replies[0]
        self.assertEqual(0, read()['account_slots']['runs_past_deadline'])
        with mock.patch.object(learning_store.time, 'time_ns', return_value=(row[0] + 1) * 1_000_000), \
                mock.patch.object(learning_store.time, 'monotonic_ns', return_value=row[1] + 1):
            reply = read()
        self.assertNotIn('ledger_busy', reply)
        self.assertEqual(1, reply['account_slots']['runs_past_deadline'])

    # MED-2: a write is acknowledged without display reads, and a display failure cannot change its answer.
    @requirements('E1b-4', 'E1b-6')
    def test_review_a_write_is_acknowledged_without_rereading_published_health(self):
        self.start_owner()
        other = self.root.parent / 'other'
        (other / '.git').mkdir(parents=True)
        self.owner.client.register_project(other)
        self.assertTrue(self.health(other)['ok'])
        reads = []
        real = self.owner.ledger.account_slot_health
        with mock.patch.object(self.owner.ledger, 'account_slot_health',
                               side_effect=lambda project: reads.append(project) or real(project)):
            self.owner.client.health_increment(self.project, 'conflict')
        self.assertEqual([], reads)
        self.assertEqual(1, self.health()['collector_health']['conflict']['count'])

    @requirements('E1b-4', 'E1b-5', 'E1b-6')
    def test_review_a_display_read_failure_never_fails_a_committed_write(self):
        self.start_owner()
        with mock.patch.object(self.owner.ledger, 'account_slot_health',
                               side_effect=sqlite3.DatabaseError('display read failed')):
            self.assertIsNone(self.owner.call(
                lambda: self.owner.ledger.health_increment(self.project, 'conflict'), project_id=self.project))
        self.assertEqual([], self.errors)
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(1, self.health()['collector_health']['conflict']['count'])

    # LOW-1: a cold project is not refused because another project's refresh is outstanding.
    @requirements('E1b-6')
    def test_review_cold_project_answers_while_another_refresh_is_outstanding(self):
        self.start_owner()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        pause = self.owner.offer(lambda: (entered.set(), release.wait(2)))
        self.assertTrue(entered.wait(2))
        self.assertTrue(self.health()['ok'])  # Published: answered, and its refresh is now outstanding.
        unregistered = self.root.parent / 'unregistered'
        (unregistered / '.git').mkdir(parents=True)
        offered, real_offer = threading.Event(), self.owner.queue.offer
        def offer(project_id, operation, **options):
            handle = real_offer(project_id, operation, **options)
            if options.get('caller') == 'health' and self.owner.queue.snapshot()['retained'] >= 3:
                offered.set()
            return handle
        replies = []
        with mock.patch.object(self.owner.queue, 'offer', side_effect=offer):
            front = threading.Thread(target=lambda: replies.append(self.health(unregistered)))
            front.start()
            offered.wait(1)
            release.set()
            front.join(2)
        self.assertTrue(pause.handled.wait(2))
        self.assertEqual([{'ok': False, 'code': 'project_unregistered'}], replies)

    # LOW-2: a busy owner leaves the owner-key report's fail-closed answer, never an unexpected error.
    @requirements('E1b-4', 'E1b-5')
    def test_review_owner_key_report_fails_closed_when_the_owner_is_busy(self):
        self.start_owner()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.owner.offer(lambda: (entered.set(), release.wait(3)))
        self.assertTrue(entered.wait(2))
        report = self.state.owner_key_report()
        release.set()
        self.assertEqual('mismatch', report['state'])

    # LOW-3: errors handed back from the owner keep their attributes and constructors.
    @requirements('E1b-5')
    def test_review_handed_back_errors_keep_their_attributes(self):
        from codex_agents.collector_contracts import CollectorError
        from codex_agents.contracts import RESULT_VALIDATION_REASON_CODES, ResultContractError
        from codex_agents.history_contracts import HistoryError
        self.start_owner()
        reason = sorted(RESULT_VALIDATION_REASON_CODES)[0]
        cases = (
            (CollectorError('refused', code='e1bw_code'), lambda error: error.code == 'e1bw_code'),
            (FileNotFoundError(2, 'missing'), lambda error: (error.errno, error.strerror) == (2, 'missing')),
            (ResultContractError(reason, 'bad result'),
             lambda error: (error.reason_code, str(error)) == (reason, 'bad result')),
            (HistoryError('history_service_unavailable', message='hidden'),
             lambda error: (error.reason_code, str(error)) == ('history_service_unavailable', 'hidden')),
        )
        for raised, check in cases:
            with self.subTest(kind=type(raised).__name__):
                def fail(raised=raised):
                    raise raised
                with self.assertRaises(type(raised)) as caught:
                    self.owner.call(fail, contain_errors=True)
                self.assertTrue(check(caught.exception), vars(caught.exception))


_PROCESS_PROBE = '''
import json, os, signal, sqlite3, sys, threading
sys.path.insert(0, sys.argv[1])
from codex_agents import account_host, learning_store
from codex_agents.sql_read_budget import SQLReadError
RESULT = os.path.join(sys.argv[2], "e1bw-result.json")
def finish(results):
    with open(RESULT, "w") as handle:
        json.dump(results, handle)
    os.kill(os.getpid(), signal.SIGTERM)
'''

# MED-1: the production collector's connection refuses every native use from another thread.
SOLE_OWNER_PROBE = _PROCESS_PROBE + '''
real_start = learning_store.CollectorServer.start
def start(self):
    real_start(self)
    connection = self.ledger.owner.ledger._connection
    cursor = sqlite3.Connection.cursor(connection)
    results = {}
    def attempt(name, action):
        try:
            action()
            results[name] = "allowed"
        except (sqlite3.ProgrammingError, SQLReadError):
            results[name] = "refused"
        except Exception as error:
            results[name] = type(error).__name__
    def other():
        attempt("execute", lambda: sqlite3.Connection.execute(connection, "SELECT 1").fetchone())
        attempt("cursor", lambda: cursor.execute("SELECT 1").fetchone())
        attempt("function", lambda: connection.create_function("e1bw", 0, lambda: 1))
        attempt("backup", lambda: connection.backup(sqlite3.connect(":memory:")))
        attempt("interrupt", connection.interrupt)
        attempt("close", connection.close)
    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    attempt("owner", lambda: sqlite3.Connection.execute(connection, "SELECT 1").fetchone())
    finish(results)
learning_store.CollectorServer.start = start
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

# MED-3: ready owner requests are drained per host loop pass, not one per 2 ms tick.
THROUGHPUT_PROBE = _PROCESS_PROBE + '''
passes, looping = [0], threading.Event()
real_serve = account_host.AccountServiceHost.serve_once
def serve_once(self):
    passes[0] += 1
    looping.set()
    return real_serve(self)
account_host.AccountServiceHost.serve_once = serve_once
real_start = learning_store.CollectorServer.start
def start(self):
    real_start(self)
    owner = self.ledger.owner
    def producer():
        if not looping.wait(30):
            return finish({"error": "the host loop never ran"})
        # A gate holds the owner while the requests arrive, so all of them are ready at once.
        entered, release = threading.Event(), threading.Event()
        gate = owner.offer(lambda: (entered.set(), release.wait(10)), caller="e1bw_gate")
        if not entered.wait(10):
            return finish({"error": "the gate never ran"})
        handles = [owner.offer(lambda: None, project_id=label, caller="e1bw_probe")
                   for label in ("prj_" + "1" * 32, "prj_" + "2" * 32) for _ in range(4)]
        released = passes[0]
        release.set()
        handled = gate.handled.wait(10) and all(handle.handled.wait(10) for handle in handles)
        finish({"handled": handled, "passes": passes[0] - released})
    threading.Thread(target=producer, daemon=True).start()
learning_store.CollectorServer.start = start
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''


@unittest.skipUnless(__import__('sys').platform.startswith('linux') or Path('/usr/bin/sandbox-exec').is_file(),
                     'account signature workers need the reviewed macOS sandbox')
class LedgerOwnerHostProcessTests(unittest.TestCase):
    """The real collector_host.py process (MED-1, MED-3 of review 040-e1bw-r1)."""
    __why_not_scenario__ = "The real collector host process; the probe only observes its connection and loop."

    def setUp(self):
        import tempfile
        from tests.learning_support import generate_test_owner_key
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents.learning_store import authorize_collector_runtime
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.state = LearningState(base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(base)
        self.state.install_owner_public_key(public)
        self.orchestration = ORCHESTRATION_ROOT

    def run_host(self, source):
        import json
        import subprocess
        import sys
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', source, str(self.orchestration),
                                    str(self.state.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        process.wait(timeout=60)
        return json.loads((self.state.root / 'e1bw-result.json').read_text())

    @requirements('E1b-1')
    def test_review_production_connection_refuses_every_use_from_another_thread(self):
        self.assertEqual({'execute': 'refused', 'cursor': 'refused', 'function': 'refused', 'backup': 'refused',
                          'interrupt': 'refused', 'close': 'refused', 'owner': 'allowed'},
                         self.run_host(SOLE_OWNER_PROBE))

    @requirements('E1b-3', 'E1b-4')
    def test_review_host_loop_drains_ready_requests_each_pass(self):
        result = self.run_host(THROUGHPUT_PROBE)
        self.assertTrue(result.get('handled'), result)
        self.assertLessEqual(result['passes'], 1, result)
