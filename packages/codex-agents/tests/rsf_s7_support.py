"""SQLite-only S7 fixtures: no git, subprocess, worker, socket or real home."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_settlement as project_fixtures
from tests.learning_support import assignment, event_record
from tests.project_schema_fixtures import legacy_orchestration_state_layout  # B6: the format 18 fixture
from tests.support import ORCHESTRATION_ROOT

from codex_agents.account_api import AccountRequest, account_request_digest
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.claim_storage import encode_storage_evidence
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime, COLLECTOR_RUNTIME_DIGEST
from codex_agents.lost_run_contracts import SettleLostInput
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity, ResultLeaseBinding, MAX_RESULT_BODY_BYTES

SCRATCH = Path(__file__).resolve().parents[3] / '.rsf-s7-scratch'
SCRATCH.mkdir(exist_ok=True)


class AccountFixture(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch('socket.socket', side_effect=AssertionError('S7 forbids sockets')))
        self.enterContext(mock.patch('subprocess.Popen', side_effect=AssertionError('S7 forbids subprocesses')))
        # Product state is normally outside repositories; this task permits writes
        # only in this worktree. Override only that placement guard for fixture state.
        self.enterContext(mock.patch('codex_agents.learning_store._inside_git_tree', return_value=False))
        self.temp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = LearningState(self.root / 'state')
        authorize_collector_runtime(self.state, ORCHESTRATION_ROOT / 'cli.py')
        self.ledger = LearningLedger(self.state, client_capabilities={})
        self.addCleanup(lambda: self.ledger.close())
        self.project_root = self.root / 'project'
        self.project_root.mkdir()
        self.project = self.ledger.register_project(self.project_root, now_ms=1)
        self.key = self.state.digest_key()
        self.handle = 'hrn_' + '1' * 48
        self.handle_digest = keyed_digest(self.key, self.handle)
        self.run_id = 'run:' + '2' * 64
        self.runtime = COLLECTOR_RUNTIME_DIGEST
        self.caller = 'a' * 64
        self.identity = '3' * 64
        self.bridge = 'brg_' + '4' * 32
        self.request = 'claim-s7'
        self.now = 1000
        self.clock = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(lambda: self.clock.stop())
        with self.ledger._account_transaction():
            self.ledger._connection.execute(
                'INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,'
                'route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) '
                "VALUES(?,?,?,?,?,?,?,'bridge-v1',1000,61000,'pending')",
                (self.project, self.handle_digest, self.run_id, *(['5' * 64] * 4)))
            self.ledger._append_checked_locked(self.project, event_record('s7-payload', observed_at_ms=1000),
                                                producer='host', timestamp=1000)

    def reopen(self):
        self.clock.stop()
        self.ledger.close()
        self.ledger = LearningLedger(self.state, client_capabilities={}, allow_schema_upgrade=True)
        self.clock = mock.patch.object(self.ledger, '_clock_ms', side_effect=lambda: self.now)
        self.clock.start()

    def migrate(self):
        """Exact schema15 layout, with its verified owner backup; preserve existing tombstones."""
        from codex_agents import learning_store
        with self.ledger._account_transaction():
            self.ledger._connection.execute('ALTER TABLE records DROP COLUMN probe_id')
            self.ledger._connection.execute('PRAGMA user_version=15')
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.project,
                                                         ledger_schema_version=15)
        raw = canonical_json({'backup_sha256': hashlib.sha256(canonical_json(body)).hexdigest(), 'body': body})
        path = self.ledger._write_owner_file('backup', self.project, 's7_before_16', raw)
        self.ledger._register_stored_file(self.project, file_id='s7_before_16', kind='backup', path=path,
                                         digest=hashlib.sha256(raw).hexdigest(), expires_at_ms=10**15)
        self.reopen()

    def prune(self):
        self.ledger.expire(self.project, now_ms=10**15)
        with self.ledger._account_transaction():
            self.ledger._connection.execute('DELETE FROM account_open_receipts WHERE project_id=?', (self.project,))
            self.ledger._connection.execute('DELETE FROM account_open_requests WHERE project_id=?', (self.project,))
            self.ledger._connection.execute('DELETE FROM host_run_bindings WHERE project_id=?', (self.project,))
            self.ledger._connection.execute('DELETE FROM launch_inputs WHERE project_id=?', (self.project,))
            self.ledger._connection.execute('DELETE FROM launch_observations WHERE project_id=?', (self.project,))

    def snapshot(self):
        return tuple(self.ledger._connection.iterdump())

    def claimed(self):
        launch = LaunchClaimInput(self.handle, self.identity)
        request = AccountRequest(1, 'account.claim_launch', self.request, self.project, self.runtime, launch.payload())
        self.binding = ResultLeaseBinding(self.project, 'primary', self.runtime, 'supervisor', 1,
            'orc_' + '6' * 48, self.handle, self.run_id, 1, self.request, account_request_digest(request))
        directory = self.root / 'history'
        directory.mkdir(mode=0o700)
        self.history = ProjectHistory(directory, self.project, result_key=self.key,
            result_capacity=ResultCapacity(4, 2 * MAX_RESULT_BODY_BYTES, 60000, 120000),
            result_clock_ms=lambda: self.now, allow_legacy_orchestration_reference=True)
        self.addCleanup(self.history.close)
        self.history.reserve_result_lease(self.binding, assignment())
        self.lease = self.history.arm_result_lease(self.binding)
        self.evidence = encode_storage_evidence(self.key, self.lease)
        scope = keyed_digest(self.key, canonical_json({'project': self.project, 'runtime': self.runtime,
                                                       'caller': self.caller}))
        with self.ledger._account_transaction():
            self.ledger._connection.execute("INSERT INTO launch_claims VALUES(?,?,?,'claimed',1000,?)",
                (self.project, self.handle_digest, self.run_id, self.bridge))
            self.ledger._connection.execute(
                'INSERT INTO active_run_capacity(project_id,handle_digest,run_id,bridge_run_id,kind,provider,'
                'account_fingerprint,runtime_digest,policy_digest,claimed_at_ms,deadline_at_ms,clock_reference_ns,'
                'execution_deadline_ns,clock_owner_digest) VALUES(?,?,?,?,?,?,?,?,?,1000,61000,1000,60000001000,?)',
                (self.project, self.handle_digest, self.run_id, self.bridge, 'claimed', 'codex', '7' * 64,
                 self.runtime, '8' * 64, '9' * 64))
            self.ledger._store_claim_receipt_locked(self.project, scope, keyed_digest(self.key, self.request),
                keyed_digest(self.key, canonical_json(launch.payload())), self.handle_digest,
                dict(state='claimed', run_id=self.run_id, reason_code=None, bridge_run_id=self.bridge,
                     deadline_at_ms=61000, recorded_at_ms=1000))
        self.settle = SettleLostInput(self.handle, self.identity, self.request, self.bridge, self.lease.lease_digest)

    def settle_options(self):
        return dict(runtime_digest=self.runtime, caller_scope=self.caller, settle=self.settle,
                    storage_binding=self.binding, storage_evidence=self.evidence)

    def withdrawal_owner(self):
        """A real keyed open receipt accepted by the product's opened_by check."""
        from codex_agents.account_original_open import open_scope_digest, _RECEIPT_DOMAIN
        scope_data = {'project_id': self.project, 'runtime_digest': self.runtime}
        scope = open_scope_digest(self.key, scope_data, '6' * 64, self.caller)
        raw = canonical_json({'scope': scope_data, 'orchestration_digest': '6' * 64, 'scope_digest': scope,
            'view': {'state': 'opened', 'run_id': self.run_id, 'inputs_digest': '7' * 64}})
        with self.ledger._account_transaction():
            self.ledger._connection.execute('INSERT INTO account_open_receipts VALUES(?,?,?,?,?,?,?,?)',
                (self.project, self.handle_digest, self.run_id, 'open-s7', scope, '8' * 64, raw,
                 keyed_digest(self.key, _RECEIPT_DOMAIN + raw)))
        return dict(caller_scope=self.caller, run_id=self.run_id, inputs_digest='7' * 64,
                    attempt=1, reason='cancelled_before_claim')


class ProjectFixture(project_fixtures.SettlementFixture):
    """Reuse only the socket-free project fixture; every temp directory stays in the worktree."""
    def setUp(self):
        self.enterContext(mock.patch('socket.socket', side_effect=AssertionError('S7 forbids sockets')))
        self.enterContext(mock.patch('subprocess.Popen', side_effect=AssertionError('S7 forbids subprocesses')))
        self.patch_temp = mock.patch.object(tempfile, 'tempdir', str(SCRATCH))
        self.patch_temp.start()
        self.addCleanup(self.patch_temp.stop)
        self.patch_root = mock.patch('tests.test_project_results.TEMP_ROOT', SCRATCH)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        super().setUp()

    def migrate(self):
        db = self.store._connection
        for name in ('project_holds_open_run', 'project_holds_open_held', 'project_registered_runs_run'):
            db.execute('DROP INDEX ' + name)
        db.execute('DROP TABLE project_result_recorders')
        legacy_orchestration_state_layout(self.store)  # batch 4: schema21 (B6, decision 131): format 18 never had it
        db.execute('PRAGMA user_version=18')
        self.base.reopen(allow_schema_upgrade=True, result_capacity=self.fixture.capacity)

    def prune(self):
        target = project_fixtures.claim_binding(1)
        with self.store._transaction():
            self.store._delete_attempt_rows(target.run_handle, target.attempt, target.claim_id)

    def reopen(self):
        self.base.reopen(result_capacity=self.fixture.capacity)
