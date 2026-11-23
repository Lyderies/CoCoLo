"""More real runs in the B2 fixture's one store (B2 fix round 2).

The account fixtures build one run. This helper builds another through the same real operations the verifier pair of
the QA tests uses (ledger host run, claim, lease with its stored record context, host open, registration, provenance,
consumption, publication): each extra run is a genuine, context-backed, committed, unrecorded run of the same
orchestration, so two distinct valid runs can exist in one store.
"""
from copy import deepcopy
from dataclasses import replace

import tests.support  # noqa: F401
from tests.test_bridge import PARENT_GRANT, result

from codex_agents.account_api import AccountRequest, account_request_digest
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.execution_grant import ExecutionRegistrationInput, checked_execution_grant
from codex_agents.history_contracts import AttemptBinding
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.host_result_contracts import HostResultReference, RecordContext
from codex_agents.learning_contracts import canonical_json, content_digest, external_id
from codex_agents.open_scope_contracts import open_inputs_digest
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultObservation


class ExtraRun:
    """One more committed, unrecorded run: `binding`, `reference`, `body`, `assignment`, `task`, `route`."""

    def __init__(self, fixture, number, *, summary=None, publish=True):
        base = fixture.base                      # RunProvenanceTests
        account = fixture.fixture                # AccountClaimStorageTests
        ledger, store = base.base.ledger, fixture.store
        bridge = account.base.fixture.fixture
        self.number = number
        self.task = {**deepcopy(fixture.task), 'id': f'b2-extra-{number}'}
        self.route = deepcopy(fixture.route)
        self.assignment = deepcopy(account.assigned)
        self.assignment.update(assignment_id=f'b2-extra-assignment-{number}', task_id=self.task['id'])
        opened = ledger.open_host_run(
            fixture.binding.project_id, assignment=self.assignment, task=self.task, route=self.route,
            execution_configuration=self.route['execution_configuration'], client_version='bridge-v1',
            orchestration_handle=fixture.binding.orchestration_handle, now_ms=account.base.stamp)
        identity = ledger.observe_provider(fixture.binding.project_id, 'codex', project_root=bridge.tools, environ={},
                                           run_handle=opened['run_handle'], phase='before')[0]
        launch = LaunchClaimInput(opened['run_handle'], content_digest(identity))
        claim_request = AccountRequest(1, 'account.claim_launch', f'b2-extra-claim-{number}', fixture.binding.project_id,
                                       fixture.binding.runtime_digest, launch.payload())
        scope = replace(account.scope, run_handle=opened['run_handle'],
                        run_id=external_id(account.key, 'run', opened['run_handle']),
                        claim_request_id=claim_request.request_id,
                        claim_request_digest=account_request_digest(claim_request))
        context = RecordContext(self.task, self.route, fixture.capability, fixture.host_class,
                                open_inputs_digest(self.assignment, self.task, self.route, PARENT_GRANT))
        store.reserve_result_lease(scope, self.assignment, record_context=context)
        store.arm_result_lease(scope)
        pending = store.attest_pending_result_lease(scope)
        request = OpenProvenanceInput(scope.run_handle, self.assignment['assignment_id'])
        proof = ledger._attest_open_provenance(scope.project_id, runtime_digest=scope.runtime_digest,
                                               caller_scope='a' * 64, request=request, storage_binding=scope,
                                               storage_evidence=pending, work_limits=base.limits)
        store.record_host_open(scope, request, proof, assignment=self.assignment, task=self.task, route=self.route,
                               before_commit=lambda: None)
        receipt = ledger.claim_launch(scope.project_id, runtime_digest=scope.runtime_digest, caller_scope='a' * 64,
                                      request_id=scope.claim_request_id, launch=launch, storage_required=True,
                                      storage_binding=scope, storage_evidence=pending)
        self.binding = AttemptBinding(**{name: getattr(scope, name) for name in LEASE_SCOPE_FIELDS},
                                      claim_id=receipt['bridge_run_id'], claim_digest=receipt['receipt_digest'])
        store.bind_result_lease(scope, self.binding, self.assignment, hard_deadline_at_ms=receipt['deadline_at_ms'])
        evidence = store.attest_bound_result_lease(scope)
        registration = ExecutionRegistrationInput(scope.run_handle, self.binding.claim_id, self.binding.claim_digest,
                                                  scope.claim_request_id, launch.identity_digest)
        grant = checked_execution_grant(ledger._register_project_execution(
            scope.project_id, runtime_digest=scope.runtime_digest, caller_scope='a' * 64, registration=registration,
            storage_binding=scope, storage_evidence=evidence)['grant'])
        proof = ledger._attest_run_provenance(
            scope.project_id, runtime_digest=scope.runtime_digest, caller_scope='a' * 64,
            request=RunProvenanceInput(registration, grant.registration_digest, self.assignment['assignment_id']),
            storage_binding=scope, storage_evidence=evidence, work_limits=base.limits)
        store.consume_execution_grant(self.binding, grant.encode(), task=self.task, route=self.route, provenance=proof)
        self.body = canonical_json({**result(), 'assignment_id': self.assignment['assignment_id'],
                                    'summary': summary or f'EXTRA-RUN-{number}'})
        self.reference = None
        if publish:
            self.publish(fixture)

    def publish(self, fixture):
        delivery = fixture.store.publish_result(
            self.binding, self.assignment, self.body,
            ResultObservation(self.binding.claim_id, fixture.fixture.base.stamp, False, True, None))
        self.delivery = delivery
        self.reference = HostResultReference(self.binding, delivery.record_id, delivery.record_content_digest,
                                             delivery.result_digest)


from codex_agents.run_provenance import RunProvenanceInput  # noqa: E402
