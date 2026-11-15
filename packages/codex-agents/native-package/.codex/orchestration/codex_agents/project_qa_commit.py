"""Atomic project QA from original private results and original check custody.

Only the persistence owner uses these functions. Native permission, original
input/runner custody and protected publication remain separate activation gates.
Routine accepted QA grants no launch, promotion, release or account authority.
"""
import re
import secrets

from .contracts import ContractError
from .history_contracts import HistoryError, MAX_SEQUENCE, _DIGEST, _ID, matches, valid_integer
from .host_result_contracts import OperationalQAContext
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, content_digest, keyed_digest, parse_bounded_json
from .learning_engine import build_operational_qa_records
from .operational_qa_contracts import checked_operational_qa
from .project_operational_qa import current_qa_scope, operational_qa_inputs
from .project_qa_checks import MAX_CHECK_RECEIPTS, consume_check_receipt, qa_scope_digest, read_check_receipt

_RECEIPT = re.compile(r'oqc_[0-9a-f]{48}')
_QA_DOMAIN = b'project-operational-qa-row-v1\0'
QA_SCHEMA = """
CREATE TABLE project_operational_qa(
 project_id TEXT NOT NULL,worker_outcome_digest TEXT NOT NULL,verifier_outcome_digest TEXT NOT NULL,
 worker_admission_digest TEXT NOT NULL,verifier_admission_digest TEXT NOT NULL,
 check_receipt_id TEXT NOT NULL,scope_digest TEXT NOT NULL,qa_digest TEXT NOT NULL,qa BLOB NOT NULL,
 outcome_record_id TEXT NOT NULL,outcome_digest TEXT NOT NULL,source_sequence INTEGER NOT NULL,
 ingested_at_ms INTEGER NOT NULL,late INTEGER NOT NULL CHECK(late IN (0,1)),
 consumed_at_ms INTEGER NOT NULL,record_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,worker_outcome_digest),UNIQUE(project_id,check_receipt_id),
 UNIQUE(project_id,outcome_record_id),FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
"""
QA_COLUMNS = {'project_operational_qa': ('project_id', 'worker_outcome_digest', 'verifier_outcome_digest',
    'worker_admission_digest', 'verifier_admission_digest',
    'check_receipt_id', 'scope_digest', 'qa_digest', 'qa', 'outcome_record_id', 'outcome_digest',
    'source_sequence', 'ingested_at_ms', 'late', 'consumed_at_ms', 'record_seal')}
_FIELDS = QA_COLUMNS['project_operational_qa']


def _key(history):
    history._require_owner()
    if type(history._result_key) is not bytes or len(history._result_key) != 32:
        raise HistoryError('history_invalid_input')
    return history._result_key


def _receipt(history, context, worker_digest, verifier_digest, receipt_id):
    value = read_check_receipt(history, receipt_id)
    if (value['worker_outcome_digest'] != worker_digest or value['verifier_outcome_digest'] != verifier_digest
            or value['scope_digest'] != qa_scope_digest(history, context)):
        raise HistoryError('history_conflict')
    return value


def _host_event(history, reference, outcome):
    """Preserve original host event provenance, including failed verifiers."""
    binding = reference['binding']
    identity = 'host_result_admission:' + keyed_digest(history._result_key,
        b'project-host-result-v1\0' + canonical_json(binding))
    entry = history.record(identity)
    admission = history._host_result_receipt(entry)['admission']
    event = parse_bounded_json(history.record(admission['event_record_id']).content)
    codes, status = {
        'unverified': ({'item_completed', 'turn_completed'}, 'completed'),
        'failed': ({'turn_failed'}, 'failed'), 'interrupted': ({'turn_interrupted'}, 'interrupted'),
        'cancelled': ({'turn_interrupted'}, 'cancelled'),
    }.get(outcome['status'], (set(), None))
    if (event['event_code'] not in codes or event['status'] != status
            or event['item_digest'] != reference['result_digest'] or event['usage'] != outcome['usage']
            or any(event[n] != outcome[n] for n in ('run_id', 'assignment_id', 'model', 'effort', 'role',
                'task_slice', 'topology', 'configuration_digest', 'coverage'))):
        raise HistoryError('history_execution_not_ready')
    return entry.content_digest


def _summary(value, *, known):
    qa = value['qa']
    return {'host_outcome_digest': value['worker_outcome_digest'],
        'verifier_host_outcome_digest': value['verifier_outcome_digest'], 'check_receipt': value['check_receipt_id'],
        'qa_record_id': qa['record_id'], 'qa_digest': value['qa_digest'],
        'outcome_record_id': value['outcome_record_id'], 'outcome_digest': value['outcome_digest'],
        **{n: qa[n] for n in ('accepted', 'deterministic_pass', 'security_pass', 'required_criteria_total',
                             'required_criteria_passed', 'verifier_disagreement')}, 'known': known}


def read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id):
    """Check all original companions without touching a clock or private file."""
    key = _key(history)
    if (type(context) is not OperationalQAContext or not matches(worker_digest, _DIGEST)
            or not matches(verifier_digest, _DIGEST) or not matches(receipt_id, _RECEIPT)):
        raise HistoryError('history_invalid_input')
    row = history._connection.execute('SELECT * FROM project_operational_qa '
        'WHERE project_id=? AND worker_outcome_digest=?', (history.project_id, worker_digest)).fetchone()
    if row is None:
        raise HistoryError('history_not_found')
    try:
        if set(row.keys()) != set(_FIELDS):
            raise ValueError()
        value = dict(row)
        raw = history._stored_blob(value['qa'], maximum=MAX_RECORD_BYTES)
        qa = checked_operational_qa(parse_bounded_json(raw))
        value['qa'] = qa
        if (raw != canonical_json(qa) or value['project_id'] != history.project_id
                or value['worker_outcome_digest'] != worker_digest or value['verifier_outcome_digest'] != verifier_digest
                or value['check_receipt_id'] != receipt_id or not matches(value['outcome_record_id'], _ID)
                or not all(matches(value[n], _DIGEST) for n in ('worker_outcome_digest', 'verifier_outcome_digest',
                    'worker_admission_digest', 'verifier_admission_digest',
                    'scope_digest', 'qa_digest', 'outcome_digest', 'record_seal'))
                or content_digest(qa) != value['qa_digest']
                or not valid_integer(value['source_sequence'], minimum=1, maximum=MAX_SEQUENCE - 1)
                or not valid_integer(value['ingested_at_ms']) or value['ingested_at_ms'] != value['consumed_at_ms']
                or type(value['late']) is not int or value['late'] not in (0, 1)):
            raise ValueError()
        material = {n: value[n] for n in _FIELDS if n != 'record_seal'}
        if not secrets.compare_digest(value['record_seal'], keyed_digest(key, _QA_DOMAIN + canonical_json(material))):
            raise ValueError()
        receipt = _receipt(history, context, worker_digest, verifier_digest, receipt_id)
        if receipt['consumed_at_ms'] != value['consumed_at_ms'] or receipt['scope_digest'] != value['scope_digest']:
            raise ValueError()
        # Historical state is metadata only. Original owned journals, admission,
        # consumed grant, terminal, outbox and usage remain mandatory companions.
        reference = current_qa_scope(history, context, worker_digest, verifier_digest, live=False)
        _, captured = context.snapshot()
        if (qa['host_outcome_digest'] != worker_digest or qa['verifier_host_outcome_digest'] != verifier_digest
                or qa['observed_at_ms'] != receipt['report']['observed_at_ms']
                or qa['required_criteria_total'] != len(captured['worker']['assignment']['required_evidence'])):
            raise ValueError()
        for name, prefix in (('worker', 'evaluator'), ('verifier', 'verifier')):
            if (qa[prefix + '_result_digest'] != reference[name]['result_digest']
                    or qa[prefix + '_assignment_digest'] != keyed_digest(key, canonical_json(captured[name]['assignment']))):
                raise ValueError()
            # Bind the exact original admission/event/outcome companion chain,
            # not just a caller's replacement records with recomputed SHA labels.
            identity = 'host_result_admission:' + keyed_digest(key,
                b'project-host-result-v1\0' + canonical_json(reference[name]['binding']))
            if history.record(identity).content_digest != value[name + '_admission_digest']:
                raise ValueError()
        for name in ('deterministic', 'security'):
            checks = receipt['report'][name + '_checks']
            if (qa[name + '_check_digests'] != sorted(content_digest(item) for item in checks)
                    or qa[name + '_pass'] != all(item['passed'] for item in checks)):
                raise ValueError()
        entry = history.record(value['outcome_record_id'])
        outcome = parse_bounded_json(entry.content)
        if (entry.source != 'verifier' or entry.content_digest != value['outcome_digest']
                or entry.source_sequence != value['source_sequence'] or entry.ingested_at_ms != value['ingested_at_ms']
                or int(entry.late) != value['late'] or outcome['record_type'] != 'outcome'
                or outcome['run_id'] != captured['worker']['binding']['run_id']
                or outcome['observed_at_ms'] != qa['observed_at_ms']
                or outcome['status'] != ('accepted' if qa['accepted'] else 'rejected')
                or any(outcome[n] != qa[n] for n in ('accepted', 'deterministic_pass', 'security_pass',
                    'required_criteria_total', 'required_criteria_passed', 'verifier_disagreement'))
                or not {worker_digest, verifier_digest, value['qa_digest']} <= set(outcome['evidence_digests'])):
            raise ValueError()
    except Exception:
        raise HistoryError('history_conflict') from None
    return value


def _known(history, context, worker_digest, verifier_digest, receipt_id, before_commit):
    row = history._connection.execute('SELECT 1 FROM project_operational_qa '
        'WHERE project_id=? AND worker_outcome_digest=?', (history.project_id, worker_digest)).fetchone()
    if row is None:
        if _receipt(history, context, worker_digest, verifier_digest, receipt_id)['consumed_at_ms'] is not None:
            # Missing QA after committed consumption is unknown, never a fresh
            # receipt, clock window or opportunity to recreate an outcome.
            raise HistoryError('history_conflict')
        return None
    value = read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id)
    before_commit()
    if read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id) != value:
        raise HistoryError('history_conflict')
    return _summary(value, known=True)


def commit_operational_qa(history, context, worker_digest, verifier_digest, receipt_id, *, before_commit):
    key = _key(history)
    if (type(context) is not OperationalQAContext or not callable(before_commit)
            or not matches(worker_digest, _DIGEST) or not matches(verifier_digest, _DIGEST)
            or worker_digest == verifier_digest or not matches(receipt_id, _RECEIPT)):
        raise HistoryError('history_invalid_input')
    _, captured = context.snapshot()
    context = OperationalQAContext.from_snapshot(captured)
    known = _known(history, context, worker_digest, verifier_digest, receipt_id, before_commit)
    if known is not None:
        return known
    receipt = _receipt(history, context, worker_digest, verifier_digest, receipt_id)
    reference, inputs, results, outcomes = operational_qa_inputs(history, context, worker_digest, verifier_digest,
                                                                before_reply=before_commit)
    if outcomes['worker']['status'] != 'unverified' or outcomes['worker'].get('execution_state', 'completed') != 'completed':
        raise HistoryError('history_execution_not_ready')
    admissions = {name: _host_event(history, reference[name], outcomes[name]) for name in ('worker', 'verifier')}
    try:
        qa, outcome = build_operational_qa_records(digest_key=key, host_outcome=outcomes['worker'],
            verifier_host_outcome=outcomes['verifier'], evaluator_assignment=inputs['worker']['assignment'],
            evaluator_result=results['worker'], verifier_assignment=inputs['verifier']['assignment'],
            verifier_result=results['verifier'], check_report=receipt['report'])
        checked_operational_qa(qa)
    except (ContractError, HistoryError):
        raise HistoryError('history_conflict') from None
    encoded_qa, encoded_outcome = canonical_json(qa), canonical_json(outcome)
    checked = history._checked_record(encoded_outcome, 'verifier', receipt['report']['observed_at_ms'])
    with history._transaction():
        known = _known(history, context, worker_digest, verifier_digest, receipt_id, before_commit)
        if known is not None:
            return known
        before_commit()
        _key(history)
        if current_qa_scope(history, context, worker_digest, verifier_digest) != reference:
            raise HistoryError('history_conflict')
        if any(_host_event(history, reference[name], outcomes[name]) != admissions[name] for name in ('worker', 'verifier')):
            raise HistoryError('history_conflict')
        if _receipt(history, context, worker_digest, verifier_digest, receipt_id) != receipt:
            raise HistoryError('history_conflict')
        if history._connection.execute('SELECT COUNT(*) FROM project_operational_qa '
                'WHERE project_id=?', (history.project_id,)).fetchone()[0] >= MAX_CHECK_RECEIPTS:
            raise HistoryError('history_result_capacity')
        consumed = consume_check_receipt(history, receipt)
        entry = history._append_locked(checked, encoded_outcome, 'verifier', consumed['consumed_at_ms'])
        material = {'project_id': history.project_id, 'worker_outcome_digest': worker_digest,
            'verifier_outcome_digest': verifier_digest, 'check_receipt_id': receipt_id,
            **{name + '_admission_digest': admissions[name] for name in ('worker', 'verifier')},
            'scope_digest': receipt['scope_digest'], 'qa_digest': content_digest(qa), 'qa': qa,
            'outcome_record_id': entry.record_id, 'outcome_digest': entry.content_digest,
            'source_sequence': entry.source_sequence, 'ingested_at_ms': entry.ingested_at_ms,
            'late': int(entry.late), 'consumed_at_ms': consumed['consumed_at_ms']}
        value = {**material, 'record_seal': keyed_digest(key, _QA_DOMAIN + canonical_json(material))}
        history._connection.execute('INSERT INTO project_operational_qa VALUES('
            + ','.join('?' for _ in _FIELDS) + ')', tuple(encoded_qa if n == 'qa' else value[n] for n in _FIELDS))
        # Reauthorize after all internal writes, immediately before the owner
        # commits. Revocation never leaves a consumed receipt or accepted row.
        before_commit()
        _key(history)
        if (current_qa_scope(history, context, worker_digest, verifier_digest) != reference
                or read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id) != value):
            raise HistoryError('history_conflict')
    return _summary(value, known=False)


def operational_qa_details(history, context, worker_digest, verifier_digest, receipt_id, *, before_reply):
    if not callable(before_reply):
        raise HistoryError('history_invalid_input')
    value = read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id)
    before_reply()
    if read_operational_qa(history, context, worker_digest, verifier_digest, receipt_id) != value:
        raise HistoryError('history_conflict')
    return {'qa': value['qa'], 'summary': _summary(value, known=True),
            'outcome_commit': history._host_result_metadata(history.record(value['outcome_record_id']))}
