"""Project-owned verifier dependency lookup, before verifier open/admission.

Only the fixed writer reads SQLite and the original private result. Native
authentication/input custody and protected publication remain activation gates.
This returns a bounded reference, never a new admission or accepted QA result.
"""
from .contracts import ContractError, validate_task
from .execution_grant import MAX_EXECUTION_GRANT_BYTES, verify_execution_grant
from .history_contracts import AttemptBinding, HistoryError, _DIGEST, matches, valid_integer
from .host_result_contracts import HostResultContext, WorkerResultContext
from .learning_contracts import canonical_json, route_identity, content_digest, keyed_digest, parse_bounded_json, provider_for_model, validate_execution_configuration
from .project_host_open import read_host_open
from .result_contracts import assignment_bytes


def current_host_reference(history, binding, outcome_digest, *, live=True):
    """Read the original consumed return, including for a verification run.

    This internal persistence-owner helper does not authenticate a caller or
    authorize QA. Its caller must establish protected original input custody.
    """
    if not matches(outcome_digest, _DIGEST) or history._result_key is None or type(live) is not bool:
        raise HistoryError('history_invalid_input')
    history._require_owner()
    binding = history._result_binding(binding)
    # No legacy missing-reference exception: a real current project journal
    # and original pre-execution host open are mandatory on this new path.
    if history._connection.execute('SELECT 1 FROM project_orchestrations '
            'WHERE project_id=? AND orchestration_handle=?',
            (binding.project_id, binding.orchestration_handle)).fetchone() is None:
        raise HistoryError('history_orchestration_unavailable')
    if live:
        history._orchestration_owner().active_for(binding)
    else:
        history._orchestration_owner().metadata_for(binding)
    lease_row = history._connection.execute('SELECT * FROM result_leases WHERE project_id=? AND claim_id=?',
        (binding.project_id, binding.claim_id)).fetchone()
    if lease_row is None:
        raise HistoryError('history_result_not_reserved')
    lease = history._lease(lease_row)
    if lease.state != 'bound' or not lease.binding.matches_attempt(binding) or lease.claim_digest != binding.claim_digest:
        raise HistoryError('history_conflict')
    opened, proof = read_host_open(history, lease.binding)
    facts = proof['provenance']
    consumed = history._connection.execute('SELECT * FROM project_execution_consumptions '
        'WHERE project_id=? AND claim_id=?', (binding.project_id, binding.claim_id)).fetchone()
    if consumed is None:
        raise HistoryError('history_execution_not_ready')
    grant = verify_execution_grant(history._result_key, binding,
        history._stored_blob(consumed['grant'], maximum=MAX_EXECUTION_GRANT_BYTES))
    registered = history._execution_provenance(grant)
    if registered is None:
        raise HistoryError('history_execution_not_ready')
    registered = registered['provenance']
    if (consumed['registration_digest'] != grant.registration_digest
            or not valid_integer(consumed['consumed_at_ms'], minimum=grant.claimed_at_ms,
                                 maximum=grant.deadline_at_ms - 1)
            or any(facts[n] != getattr(grant, n) for n in
                ('assignment_digest', 'task_digest', 'route_digest', 'configuration_digest', 'lease_digest'))
            or any(facts[n] != registered[n] for n in ('assignment_id', 'provider', 'task_family', 'client_version',
                'admission_record_id', 'admission_content_digest', 'admission_observed_at_ms',
                'start_record_id', 'start_content_digest', 'observation_deadline_at_ms'))
            or facts['opened_at_ms'] != registered['started_at_ms']):
        raise HistoryError('history_conflict')
    identity = 'host_result_admission:' + keyed_digest(history._result_key,
        b'project-host-result-v1\0' + canonical_json(binding.as_data()))
    entry = history.record(identity)
    receipt = history._host_result_receipt(entry)['admission']
    terminal = receipt['dispatch_result']
    reservation = history.result_reservation(binding) if live else history._result_row(binding)[1]
    if live and reservation.state in {'expired', 'retiring'}:
        raise HistoryError('history_result_expired')
    if (entry.source != 'host' or receipt['run_id'] != binding.run_id
            or receipt['registration_digest'] != grant.registration_digest
            or receipt['binding_digest'] != identity.split(':', 1)[1]
            or receipt['host_outcome_digest'] != outcome_digest
            or terminal['bridge_run_id'] != binding.claim_id
            or (live and reservation.state != 'committed') or reservation.committed_at_ms is None
            or terminal['result_digest'] != reservation.result_digest
            or reservation.assignment_digest != grant.assignment_digest
            or reservation.hard_deadline_at_ms != grant.deadline_at_ms
            or terminal['record_id'] != reservation.terminal_record_id):
        raise HistoryError('history_conflict')
    outcome = parse_bounded_json(history.record(receipt['outcome_record_id']).content)
    if (outcome['task_slice'].split('-')[0] != facts['task_family']
            or outcome['assignment_id'] != facts['assignment_id']
            or outcome['configuration_digest'] != grant.configuration_digest
            or reservation.result_digest not in outcome['evidence_digests']
            or grant.assignment_digest not in outcome['evidence_digests']):
        raise HistoryError('history_conflict')
    # A mutable reservation/result label cannot replace the keyed original
    # prepared request or the actual protected terminal/outbox companions.
    outbox = history._connection.execute('SELECT * FROM terminal_outbox WHERE project_id=? AND claim_id=?',
        (binding.project_id, binding.claim_id)).fetchone()
    if outbox is None:
        raise HistoryError('history_result_not_ready')
    delivery = history._delivery(outbox)
    terminal_entry = history.record(delivery.record_id)
    if (delivery.binding != binding or terminal_entry.source != 'host'
            or terminal_entry.content != canonical_json(terminal)
            or terminal_entry.content_digest != receipt['terminal_content_digest']
            or delivery.record_content_digest != terminal_entry.content_digest
            or delivery.result_digest != reservation.result_digest):
        raise HistoryError('history_conflict')
    material = {'binding': binding.as_data(), 'record': terminal,
        'result_digest': reservation.result_digest, 'assignment_digest': grant.assignment_digest}
    usage = history._client_usage_entry(binding, terminal, outcome['model'])
    if usage is not None:
        material['client_usage_record'] = parse_bounded_json(usage.content)
    row, _ = history._result_row(binding)
    if row['prepared_digest'] != keyed_digest(history._result_key, canonical_json(material)):
        raise HistoryError('history_conflict')
    reference = {'binding': binding.as_data(), 'host_outcome_digest': outcome_digest,
        'provider': facts['provider'], 'task_family': facts['task_family'],
        'host_open_record_id': opened.record_id, 'host_open_content_digest': opened.content_digest,
        'result_digest': reservation.result_digest, 'body_size': reservation.body_size,
        'terminal_record_id': reservation.terminal_record_id, 'expires_at_ms': reservation.expires_at_ms}
    return reference, reservation, facts, grant, outcome


def current_input_reference(history, context, outcome_digest, *, live=True):
    """Bind one original full input snapshot to its actual consumed return."""
    if type(context) is not HostResultContext:
        raise HistoryError('history_invalid_input')
    _, original = context.snapshot()
    binding = AttemptBinding(**original['binding'])
    values = current_host_reference(history, binding, outcome_digest, live=live)
    _, _, facts, grant, outcome = values
    try:
        task = validate_task(original['task'])
        route, assignment = original['route'], original['assignment']
        configuration = validate_execution_configuration(route['execution_configuration'])
        if (grant.assignment_digest != keyed_digest(history._result_key, assignment_bytes(assignment, binding))
                or grant.task_digest != keyed_digest(history._result_key, canonical_json(task))
                or grant.route_digest != keyed_digest(history._result_key, canonical_json(route_identity(route)))
                or grant.configuration_digest != content_digest(configuration)
                or configuration['client_digest'] != content_digest(original['client_capability'])
                or any(outcome[field] != assignment[field] for field in ('assignment_id', 'model', 'effort', 'role'))
                or facts['provider'] != route.get('provider', 'codex')
                or provider_for_model(outcome['model']) != facts['provider']
                or task['family'] != facts['task_family']):
            raise ValueError()
    except (ContractError, KeyError, TypeError, ValueError):
        raise HistoryError('history_conflict') from None
    return values


def current_worker_reference(history, context, outcome_digest):
    if type(context) is not WorkerResultContext:
        raise HistoryError('history_invalid_input')
    _, captured = context.snapshot()
    try:
        task = validate_task(captured['task'])
        if task.get('verification_for_outcome') != outcome_digest:
            raise ValueError()
    except Exception:
        raise HistoryError('history_invalid_input') from None
    reference, reservation, facts, _, _ = current_host_reference(
        history, AttemptBinding(**captured['binding']), outcome_digest)
    if (facts['worker_outcome_digest'] is not None or facts['task_family'] == 'verification'
            or facts['task_family'] != task['verification_for_family']
            or facts['provider'] != task['verification_for_provider']):
        raise HistoryError('history_conflict')
    return reference, reservation


def verification_worker_reference(history, context, outcome_digest, *, before_reply):
    if not callable(before_reply):
        raise HistoryError('history_invalid_input')
    reference, reservation = current_worker_reference(history, context, outcome_digest)
    # The existing reader checks fixed regular/no-symlink files, original expiry,
    # exact keyed body digest and size. No artifact path or body enters this reply.
    history.read_result(AttemptBinding(**reference['binding']))
    before_reply()  # Current native authority after artifact I/O, outside SQL transactions.
    history._require_owner()
    current, current_reservation = current_worker_reference(history, context, outcome_digest)
    if current != reference or current_reservation != reservation:
        raise HistoryError('history_conflict')
    return reference
