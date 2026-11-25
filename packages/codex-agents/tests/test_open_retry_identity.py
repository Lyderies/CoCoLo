"""Socket/process-free account preflight replay with a retained original-open journal."""
from dataclasses import replace
import unittest
from unittest import mock

from test_framework import requirements
from tests import test_account_open_schema as schema
from tests import test_bridge as bridge
from tests.support import BUNDLE_ROOT, ORCHESTRATION_ROOT

from codex_agents.account_open_contracts import AccountOpenContext, AccountOpenInput, OpenError, OpenOwnerLimits
from codex_agents.account_original_open import preflight_open
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_contracts import canonical_json, content_digest, external_id, keyed_digest, route_identity
from codex_agents.open_scope_contracts import OpenScopeContext, open_inputs_digest, seal_open_scope
from codex_agents.orchestration_api import OrchestrationScope
from codex_agents.routing import load_policy
from codex_agents.sql_read_budget import SQLReadLimits


class OpenRetryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.base = schema.AccountOpenSchemaTests(methodName='runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.ledger = self.base.ledger
        # Reuse pure fixture value builders, without starting the bridge fixture or any child.
        values = bridge.BridgeTestCase(methodName='runTest')
        values.bundle = BUNDLE_ROOT
        values.policy = load_policy(ORCHESTRATION_ROOT / 'config' / 'routing-policy.json')
        values.clients = load_client_capabilities(ORCHESTRATION_ROOT / 'config' / 'learning-clients.json')
        self.task = values.task()
        self.route = values.bridged_route('codex')
        self.assignment = values.assignment_for(self.route)
        self.scope = OrchestrationScope(self.base.project, 'm11-primary', 'd'*64, 'm11-supervisor', 1)
        self.handle = 'orc_' + 'a'*48
        self.context = OpenScopeContext(self.scope, self.handle, self.assignment, self.task, self.route,
                                        bridge.PARENT_GRANT)

    def preflight(self):
        key = self.ledger.state.digest_key()
        digest = open_inputs_digest(self.assignment, self.task, self.route, self.context.parent_grant)
        facts = {'scope': self.scope.as_data(), 'orchestration_handle': self.handle,
            'orchestration_id': external_id(key, 'orc', self.handle), 'checkpoint_digest': 'c'*64,
            'sequence': 1, 'client_version': 'codex-host-v1', 'inputs_digest': digest,
            'assignment_id': self.assignment['assignment_id'], 'attempt': self.assignment['attempt'],
            **{name+'_digest': keyed_digest(key, canonical_json(value)) for name, value in (
                ('assignment', self.assignment), ('task', self.task), ('route', route_identity(self.route)),
                ('parent_grant', self.context.parent_grant))},
            'configuration_digest': content_digest(self.route['execution_configuration']), 'provider': 'codex',
            'task_family': self.task['family'], 'worker': None, 'worker_context_digest': None}
        request = AccountOpenInput(self.scope, self.handle, digest)
        context = AccountOpenContext(self.context, seal_open_scope(key, facts))
        return preflight_open(self.ledger, self.base.project, runtime_digest=self.scope.runtime_digest,
            caller_scope='b'*64, request_id='cli-open-'+content_digest(request.payload()), request=request,
            context=context, limits=OpenOwnerLimits(4, 1_048_576),
            work_limits=SQLReadLimits(100_000, 1000, 2*1_048_576), before_commit=lambda: None)

    @requirements('M11-3', why_not_scenario='Real account preflight and SQLite journal; no sockets or processes.')
    def test_M11_availability_refresh_replays_the_leftover_journal_without_a_new_open(self):
        first, _ = self.preflight()
        before = self.ledger._connection.total_changes
        self.route = {**self.route, 'availability': {**self.route.get('availability', {}),
            'evaluated_at_ms': 2000, 'snapshot_ref': 'e'*64}}
        self.context = replace(self.context, route=self.route)
        with mock.patch.object(self.ledger, '_clock_ms', side_effect=AssertionError('renewed open window')):
            retried, known = self.preflight()
        self.assertIsNone(known)
        for name in ('request_key', 'original_key', 'parameters_digest', 'run_handle', 'run_id',
                     'opened_at_ms', 'observation_deadline_at_ms', 'journal_seal'):
            self.assertEqual(getattr(first, name), getattr(retried, name), name)
        self.assertEqual(before, self.ledger._connection.total_changes)
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM account_open_requests').fetchone()[0])
        for table in ('host_run_bindings', 'launch_claims', 'active_run_capacity', 'run_holds'):
            self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0])
        # A fresh, valid project proof over changed original content still conflicts.
        self.assignment = {**self.assignment, 'objective': 'different execution input'}
        self.context = replace(self.context, assignment=self.assignment)
        with self.assertRaisesRegex(OpenError, '^open_conflict$'):
            self.preflight()
