"""Closed approved QA metadata; no persistence, keys or native authority."""
from .history_contracts import HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import MAX_RECORD_BYTES, canonical_json, content_digest

QA_FIELDS = frozenset({
    'schema_version', 'record_type', 'record_id', 'host_outcome_digest', 'evaluator_assignment_digest',
    'evaluator_result_digest', 'verifier_host_outcome_digest', 'verifier_assignment_digest', 'verifier_result_digest',
    'deterministic_check_digests', 'security_check_digests', 'deterministic_pass', 'security_pass',
    'required_criteria_total', 'required_criteria_passed', 'verifier_disagreement', 'accepted', 'observed_at_ms'})
QA_SUMMARY_FIELDS = frozenset({
    'host_outcome_digest', 'verifier_host_outcome_digest', 'check_receipt', 'qa_record_id', 'qa_digest',
    'outcome_record_id', 'outcome_digest', 'accepted', 'deterministic_pass', 'security_pass',
    'required_criteria_total', 'required_criteria_passed', 'verifier_disagreement', 'known'})


def checked_operational_qa(value):
    if (type(value) is not dict or value.keys() != QA_FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['record_type'] != 'operational_qa'
            or not all(matches(value[n], _DIGEST) for n in ('host_outcome_digest', 'evaluator_assignment_digest',
                'evaluator_result_digest', 'verifier_host_outcome_digest', 'verifier_assignment_digest', 'verifier_result_digest'))
            or value['host_outcome_digest'] == value['verifier_host_outcome_digest']
            or not all(type(value[n]) is bool for n in ('deterministic_pass', 'security_pass', 'verifier_disagreement', 'accepted'))
            or not valid_integer(value['required_criteria_total'], minimum=1, maximum=10_000)
            or not valid_integer(value['required_criteria_passed'], maximum=value['required_criteria_total'])
            or not valid_integer(value['observed_at_ms'])):
        raise HistoryError('history_invalid_response')
    for name in ('deterministic_check_digests', 'security_check_digests'):
        if (type(value[name]) is not list or not 1 <= len(value[name]) <= 256
                or not all(matches(v, _DIGEST) for v in value[name]) or value[name] != sorted(value[name])):
            raise HistoryError('history_invalid_response')
    if (value['record_id'] != 'operational_qa:' + content_digest({k: v for k, v in value.items() if k != 'record_id'})
            or len(canonical_json(value)) > MAX_RECORD_BYTES
            or (value['accepted'] and (not value['deterministic_pass'] or not value['security_pass']
                or value['verifier_disagreement'] or value['required_criteria_passed'] != value['required_criteria_total']))):
        raise HistoryError('history_invalid_response')
    return value


def checked_qa_summary(value, worker_digest, verifier_digest, check_receipt):
    if (type(value) is not dict or value.keys() != QA_SUMMARY_FIELDS
            or value['host_outcome_digest'] != worker_digest or value['verifier_host_outcome_digest'] != verifier_digest
            or value['check_receipt'] != check_receipt
            or not all(matches(value[n], _DIGEST) for n in ('host_outcome_digest', 'verifier_host_outcome_digest',
                'qa_digest', 'outcome_digest'))
            or not matches(value['qa_record_id'], _ID) or not value['qa_record_id'].startswith('operational_qa:')
            or not matches(value['qa_record_id'].split(':', 1)[1], _DIGEST)
            or not matches(value['outcome_record_id'], _ID)
            or not all(type(value[n]) is bool for n in ('accepted', 'deterministic_pass', 'security_pass',
                'verifier_disagreement', 'known'))
            or not valid_integer(value['required_criteria_total'], minimum=1, maximum=10_000)
            or not valid_integer(value['required_criteria_passed'], maximum=value['required_criteria_total'])):
        raise HistoryError('history_invalid_response')
    if value['accepted'] and (not value['deterministic_pass'] or not value['security_pass']
            or value['verifier_disagreement'] or value['required_criteria_passed'] != value['required_criteria_total']):
        raise HistoryError('history_invalid_response')
    return value
