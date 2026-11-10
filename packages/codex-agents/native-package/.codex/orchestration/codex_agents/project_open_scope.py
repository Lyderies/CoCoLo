"""Project owner's original-input/verifier handoff before account host open.

Reads only local original history and the optional private worker result. It
creates no run, lease, account history, admission, clock window or execution.
Native authentication and current permission remain independent adapter gates.
"""
from .contracts import validate_assignment, validate_task
from .history_contracts import HistoryError
from .learning_contracts import canonical_json, content_digest, keyed_digest, provider_for_model, validate_execution_configuration
from .open_scope_contracts import OpenScopeContext, open_inputs_digest, seal_open_scope
from .project_worker_result import current_input_reference
from .result_contracts import checked_result_body


def current_open_scope(history, context):
    if type(context) is not OpenScopeContext:
        raise HistoryError('history_invalid_input')
    _, captured = context.snapshot()
    context = OpenScopeContext.from_snapshot(captured)
    history._require_owner()
    if (history._result_key is None or context.scope.project_id != history.project_id
            or history._orchestration_limits is None):
        raise HistoryError('history_not_authorized')
    # Never use the legacy missing-reference exception on a new open.
    view = history._orchestration_owner()._current(context.scope, context.orchestration_handle)
    snapshot = view['snapshot']
    if snapshot['status'] != 'active':
        raise HistoryError('history_orchestration_state')
    worker = None
    try:
        task = validate_task(captured['task'])
        route = captured['route']
        assignment = validate_assignment(captured['assignment'], task=task, route=route, parent_grant=captured['parent_grant'])
        configuration = validate_execution_configuration(route['execution_configuration'])
        if (assignment['task_id'] != task['id'] or route['task_id'] != task['id']
                or route.get('provider', 'codex') != 'codex' or route.get('dispatch') != 'bridge'
                or provider_for_model(assignment['model']) != 'codex' or route.get('sandbox_mode') != 'read-only'
                or any(not p.endswith(':read') for p in assignment['permissions']['filesystem'])
                or assignment['permissions']['mcps']
                or any(assignment[n] != configuration[n] or assignment[n] != route[n] for n in ('model', 'effort', 'role'))
                or assignment['verification_required'] is not configuration['verification_required']
                or route['verification_required'] is not configuration['verification_required']):
            raise ValueError()
        worker_digest = task.get('verification_for_outcome')
        if worker_digest is None:
            if context.worker is not None or task['family'] == 'verification':
                raise ValueError()
        else:
            if (context.worker is None or task['family'] != 'verification'
                    or not context.scope.matches_attempt(context.worker.binding)
                    or context.worker.binding.orchestration_handle != context.orchestration_handle):
                raise ValueError()
            reference, _, facts, _, _ = current_input_reference(history, context.worker, worker_digest)
            original = captured['worker']['assignment']
            if (facts['worker_outcome_digest'] is not None or facts['task_family'] == 'verification'
                    or facts['task_family'] != task['verification_for_family']
                    or facts['provider'] != task['verification_for_provider']
                    or assignment['role'] != 'codex_agents_verification_worker'
                    or assignment['assignment_id'] == original['assignment_id']
                    or assignment['context'] != [worker_digest]
                    or assignment['required_evidence'] != original['required_evidence']
                    or original['verification_required'] is not True or assignment['verification_required'] is not False):
                raise ValueError()
            worker = reference
    except HistoryError:
        raise
    except Exception:
        raise HistoryError('history_conflict') from None
    key = history._result_key
    return seal_open_scope(key, {'scope': context.scope.as_data(), 'orchestration_handle': context.orchestration_handle,
        'orchestration_id': snapshot['orchestration_id'], 'checkpoint_digest': view['checkpoint_digest'],
        'sequence': snapshot['sequence'], 'client_version': snapshot['client_version'],
        'inputs_digest': open_inputs_digest(assignment, task, route, captured['parent_grant']),
        'assignment_id': assignment['assignment_id'], 'attempt': assignment['attempt'],
        **{n + '_digest': keyed_digest(key, canonical_json(v)) for n, v in
           (('assignment', assignment), ('task', task), ('route', route), ('parent_grant', captured['parent_grant']))},
        'configuration_digest': content_digest(configuration), 'provider': 'codex', 'task_family': task['family'],
        'worker': worker, 'worker_context_digest': None if context.worker is None else keyed_digest(key,
            b'project-open-worker-input-v1\0'+canonical_json(captured['worker']))})


def original_open_scope(history, context, *, before_reply):
    if type(context) is not OpenScopeContext or not callable(before_reply):
        raise HistoryError('history_invalid_input')
    context = OpenScopeContext.from_snapshot(context.snapshot()[1])
    proof = current_open_scope(history, context)
    worker = proof['facts']['worker']
    if worker is not None:
        # The original typed result must still be retained and exactly bound.
        body = history.read_result(context.worker.binding)
        terminal = history.record(worker['terminal_record_id'])
        from .learning_contracts import parse_bounded_json
        checked_result_body(body, context.worker.assignment, parse_bounded_json(terminal.content))
    before_reply()
    if current_open_scope(history, context) != proof:
        raise HistoryError('history_conflict')
    return proof
