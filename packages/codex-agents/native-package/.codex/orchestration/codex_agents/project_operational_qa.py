"""Original project-owned QA dependencies before running fixed operational checks.

Only the persistence owner reads the two private bodies. This lookup creates no
check receipt, accepted outcome, account admission or release authority. Native
input custody, enrollment and protected publication remain activation gates.
"""
from .contracts import ContractError, validate_task
from .history_contracts import AttemptBinding, HistoryError
from .host_result_contracts import OperationalQAContext
from .learning_contracts import (canonical_json, content_digest, keyed_digest, parse_bounded_json,
                                 provider_for_model, validate_execution_configuration)
from .orchestration_api import SCOPE_FIELDS
from .project_worker_result import current_host_reference
from .result_contracts import assignment_bytes, checked_result_body


def current_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest):
    """Check both original metadata chains against independently held inputs."""
    if type(context) is not OperationalQAContext:
        raise HistoryError('history_invalid_input')
    _, captured = context.snapshot()
    bindings = {name: AttemptBinding(**captured[name]['binding']) for name in ('worker', 'verifier')}
    worker, verifier = bindings['worker'], bindings['verifier']
    if (any(getattr(worker, name) != getattr(verifier, name) for name in (*SCOPE_FIELDS, 'orchestration_handle'))
            or worker.claim_id == verifier.claim_id or worker.run_handle == verifier.run_handle
            or worker.run_id == verifier.run_id or host_outcome_digest == verifier_host_outcome_digest):
        raise HistoryError('history_conflict')
    results = {}
    for name, digest in (('worker', host_outcome_digest), ('verifier', verifier_host_outcome_digest)):
        reference, _, facts, grant, outcome = current_host_reference(history, bindings[name], digest)
        original = captured[name]
        try:
            task = validate_task(original['task'])
            route, assignment = original['route'], original['assignment']
            configuration = validate_execution_configuration(route['execution_configuration'])
            if (grant.assignment_digest != keyed_digest(history._result_key, assignment_bytes(assignment, bindings[name]))
                    or grant.task_digest != keyed_digest(history._result_key, canonical_json(task))
                    or grant.route_digest != keyed_digest(history._result_key, canonical_json(route))
                    or grant.configuration_digest != content_digest(configuration)
                    or configuration['client_digest'] != content_digest(original['client_capability'])
                    or any(outcome[field] != assignment[field] for field in ('assignment_id', 'model', 'effort', 'role'))
                    or facts['provider'] != route.get('provider', 'codex')
                    or provider_for_model(outcome['model']) != facts['provider']
                    or task['family'] != facts['task_family']):
                raise ValueError()
        except (ContractError, KeyError, TypeError, ValueError):
            raise HistoryError('history_conflict') from None
        results[name] = (reference, facts)
    worker_reference, worker_facts = results['worker']
    verifier_reference, verifier_facts = results['verifier']
    worker_input, verifier_input = captured['worker'], captured['verifier']
    task = verifier_input['task']
    roles = {'codex': 'codex_agents_verification_worker', 'claude': 'claude_agents_verification_worker'}
    if (worker_facts['task_family'] == 'verification' or worker_facts['worker_outcome_digest'] is not None
            or verifier_facts['task_family'] != 'verification'
            or verifier_facts['worker_outcome_digest'] != host_outcome_digest
            or task.get('verification_for_outcome') != host_outcome_digest
            or task.get('verification_for_family') != worker_facts['task_family']
            or task.get('verification_for_provider') != worker_facts['provider']
            or verifier_input['assignment']['role'] != roles.get(verifier_facts['provider'])
            or (worker_facts['provider'] == verifier_facts['provider'] and worker_facts['provider'] != 'codex')
            or worker_input['assignment']['assignment_id'] == verifier_input['assignment']['assignment_id']
            or verifier_input['assignment']['context'] != [host_outcome_digest]
            or verifier_input['assignment']['required_evidence'] != worker_input['assignment']['required_evidence']
            or worker_input['assignment']['verification_required'] is not True
            or verifier_input['assignment']['verification_required'] is not False):
        raise HistoryError('history_conflict')
    return {'worker': worker_reference, 'verifier': verifier_reference}


def operational_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest, *, before_reply):
    if type(context) is not OperationalQAContext or not callable(before_reply):
        raise HistoryError('history_invalid_input')
    # A private immutable copy survives mutable server adapters and body I/O.
    _, captured = context.snapshot()
    context = OperationalQAContext.from_snapshot(captured)
    reference = current_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest)
    for name in ('worker', 'verifier'):
        binding = AttemptBinding(**reference[name]['binding'])
        body = history.read_result(binding)
        terminal = history.record(reference[name]['terminal_record_id'])
        checked_result_body(body, captured[name]['assignment'], parse_bounded_json(terminal.content))
    before_reply()  # Fresh native permission after both artifact reads, outside SQL.
    current = current_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest)
    if current != reference:
        raise HistoryError('history_conflict')
    return reference
