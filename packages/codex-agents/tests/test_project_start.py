"""RS-E E2c: the collector's starter, its limits and budgets, the crash-loop park and the status lines.

Scenario tests run the real collector (`collector_host.py`, with its account services) and real project hosts
started from each project's protected runtime copy, on a short TMPDIR. The collector is launched through a small
launcher that only (a) points the account's home folder at a test folder (`host_commands.account_home`; a protected
copy must sit in the account's own ~/.codex, which tests may not touch) and lets that folder be trusted although it
sits under the temporary directory (`temp_roots`), (b) shortens the starter's named intervals, and (c) appends one line
per start step to a log file the test reads (and, for one test, holds a spawn until a file appears). The wire, the
ledger, the starter's decisions and the processes are the product's.
"""
from __future__ import annotations

import contextlib
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
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
from tests import test_upgrade_handover as upgrade_tests
from tests.learning_support import generate_test_owner_key
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from tests.test_host_commands import install_package

from codex_agents import host_commands, learning_store, project_start
from codex_agents.account_api import AccountRequest, decode_account_response, encode_account_request
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from codex_agents.collector_paths import CollectorPaths
from codex_agents.control_api import ControlScope
from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
from codex_agents.hosts import CODEX_HOST_CLIENT
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import (
    LEARNING_ACTIVATION_PENDING_KEY, LEARNING_MOVED_KEY, LearningLedger, LearningState, authorize_collector_runtime,
)
from codex_agents.project_host_client import SERVICE_START_REASONS, project_host_serving
from codex_agents.service_enrollment import (
    account_endpoint_path, enrollment_preamble, proof_directory, read_wake_capability,
)
from codex_agents.service_transport import exchange_frame

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'scripts'))
from test_framework import requirements  # noqa: E402

SCENARIO = 'Real collector and project host processes over their sockets; only the home folder seam and named intervals are set.'

LAUNCHER = r'''
import json, os, runpy, sys, time
orchestration, home, state_root, options = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
sys.path.insert(0, orchestration)
from pathlib import Path
from codex_agents import host_commands, learning_store, project_start
host_commands.account_home = lambda: Path(home)
if options.get('trust_temp', True):
    host_commands.temp_roots = lambda: set()
for name, value in options['constants'].items():
    assert hasattr(project_start, name), name
    setattr(project_start, name, value)
for name, value in options['store'].items():
    assert hasattr(learning_store, name), name
    setattr(learning_store, name, value)
log = options['log']

def note(**fields):
    with open(log, 'a') as handle:
        handle.write(json.dumps({**fields, 't': time.monotonic()}) + '\n')

# Fault injection (review round 1, HIGH 2): the first `count` calls of a ledger step are refused, as a busy or
# failing ledger refuses them.
refusals = dict(options.get('fail') or {})
for method_name in list(refusals):
    real_method = getattr(learning_store.LearningLedger, method_name)
    def refused(self, *args, _real=real_method, _name=method_name, **kwargs):
        if refusals[_name] > 0:
            refusals[_name] -= 1
            note(event='refused', step=_name)
            raise learning_store.LedgerError('refused by the test')
        return _real(self, *args, **kwargs)
    setattr(learning_store.LearningLedger, method_name, refused)
# Review round 2 (HIGH 2/3): while the file `unserved` names exists, the starter sees no host serving (its hosts still
# run): a child that lives without serving, as a slow or stuck host does. Only the starter's view is changed.
if options.get('unserved'):
    import types
    from codex_agents import project_host_client
    view = types.ModuleType('project_host_client_view')
    view.__dict__.update(project_host_client.__dict__)
    def unserved_serving(*args, _real=project_host_client.project_host_serving, **kwargs):
        return False if os.path.exists(options['unserved']) else _real(*args, **kwargs)
    view.project_host_serving = unserved_serving
    project_start.project_host_client = view
claim_due = learning_store.LearningLedger.claim_due_starts
def timed_claim(self, **kwargs):
    began = time.perf_counter()
    try:
        return claim_due(self, **kwargs)
    finally:
        note(event='claim', seconds=time.perf_counter() - began)
learning_store.LearningLedger.claim_due_starts = timed_claim

attempt = project_start.ProjectStarter._attempt
def logged_attempt(self, item):
    note(event='begin', project=item.project_id, copy=item.copy)
    try:
        return attempt(self, item)
    finally:
        note(event='end', project=item.project_id)
project_start.ProjectStarter._attempt = logged_attempt
spawn = project_start.ProjectStarter._spawn
def logged_spawn(self, item, entry):
    note(event='spawning', project=item.project_id)
    if options.get('block'):
        while not os.path.exists(options['block']):
            time.sleep(0.01)
    return spawn(self, item, entry)
project_start.ProjectStarter._spawn = logged_spawn
posix_spawn = os.posix_spawn
def logged_posix_spawn(path, argv, env, **kwargs):
    pid = posix_spawn(path, argv, env, **kwargs)
    if any(str(part).endswith('project_host.py') for part in argv):
        note(event='spawned', pid=pid, argv=[str(part) for part in argv], env=dict(env))
    return pid
os.posix_spawn = logged_posix_spawn
sys.argv = [orchestration + '/collector_host.py', '--state-root', state_root]
runpy.run_path(orchestration + '/collector_host.py', run_name='__main__')
'''


def wait_for(test, predicate, seconds=20.0, message='condition never held'):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    test.fail(message() if callable(message) else message)


class StarterBase(unittest.TestCase):
    CONSTANTS = {'STARTER_CHECK_SECONDS': 0.2, 'START_RECHECK_SECONDS': 1.0}
    STORE = {}
    pycache = None  # the class's shared bytecode cache for its test-only interpreters (S13C-1)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pycache = tempfile.mkdtemp(prefix='start-pyc-', dir=SHORT_TMP)
        cls.addClassCleanup(shutil.rmtree, cls.pycache, ignore_errors=True)
        # S13C-1: the runtime is compiled into the class cache before any test's clock starts, so the
        # first collector of the class does not compile inside a measured window.
        subprocess.run([sys.executable, '-I', '-X', 'pycache_prefix=' + cls.pycache, '-m', 'compileall', '-q',
                        str(ORCHESTRATION_ROOT)], stdout=subprocess.DEVNULL, check=True)

    @classmethod
    def python_flags(cls, *flags):
        """`flags` (isolation) and the class's shared bytecode cache instead of `-B`."""
        return [*flags, '-X', 'pycache_prefix=' + cls.pycache]

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.home = self.base / 'home'
        self.home.mkdir()
        self.state = LearningState(self.base / 's')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.paths = CollectorPaths(self.state.root)
        self.projects = {}
        self.collector = None
        self.log = self.base / 'starts.log'
        self.held = []
        self.addCleanup(self.stop_all)

    @contextlib.contextmanager
    def located(self):
        with mock.patch.object(host_commands, 'account_home', lambda: self.home), \
                mock.patch.object(host_commands, 'temp_roots', lambda: set()):
            yield

    def add(self, name, *, record=True):
        """A registered project with an installed package and, by default, setup's launch record (collector down)."""
        root = install_package(self.base / name).resolve()
        (root / '.git').mkdir()
        ledger = LearningLedger(self.state)
        try:
            project_id = ledger.register_project(root)
            copy = self.install_copy(root)
            if record:
                ledger.record_launch(project_id, str(copy), copy.name)
        finally:
            ledger.close()
        self.projects[name] = (root, project_id, copy)
        return root, project_id, copy

    def install_copy(self, root):
        with self.located():
            copy, _ = host_commands.install_runtime_copy(root, {'HOME': str(self.home)})
        return copy

    def offline(self):
        return contextlib.closing(LearningLedger(self.state))

    def break_store(self, name):
        """The project's own store is not a database: its host ends at once with a fixed reason."""
        folder = self.state.root / 'projects' / self.projects[name][1]
        for level in (folder.parent, folder):
            level.mkdir(mode=0o700, exist_ok=True)
        (folder / 'history.sqlite3').write_bytes(b'not a database' * 64)
        (folder / 'history.sqlite3').chmod(0o600)

    def mend_store(self, name):
        (self.state.root / 'projects' / self.projects[name][1] / 'history.sqlite3').unlink()

    def launch(self, constants=None, store=None, env=None, block=None, fail=None, trust_temp=True,
               orchestration=ORCHESTRATION_ROOT, unserved=None):
        options = {'constants': {**self.CONSTANTS, **(constants or {})}, 'store': {**self.STORE, **(store or {})},
                   'log': str(self.log), 'block': block, 'fail': fail, 'trust_temp': trust_temp,
                   'unserved': unserved}
        self.options = options
        errors = open(self.base / 'collector.err', 'ab')
        self.addCleanup(errors.close)
        self.collector = subprocess.Popen(
            [sys.executable, *self.python_flags('-I'), '-c', LAUNCHER, str(orchestration), str(self.home), str(self.state.root),
             json.dumps(options)], env={'PATH': os.defpath, 'TMPDIR': SHORT_TMP, **(env or {})},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors, start_new_session=True)
        end = time.monotonic() + 30
        while True:
            try:
                if orchestration != ORCHESTRATION_ROOT:  # another runtime's collector: this client may refuse it
                    reply = json.loads(exchange_frame(self.state.socket, canonical_json({'protocol': 1, 'op': 'health'}),
                                                      timeout=2.0, request_limit=4096, response_limit=16_384))
                    if reply.get('ok') is True:
                        return reply
                    raise RuntimeError('not ready')
                return CollectorClient(self.state.socket, timeout=2).health()
            except Exception:
                if self.collector.poll() is not None or time.monotonic() > end:
                    self.fail('collector did not start: ' + (self.base / 'collector.err').read_text()[-3000:])
                time.sleep(0.05)

    def constant(self, name):
        """A starter constant as the running collector has it."""
        return self.options['constants'].get(name, getattr(project_start, name))

    def stop_collector(self):
        if self.collector is not None and self.collector.poll() is None:
            self.collector.terminate()
            self.collector.wait(30)
        self.collector = None

    def host_pids(self, name=None):
        """This test's project hosts (its own state root is in their arguments), or one project's."""
        pattern = 'project_host.py --state-root ' + str(self.state.root) + ' '
        if name is not None:
            pattern += '--project-root ' + str(self.root(name)) + '$'
        listed = subprocess.run(['pgrep', '-f', pattern], stdout=subprocess.PIPE, text=True, check=False)
        return sorted(int(value) for value in listed.stdout.split())

    def stop_hosts(self):
        for pid in self.host_pids():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        end = time.monotonic() + 20
        while self.host_pids() and time.monotonic() < end:
            time.sleep(0.05)
        for pid in self.host_pids():
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)

    def stop_all(self):
        for process in self.held:
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(5)
        self.stop_hosts()
        self.stop_collector()
        self.stop_hosts()

    def root(self, name):
        return self.projects[name][0]

    def serving(self, name):
        return project_host_serving(self.state.root, self.root(name))

    def starts(self, name):
        connection = sqlite3.connect(self.state.database.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            found = connection.execute('SELECT * FROM project_starts WHERE project_id=?',
                                       (self.projects[name][1],)).fetchone()
            return None if found is None else dict(found)
        finally:
            connection.close()

    def wake(self, name):
        connection = sqlite3.connect(self.state.database.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            found = connection.execute('SELECT w.*,p.launch_digest FROM projects p LEFT JOIN project_wake w '
                                       'ON w.project_id=p.project_id WHERE p.project_id=?',
                                       (self.projects[name][1],)).fetchone()
            return None if found is None else dict(found)
        finally:
            connection.close()

    def migration_keys(self, name):
        connection = sqlite3.connect(self.state.database.as_uri() + '?mode=ro', uri=True, timeout=5)
        try:
            project_id = self.projects[name][1]
            return {row[0] for row in connection.execute(
                'SELECT feature_key FROM additive_migrations WHERE feature_key IN (?,?)',
                (LEARNING_MOVED_KEY + project_id, LEARNING_ACTIVATION_PENDING_KEY + project_id))}
        finally:
            connection.close()

    def status(self, name):
        return CollectorClient(self.state.socket, timeout=5).start_status(self.root(name))

    def events(self, kind=None, name=None):
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]
        project = None if name is None else self.projects[name][1]
        return [row for row in rows if (kind is None or row['event'] == kind)
                and (project is None or row.get('project') == project)]

    def name_of(self, project_id):
        return next(name for name, value in self.projects.items() if value[1] == project_id)

    def cli(self, command, name):
        env = {k: v for k, v in os.environ.items()
               if not any(s in k.upper() for s in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for variable in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CLAUDECODE', 'CLAUDE_CODE_SESSION_ID'):
            env.pop(variable, None)
        env.update(CODEX_THREAD_ID='e2c-primary', TMPDIR=SHORT_TMP)
        done = subprocess.run([sys.executable, *self.python_flags('-I', '-S'), str(ORCHESTRATION_ROOT / 'cli.py'), command,
                               '--state-root', str(self.state.root), '--project-root', str(self.root(name))],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=90, check=False)
        return done.returncode, json.loads(done.stdout or b'{}')

    def publish(self, name, payload):
        """One wake publication over the account host with the project's own wake key (as its service sends it)."""
        root, project_id, _copy = self.projects[name]
        request = AccountRequest(1, 'account.publish_wake', 'w-' + os.urandom(8).hex(), project_id,
                                 COLLECTOR_RUNTIME_DIGEST, payload)
        frame = encode_account_request(request)
        endpoint = account_endpoint_path(self.paths)
        line = enrollment_preamble(read_wake_capability(self.paths, root), 'project', CODEX_HOST_CLIENT, 1, frame,
                                   proof_directory(endpoint), subject=str(root))
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
        return decode_account_response(bytes(received).split(b'\n', 1)[0], request)

    def now_ms(self):
        return time.time_ns() // 1_000_000

    def command(self, name):
        return f'run learning-start-project-service --project-root {self.root(name)}'


def bulk_projects(ledger, template_id, count, *, due_ms, prefix, copy_for=None):
    """`count` active projects (as registration writes them) with wake rows due at `due_ms` (None: settled), with
    the launch record `copy_for(root)` returns ((copy, digest), or None for no record)."""
    connection = ledger._connection
    template = dict(connection.execute('SELECT * FROM projects WHERE project_id=?', (template_id,)).fetchone())
    columns = list(template)
    rows, wakes = [], []
    for index in range(count):
        project_id = 'prj_' + f'{prefix}{index:0{32 - len(prefix)}d}'
        root = f'/var/empty/{prefix}/{index}'
        record = None if copy_for is None else copy_for(root)
        row = {**template, 'project_id': project_id, 'canonical_root': root,
               'launch_copy': None if record is None else record[0],
               'launch_digest': None if record is None else record[1]}
        for column in columns:
            if column.endswith('_digest') and column != 'launch_digest' and type(row[column]) is str:
                row[column] = row[column][:-12] + f'{index:012x}'
        rows.append(tuple(row[column] for column in columns))
        wakes.append((project_id, due_ms, 1))
    with ledger._lock:
        connection.execute('BEGIN IMMEDIATE')
        connection.executemany(f'INSERT INTO projects({",".join(columns)}) VALUES({",".join("?" * len(columns))})',
                               rows)
        connection.executemany('INSERT INTO project_wake(project_id,allocated_epoch,refused,due_at_ms,'
                               'updated_at_ms) VALUES(?,0,0,?,?)', wakes)
        connection.commit()


def overlaps(events):
    """The most starts in flight at once, overall and for any one project, from begin/end lines."""
    running, most, per_project = {}, 0, 0
    for row in sorted(events, key=lambda row: (row['t'], row['event'] == 'begin')):
        if row['event'] == 'begin':
            running[row['project']] = running.get(row['project'], 0) + 1
            per_project = max(per_project, running[row['project']])
            most = max(most, sum(running.values()))
        elif row['event'] == 'end':
            running[row['project']] -= 1
    return most, per_project


class StartTests(StarterBase):
    @requirements('E2c-1', 'S13C-1', 'S13C-2', 'S13C-3', why_not_scenario=SCENARIO)
    def test_E2c_1_a_stopped_project_with_due_holds_starts_without_any_client(self):
        self.add('never')  # never ran: unknown, so due at collector start
        self.add('stopped')
        self.add('idle')
        past = self.now_ms() - 60_000
        with self.offline() as ledger:
            for name, due in (('stopped', past), ('idle', None)):
                project_id = self.projects[name][1]
                ledger.publish_wake(project_id, epoch=None, seq=1, state='pending', next_due_at_ms=None)
                ledger.publish_wake(project_id, epoch=1, seq=2, state='clean_stop', next_due_at_ms=due)
        self.launch()
        ready = time.monotonic()
        # Review round 1 (MED 2): within one interval. Each due project's start begins within STARTER_CHECK_SECONDS
        # of the collector being ready (one tick of slack) and serves within its start's own deadline.
        check = self.constant('STARTER_CHECK_SECONDS')
        deadline = ready + check + min(self.constant('STARTER_TICK_SECONDS'), check) + 1.0
        for name in ('never', 'stopped'):
            begun = wait_for(self, lambda: self.events('begin', name), max(0.0, deadline - time.monotonic()),
                             f'{name}: no start began within one interval')
            self.assertLessEqual(begun[0]['t'], deadline)
        # Review round 2 (decision 243, MED 2): serving, not only begun, by that same deadline.
        served = None
        while served is None and time.monotonic() <= deadline:
            if self.serving('never') and self.serving('stopped'):
                served = time.monotonic()  # both checks answered serving by this time
            else:
                time.sleep(0.05)
        self.assertIsNotNone(served, 'the due projects were not serving within one interval')
        self.assertLessEqual(served, deadline)
        time.sleep(1.0)
        self.assertFalse(self.serving('idle'))
        self.assertEqual([], self.events('begin', 'idle'))
        self.assertLessEqual(project_start.STARTER_CHECK_SECONDS, learning_store.WORKER_RESULT_SWEEP_SECONDS)
        self.assertTrue(self.status('stopped').startswith('recovery: started '), self.status('stopped'))

    @requirements('E2c-5', why_not_scenario=SCENARIO)
    def test_E2c_5_the_child_is_the_copy_s_entry_with_isolation_flags_and_a_bare_environment(self):
        root, _project_id, copy = self.add('alpha')
        self.launch(env={'PLANTED_E2C': 'must-not-leak', 'HOME': str(self.home)})
        wait_for(self, lambda: self.serving('alpha'), 30, 'alpha never served')
        spawned = self.events('spawned')
        self.assertEqual(1, len(spawned), spawned)
        entry = copy / '.codex' / 'orchestration' / 'project_host.py'
        self.assertEqual([sys.executable, *PYTHON_ISOLATION_FLAGS, str(entry), '--state-root', str(self.state.root),
                          '--project-root', str(root)], spawned[0]['argv'])
        self.assertEqual({'PATH': os.defpath, 'xcrun_nocache': '1'}, spawned[0]['env'])
        shown = subprocess.run(['ps', '-ww', '-o', 'command=', '-p', str(spawned[0]['pid'])],
                               stdout=subprocess.PIPE, text=True, check=False).stdout
        self.assertIn(str(entry), shown)

    @requirements('E2c-2', why_not_scenario=SCENARIO)
    def test_E2c_2_limits_and_round_robin_with_two_projects_ending_at_once(self):
        for name in ('alpha', 'bravo', 'charlie'):
            self.add(name)
        self.break_store('bravo')
        self.break_store('charlie')
        self.launch(constants={'START_BACKOFF_FIRST_SECONDS': 0.4, 'START_BACKOFF_MAX_SECONDS': 0.8})
        wait_for(self, lambda: self.serving('alpha'), 30, 'alpha never served')
        wait_for(self, lambda: min(len(self.events('begin', 'bravo')), len(self.events('begin', 'charlie'))) >= 4,
                 40, 'the broken projects were not retried')
        events = self.events()
        most, per_project = overlaps([row for row in events if row['event'] in ('begin', 'end')])
        self.assertLessEqual(most, 2)
        self.assertEqual(1, per_project)
        begins = [self.name_of(row['project']) for row in events if row['event'] == 'begin']
        self.assertEqual({'alpha', 'bravo', 'charlie'}, set(begins[:3]))
        counts = {'bravo': 0, 'charlie': 0}
        for name in begins[3:]:
            if name in counts:
                counts[name] += 1
                self.assertLessEqual(abs(counts['bravo'] - counts['charlie']), 1, begins)
        self.assertEqual(1, len(self.events('begin', 'alpha')))  # served: never started again

    @requirements('E2c-2', why_not_scenario=SCENARIO)
    def test_E2c_2_a_start_blocked_in_its_spawn_holds_no_ledger_lock(self):
        self.add('alpha')
        go = self.base / 'go'
        self.launch(block=str(go))
        wait_for(self, lambda: self.events('spawning', 'alpha'), 20, 'the start never reached its spawn')
        client = CollectorClient(self.state.socket, timeout=2)
        began = time.monotonic()
        client.health()
        other = self.base / 'other'
        (other / '.git').mkdir(parents=True)
        self.assertRegex(client.register(other), r'^prj_[0-9a-f]{32}$')  # a ledger write while the start waits
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertIsNotNone(self.starts('alpha')['attempt'])  # still outstanding, read beside it
        go.touch()
        wait_for(self, lambda: self.serving('alpha'), 30, 'the unblocked start never served')

    @requirements('E2c-6', why_not_scenario=SCENARIO)
    def test_E2c_6_a_changed_or_missing_install_starts_nothing(self):
        _root, _id, copy = self.add('copy_changed')
        target = copy / '.codex' / 'orchestration' / 'codex_agents' / '__init__.py'
        os.chmod(target, 0o600)
        target.write_bytes(target.read_bytes() + b'\n# changed\n')
        root, _id, _copy = self.add('project_changed')
        installed = root / '.codex' / 'orchestration' / 'codex_agents' / '__init__.py'
        installed.write_bytes(installed.read_bytes() + b'\n# changed\n')
        _root, _id, copy = self.add('missing')
        for folder, _dirs, _files in os.walk(copy):
            os.chmod(folder, 0o700)
        shutil.rmtree(copy)
        self.launch()
        for name in ('copy_changed', 'project_changed', 'missing'):
            wait_for(self, lambda: self.status(name) == 'recovery pending: install changed', 20,
                     f'{name}: {self.status(name)}')
        self.assertEqual([], self.events('spawned'))
        self.assertEqual([], self.host_pids())

    @requirements('E2c-7', why_not_scenario=SCENARIO)
    def test_E2c_7_another_version_s_live_host_is_not_replaced(self):
        root, project_id, _copy = self.add('alpha')
        directory = self.state.root / 'rt-other'
        directory.mkdir(mode=0o700)
        lock = os.open(directory / 'host.lock', os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, lock)
        import fcntl
        fcntl.flock(lock, fcntl.LOCK_EX)
        for endpoint in ('dispatch.sock', 'result.sock'):  # the other host's live sockets
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(directory / endpoint))
            (directory / endpoint).chmod(0o600)
            listener.listen(1)
        hint =self.state.root / 'client-runtimes' / (keyed_digest(self.paths.digest_key(), str(root)) + '.json')
        hint.parent.mkdir(mode=0o700, exist_ok=True)
        scope = ControlScope(project_id=project_id, primary_session_id='other-session', runtime_digest='f' * 64,
                             supervisor_instance_id='other-supervisor', supervisor_generation=1)
        hint.write_bytes(canonical_json({'schema_version': 2,
            'project_root_digest': keyed_digest(self.paths.digest_key(), str(root)), 'scope': asdict(scope),
            'account_endpoint': str(directory / 'account.sock'), 'dispatch_endpoint': str(directory / 'dispatch.sock'),
            'result_endpoint': str(directory / 'result.sock')}))
        hint.chmod(0o600)
        started = self.launch()['started_at_ms']
        expected = 'recovery: needs update handover: ' + self.command('alpha')
        wait_for(self, lambda: self.status('alpha') == expected, 20, f'status: {self.status("alpha")}')
        time.sleep(1.5)  # rechecked, still not replaced
        self.assertEqual([], self.events('spawned'))
        self.assertIsNone(self.collector.poll())
        self.assertEqual(started, CollectorClient(self.state.socket, timeout=2).health()['started_at_ms'])
        self.assertTrue(hint.exists())

    @requirements('E2c-8', why_not_scenario=SCENARIO)
    def test_E2c_8_pre_start_steps(self):
        self.add('empty')
        self.add('pending')
        self.add('broken')
        with self.offline() as ledger:
            ledger._connection.execute('INSERT INTO additive_migrations VALUES(?)',
                                       (LEARNING_ACTIVATION_PENDING_KEY + self.projects['pending'][1],))
        self.break_store('broken')
        self.assertEqual(set(), self.migration_keys('empty'))
        self.launch(constants={'START_BACKOFF_FIRST_SECONDS': 60.0})
        wait_for(self, lambda: self.serving('empty'), 30, 'the empty project never served')
        self.assertIn(LEARNING_MOVED_KEY + self.projects['empty'][1], self.migration_keys('empty'))
        expected = 'recovery pending: learning migration: ' + self.command('pending')
        wait_for(self, lambda: self.status('pending') == expected, 20, f'status: {self.status("pending")}')
        self.assertEqual([], self.events('spawned', 'pending'))
        line = wait_for(self, lambda: (self.status('broken') or '').startswith('recovery: backing off ')
                        and self.status('broken'), 20, f'status: {self.status("broken")}')
        reason = self.starts('broken')['reason']
        self.assertIn(reason, SERVICE_START_REASONS)
        self.assertIn(reason, line)


class BackoffTests(StarterBase):
    @requirements('E2c-4', why_not_scenario=SCENARIO)
    def test_E2c_4_a_crash_loop_parks_and_only_the_owner_unparks(self):
        self.add('broken')
        self.break_store('broken')
        self.launch(constants={'START_BACKOFF_FIRST_SECONDS': 0.3, 'START_BACKOFF_MAX_SECONDS': 0.6,
                               'START_PARK_AFTER_SECONDS': 2.0})
        line = wait_for(self, lambda: (self.status('broken') or '').startswith('recovery failed: ')
                        and self.status('broken'), 30, f'never parked: {self.status("broken")}')
        row = self.starts('broken')
        self.assertEqual(f'recovery failed: {row["parked_reason"]}; stopped trying after an hour of failed starts: '
                         + self.command('broken'), line)
        self.assertGreaterEqual(row['failures'], 3)
        waits = [b['t'] - a['t'] for a, b in zip(self.events('begin', 'broken'), self.events('begin', 'broken')[1:])]
        self.assertGreater(waits[1], waits[0])  # the backoff grows (exact waits: LedgerStartTests)
        attempts = len(self.events('begin', 'broken'))
        # The project reports again: still parked.
        accepted = self.publish('broken', {'epoch': None, 'seq': 1, 'state': 'pending', 'next_due_at_ms': None})
        self.publish('broken', {'epoch': accepted['epoch'], 'seq': 2, 'state': 'exact',
                                'next_due_at_ms': self.now_ms() - 1})
        time.sleep(2.0)
        self.assertEqual(attempts, len(self.events('begin', 'broken')))
        self.assertTrue(self.status('broken').startswith('recovery failed: '))
        # The owner's start command unparks it (and its streak starts afresh).
        parked = self.starts('broken')
        self.cli('learning-start-project-service', 'broken')
        wait_for(self, lambda: len(self.events('begin', 'broken')) > attempts, 20, 'never tried again')
        # A fresh streak (it may park again later on its own hour: here 2 s).
        fresh = self.starts('broken')['streak_first_ms']
        self.assertTrue(fresh is None or fresh > parked['outcome_at_ms'], (parked, self.starts('broken')))

    @requirements('E2c-4', why_not_scenario=SCENARIO)
    def test_E2c_4_an_early_exit_counts_failed_and_a_lasting_start_ends_the_streak(self):
        self.add('alpha')
        self.break_store('alpha')
        self.launch(constants={'START_BACKOFF_FIRST_SECONDS': 0.3, 'START_BACKOFF_MAX_SECONDS': 0.6,
                               'START_PROBATION_SECONDS': 4.0}, store={'WORKER_RESULT_SWEEP_SECONDS': 1.0})
        wait_for(self, lambda: (self.starts('alpha') or {}).get('failures', 0) >= 2, 20, 'no failures counted')
        self.mend_store('alpha')
        wait_for(self, lambda: self.serving('alpha'), 30, 'the mended project never served')
        failures = self.starts('alpha')['failures']
        for pid in self.host_pids():
            os.kill(pid, signal.SIGKILL)  # ended within its probation
        wait_for(self, lambda: self.starts('alpha')['failures'] == failures + 1, 20,
                 f'early exit not counted: {self.starts("alpha")}')
        wait_for(self, lambda: self.serving('alpha'), 30, 'not started again after the early exit')
        row = wait_for(self, lambda: self.starts('alpha')['failures'] == 0 and self.starts('alpha'), 20,
                       f'streak never ended: {self.starts("alpha")}')
        self.assertIsNone(row['streak_first_ms'])


class StatusTests(StarterBase):
    @requirements('E2c-9', why_not_scenario=SCENARIO)
    def test_E2c_9_status_is_the_owner_s_and_learning_status_shows_it(self):
        self.add('broken')
        self.break_store('broken')
        self.launch(constants={'START_BACKOFF_FIRST_SECONDS': 60.0})
        wait_for(self, lambda: (self.status('broken') or '').startswith('recovery: backing off (1 failed: '), 20,
                 f'status: {self.status("broken")}')
        unknown = self.base / 'unknown'
        unknown.mkdir()

        def unsigned(root):
            return exchange_frame(self.state.socket, canonical_json({'protocol': 1, 'op': 'start_status',
                                                                     'project_root': str(root)}),
                                  timeout=2.0, request_limit=65_536, response_limit=16_384)
        replies = {unsigned(self.root('broken')), unsigned(unknown)}
        self.assertEqual(1, len(replies), replies)
        self.assertFalse(json.loads(replies.pop())['ok'])
        code, report = self.cli('learning-status', 'broken')
        self.assertEqual(self.status('broken'), report['recovery'])
        self.assertNotIn('orc_', report['recovery'])  # a plain line, no hold or result content


class StrandedTests(StarterBase):
    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_claims_left_by_a_dead_collector_are_reconciled(self):
        self.add('dead_child')
        self.add('live_child')
        finished = subprocess.Popen(['/bin/sh', '-c', 'exit 0'])
        finished.wait()
        survivor = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
        self.held.append(survivor)
        now = self.now_ms()
        with self.offline() as ledger:
            ledger.mark_wakes_due([(self.projects[name][1], str(self.root(name)), None, None)
                                   for name in ('dead_child', 'live_child')], now_ms=now)
            claims = {item.project_id: item for item in ledger.claim_due_starts(now_ms=now, limit=64)}
            self.assertEqual({self.projects['dead_child'][1], self.projects['live_child'][1]}, set(claims))
            ledger.set_start_child(self.projects['dead_child'][1], claims[self.projects['dead_child'][1]].attempt,
                                   finished.pid)
            ledger.set_start_child(self.projects['live_child'][1], claims[self.projects['live_child'][1]].attempt,
                                   survivor.pid)
        kept = self.starts('live_child')['attempt']
        self.launch(constants={'START_TIMEOUT_SECONDS': 30.0, 'START_BACKOFF_FIRST_SECONDS': 0.2},
                    store={'WORKER_RESULT_SWEEP_SECONDS': 1.0})
        wait_for(self, lambda: self.serving('dead_child'), 30, 'the dead child\'s claim was never released')
        time.sleep(1.5)
        self.assertEqual(kept, self.starts('live_child')['attempt'])  # its surviving child keeps it
        self.assertEqual('recovery: starting', self.status('live_child'))
        survivor.kill()
        survivor.wait(5)
        wait_for(self, lambda: self.serving('live_child'), 30, 'never started once its child ended')

    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_a_host_that_does_not_serve_in_time_is_killed_at_twice_the_timeout(self):
        self.add('slow')
        self.launch(constants={'START_TIMEOUT_SECONDS': 0.02, 'START_BACKOFF_FIRST_SECONDS': 60.0})
        row = wait_for(self, lambda: (self.starts('slow') or {}).get('outcome') == 'failed' and self.starts('slow'),
                       20, f'never failed: {self.starts("slow")}')
        self.assertEqual('start_timeout', row['reason'])
        pid = self.events('spawned')[0]['pid']
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)  # its whole process group is gone
        self.assertEqual([], self.host_pids())

    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_a_killed_service_starts_and_a_restart_keeps_the_backoff(self):
        from tests.test_e2a_wake_wire import KILLER
        self.add('killed', record=False)
        self.add('broken')
        self.break_store('broken')
        # A short probation, served out before the kill below: a host killed within its probation counts failed
        # (E2c-4, kept across the restart since review round 1), and this test is about an unknown project.
        constants = {'START_BACKOFF_FIRST_SECONDS': 300.0, 'START_PROBATION_SECONDS': 1.0}
        self.launch(constants=constants)
        root, project_id, copy = self.projects['killed']
        done = subprocess.run([sys.executable, *self.python_flags('-I'), '-c', KILLER, str(ORCHESTRATION_ROOT), str(self.state.root),
                               str(root), project_id, 'after_commit'], capture_output=True, text=True, timeout=60,
                              env={'PATH': os.defpath, 'TMPDIR': SHORT_TMP})
        self.assertEqual(-signal.SIGKILL, done.returncode, done.stderr)
        # The killer's hold is a bare test row no real service can settle (E2a removes it the same way); the
        # ledger keeps the killed service's `pending` wake, due at its hold's time.
        connection = sqlite3.connect(str(self.state.root / 'projects' / project_id / 'history.sqlite3'))
        with connection:
            connection.execute("DELETE FROM project_holds WHERE identity='orc_kill'")
        connection.close()
        self.assertEqual(('pending', 123_456), (self.wake('killed')['state'], self.wake('killed')['due_at_ms']))
        CollectorClient(self.state.socket, timeout=30).record_launch(root, str(copy), copy.name)
        wait_for(self, lambda: self.serving('killed'), 30,
                 lambda: f'the killed service was never started: {self.starts("killed")} {self.wake("killed")}')
        before = wait_for(self, lambda: (self.starts('broken') or {}).get('failures') == 1 and self.starts('broken'),
                          20, 'no failure counted')
        wait_for(self, lambda: self.starts('killed')['started_at_ms'] is not None
                 and self.starts('killed')['attempt'] is None and self.starts('killed')['probation_attempt'] is None,
                 20, lambda: f'the probation never ended: {self.starts("killed")}')
        self.stop_collector()  # first, so no probation watcher sees the kill below
        for pid in self.host_pids():
            os.kill(pid, signal.SIGKILL)  # its state is now unknown (exact, not serving)
        self.launch(constants=constants)
        wait_for(self, lambda: self.serving('killed'), 30,
                 lambda: f'the unknown project was not reconciled and started: {self.starts("killed")} '
                 f'{self.wake("killed")}')
        after = self.starts('broken')
        self.assertEqual((1, before['next_start_at_ms'], before['streak_first_ms']),
                         (after['failures'], after['next_start_at_ms'], after['streak_first_ms']))
        self.assertTrue(self.status('broken').startswith('recovery: backing off (1 failed: '))


class StopTests(StarterBase):
    @requirements('E2c-11', why_not_scenario=SCENARIO)
    def test_E2c_11_a_stop_with_the_collector_s_intake_closed_is_not_delayed(self):
        self.add('alpha')
        self.launch()
        wait_for(self, lambda: self.serving('alpha'), 30, 'alpha never served')
        pids = self.host_pids()
        self.assertEqual(1, len(pids))
        self.stop_collector()
        began = time.monotonic()
        os.kill(pids[0], signal.SIGTERM)
        wait_for(self, lambda: not self.host_pids(), 10, 'the host did not stop')
        self.assertLess(time.monotonic() - began, 2.0 + 3.0)


class LedgerStartTests(unittest.TestCase):
    """The claim, backoff and park rules on a real ledger with the product's constants (E2c-2, E2c-4)."""

    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.ledger.record_launch(self.project, self.copy('a' * 64), 'a' * 64)
        self.ledger.mark_wakes_due([(self.project, str(self.fixture.project_a), None, None)], now_ms=1_000)

    def copy(self, digest):
        return str(Path('/var/empty/owner/.codex/agent-orchestration')
                   / host_commands.project_digest(Path(self.fixture.project_a).resolve()) / digest)

    def fail_once(self, now_ms):
        claims = self.ledger.claim_due_starts(now_ms=now_ms, limit=64)
        self.assertEqual([self.project], [item.project_id for item in claims])
        return self.ledger.finish_start(self.project, claims[0].attempt, outcome='failed',
                                        reason='history_unavailable', now_ms=now_ms, **project_start.start_policy())

    @requirements('E2c-4', why_not_scenario='The ledger rule with the product constants and a controlled clock.')
    def test_E2c_4_backoff_doubles_to_its_cap_and_parks_once_the_streak_is_an_hour_old(self):
        now, waits = 1_000, []
        while True:
            row = self.fail_once(now)
            if row['parked_reason'] is not None:
                break
            waits.append(row['next_start_at_ms'] - now)
            self.assertEqual([], self.ledger.claim_due_starts(now_ms=row['next_start_at_ms'] - 1, limit=64))
            now = row['next_start_at_ms']
        self.assertEqual([30_000, 60_000, 120_000, 240_000, 480_000, 900_000], waits[:6])
        self.assertTrue(all(wait == 900_000 for wait in waits[6:]))
        self.assertGreaterEqual(now - 1_000, 3_600_000)
        self.assertLess(now - 1_000 - waits[-1], 3_600_000)  # the failure before it was inside the hour
        self.assertEqual('history_unavailable', row['parked_reason'])
        later = now + 10 * 3_600_000
        self.assertEqual([], self.ledger.claim_due_starts(now_ms=later, limit=64))
        # A publish (the project reporting again) does not unpark it or reset its failures.
        first = self.ledger.publish_wake(self.project, epoch=None, seq=1, state='pending', next_due_at_ms=None)
        self.ledger.publish_wake(self.project, epoch=first['epoch'], seq=2, state='exact', next_due_at_ms=5)
        self.assertEqual([], self.ledger.claim_due_starts(now_ms=later, limit=64))
        # A new launch digest (setup ran again) unparks it with a fresh streak.
        self.ledger.record_launch(self.project, self.copy('b' * 64), 'b' * 64)
        claims = self.ledger.claim_due_starts(now_ms=later, limit=64)
        self.assertEqual([self.project], [item.project_id for item in claims])
        row = self.ledger.finish_start(self.project, claims[0].attempt, outcome='failed', reason='history_unavailable',
                                       now_ms=later, **project_start.start_policy())
        self.assertEqual((1, later + 30_000, None), (row['failures'], row['next_start_at_ms'], row['parked_reason']))

    @requirements('E2c-4', why_not_scenario='The ledger rule with the product constants and a controlled clock.')
    def test_E2c_4_the_owner_unpark_and_a_lasting_start_end_the_streak(self):
        now, row = 1_000, {'parked_reason': None}
        while row['parked_reason'] is None:
            row = self.fail_once(now)
            now = row['next_start_at_ms'] or now
        self.ledger.unpark_start(self.project, now_ms=now)
        row = self.fail_once(now)
        self.assertEqual((1, None), (row['failures'], row['parked_reason']))
        claims = self.ledger.claim_due_starts(now_ms=row['next_start_at_ms'], limit=64)
        self.ledger.finish_start(self.project, claims[0].attempt, outcome='started', reason=None,
                                 now_ms=row['next_start_at_ms'], **project_start.start_policy())
        row = self.ledger.start_probation(self.project, survived=True, now_ms=row['next_start_at_ms'] + 60_000,
                                          **project_start.start_policy())
        self.assertEqual((0, None), (row['failures'], row['streak_first_ms']))

    @requirements('E2c-2', why_not_scenario='The claim rule on a real ledger.')
    def test_E2c_2_a_claim_is_one_per_project_until_it_ends(self):
        claims = self.ledger.claim_due_starts(now_ms=2_000, limit=64)
        self.assertEqual(1, len(claims))
        self.assertRegex(claims[0].attempt, r'^[0-9a-f]{32}$')
        self.assertEqual([], self.ledger.claim_due_starts(now_ms=3_000, limit=64))  # outstanding
        self.ledger.finish_start(self.project, 'f' * 32, outcome='started', reason=None, now_ms=3_000,
                                 **project_start.start_policy())  # another attempt's end changes nothing
        self.assertEqual([], self.ledger.claim_due_starts(now_ms=3_000, limit=64))


class BudgetTests(unittest.TestCase):
    """E2c-3 on a real ledger and a real in-process collector (decision 150's budgets)."""

    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger

    def bulk_projects(self, count, *, due_ms, prefix):
        """`count` active projects (as registration writes them) with wake rows due at `due_ms` (None: settled)."""
        bulk_projects(self.ledger, self.fixture.project_a_id, count, due_ms=due_ms, prefix=prefix)

    def timed_claims(self, now_ms):
        durations, claimed = [], 0
        while True:
            began = time.perf_counter()
            batch = self.ledger.claim_due_starts(now_ms=now_ms, limit=64)
            durations.append(time.perf_counter() - began)
            if not batch:
                return durations, claimed
            claimed += len(batch)
            self.assertLessEqual(len(batch), 64)

    @requirements('E2c-10', why_not_scenario='The stranded-claim pass on a real ledger; no process boundary.')
    def test_E2c_10_the_stranded_pass_reaches_every_claim_past_one_page(self):
        import types
        self.bulk_projects(70, due_ms=1, prefix='s')
        claimed = self.ledger.claim_due_starts(now_ms=10, limit=64) + self.ledger.claim_due_starts(now_ms=10, limit=64)
        self.assertEqual(70, len(claimed))  # a dead collector's claims, none with a child
        server = types.SimpleNamespace(ledger=self.ledger, state=self.fixture.state, _sweeps_stop=threading.Event())
        project_start.ProjectStarter(server)._reconcile_stranded()
        self.assertEqual([], self.ledger.outstanding_starts(limit=64))

    @requirements('E2c-4', why_not_scenario='The kept-probation pass on a real ledger; no process boundary.')
    def test_E2c_4_the_probation_pass_reaches_every_probation_past_one_page(self):
        import types
        self.bulk_projects(70, due_ms=1, prefix='p')
        claimed = self.ledger.claim_due_starts(now_ms=10, limit=64) + self.ledger.claim_due_starts(now_ms=10, limit=64)
        self.assertEqual(70, len(claimed))
        for claim in claimed:  # a dead collector's started hosts, each within its probation
            self.ledger.finish_start(claim.project_id, claim.attempt, outcome='started', reason=None, now_ms=10,
                                     child_pid=2, **project_start.start_policy())
        server = types.SimpleNamespace(ledger=self.ledger, state=self.fixture.state, _sweeps_stop=threading.Event())
        starter = project_start.ProjectStarter(server)
        starter._reconcile_probations()
        self.assertEqual({claim.project_id for claim in claimed}, set(starter._foreign))
        self.assertEqual([], self.ledger.claim_due_starts(now_ms=20, limit=64))  # none claimed within probation

    @requirements('E2c-3', why_not_scenario='Lock and batch budgets measured on a real ledger.')
    def test_E2c_3_claim_batches_are_short_indexed_and_flat_at_100000_settled_rows(self):
        self.bulk_projects(1_000, due_ms=1, prefix='d')
        plan = ' '.join(str(row[-1]) for row in self.ledger._connection.execute(
            'EXPLAIN QUERY PLAN ' + learning_store.START_CLAIM_SQL, (10, 10, 64)))
        self.assertIn('project_wake_due', plan)
        durations, claimed = self.timed_claims(10)
        self.assertEqual(1_000, claimed)
        self.assertLess(max(durations), 0.050, durations)
        self.bulk_projects(100_000, due_ms=None, prefix='t')
        with self.ledger._lock:
            self.ledger._connection.execute('UPDATE project_starts SET attempt=NULL,claimed_at_ms=NULL')
        settled, claimed = self.timed_claims(10)
        self.assertEqual(1_000, claimed)
        self.assertLess(max(settled), 0.050, settled)
        self.assertLess(sum(settled), 3 * sum(durations) + 0.050)

    @requirements('E2c-3', why_not_scenario='A real collector (in process, its own starter thread) over a real ledger.')
    def test_E2c_3_a_collector_drains_1000_due_projects_within_60_seconds_and_starts_no_later(self):
        self.bulk_projects(1_000, due_ms=1, prefix='d')
        holds = []
        real = self.ledger.claim_due_starts

        def claim(**options):
            began = time.perf_counter()
            try:
                return real(**options)
            finally:
                holds.append(time.perf_counter() - began)
        timings = {}
        for label in ('settled', 'due'):
            if label == 'due':
                with self.ledger._lock:
                    self.ledger._connection.execute('UPDATE project_wake SET due_at_ms=1')
            else:
                with self.ledger._lock:
                    self.ledger._connection.execute("UPDATE project_wake SET due_at_ms=NULL,state='clean_stop',"
                                                    "epoch=1,seq=1,allocated_epoch=1")
            server = learning_store.CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
            # Review round 1 (HIGH 1, MED 2): the product's own intervals, unchanged.
            with mock.patch.object(self.ledger, 'claim_due_starts', side_effect=claim):
                began = time.perf_counter()
                server.start()
                timings[label] = time.perf_counter() - began
                try:
                    if label == 'due':
                        end = time.monotonic() + 60
                        while time.monotonic() < end:
                            with self.ledger._lock:
                                left = self.ledger._connection.execute(
                                    'SELECT COUNT(*) FROM project_wake w LEFT JOIN project_starts s '
                                    'ON s.project_id=w.project_id WHERE w.due_at_ms IS NOT NULL '
                                    'AND (s.project_id IS NULL OR s.attempt IS NOT NULL)').fetchone()[0]
                            if left == 0:
                                break
                            time.sleep(0.1)
                        self.assertEqual(0, left)
                finally:
                    server.stop()
        self.assertLess(timings['due'], timings['settled'] + 0.200, timings)
        self.assertTrue(holds)
        self.assertLess(max(holds), 0.750)


def update_ledger(test, sql, parameters):
    """A direct write to the account ledger beside the running collector (test setup only)."""
    connection = sqlite3.connect(str(test.state.database), timeout=10)
    try:
        with connection:
            connection.execute(sql, parameters)
    finally:
        connection.close()


class OtherVersionTests(StarterBase):
    def older_runtime(self, version='0.8.1'):
        """This package installed as an earlier release would be: a lower manifest version and one runtime file
        different, so its runtime version and digest are another runtime's."""
        import hashlib
        old = install_package(self.base / 'older').resolve()
        orchestration = old / '.codex' / 'orchestration'
        target = orchestration / 'codex_agents' / '__init__.py'
        target.write_bytes(target.read_bytes() + b'\n# an earlier build\n')
        manifest_path = orchestration / 'install-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['version'] = version
        manifest['files']['.codex/orchestration/codex_agents/__init__.py'] = hashlib.sha256(
            target.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return orchestration

    @requirements('E2c-7', why_not_scenario=SCENARIO)
    def test_E2c_7_a_real_host_of_an_earlier_runtime_is_not_replaced(self):
        """Review round 1, MED 2: the competing host is a real project host process of another runtime."""
        from codex_agents.project_host_client import other_version_host
        root, _project_id, copy = self.add('alpha', record=False)
        older = self.older_runtime()
        # The earlier runtime was the authorized one when its service started (as before an update).
        self.state.collector_authorization.unlink()
        done = subprocess.run(  # authorized by that runtime's own code, as its registration did
            [sys.executable, '-I', '-B', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; '
             'from codex_agents.learning_store import LearningState, authorize_collector_runtime; '
             'authorize_collector_runtime(LearningState(Path(sys.argv[2]), create=False), Path(sys.argv[1]) / "cli.py")',
             str(older), str(self.state.root)], env={'PATH': os.defpath, 'TMPDIR': SHORT_TMP},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stderr[-2000:])
        self.launch(orchestration=older)
        host = subprocess.Popen(
            [sys.executable, '-I', '-B', str(older / 'project_host.py'), '--state-root', str(self.state.root),
             '--project-root', str(root)], env={'PATH': os.defpath, 'TMPDIR': SHORT_TMP},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.held.append(host)
        wait_for(self, lambda: other_version_host(self.state.root, root) is not None, 30,
                 lambda: f'the earlier runtime\'s host never served (exit {host.poll()})')
        self.stop_collector()
        # The update: this runtime is authorized and its collector runs; setup records the launch.
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        started = self.launch()['started_at_ms']
        CollectorClient(self.state.socket, timeout=30).record_launch(root, str(copy), copy.name)
        expected = 'recovery: needs update handover: ' + self.command('alpha')
        wait_for(self, lambda: self.status('alpha') == expected, 20, lambda: f'status: {self.status("alpha")}')
        time.sleep(1.5)  # rechecked, still not replaced
        self.assertIsNone(host.poll())  # the earlier runtime's host still serves
        self.assertIsNotNone(other_version_host(self.state.root, root))
        self.assertEqual([], self.events('spawned'))
        self.assertIsNone(self.collector.poll())
        self.assertEqual(started, CollectorClient(self.state.socket, timeout=2).health()['started_at_ms'])  # no stop


class OwnershipTests(StarterBase):
    """Review round 1, HIGH 2: a spawned child stays its start's until it ends; ledger refusals end no worker."""

    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_a_child_whose_pid_record_is_refused_is_still_timed_killed_and_reaped(self):
        self.add('slow')
        self.launch(constants={'START_TIMEOUT_SECONDS': 0.02, 'START_BACKOFF_FIRST_SECONDS': 60.0},
                    fail={'set_start_child': 1000})
        row = wait_for(self, lambda: (self.starts('slow') or {}).get('outcome') == 'failed' and self.starts('slow'),
                       20, lambda: f'never failed: {self.starts("slow")} {self.events()[-6:]}')
        self.assertEqual('start_timeout', row['reason'])
        self.assertTrue(self.events('refused'))
        spawned = self.events('spawned')
        self.assertEqual(1, len(spawned), spawned)  # its slot was held: no second start beside it
        with self.assertRaises(ProcessLookupError):
            os.killpg(spawned[0]['pid'], 0)  # killed and reaped (a zombie would still answer)
        self.assertEqual([], self.host_pids())
        ended = self.events('end', 'slow')
        self.assertEqual(1, len(ended))

    @requirements('E2c-2', why_not_scenario=SCENARIO)
    def test_E2c_2_refused_ledger_steps_are_retried_and_no_worker_ends(self):
        names = ('alpha', 'bravo', 'charlie', 'delta')
        for name in names:
            self.add(name, record=False)
        self.launch(fail={'finish_start': 4, 'release_start': 4})
        for name in names:
            wait_for(self, lambda: (self.starts(name) or {}).get('outcome') == 'no_record'
                     and self.starts(name)['attempt'] is None, 20, lambda: f'{name}: {self.starts(name)}')
        self.assertGreaterEqual(len(self.events('refused')), 4)
        # Both workers still run: every project is looked at again after its recheck time.
        for name in names:
            wait_for(self, lambda: len(self.events('begin', name)) >= 2, 20,
                     lambda: f'{name} was never looked at again: {self.events()[-8:]}')


class SlotTests(StarterBase):
    @requirements('E2c-2', 'E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_2_surviving_stranded_children_hold_their_slots(self):
        """Review round 1, HIGH 3: never more than START_SLOTS starts across a collector restart."""
        for name in ('one', 'two', 'fresh'):
            self.add(name)
        survivors = {}
        for name in ('one', 'two'):
            survivors[name] = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
            self.held.append(survivors[name])
        now = self.now_ms()
        with self.offline() as ledger:
            ledger.mark_wakes_due([(self.projects[name][1], str(self.root(name)), None, None)
                                   for name in ('one', 'two')], now_ms=now)
            claims = {item.project_id: item for item in ledger.claim_due_starts(now_ms=now, limit=64)}
            self.assertEqual({self.projects['one'][1], self.projects['two'][1]}, set(claims))
            for name in ('one', 'two'):
                ledger.set_start_child(self.projects[name][1], claims[self.projects[name][1]].attempt,
                                       survivors[name].pid)
        self.launch(constants={'START_TIMEOUT_SECONDS': 30.0, 'START_BACKOFF_FIRST_SECONDS': 60.0},
                    store={'WORKER_RESULT_SWEEP_SECONDS': 1.0})
        time.sleep(3.0)
        self.assertEqual([], self.events('begin'))  # both slots are held by the surviving children
        self.assertEqual('recovery: starting', self.status('one'))
        survivors['one'].kill()
        survivors['one'].wait(5)
        wait_for(self, lambda: self.events('begin', 'fresh'), 10, 'no start once a slot was free')
        wait_for(self, lambda: self.serving('fresh'), 30, 'fresh never served')
        self.assertEqual(('failed', 'service_ended'), (self.starts('one')['outcome'], self.starts('one')['reason']))
        self.assertEqual('recovery: starting', self.status('two'))
        self.assertEqual(['fresh'], [self.name_of(row['project']) for row in self.events('begin')])


class ProbationTests(StarterBase):
    """Review round 1, HIGH 4: a started host's probation is kept with its attempt and child."""

    @requirements('E2c-4', why_not_scenario=SCENARIO)
    def test_E2c_4_a_serving_recheck_never_replaces_an_active_probation(self):
        self.add('alpha')
        self.launch(constants={'START_PROBATION_SECONDS': 30.0, 'START_BACKOFF_FIRST_SECONDS': 60.0})
        wait_for(self, lambda: (self.starts('alpha') or {}).get('outcome') == 'started'
                 and self.starts('alpha')['attempt'] is None, 30, lambda: f'never started: {self.starts("alpha")}')
        # The project falls due again while its host serves (a hold of its falls due): a serving recheck.
        update_ledger(self, 'UPDATE project_wake SET due_at_ms=? WHERE project_id=?',
                      (self.now_ms() - 1_000, self.projects['alpha'][1]))
        time.sleep(1.5)
        self.assertEqual('started', self.starts('alpha')['outcome'])
        self.assertTrue(self.status('alpha').startswith('recovery: started '), self.status('alpha'))
        for pid in self.host_pids('alpha'):
            os.kill(pid, signal.SIGKILL)  # it ends within its probation
        row = wait_for(self, lambda: self.starts('alpha')['failures'] == 1 and self.starts('alpha'), 20,
                       lambda: f'the early end was not counted: {self.starts("alpha")}')
        self.assertEqual('service_ended_early', row['reason'])

    @requirements('E2c-4', 'E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_4_a_probation_is_kept_across_a_collector_restart(self):
        self.add('lasts')
        root, _project_id, copy = self.add('dies', record=False)
        constants = {'START_PROBATION_SECONDS': 10.0, 'START_BACKOFF_FIRST_SECONDS': 60.0}
        self.launch(constants=constants)

        def started(name):
            row = self.starts(name) or {}
            return row.get('outcome') == 'started' and row.get('attempt') is None and row

        lasts = wait_for(self, lambda: started('lasts'), 30, lambda: f'never started: {self.starts("lasts")}')
        self.assertIsNotNone(lasts['probation_pid'])
        time.sleep(max(0.0, (lasts['started_at_ms'] + 6_000 - self.now_ms()) / 1000))
        CollectorClient(self.state.socket, timeout=30).record_launch(root, str(copy), copy.name)
        dies = wait_for(self, lambda: started('dies'), 30, lambda: f'never started: {self.starts("dies")}')
        self.stop_collector()  # both hosts are within their probation and keep serving
        for pid in self.host_pids('dies'):
            os.kill(pid, signal.SIGKILL)  # ended while no collector watched it
        # 'lasts' had a failure streak before this start; its probation ends while no collector runs.
        update_ledger(self, 'UPDATE project_starts SET failures=2,streak_first_ms=? WHERE project_id=?',
                      (self.now_ms() - 60_000, self.projects['lasts'][1]))
        time.sleep(max(0.0, (lasts['probation_until_ms'] + 500 - self.now_ms()) / 1000))
        self.assertLess(self.now_ms(), dies['probation_until_ms'] - 2_000)
        self.launch(constants=constants)
        row = wait_for(self, lambda: self.starts('dies')['failures'] == 1 and self.starts('dies'), 20,
                       lambda: f'the early end was not counted after the restart: {self.starts("dies")}')
        self.assertEqual('service_ended_early', row['reason'])
        row = wait_for(self, lambda: self.starts('lasts')['failures'] == 0 and self.starts('lasts'), 20,
                       lambda: f'the served-out probation did not end the streak: {self.starts("lasts")}')
        self.assertEqual((None, None), (row['streak_first_ms'], row['probation_attempt']))
        self.assertTrue(self.serving('lasts'))


class OwnerCommandTests(StarterBase):
    @requirements('E2c-4', why_not_scenario=SCENARIO)
    def test_E2c_4_the_owner_start_command_unparks_with_the_collector_down(self):
        """Review round 1, MED 1: the compatible collector first, then the owner-signed unpark, then the start."""
        self.add('broken')
        self.break_store('broken')
        project_id = self.projects['broken'][1]
        with self.offline() as ledger:  # parked by the ledger's own rule while no collector runs
            now = self.now_ms() - 2 * 3_600_000
            ledger.mark_wakes_due([(project_id, str(self.root('broken')), None, None)], now_ms=now)
            while True:
                claims = ledger.claim_due_starts(now_ms=now, limit=64)
                self.assertEqual([project_id], [item.project_id for item in claims])
                row = ledger.finish_start(project_id, claims[0].attempt, outcome='failed', reason='history_unavailable',
                                          now_ms=now, **project_start.start_policy())
                if row['parked_reason'] is not None:
                    break
                now = row['next_start_at_ms']
        parked_failures = row['failures']
        self.addCleanup(self.stop_cli_collector)
        with self.assertRaises(Exception):
            CollectorClient(self.state.socket, timeout=2).health()  # the collector is down
        self.cli('learning-start-project-service', 'broken')
        row = self.starts('broken')
        self.assertIsNone(row['parked_reason'], row)
        self.assertLess(row['failures'], parked_failures, row)  # its streak ended; any failure since is a new one

    def stop_cli_collector(self):
        """The collector the CLI started (not this test's launcher): its own drained stop, then wait for it."""
        for extra in ({'runtime_digest': COLLECTOR_RUNTIME_DIGEST}, {}):
            try:
                exchange_frame(self.state.socket, canonical_json({'protocol': 1, 'op': 'shutdown_if_drained', **extra}),
                               timeout=3.0, request_limit=4096, response_limit=16_384)
            except Exception:
                pass
        end = time.monotonic() + 15
        while self.state.socket.exists() and time.monotonic() < end:
            time.sleep(0.1)


class TrustBoundaryTests(StarterBase):
    @requirements('E2c-6', why_not_scenario=SCENARIO)
    def test_E2c_6_location_refusals_hold_in_the_product_path(self):
        """Review round 1, MED 2: the fixture trusts its temporary home; without that, the product refuses it."""
        root, _project_id, copy = self.add('alpha')
        self.launch(trust_temp=False)  # only the account home is set: temporary directories stay untrusted
        wait_for(self, lambda: self.status('alpha') == 'recovery pending: install changed', 20,
                 lambda: f'status: {self.status("alpha")}')
        self.assertEqual([], self.events('spawned'))
        self.assertEqual([], self.host_pids())
        with mock.patch.object(host_commands, 'account_home', lambda: self.home):
            self.assertIsNotNone(host_commands.launch_target_problem(copy, root, {}))
            inside = root / '.codex-copy' / copy.name  # a copy inside the project itself
            self.assertIsNotNone(host_commands.launch_target_problem(inside, root, {}))


class ProductionIntervalTests(StarterBase):
    """E2c-1/E2c-3 with the product's own intervals (review round 1, HIGH 1 and MED 2)."""
    CONSTANTS = {}

    @requirements('E2c-3', 'E2c-1', why_not_scenario=SCENARIO)
    def test_E2c_3_1000_due_projects_with_launch_records_drain_within_60_seconds(self):
        served = [f'real{index}' for index in range(4)]
        for name in served:
            self.add(name)
        changed = [f'changed{index}' for index in range(16)]
        for name in changed:  # full verification, refused at its last step: the project's runtime changed
            root, _id, _copy = self.add(name)
            installed = root / '.codex' / 'orchestration' / 'codex_agents' / '__init__.py'
            installed.write_bytes(installed.read_bytes() + b'\n# changed\n')

        def missing_copy(root):  # a launch record whose copy is gone (removed since setup)
            digest = format(abs(hash(root)), 'x').rjust(64, '0')[:64]
            return (str(self.home / '.codex' / host_commands.RUNTIME_CONTAINER
                        / host_commands.project_digest(Path(root)) / digest), digest)
        with self.offline() as ledger:
            bulk_projects(ledger, self.projects['real0'][1], 1_000 - len(served) - len(changed), due_ms=1,
                          prefix='d', copy_for=missing_copy)
        self.launch()
        ready = time.monotonic()
        end = ready + 60.0
        query = ('SELECT COUNT(*) FROM projects p LEFT JOIN project_starts s ON s.project_id=p.project_id '
                 'WHERE s.project_id IS NULL OR s.attempt IS NOT NULL OR s.outcome IS NULL')
        while True:
            connection = sqlite3.connect(self.state.database.as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                left = connection.execute(query).fetchone()[0]
            finally:
                connection.close()
            if left == 0 or time.monotonic() > end:
                break
            time.sleep(0.25)
        drained = time.monotonic() - ready
        self.assertEqual(0, left, f'{left} of 1000 projects not drained in {drained:.1f} s')
        self.assertLess(drained, 60.0)
        for name in served:
            self.assertEqual('started', self.starts(name)['outcome'], name)
            self.assertTrue(self.serving(name), name)
        for name in changed:
            self.assertEqual('install_changed', self.starts(name)['outcome'], name)
        self.assertEqual(4, len(self.events('spawned')))
        holds = [row['seconds'] for row in self.events('claim')]
        self.assertTrue(holds)
        self.assertLess(max(holds), 0.750, holds)
        first = min(row['t'] for row in self.events('begin'))
        # E2c-1 at production intervals: within one STARTER_CHECK_SECONDS (one tick of slack, as T1).
        self.assertLessEqual(first - ready, project_start.STARTER_CHECK_SECONDS + project_start.STARTER_TICK_SECONDS)
        print(f'\nE2c-3 production drain: {drained:.1f} s, {len(holds)} claims, longest {max(holds) * 1000:.1f} ms',
              file=sys.stderr)


class FairnessTests(unittest.TestCase):
    """E2c-2 round robin on a real ledger with the real starter's claim step (review round 1, HIGH 1)."""

    def setUp(self):
        self.fixture = store_tests.LearningStoreTests(methodName='runTest')
        with mock.patch.object(store_tests, 'TEMP_ROOT', SHORT_TMP):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        import types
        self.server = types.SimpleNamespace(ledger=self.ledger, state=self.fixture.state,
                                            _sweeps_stop=threading.Event(), sweep_error=None)

    @requirements('E2c-2', why_not_scenario='The claim order on a real ledger; no process boundary.')
    def test_E2c_2_a_retry_never_goes_ahead_of_a_project_not_yet_tried(self):
        bulk_projects(self.ledger, self.fixture.project_a_id, 200, due_ms=1, prefix='f')
        starter = project_start.ProjectStarter(self.server)
        policy = {**project_start.start_policy(), 'backoff_first_ms': 1, 'backoff_max_ms': 1}
        order = []
        for _ in range(12):
            starter._claim()
            while starter._queue:
                claim = starter._queue.popleft()
                starter._queued.discard(claim.project_id)
                order.append(claim.project_id)
                self.ledger.finish_start(claim.project_id, claim.attempt, outcome='failed', reason='service_ended',
                                         now_ms=time.time_ns() // 1_000_000, **policy)  # due again at once
            time.sleep(0.002)
        due = {row[0] for row in self.ledger._connection.execute(
            'SELECT project_id FROM project_wake WHERE due_at_ms IS NOT NULL')}
        self.assertGreaterEqual(len(order), len(due))
        self.assertEqual(due, set(order[:len(due)]))  # every due project once before any is tried again

    @requirements('E2c-2', why_not_scenario='The starter past its queue and slot limits on a real ledger.')
    def test_E2c_2_past_the_queue_and_slot_limits_every_due_project_is_still_started(self):
        bulk_projects(self.ledger, self.fixture.project_a_id, 200, due_ms=1, prefix='q')
        server = learning_store.CollectorServer(self.ledger, client_capabilities=self.fixture.clients)
        with mock.patch.object(project_start, 'START_QUEUE_LIMIT', 16):
            server.start()
            try:
                end = time.monotonic() + 30
                while time.monotonic() < end:
                    with self.ledger._lock:
                        left = self.ledger._connection.execute(
                            "SELECT COUNT(*) FROM project_wake w LEFT JOIN project_starts s "
                            "ON s.project_id=w.project_id WHERE w.due_at_ms IS NOT NULL "
                            "AND (s.project_id IS NULL OR s.attempt IS NOT NULL OR s.outcome IS NULL)").fetchone()[0]
                    if left == 0:
                        break
                    time.sleep(0.05)
                starter = server._starter
            finally:
                server.stop()
        self.assertEqual(0, left)  # 200 projects through a queue of 16 and 2 slots
        self.assertLessEqual(starter.most_queued, 16)
        self.assertLessEqual(starter.most_busy, project_start.START_SLOTS)
        self.assertEqual(200, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM project_starts WHERE outcome='no_record' AND project_id LIKE 'prj_q%'").fetchone()[0])



def group_alive(pid):
    try:
        os.killpg(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False  # not this user's: the child (this user's) is gone and its pid was reused
    return True


class RecordRefusedTests(StarterBase):
    """Review round 2 (decision 243, HIGH 2): a spawned child is never lost. While the ledger refuses its pid record,
    the claim stays until the child is proven gone, through a clean stop and through the collector's loss."""

    TIMEOUT = 2.0

    def spawn_unrecorded(self):
        self.add('alpha')
        unserved = self.base / 'unserved'
        unserved.touch()  # the host runs but the starter does not see it serve: it stays a starting child
        self.launch(constants={'START_TIMEOUT_SECONDS': self.TIMEOUT, 'START_BACKOFF_FIRST_SECONDS': 60.0},
                    fail={'set_start_child': 10 ** 6}, unserved=str(unserved))
        spawned = wait_for(self, lambda: self.events('spawned'), 20, 'alpha was never spawned')
        wait_for(self, lambda: self.events('refused'), 10, lambda: f'the pid record was not refused: {self.events()[-6:]}')
        time.sleep(1.0)  # well past the pid record's first tries
        row = self.starts('alpha')
        self.assertIsNotNone(row['attempt'])
        self.assertIsNone(row['child_pid'])  # refused: the ledger does not know the child
        self.assertTrue(group_alive(spawned[0]['pid']))
        return spawned[0]['pid'], row['attempt'], unserved

    def assert_kept_until_gone(self, pid, attempt, seconds):
        """Until the claim `attempt` ends, the child's group may live; once it ended, the child is gone."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            alive = group_alive(pid)
            row = self.starts('alpha')
            if row['attempt'] != attempt:
                self.assertFalse(alive and group_alive(pid),
                                 f'the claim ended while its child still ran: {row} {self.events()[-6:]}')
                return row
            time.sleep(0.02)
        self.fail(f'the claim never ended: {self.starts("alpha")}')

    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_a_clean_stop_never_releases_an_unrecorded_live_child_s_claim(self):
        pid, attempt, _unserved = self.spawn_unrecorded()
        self.stop_collector()
        row = self.starts('alpha')
        if row['attempt'] == attempt:
            return  # kept: a later collector reconciles it
        self.assertFalse(group_alive(pid), f'released while its child still runs: {row}')
        self.assertEqual([], self.host_pids())

    @requirements('E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_10_after_the_collector_s_loss_an_unrecorded_child_keeps_its_claim_until_it_is_gone(self):
        pid, attempt, unserved = self.spawn_unrecorded()
        self.collector.kill()  # lost: no stop step runs
        self.collector.wait(10)
        self.collector = None
        self.assertTrue(group_alive(pid))
        self.assertEqual(attempt, self.starts('alpha')['attempt'])
        self.launch(constants={'START_TIMEOUT_SECONDS': self.TIMEOUT, 'START_BACKOFF_FIRST_SECONDS': 60.0},
                    unserved=str(unserved), store={'WORKER_RESULT_SWEEP_SECONDS': 1.0})
        row = self.assert_kept_until_gone(pid, attempt, 4 * self.TIMEOUT + 15)
        self.assertEqual(('failed', 'start_timeout'), (row['outcome'], row['reason']))


class TimeoutSlotTests(StarterBase):
    @requirements('E2c-2', 'E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_2_a_surviving_child_s_slot_is_freed_only_once_it_is_killed_and_gone(self):
        """Review round 2 (decision 243, HIGH 3): never more than START_SLOTS live started children, also when a
        surviving stranded child reaches twice the start timeout."""
        for name in ('one', 'two', 'fresh'):
            self.add(name)
        survivors, exits = {}, {}
        for name in ('one', 'two'):
            survivors[name] = subprocess.Popen(['/bin/sleep', '60'], start_new_session=True)
            self.held.append(survivors[name])

            def reap(name=name):  # as the system reaps an orphan: a killed survivor leaves no zombie behind
                survivors[name].wait()
                exits[name] = time.monotonic()
            threading.Thread(target=reap, daemon=True).start()
        now = self.now_ms()
        with self.offline() as ledger:
            ledger.mark_wakes_due([(self.projects[name][1], str(self.root(name)), None, None)
                                   for name in ('one', 'two')], now_ms=now)
            claims = {item.project_id: item for item in ledger.claim_due_starts(now_ms=now, limit=64)}
            for name in ('one', 'two'):
                ledger.set_start_child(self.projects[name][1], claims[self.projects[name][1]].attempt,
                                       survivors[name].pid)
        self.launch(constants={'START_TIMEOUT_SECONDS': 1.0, 'START_BACKOFF_FIRST_SECONDS': 60.0},
                    store={'WORKER_RESULT_SWEEP_SECONDS': 1.0})
        begun = wait_for(self, lambda: self.events('begin', 'fresh'), 20, 'no start once the survivors timed out')
        for name in ('one', 'two'):
            self.assertIn(name, exits, f'{name} still ran when fresh began: a third live child')
            self.assertLessEqual(exits[name], begun[0]['t'])
            self.assertEqual(('failed', 'start_timeout'), (self.starts(name)['outcome'], self.starts(name)['reason']))
        wait_for(self, lambda: self.serving('fresh'), 30, 'fresh never served')


class ProbationDeadlineTests(StarterBase):
    @requirements('E2c-4', 'E2c-10', why_not_scenario=SCENARIO)
    def test_E2c_4_a_host_that_died_early_is_failed_when_the_restart_comes_after_its_deadline(self):
        """Review round 2 (decision 243, HIGH 4): survival needs the host alive at or after its probation's end."""
        self.add('dies')
        constants = {'START_PROBATION_SECONDS': 3.0, 'START_BACKOFF_FIRST_SECONDS': 60.0}
        self.launch(constants=constants)
        row = wait_for(self, lambda: (self.starts('dies') or {}).get('outcome') == 'started'
                       and self.starts('dies')['attempt'] is None and self.starts('dies'), 30,
                       lambda: f'never started: {self.starts("dies")}')
        self.assertIsNotNone(row['probation_pid'])
        self.stop_collector()  # within its probation
        for pid in self.host_pids('dies'):
            os.kill(pid, signal.SIGKILL)  # it ends early, while no collector watches it
        wait_for(self, lambda: not self.host_pids('dies'), 10, 'the host did not end')
        time.sleep(max(0.0, (row['probation_until_ms'] + 1_000 - self.now_ms()) / 1000))
        self.launch(constants=constants)  # after its probation's deadline
        after = wait_for(self, lambda: self.starts('dies')['probation_attempt'] is None and self.starts('dies'), 20,
                         lambda: f'the kept probation was never reconciled: {self.starts("dies")}')
        self.assertEqual((1, 'service_ended_early'), (after['failures'], after['reason']))


class ReleasedLedgerUpgradeTests(upgrade_tests.Fixture, unittest.TestCase):
    """Review round 2 (decision 243, new HIGH): a ledger the released 0.8.59 runtime created (real processes: a git
    archive of its release commit registers a project and runs its own collector, as tests/test_upgrade_handover.py
    does) opens and works under this build: E2a's launch columns and wake tables and E2c's start table are added by
    an additive, idempotent migration. S2 then adds the machine-local epoch in step 16→17."""

    RELEASED_COMMIT = '227b0c5e'  # 0.8.59
    EXPECTED = {
        'projects': {'launch_copy', 'launch_digest'},
        'project_wake': {'project_id', 'allocated_epoch', 'epoch', 'seq', 'state', 'next_due_at_ms', 'body_digest',
                         'due_at_ms', 'refused', 'updated_at_ms'},
        'project_wake_instances': {'project_id', 'instance', 'epoch'},
        'project_starts': {'project_id', 'attempt', 'claimed_at_ms', 'child_pid', 'failures', 'streak_first_ms',
                           'next_start_at_ms', 'parked_reason', 'parked_digest', 'outcome', 'reason', 'outcome_at_ms',
                           'started_at_ms', 'probation_attempt', 'probation_pid', 'probation_until_ms',
                           'updated_at_ms'},
    }

    @requirements('E2a-1', 'E2c-10', why_not_scenario='Real processes of the released runtime and of this build.')
    def test_E2c_10_a_released_0_8_59_ledger_opens_and_works_under_this_build(self):
        top = subprocess.run(['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True).stdout.strip()
        if subprocess.run(['git', '-C', top, 'cat-file', '-e', self.RELEASED_COMMIT + '^{commit}']).returncode:
            self.skipTest('the released commit is not in this repository')
        self.prepare()
        archive = subprocess.run(['git', '-C', top, 'archive', self.RELEASED_COMMIT,
                                  'packages/codex-agents/native-package'], capture_output=True, check=True).stdout
        old = self.base / 'old'
        old.mkdir()
        subprocess.run(['tar', '-x', '-C', str(old)], input=archive, check=True)
        self.assertEqual('0.8.59', (old / 'packages/codex-agents/native-package/VERSION').read_text().strip())
        started = self.run_cli(old / upgrade_tests.CLI_RELATIVE)  # the released setup: its ledger, its collector
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)
        self.stop_collector()
        with contextlib.closing(sqlite3.connect(self.state / 'learning.sqlite3')) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(16,  # the released fixture's version, before S2's epoch-only upgrade
                             connection.execute('PRAGMA user_version').fetchone()[0])
        self.assertFalse({'project_wake', 'project_starts'} & tables)  # the released schema, before E2a and E2c
        state = LearningState(self.state, create=False)
        for _ in range(2):  # opened twice: the migration is idempotent
            with contextlib.closing(LearningLedger(state)) as ledger:
                for table, columns in self.EXPECTED.items():
                    have = {row[1] for row in ledger._connection.execute(f'PRAGMA table_info({table})')}
                    self.assertLessEqual(columns, have, table)
                self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION,
                                 ledger._connection.execute('PRAGMA user_version').fetchone()[0])
        # The starter's ledger steps on the released ledger, with this build's code (this branch keeps the 0.8.59
        # version until the release bookkeeping, so its own setup refuses the 0.8.59-authorized state by design).
        with contextlib.closing(LearningLedger(state)) as ledger:
            project_id = ledger.register_project(self.project)
            now = time.time_ns() // 1_000_000
            ledger.mark_wakes_due([(project_id, str(self.project), None, None)], now_ms=now)
            claims = ledger.claim_due_starts(now_ms=now, limit=64)
            self.assertEqual([project_id], [claim.project_id for claim in claims])
            ledger.set_start_child(project_id, claims[0].attempt, os.getpid())
            ledger.finish_start(project_id, claims[0].attempt, outcome='started', reason=None, now_ms=now,
                                child_pid=os.getpid(), **project_start.start_policy())
            self.assertEqual([project_id], [row['project_id'] for row in ledger.probation_starts()])
            ledger.start_probation(project_id, attempt=claims[0].attempt, survived=True, now_ms=now,
                                   **project_start.start_policy())
            self.assertEqual('started', ledger.start_record(project_id)['outcome'])

    def released_ledger(self):
        """The released 0.8.59 setup's own ledger (real processes), its collector stopped; None when absent."""
        top = subprocess.run(['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', '--show-toplevel'],
                             capture_output=True, text=True).stdout.strip()
        if subprocess.run(['git', '-C', top, 'cat-file', '-e', self.RELEASED_COMMIT + '^{commit}']).returncode:
            self.skipTest('the released commit is not in this repository')
        self.prepare()
        archive = subprocess.run(['git', '-C', top, 'archive', self.RELEASED_COMMIT,
                                  'packages/codex-agents/native-package'], capture_output=True, check=True).stdout
        old = self.base / 'old'
        old.mkdir()
        subprocess.run(['tar', '-x', '-C', str(old)], input=archive, check=True)
        self.assertEqual('0.8.59', (old / 'packages/codex-agents/native-package/VERSION').read_text().strip())
        started = self.run_cli(old / upgrade_tests.CLI_RELATIVE)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)
        self.stop_collector()
        return LearningState(self.state, create=False)

    @requirements('E2a-1', why_not_scenario='Real processes of the released runtime, then this build on its ledger.')
    def test_E2a_1_a_released_0_8_59_ledger_backs_up_restores_and_moves_after_the_upgrade(self):
        """0.8.60 full-run fixes: after the additive upgrade, with a launch record, wake, epoch and starter rows
        present, the project's backup keeps its exact earlier format (six project columns, no machine-local table),
        verifies by an isolated restore that matches the current records, and the learning move completes."""
        from codex_agents import learning_migration
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import ResultCapacity
        from codex_agents.learning_contracts import content_digest
        from tests.learning_support import event_record, verification_outcome
        state = self.released_ledger()
        # The learning move is offline owner work (no collector, explicit upgrade), as learning-migrate-project runs.
        with contextlib.closing(LearningLedger(state, allow_schema_upgrade=True)) as ledger:
            project_id = ledger.register_project(self.project)
            now = time.time_ns() // 1_000_000
            ledger.append(project_id, event_record('released_upgrade_1', observed_at_ms=now), producer='hook',
                          now_ms=now)
            outcome = verification_outcome('released_upgrade_outcome')  # learning history: the move has rows
            sequence = ledger._connection.execute('SELECT MAX(source_sequence) FROM records WHERE project_id=?',
                                                  (project_id,)).fetchone()[0] + 1
            ledger._connection.execute(
                'INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,'
                'ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (project_id, outcome['record_id'], 'outcome', 'host', sequence,
                 outcome.get('observed_at_ms', now), now, None,
                 canonical_json(outcome), content_digest(outcome), 0))
            ledger._connection.commit()
            canonical = ledger._connection.execute('SELECT canonical_root FROM projects WHERE project_id=?',
                                                   (project_id,)).fetchone()[0]
            folder = hashlib.sha256(canonical.encode()).hexdigest()[:12]
            digest = 'e' * 64
            ledger.record_launch(project_id, str(Path('/var/empty/owner/.codex/agent-orchestration') / folder
                                                 / digest), digest)
            published = ledger.publish_wake(project_id, epoch=None, seq=1, state='pending',
                                            next_due_at_ms=now + 60_000, now_ms=now, instance='a' * 32)
            ledger.mark_wakes_due([(project_id, canonical, published['epoch'], 1)], now_ms=now + 1)
            self.assertEqual([project_id], [c.project_id for c in ledger.claim_due_starts(now_ms=now + 1, limit=64)])
            for table in learning_store.LOCAL_TABLE_COLUMNS:
                self.assertEqual(1, ledger._connection.execute(
                    f'SELECT COUNT(*) FROM {table} WHERE project_id=?', (project_id,)).fetchone()[0], table)
            backup = ledger.create_sanitized_backup(project_id, file_id='released_upgrade',
                                                    expires_at_ms=now + 600_000)
            verified = ledger.verify_sanitized_backup(project_id, file_id='released_upgrade')
            self.assertEqual(learning_store.LEDGER_SCHEMA_VERSION, verified['ledger_schema_version'])
            self.assertEqual('ok', verified['isolated_restore_integrity'])
            with contextlib.closing(sqlite3.connect(f'file:{backup}?mode=ro', uri=True)) as kept:
                self.assertEqual(list(learning_store._BACKUP_PROJECT_COLUMNS),
                                 [row[1] for row in kept.execute('PRAGMA table_info(projects)')])
                names = {row[0] for row in kept.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertFalse(names & set(learning_store.LOCAL_TABLE_COLUMNS))
                self.assertEqual(2, kept.execute('SELECT COUNT(*) FROM records WHERE project_id=?',
                                                 (project_id,)).fetchone()[0])
            directory = self.base / 'moved-history'
            directory.mkdir(mode=0o700)
            with contextlib.closing(ProjectHistory(directory, project_id, result_key=state.digest_key(),
                                                   result_capacity=ResultCapacity(2, 2 * 1_048_576, 120_000,
                                                                                  120_000),
                                                   allow_schema_upgrade=True)) as history:
                report = learning_migration.migrate_project(ledger, history, project_id, now_ms=now)
                self.assertEqual('complete', report['state'])
            self.assertTrue(ledger.learning_moved(project_id))
            # A fresh ledger of this build has exactly the upgraded layout (the migration is additive, never an
            # in-place change of a released CREATE statement).
            fresh_root = self.base / 'fresh-state'
            fresh_root.mkdir(mode=0o700)
            with contextlib.closing(LearningLedger(LearningState(fresh_root))) as fresh:
                def layout(connection):
                    return {kind + ':' + name: ' '.join(sql.replace('"', '').split()) for kind, name, sql in
                            connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
                                               "AND type IN ('table','index')")}
                # Triggers are left out: the collector adds and drops cleanup_host_binding_with_event while it runs
                # (also in 0.8.59), and the released collector here was stopped, not closed.
                self.assertEqual(layout(fresh._connection), layout(ledger._connection))
            # The machine-local rows stay with this machine's ledger, untouched by the move.
            self.assertEqual((str(Path('/var/empty/owner/.codex/agent-orchestration') / folder / digest), digest),
                             ledger.launch_record(project_id))


if __name__ == '__main__':
    unittest.main()
