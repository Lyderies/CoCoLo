"""Protected project fixed-check receipts; no check execution or QA acceptance.

The server must independently hold the actual approved runner's report and the
original pair inputs. This owner helper cannot establish that native custody.
Known receipts are historical metadata and never renew execution or QA windows.
"""
import re
import secrets

from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, matches, valid_integer
from .host_result_contracts import OperationalQAContext
from .learning_contracts import canonical_json, content_digest, keyed_digest, parse_bounded_json
from .learning_engine import _check_report_digests
from .project_operational_qa import current_qa_scope, operational_qa_scope
from .project_worker_result import current_host_reference

MAX_CHECK_REPORT_BYTES = 65_536
MAX_PENDING_CHECK_RECEIPTS = 64
MAX_CHECK_RECEIPTS = 4096
CHECK_TTL_MS = 600_000
_RECEIPT = re.compile(r'oqc_[0-9a-f]{48}')
_SCOPE_DOMAIN = b'project-qa-check-scope-v1\0'
_REQUEST_DOMAIN = b'project-qa-check-request-v1\0'
_SEAL_DOMAIN = b'project-qa-check-row-v1\0'
_IDENTITY_DOMAIN = b'project-qa-check-identity-v1\0'
_JOURNAL_DOMAIN = b'project-qa-check-journal-v1\0'
CHECK_SCHEMA = """
CREATE TABLE project_qa_check_receipts(
 project_id TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,
 receipt_id TEXT NOT NULL,worker_outcome_digest TEXT NOT NULL,verifier_outcome_digest TEXT NOT NULL,
 scope_digest TEXT NOT NULL,report_digest TEXT NOT NULL,report BLOB NOT NULL,
 created_at_ms INTEGER NOT NULL,expires_at_ms INTEGER NOT NULL,consumed_at_ms INTEGER,
 record_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,request_id),UNIQUE(project_id,receipt_id),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE TABLE project_qa_check_requests(
 project_id TEXT NOT NULL,request_id TEXT NOT NULL,request_digest TEXT NOT NULL,
 receipt_id TEXT NOT NULL,receipt_identity_digest TEXT NOT NULL,created_at_ms INTEGER NOT NULL,
 request_seal TEXT NOT NULL,
 PRIMARY KEY(project_id,request_id),UNIQUE(project_id,receipt_id),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
"""
CHECK_COLUMNS = {'project_qa_check_receipts': (
    'project_id', 'request_id', 'request_digest', 'receipt_id', 'worker_outcome_digest',
    'verifier_outcome_digest', 'scope_digest', 'report_digest', 'report',
    'created_at_ms', 'expires_at_ms', 'consumed_at_ms', 'record_seal'),
    'project_qa_check_requests': ('project_id', 'request_id', 'request_digest', 'receipt_id',
        'receipt_identity_digest', 'created_at_ms', 'request_seal')}
_FIELDS = CHECK_COLUMNS['project_qa_check_receipts']
_REQUEST_FIELDS = CHECK_COLUMNS['project_qa_check_requests']


def _key(history):
    history._require_owner()
    if type(history._result_key) is not bytes or len(history._result_key) != 32:
        raise HistoryError('history_invalid_input')
    return history._result_key


def _report(value):
    try:
        raw = canonical_json(value)
        if not 0 < len(raw) <= MAX_CHECK_REPORT_BYTES:
            raise ValueError()
        report = parse_bounded_json(raw, max_bytes=MAX_CHECK_REPORT_BYTES)
        if (type(report) is not dict or report.keys() != {'deterministic_checks', 'security_checks', 'observed_at_ms'}
                or not valid_integer(report['observed_at_ms'])):
            raise ValueError()
        _, deterministic = _check_report_digests(report['deterministic_checks'], 'deterministic_checks')
        _, security = _check_report_digests(report['security_checks'], 'security_checks')
    except Exception:
        raise HistoryError('history_invalid_input') from None
    return raw, report, deterministic and security


def _seal(key, material):
    return keyed_digest(key, _SEAL_DOMAIN + canonical_json(material))


def qa_scope_digest(history, context):
    """Bind internal consumers to the same protected original input snapshot."""
    key = _key(history)
    if type(context) is not OperationalQAContext:
        raise HistoryError('history_invalid_input')
    raw, _ = context.snapshot()
    return keyed_digest(key, _SCOPE_DOMAIN + raw)


def _receipt_identity(key, value):
    # Consumption changes its sealed state, never its original issuance identity.
    return keyed_digest(key, _IDENTITY_DOMAIN + canonical_json({n: value[n]
        for n in _FIELDS if n not in {'record_seal', 'consumed_at_ms'}}))


def _request_journal(history, request_id):
    key = _key(history)
    row = history._connection.execute('SELECT * FROM project_qa_check_requests '
        'WHERE project_id=? AND request_id=?', (history.project_id, request_id)).fetchone()
    if row is None:
        return None
    try:
        value = dict(row)
        if (set(value) != set(_REQUEST_FIELDS) or value['project_id'] != history.project_id
                or value['request_id'] != request_id or not matches(value['request_id'], _ID)
                or not matches(value['receipt_id'], _RECEIPT) or not valid_integer(value['created_at_ms'])
                or not all(matches(value[n], _DIGEST) for n in ('request_digest', 'receipt_identity_digest', 'request_seal'))):
            raise ValueError()
        material = {n: value[n] for n in _REQUEST_FIELDS if n != 'request_seal'}
        expected = keyed_digest(key, _JOURNAL_DOMAIN + canonical_json(material))
        if not secrets.compare_digest(value['request_seal'], expected):
            raise ValueError()
    except Exception:
        raise HistoryError('history_conflict') from None
    return value


def read_check_receipt(history, receipt_id):
    """Validate original internal material without a clock or acceptance decision."""
    key = _key(history)
    if not matches(receipt_id, _RECEIPT):
        raise HistoryError('history_invalid_input')
    row = history._connection.execute('SELECT * FROM project_qa_check_receipts '
        'WHERE project_id=? AND receipt_id=?', (history.project_id, receipt_id)).fetchone()
    if row is None:
        raise HistoryError('history_not_found')
    try:
        if set(row.keys()) != set(_FIELDS):
            raise ValueError()
        value = dict(row)
        raw = history._stored_blob(value['report'], maximum=MAX_CHECK_REPORT_BYTES)
        encoded, report, _ = _report(parse_bounded_json(raw, max_bytes=MAX_CHECK_REPORT_BYTES))
        value['report'] = report
        if (raw != encoded or value['project_id'] != history.project_id
                or value['receipt_id'] != receipt_id or not matches(value['request_id'], _ID)
                or not all(matches(value[n], _DIGEST) for n in ('request_digest', 'worker_outcome_digest',
                    'verifier_outcome_digest', 'scope_digest', 'report_digest', 'record_seal'))
                or value['worker_outcome_digest'] == value['verifier_outcome_digest']
                or content_digest(report) != value['report_digest']
                or not valid_integer(value['created_at_ms'])
                or not valid_integer(value['expires_at_ms'], minimum=value['created_at_ms'] + 1)
                or value['expires_at_ms'] > value['created_at_ms'] + CHECK_TTL_MS
                or report['observed_at_ms'] > value['created_at_ms'] + 1000
                or (value['consumed_at_ms'] is not None and not valid_integer(value['consumed_at_ms'],
                    minimum=value['created_at_ms'], maximum=value['expires_at_ms'] - 1))):
            raise ValueError()
        material = {n: value[n] for n in _FIELDS if n != 'record_seal'}
        if not secrets.compare_digest(value['record_seal'], _seal(key, material)):
            raise ValueError()
        journal = _request_journal(history, value['request_id'])
        if (journal is None or journal['receipt_id'] != receipt_id
                or journal['request_digest'] != value['request_digest']
                or journal['created_at_ms'] != value['created_at_ms']
                or journal['receipt_identity_digest'] != _receipt_identity(key, value)):
            raise ValueError()
    except Exception:
        raise HistoryError('history_conflict') from None
    return value


def _metadata(row, *, known):
    _, _, passed = _report(row['report'])
    return {'check_receipt': row['receipt_id'], 'check_report_digest': row['report_digest'],
            'checks_passed': passed, 'expires_at_ms': row['expires_at_ms'], 'known': known}


def consume_check_receipt(history, receipt):
    """Internal atomic-QA transition; never a separately activated API."""
    key = _key(history)
    if not history._connection.in_transaction or type(receipt) is not dict:
        raise HistoryError('history_invalid_input')
    current = read_check_receipt(history, receipt.get('receipt_id'))
    now = history._result_now()
    if (current != receipt or current['consumed_at_ms'] is not None
            or not valid_integer(now, minimum=current['created_at_ms'], maximum=current['expires_at_ms'] - 1)):
        raise HistoryError('history_conflict')
    material = {n: current[n] for n in _FIELDS if n != 'record_seal'}
    material['consumed_at_ms'] = now
    seal = _seal(key, material)
    updated = history._connection.execute('UPDATE project_qa_check_receipts '
        'SET consumed_at_ms=?,record_seal=? WHERE project_id=? AND receipt_id=? '
        'AND consumed_at_ms IS NULL AND record_seal=?',
        (now, seal, history.project_id, current['receipt_id'], current['record_seal']))
    if updated.rowcount != 1:
        raise HistoryError('history_conflict')
    return {**material, 'record_seal': seal}


def _known(history, request_id, request_digest, before_commit):
    row = history._connection.execute('SELECT receipt_id FROM project_qa_check_receipts '
        'WHERE project_id=? AND request_id=?', (history.project_id, request_id)).fetchone()
    journal = _request_journal(history, request_id)
    if row is None and journal is None:
        return None
    if row is None or journal is None or row['receipt_id'] != journal['receipt_id']:
        raise HistoryError('history_conflict')
    value = read_check_receipt(history, row['receipt_id'])
    if value['request_id'] != request_id or value['request_digest'] != request_digest:
        raise HistoryError('history_conflict')
    before_commit()
    _key(history)
    current = read_check_receipt(history, row['receipt_id'])
    if current != value:
        raise HistoryError('history_conflict')
    return _metadata(current, known=True)


def issue_check_receipt(history, context, host_outcome_digest, verifier_host_outcome_digest,
                        report, *, request_id, before_commit):
    """Issue once from protected originals; no model/client-selected checks run."""
    key = _key(history)
    if (type(context) is not OperationalQAContext or not matches(request_id, _ID) or not callable(before_commit)
            or not matches(host_outcome_digest, _DIGEST) or not matches(verifier_host_outcome_digest, _DIGEST)
            or host_outcome_digest == verifier_host_outcome_digest):
        raise HistoryError('history_invalid_input')
    _, captured = context.snapshot()
    context = OperationalQAContext.from_snapshot(captured)
    encoded_report, report, _ = _report(report)
    report_digest = content_digest(report)
    scope_digest = qa_scope_digest(history, context)
    request_digest = keyed_digest(key, _REQUEST_DOMAIN + canonical_json({
        'request_id': request_id, 'scope_digest': scope_digest, 'worker': host_outcome_digest,
        'verifier': verifier_host_outcome_digest, 'report_digest': report_digest}))
    known = _known(history, request_id, request_digest, before_commit)
    if known is not None:
        return known
    reference = operational_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest,
                                     before_reply=before_commit)
    outcomes = [current_host_reference(history, AttemptBinding(**reference[name]['binding']), digest)[4]
        for name, digest in (('worker', host_outcome_digest), ('verifier', verifier_host_outcome_digest))]
    if report['observed_at_ms'] < max(value['observed_at_ms'] for value in outcomes):
        raise HistoryError('history_invalid_input')
    # No artifact reads or runner execution enter this fixed transaction.
    with history._transaction():
        known = _known(history, request_id, request_digest, before_commit)
        if known is not None:
            return known
        before_commit()
        _key(history)
        if current_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest) != reference:
            raise HistoryError('history_conflict')
        now = history._result_now()
        expiry = min(now + CHECK_TTL_MS, *(reference[name]['expires_at_ms'] for name in ('worker', 'verifier')))
        if report['observed_at_ms'] > now + 1000 or expiry <= now:
            raise HistoryError('history_result_expired')
        total, pending = history._connection.execute('SELECT COUNT(*),COALESCE(SUM('
            'CASE WHEN consumed_at_ms IS NULL AND expires_at_ms>? THEN 1 ELSE 0 END),0) '
            'FROM project_qa_check_receipts WHERE project_id=?', (now, history.project_id)).fetchone()
        requests = history._connection.execute('SELECT COUNT(*) FROM project_qa_check_requests '
            'WHERE project_id=?', (history.project_id,)).fetchone()[0]
        orphan = history._connection.execute('SELECT 1 FROM project_qa_check_requests q '
            'LEFT JOIN project_qa_check_receipts r ON r.project_id=q.project_id AND r.request_id=q.request_id '
            'WHERE q.project_id=? AND (r.receipt_id IS NULL OR r.receipt_id!=q.receipt_id '
            'OR r.request_digest!=q.request_digest) LIMIT 1', (history.project_id,)).fetchone()
        if requests != total or orphan is not None:
            raise HistoryError('history_conflict')
        if total >= MAX_CHECK_RECEIPTS or pending >= MAX_PENDING_CHECK_RECEIPTS:
            raise HistoryError('history_result_capacity')
        material = {'project_id': history.project_id, 'request_id': request_id, 'request_digest': request_digest,
            'receipt_id': 'oqc_' + secrets.token_hex(24), 'worker_outcome_digest': host_outcome_digest,
            'verifier_outcome_digest': verifier_host_outcome_digest, 'scope_digest': scope_digest,
            'report_digest': report_digest, 'report': report, 'created_at_ms': now,
            'expires_at_ms': expiry, 'consumed_at_ms': None}
        value = {**material, 'record_seal': _seal(key, material)}
        history._connection.execute('INSERT INTO project_qa_check_receipts VALUES('
            + ','.join('?' for _ in _FIELDS) + ')',
            tuple(encoded_report if n == 'report' else value[n] for n in _FIELDS))
        journal = {n: value[n] for n in ('project_id', 'request_id', 'request_digest', 'receipt_id', 'created_at_ms')}
        journal['receipt_identity_digest'] = _receipt_identity(key, value)
        journal['request_seal'] = keyed_digest(key, _JOURNAL_DOMAIN + canonical_json(journal))
        history._connection.execute('INSERT INTO project_qa_check_requests VALUES('
            + ','.join('?' for _ in _REQUEST_FIELDS) + ')', tuple(journal[n] for n in _REQUEST_FIELDS))
        # Permission can change after an earlier authorization and before the
        # durable commit. Denial here rolls back both original journals.
        before_commit()
        _key(history)
        if (current_qa_scope(history, context, host_outcome_digest, verifier_host_outcome_digest) != reference
                or read_check_receipt(history, value['receipt_id']) != value):
            raise HistoryError('history_conflict')
    return _metadata(value, known=False)
