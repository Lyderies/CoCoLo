"""Fresh status CLI uses actual metadata APIs; native enrollment is synthetic."""
from dataclasses import asdict
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import generate_test_owner_key
from tests.test_app_cli_client_boundary import GUARDED_CLI

from codex_agents.account_api import account_request_digest
from codex_agents.account_service import AccountAuthorization, AccountOwnerViews, AccountService
from codex_agents.client_runtime import runtime_hint_path
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient
from codex_agents.collector_paths import CollectorPaths
from codex_agents.control_api import ControlScope
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, authorize_collector_runtime


class StatusCliClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.state = LearningState(self.base / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(self.base)
        self.state.install_owner_public_key(public)
        self.project = self.base / 'project'
        (self.project / '.git').mkdir(parents=True)
        self.ledger = LearningLedger(self.state)
        self.addCleanup(self.ledger.close)
        self.project_id = self.ledger.register_project(self.project)
        self.native_context = object()
        self.authorized = True
        self.requests = []
        self.on_account_request = None  # called as each account request arrives

        def authorize(request, context):
            if not self.authorized or context is not self.native_context or request.project_id != self.project_id:
                return None
            return AccountAuthorization(self.project_id, COLLECTOR_RUNTIME_DIGEST,
                                        account_request_digest(request), 'host', frozenset({'account.owner_key'}))

        self.account = AccountService(COLLECTOR_RUNTIME_DIGEST, AccountOwnerViews(self.ledger), authorizer=authorize)
        self.addCleanup(self.account.close)
        self.endpoint = self.state.root / 'account.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.endpoint))
        self.endpoint.chmod(0o600)
        self.listener.listen(4)
        self.listener.setblocking(False)
        self.addCleanup(self.listener.close)
        self.collector = CollectorServer(self.ledger)
        self.collector.start()
        self.addCleanup(self.collector.stop)
        self.client = CollectorClient(self.state.socket, timeout=2)
        self.started = self.client.health(self.project)['started_at_ms']
        state = CollectorPaths(self.state.root)
        hint = runtime_hint_path(state, self.project)
        hint.parent.mkdir(mode=0o700)
        scope = ControlScope(project_id=self.project_id, primary_session_id='status-session',
                             runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                             supervisor_instance_id='status-supervisor', supervisor_generation=1)
        hint.write_bytes(canonical_json({'schema_version': 2,
            'project_root_digest': keyed_digest(state.digest_key(), str(self.project.resolve())),
            'scope': asdict(scope), 'account_endpoint': str(self.endpoint),
            'dispatch_endpoint': str(self.state.root / 'absent-dispatch.sock'),
            'result_endpoint': str(self.state.root / 'absent-result.sock')}))
        hint.chmod(0o600)
        self.hint = hint

    def command(self):
        guard = GUARDED_CLI.replace('if event == "sqlite3.connect":',
                                   'if event in ("sqlite3.connect", "subprocess.Popen"):')
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', guard,
            str(ORCHESTRATION_ROOT / 'cli.py'), 'learning-status', '--state-root', str(self.state.root),
            '--project-root', str(self.project)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 15
        try:
            while process.poll() is None:
                self.assertLess(time.monotonic(), deadline, 'status client did not finish within its bounded exchanges')
                try:
                    connection, _ = self.listener.accept()
                except BlockingIOError:
                    time.sleep(.002)
                    continue
                with connection:
                    connection.settimeout(2)
                    raw = b''
                    while not raw.endswith(b'\n'):
                        chunk = connection.recv(16_385 - len(raw))
                        self.assertTrue(chunk)
                        raw += chunk
                        self.assertLessEqual(len(raw), 16_384)
                    self.requests.append(json.loads(raw))
                    if self.on_account_request is not None:
                        self.on_account_request()
                    response = self.account.handle(raw[:-1], self.native_context)
                    connection.sendall(response + b'\n')
            stdout, stderr = process.communicate(timeout=2)
            return process.returncode, stdout, stderr
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()

    def test_healthy_status_preserves_report_through_original_apis_without_client_sqlite(self):
        original_key = self.state.owner_key_report()
        changes = self.ledger._connection.total_changes
        code, stdout, stderr = self.command()
        self.assertEqual(0, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertTrue(report['ready'])
        self.assertTrue(report['active'])
        self.assertEqual(original_key, report['owner_key'])
        self.assertEqual(self.started, report['started_at_ms'])
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, report['collector_runtime_digest'])
        self.assertEqual('active', report['ingestion']['registered_project_state'])
        self.assertEqual({}, report['client_failures'])
        self.assertEqual(['account.owner_key'], [r['operation'] for r in self.requests])
        self.assertEqual(changes, self.ledger._connection.total_changes)
        self.assertEqual(self.started, self.client.health(self.project)['started_at_ms'])

    def test_account_denial_keeps_observed_collector_state_and_unknown_owner_without_fallback(self):
        self.authorized = False
        code, stdout, stderr = self.command()
        self.assertEqual(1, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertFalse(report['ready'])
        self.assertTrue(report['active'])
        self.assertEqual('status_account_unavailable', report['reason_code'])
        self.assertEqual('account_not_authorized', report['account_reason_code'])
        self.assertEqual('unavailable', report['owner_key']['state'])
        self.assertIsNone(report['owner_key']['replacements'])
        self.assertNotIn('inspected a database', stdout + stderr)
        self.assertEqual(1, len(self.requests))

    def test_H1a_health_carries_the_lane_notes_and_this_projects_slots(self):
        lanes = {'probe_worker': {'faulted': False, 'kept': 1, 'busy': 0, 'queued': 0,
                                  'last_failure': None, 'held_since_ms': 5}}
        self.collector.account_lanes = lambda: lanes
        health = self.client.health(self.project)
        self.assertEqual(lanes, health['account_lanes'])
        self.assertTrue(health['account_slots']['available'])
        self.assertEqual(lanes, self.client.health()['account_lanes'])  # project-less: notes, no slots
        self.assertNotIn('account_slots', self.client.health())
        self.collector.account_lanes = lambda: 1 / 0  # a failing view never breaks health
        self.assertEqual({'available': False}, self.client.health(self.project)['account_lanes'])

    @staticmethod
    def lane(**changes):
        return {'faulted': False, 'kept': 0, 'busy': 0, 'queued': 0, 'last_failure': None,
                'held_since_ms': None, **changes}

    def status_with(self, lanes):
        self.collector.account_lanes = (lambda: lanes) if lanes is not None else None
        code, stdout, stderr = self.command()
        return code, json.loads(stdout)

    def test_H1a_4_account_health_states_and_ready_is_unchanged(self):
        clean = {name: self.lane() for name in ('signature', 'claims', 'observations', 'ingress', 'billing',
                                                 'probe_worker')}
        code, report = self.status_with(clean)
        self.assertEqual((0, True, 'ok'), (code, report['ready'], report['account_health']['state']))
        self.assertEqual([], report['account_health']['attention'])
        held = {**clean, 'probe_worker': self.lane(kept=1, held_since_ms=1_700_000_000_000)}
        code, report = self.status_with(held)
        health = report['account_health']
        self.assertEqual((0, True, 'degraded'), (code, report['ready'], health['state']))
        self.assertTrue(any('probe worker' in line and 'held since' in line for line in health['attention']), health)
        failed = {**held, 'ingress': self.lane(faulted=True, last_failure={
            'cause': 'ingress_cleanup_failed', 'type': 'OSError', 'at_ms': 1_700_000_000_000})}
        code, report = self.status_with(failed)
        health = report['account_health']
        self.assertEqual(('faulted', True), (health['state'], report['ready']))
        self.assertTrue(any('ingress_cleanup_failed' in line for line in health['attention']), health)
        self.assertTrue(all('account-wide' in line for line in health['attention'] if 'slot' not in line))
        code, report = self.status_with(None)  # no notes (a collector started otherwise)
        self.assertEqual(('unknown', 'account_health_unavailable'),
                         (report['account_health']['state'], report['account_health']['reason_code']))

    def test_C2_C3_malformed_or_missing_sources_never_stop_status_and_never_say_ok(self):
        clean = {name: self.lane() for name in ('signature', 'claims', 'observations', 'ingress', 'billing',
                                                 'probe_worker')}
        for slots, reason in (({'available': False, 'reason': []}, 'account_health_invalid'),
                              ([1, 2], 'account_health_invalid')):
            with self.subTest(slots=slots), mock.patch.object(
                    self.ledger, 'account_slot_health', return_value=slots):
                code, report = self.status_with(clean)
                self.assertEqual((0, True), (code, report['ready']))
                self.assertEqual(('unknown', reason), (report['account_health']['state'],
                                                       report['account_health']['reason_code']))
        code, report = self.status_with({'billing': self.lane()})  # five lanes missing
        self.assertEqual(('unknown', 'account_health_unavailable'),
                         (report['account_health']['state'], report['account_health']['reason_code']))

    def test_ST1_account_services_still_starting_say_starting(self):
        self.collector.account_services = 'starting'
        self.hint.unlink()
        code, stdout, stderr = self.command()
        self.assertEqual(1, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertFalse(report['ready'])
        self.assertEqual('status_account_starting', report['reason_code'])
        self.assertEqual('starting', report['account_services'])

    def test_ST2_a_ready_status_never_reports_health_from_before_the_account_came_up(self):
        self.collector.account_services = 'starting'

        def came_up_meanwhile():  # the account answers, and its host has meanwhile finished starting
            self.collector.account_services = 'running'
            self.collector.startup_phases = {'ledger_open': 1, 'collector': 2, 'account': 3}
        self.on_account_request = came_up_meanwhile
        code, stdout, stderr = self.command()
        self.assertEqual(0, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertTrue(report['ready'])
        self.assertEqual('running', report['account_services'])
        self.assertEqual({'ledger_open': 1, 'collector': 2, 'account': 3}, report['collector_startup_ms'])

    def test_missing_hint_does_not_read_database_or_start_runtime(self):
        self.hint.unlink()
        code, stdout, stderr = self.command()
        self.assertEqual(1, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertFalse(report['ready'])
        self.assertEqual('status_account_unavailable', report['reason_code'])
        self.assertTrue(report['active'])
        self.assertEqual([], self.requests)
        self.assertNotIn('inspected a database', stdout + stderr)

    def test_unavailable_collector_retains_actual_account_observation_without_startup(self):
        self.collector.stop()
        original_key = self.state.owner_key_report()
        code, stdout, stderr = self.command()
        self.assertEqual(1, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertFalse(report['ready'])
        self.assertFalse(report['active'])
        self.assertEqual('status_collector_transport_unavailable', report['reason_code'])
        self.assertEqual(original_key, report['owner_key'])
        self.assertEqual('unavailable', report['ingestion']['freshness']['state'])
        self.assertEqual(['account.owner_key'], [r['operation'] for r in self.requests])
        self.assertNotIn('inspected a database', stdout + stderr)

    def test_wrong_project_hint_cannot_read_foreign_owner_metadata(self):
        foreign = self.base / 'foreign-project'
        foreign.mkdir()
        identity = self.ledger.register_project(foreign)
        hint = json.loads(self.hint.read_bytes())
        hint['scope']['project_id'] = identity
        self.hint.write_bytes(canonical_json(hint))
        code, stdout, stderr = self.command()
        self.assertEqual(1, code, stdout + stderr)
        report = json.loads(stdout)
        self.assertFalse(report['ready'])
        self.assertEqual('unavailable', report['owner_key']['state'])
        self.assertIsNone(report['owner_key']['fingerprint'])
        self.assertEqual('status_account_unavailable', report['reason_code'])
        self.assertEqual([identity], [r['project_id'] for r in self.requests])

    def test_malformed_collector_metadata_is_unavailable_and_never_echoed(self):
        original = self.client.health(self.project)['ingestion']
        variants = [dict(original, private_content='CANARY-private'),
                    dict(original, freshness={'state': 'fresh', 'age_ms': 0, 'message': 'CANARY-private'}),
                    dict(original, accepted_hook_event_count=True),
                    dict(original, coverage=dict(original['coverage'], root_usage=1))]
        for value in variants:
            with self.subTest(value=value), mock.patch.object(self.ledger, 'ingestion_snapshot', return_value=value):
                code, stdout, stderr = self.command()
            self.assertEqual(1, code, stdout + stderr)
            report = json.loads(stdout)
            self.assertFalse(report['ready'])
            self.assertFalse(report['active'])
            self.assertEqual('status_collector_unavailable', report['reason_code'])
            self.assertEqual('unavailable', report['ingestion']['freshness']['state'])
            self.assertNotIn('CANARY', stdout + stderr)
