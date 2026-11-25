"""S7 (M4b E2/E3): the sole ledger owner under load, through the real collector and account boundaries.

Hook ingests from several threads and projects and account requests arrive at the same time, against the real
`LedgerOwner` (one thread owns the ledger and runs its queue), the real `CollectorServer` request dispatch and the
real `AccountService`. No test waits on a fixed delay: a caller returns only once the owner has answered it, the
owner's own `handled` acknowledgement of every accepted request is awaited, a paused owner is released by an event,
and the arrival of work behind it is observed from the queue's own admission, never guessed. Every bound named
below is a count of requests, not a time.

Invariants checked (E1b: one writer, bounded queue, fair turns, health that never waits for the owner):
- nothing is lost or duplicated: every accepted request is handled once; the records on disk are exactly the ones
  acknowledged; a request the queue refuses is refused with its counted cause, and sent == accepted + refused;
- one project's backlog does not take another project's turns (real ingests, not synthetic callbacks);
- health, answered from published state, does not wait behind a queue that is full or an owner that is busy.
"""
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT  # noqa: F401  (bootstraps the import path)
import collections
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_framework import requirements
from tests.learning_support import generate_test_owner_key
from codex_agents import collector_client
from codex_agents.account_api import (
    ACCOUNT_READ_OPERATIONS as ACCOUNT_OPERATIONS, ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest,
    decode_account_response, encode_account_request)
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from codex_agents.collector_contracts import CollectorError
from codex_agents.ledger_owner import LedgerOwner
from codex_agents.learning_adapters import normalize_hook
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import CollectorServer, LearningState, authorize_collector_runtime
from codex_agents.ledger_owner_queue import DEFAULT_LEDGER_QUEUE_LIMITS

CLIENT_VERSION = 'codex-hooks-v1'
RUNTIME = COLLECTOR_RUNTIME_DIGEST
TOKEN = object()  # the fixture-only server context an account request is authorized against
PER_PROJECT = DEFAULT_LEDGER_QUEUE_LIMITS.per_project


def _sorted_counts_by(values, keys):
    counts = collections.Counter(values)
    return {key: counts[key] for key in keys}


class OwnerLoadTests(unittest.TestCase):
    __why_not_scenario__ = "Canonical real SQLite, collector dispatch and account service boundary; no model launch is needed."

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.roots = [self.base / name for name in ('project-a', 'project-b')]
        for root in self.roots:
            (root / '.git').mkdir(parents=True)
        self.handles, self.admitted = [], threading.Condition()
        self.returned, self.returned_count = threading.Condition(), 0
        self.sql_threads = set()
        self.account_tasks = collections.deque()
        self.start_owner()
        patch = mock.patch.object(collector_client, 'exchange_frame', side_effect=self.exchange)
        patch.start()
        self.addCleanup(patch.stop)

    # --- the owner: the thread that opens the ledger builds, serves and pumps everything, as collector_host does

    def start_owner(self):
        ready, self.stopped = threading.Event(), threading.Event()
        self.errors = []
        original_connect = self.original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(lambda statement: self.sql_threads.add(threading.current_thread()))
            return connection

        def run():
            owner = None
            try:
                owner = LedgerOwner(self.state)
                self.owner = owner
                original_offer = owner.queue.offer

                def spy(*args, **kwargs):
                    handle = original_offer(*args, **kwargs)  # a refusal raises here and is counted by the queue
                    with self.admitted:
                        self.handles.append(handle)
                        self.admitted.notify_all()
                    return handle
                owner.queue.offer = spy
                self.projects = [owner.ledger.register_project(root) for root in self.roots]
                self.views = AccountOwnerViews(owner.ledger)
                self.service = AccountService(RUNTIME, self.views, authorizer=self.authorize)
                self.server = CollectorServer(owner.client)
                for root in self.roots:
                    self.server.publish_health(root)
                ready.set()
                while not self.stopped.is_set():  # the collector host's loop: wake on offered work, handle a pass
                    owner.queue.wait_for_work(0.002)
                    owner.pump_ready(DEFAULT_LEDGER_QUEUE_LIMITS.requests)
                    while self.account_tasks:  # the account step of a pass: its requests run on this thread
                        self.account_tasks.popleft()()
            except BaseException as error:
                self.errors.append(error)
                ready.set()
            finally:
                if owner is not None:
                    try:
                        self.service.close()
                        owner.close()
                    except BaseException as error:
                        self.errors.append(error)

        patch = mock.patch('codex_agents.learning_store.sqlite3.connect', side_effect=connect)
        patch.start()
        self.addCleanup(patch.stop)
        self.thread = threading.Thread(target=run, name='s7-ledger-owner')
        self.thread.start()
        self.assertTrue(ready.wait(10), 'owner start-up acknowledgement')
        if self.errors:
            self.thread.join(10)
            raise self.errors[0]
        self.addCleanup(self.stop_owner)

    def stop_owner(self):
        if self.thread.is_alive():
            self.stopped.set()
            self.thread.join(10)
        self.assertFalse(self.thread.is_alive(), 'owner stopped acknowledgement')
        self.assertEqual([], self.errors)

    def authorize(self, command, context):
        if context is not TOKEN or command.project_id not in self.projects:
            return None
        return AccountAuthorization(command.project_id, RUNTIME, account_request_digest(command), 'host', ACCOUNT_OPERATIONS)

    # --- callers: other threads, through the same boundaries the collector and the account sockets use

    def exchange(self, path, raw, **kwargs):
        return canonical_json(self.server._dispatch(json.loads(raw)))

    def client(self):
        return CollectorClient(self.base / 'unused.sock', timeout=10)

    def hook_record(self, root, session, number):
        payload = {'session_id': session, 'cwd': str(root), 'hook_event_name': 'UserPromptSubmit',
                   'turn_id': f'turn-{session}-{number}', 'prompt': 'x', 'model': 'gpt-6-luna',
                   'permission_mode': 'default', 'transcript_path': None}
        return normalize_hook(payload, digest_key=self.state.digest_key(), client_version=CLIENT_VERSION)

    def ingest(self, root, session, number, *, token=None):
        """What the hook does: bind, then ingest one event. (outcome, record id); outcome 'ok' or the refusal code."""
        client, record = self.client(), self.hook_record(root, session, number)
        try:
            token = token or client.bind(root, session, 'hook', CLIENT_VERSION)
            client.ingest(token, record)
        except CollectorError as error:
            return error.code or 'refused', record['record_id']
        return 'ok', record['record_id']

    def account_call(self, project, number):
        """An account request, as the account socket's turn runs it: on the owner thread; this caller waits for its answer."""
        request = AccountRequest(ACCOUNT_PROTOCOL_VERSION, 'account.project', f'load-{project[-6:]}-{number}', project, RUNTIME, {})
        done, answer = threading.Event(), []

        def task():
            try:
                answer.append(decode_account_response(self.service.handle(encode_account_request(request), TOKEN), request))
            except BaseException as error:  # the caller reads it; the owner keeps serving
                answer.append(error)
            done.set()
        self.account_tasks.append(task)
        self.assertTrue(done.wait(30), 'account answer')
        return answer[0]

    def start_threads(self, jobs):
        """Start every job's thread and release them together; (threads, results). Each ends on its caller's answer."""
        gate, results, threads = threading.Event(), {}, []

        def worker(name, job):
            gate.wait(30)
            try:
                results[name] = job()
            except BaseException as error:
                results[name] = error
            with self.returned:
                self.returned_count += 1
                self.returned.notify_all()
        for name, job in jobs.items():
            threads.append(threading.Thread(target=worker, args=(name, job)))
        for thread in threads:
            thread.start()
        gate.set()
        return threads, results

    def wait_returned(self, count):
        """Until `count` callers in all have been answered (an answer is a caller returning)."""
        with self.returned:
            self.assertTrue(self.returned.wait_for(lambda: self.returned_count >= count, 30), 'callers answered')

    def pause_owner(self, project):
        """Hold the owner inside one request of `project` until the returned event is set (an event, not a delay)."""
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.owner.offer(lambda: (entered.set(), release.wait(60)), project_id=project, caller='load_pause')
        self.assertTrue(entered.wait(30), 'owner entered the held request')
        return release

    def bound_tokens(self, root, session, count):
        return [self.client().bind(root, session, 'hook', CLIENT_VERSION) for _ in range(count)]

    def finish_threads(self, threads):
        for thread in threads:
            thread.join(60)
            self.assertFalse(thread.is_alive(), 'a caller never got an answer')

    def run_threads(self, jobs):
        threads, results = self.start_threads(jobs)
        self.finish_threads(threads)
        return results

    def wait_admitted(self, count):
        """Until the queue has admitted `count` requests in all (its own admission, observed, not timed)."""
        with self.admitted:
            self.assertTrue(self.admitted.wait_for(lambda: len(self.handles) >= count, 30), 'queue admission')

    def all_handled(self):
        """Every request the queue ever admitted is acknowledged `handled` by the owner (E1b-7)."""
        with self.admitted:
            handles = list(self.handles)
        for handle in handles:
            self.assertTrue(handle.handled.wait(30), 'handled acknowledgement')
        self.assertEqual(0, self.owner.queue.snapshot()['retained'])
        return len(handles)

    def event_rows(self):
        """The events on disk, in the order the owner wrote them (rowid; source_sequence counts per project): (project id, record id, turn id). A separate read-only connection."""
        connection = self.original_connect(f"file:{self.state.root / 'learning.sqlite3'}?mode=ro", uri=True)
        try:
            return [(project, *(json.loads(bytes(content))[key] for key in ('record_id', 'turn_id')))
                    for project, content in connection.execute(
                        "select project_id, content from records where record_type = 'event' order by rowid")]
        finally:
            connection.close()


    def health(self, root=None):
        request = {'protocol': 1, 'op': 'health'}
        if root is not None:
            request['project_root'] = str(root)
        return self.server._dispatch(request)

    # --- E2: hooks and account calls at once, nothing lost or duplicated, one thread on the connection

    @requirements('M4b-E2', 'M4b-E3')
    def test_hooks_and_account_calls_at_once_lose_and_duplicate_nothing_on_one_ledger_thread(self):
        per_thread, threads_per_project, account_calls = 8, 3, 6
        jobs, turns = {}, {project: set() for project in self.projects}
        for index, (root, project) in enumerate(zip(self.roots, self.projects)):
            for thread in range(threads_per_project):
                session = f'p{index}-s{thread}'  # a session names one project: turn ids are unique across all
                turns[project].update(self.hook_record(root, session, n)['turn_id'] for n in range(per_thread))
                jobs[f'hook-{session}'] = lambda root=root, session=session: [
                    self.ingest(root, session, n) for n in range(per_thread)]
            jobs[f'account-{index}'] = lambda project=project: [self.account_call(project, n) for n in range(account_calls)]
        results = self.run_threads(jobs)
        sent = per_thread * threads_per_project * len(self.roots)
        outcomes = [pair for name, value in results.items() if name.startswith('hook-') for pair in value]
        self.assertEqual(sent, len(outcomes), 'a caller returned fewer answers than it sent')
        self.assertEqual({'ok'}, {outcome for outcome, _ in outcomes}, 'a hook event was refused inside the queue limits')
        self.assertGreater(self.all_handled(), sent)  # every admitted request is acknowledged, binds and records included
        self.assertEqual({}, self.owner.queue.snapshot()['refusals'])
        # Exactly the acknowledged events are on disk, once each, in their own project.
        rows = self.event_rows()
        self.assertEqual(sent, len(rows))
        self.assertEqual({record for _, record in outcomes}, {record for _, record, _ in rows})
        self.assertEqual(sent, len({record for _, record, _ in rows}))
        for project in self.projects:
            self.assertEqual(turns[project], {turn for owner, _, turn in rows if owner == project})
            self.assertEqual(len(turns[project]), len([1 for owner, _, _ in rows if owner == project]))
        # Every account request got its own answer for its own project (decode_account_response checks the identity).
        for index, project in enumerate(self.projects):
            answers = results[f'account-{index}']
            self.assertEqual(account_calls, len(answers))
            self.assertTrue(all(type(a) is dict and a['project_id'] == project for a in answers), answers)
        # The counters the health answer reports agree with the disk.
        for root, project in zip(self.roots, self.projects):
            ingestion = self.health(root)['ingestion']
            self.assertEqual(per_thread * threads_per_project, ingestion['accepted_hook_event_count'])
            self.assertEqual(0, ingestion['rejected_hook_event_count'])
        # The sole-owner invariant under load: the owner thread made every statement the ledger ran.
        self.assertEqual({self.thread}, self.sql_threads)

    # --- E2: a full project refuses its overflow, counted, and loses nothing it accepted

    @requirements('M4b-E2', 'M4b-E3')
    def test_a_full_project_refuses_its_overflow_counted_while_another_project_is_still_served(self):
        root_a, root_b = self.roots
        project_a, project_b = self.projects
        overflow = 5
        tokens_a = self.bound_tokens(root_a, 'sa', PER_PROJECT - 1 + overflow)
        tokens_b = self.bound_tokens(root_b, 'sb', 3)
        release = self.pause_owner(project_a)  # the held request is one of project A's PER_PROJECT places
        held = len(self.handles)
        jobs_a = {f'a{n}': (lambda n=n: self.ingest(root_a, 'sa', n, token=tokens_a[n])) for n in range(len(tokens_a))}
        threads_a, results = self.start_threads(jobs_a)
        self.wait_returned(overflow)  # the refused callers are answered at once, with the queue still held
        self.wait_admitted(held + PER_PROJECT - 1)
        refused = self.owner.queue.snapshot()['refusals']
        self.assertEqual(overflow, sum(sum(causes.values()) for causes in refused.values()), refused)
        # Project A is full; project B has its own share and is admitted (a backlog cannot take another's turns).
        jobs_b = {f'b{n}': (lambda n=n: self.ingest(root_b, 'sb', n, token=tokens_b[n])) for n in range(len(tokens_b))}
        threads_b, results_b = self.start_threads(jobs_b)
        self.wait_admitted(held + PER_PROJECT - 1 + len(tokens_b))
        self.assertEqual(overflow, self.returned_count, 'project B was answered before the owner was released')
        release.set()
        self.finish_threads(threads_a + threads_b)
        results.update(results_b)
        accepted_a = [value for name, value in results.items() if name.startswith('a') and value[0] == 'ok']
        refused_a = [value for name, value in results.items() if name.startswith('a') and value[0] != 'ok']
        self.assertEqual(PER_PROJECT - 1, len(accepted_a))
        self.assertEqual(overflow, len(refused_a))
        self.assertEqual(1, len({code for code, _ in refused_a}), refused_a)  # one existing, named refusal
        self.assertEqual({'ok'}, {value[0] for name, value in results.items() if name.startswith('b')})
        self.all_handled()
        # Sent == accepted + refused, and the disk holds exactly the accepted ones, once each.
        rows = self.event_rows()
        self.assertEqual({record for _, record in accepted_a}, {record for owner, record, _ in rows if owner == project_a})
        self.assertEqual({record for name, (_, record) in results.items() if name.startswith('b')},
                         {record for owner, record, _ in rows if owner == project_b})
        self.assertEqual(len(accepted_a) + 3, len(rows))
        self.assertEqual(overflow, sum(sum(causes.values()) for causes in self.owner.queue.snapshot()['refusals'].values()))
        self.assertEqual({self.thread}, self.sql_threads)

    # --- E2: fair turns among real ingests

    @requirements('M4b-E2', 'M4b-E3')
    def test_a_project_backlog_of_real_ingests_yields_turns_to_another_project(self):
        root_a, root_b = self.roots
        project_a, project_b = self.projects
        a_count, b_count = PER_PROJECT - 1, 3
        tokens_a, tokens_b = self.bound_tokens(root_a, 'sa', a_count), self.bound_tokens(root_b, 'sb', b_count)
        release = self.pause_owner(project_a)
        held = len(self.handles)
        threads_a, results = self.start_threads({f'a{n}': (lambda n=n: self.ingest(root_a, 'sa', n, token=tokens_a[n]))
                                                 for n in range(a_count)})
        self.wait_admitted(held + a_count)  # A's whole backlog is queued before B's first request arrives
        threads_b, results_b = self.start_threads({f'b{n}': (lambda n=n: self.ingest(root_b, 'sb', n, token=tokens_b[n]))
                                                   for n in range(b_count)})
        self.wait_admitted(held + a_count + b_count)
        release.set()
        self.finish_threads(threads_a + threads_b)
        results.update(results_b)
        self.assertEqual({'ok'}, {value[0] for value in results.values()})
        self.all_handled()
        order = [owner for owner, _, _ in self.event_rows()]
        self.assertEqual({project_a: a_count, project_b: b_count}, _sorted_counts_by(order, self.projects))
        # One turn per active project per round: B's three events are written among A's first ones; a first-in
        # first-out queue would write them after all of A's backlog (positions a_count to the end).
        positions = [index for index, owner in enumerate(order) if owner == project_b]
        self.assertLess(positions[-1], 2 * b_count, order)
        self.assertLess(positions[0], 2, order)

    # --- E2: health that stays responsive

    @requirements('M4b-E2', 'M4b-E3')
    def test_health_answers_while_the_owner_is_held_and_its_project_is_full(self):
        root_a, _ = self.roots
        project_a = self.projects[0]
        tokens = self.bound_tokens(root_a, 'sa', PER_PROJECT - 1)
        release = self.pause_owner(project_a)
        held = len(self.handles)
        threads, results = self.start_threads({f'a{n}': (lambda n=n: self.ingest(root_a, 'sa', n, token=tokens[n]))
                                               for n in range(len(tokens))})
        self.wait_admitted(held + len(tokens))
        self.assertEqual(PER_PROJECT, self.owner.queue.snapshot()['retained'])  # project A is at its share
        answers, answered = {}, threading.Event()

        def ask():
            answers['plain'] = self.health()
            answers['project'] = self.health(root_a)
            answered.set()
        asker = threading.Thread(target=ask)
        asker.start()
        try:
            self.assertTrue(answered.wait(30), 'health waited behind the held owner')  # an event, fails only if it hangs
            self.assertTrue(answers['plain']['ok'])
            # Published state when it is fresh; the existing bounded busy shape when a write marked it and the owner
            # cannot refresh it. Either way the answer came without the owner.
            self.assertTrue(answers['project']['ok'])
            self.assertTrue('ingestion' in answers['project'] or answers['project'].get('ledger_busy') is True)
            # The held owner and the full project queue are still held: nothing was waited for.
            self.assertEqual(PER_PROJECT, self.owner.queue.snapshot()['retained'])
        finally:
            release.set()
            asker.join(30)
        self.finish_threads(threads)
        self.assertEqual({'ok'}, {value[0] for value in results.values()})
        self.all_handled()
        # With the owner free again the project answers in full, and its counters match what was written.
        after = self.health(root_a)
        self.assertNotIn('ledger_busy', after)
        self.assertEqual(len(tokens), after['ingestion']['accepted_hook_event_count'])
