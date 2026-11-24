"""Closed, content-free B7a status reply. Safe to import in a client without a store."""
from .history_contracts import HistoryError, valid_integer

HOLD_KINDS = ('driver_entry', 'preparation', 'lease', 'reservation', 'orchestration', 'open_binding', 'result_custody')
UNTRACKED_KINDS = frozenset({'driver_entry', 'preparation', 'orchestration'})
TIMER_KINDS = frozenset({'orchestration', 'result_custody'})
MAX_REPAIR_ENTRIES = 20
CONTEXT_BYTES_LIMIT = 256 * 1_048_576
REPORT_FIELDS = frozenset({'project_holds', 'result_custody', 'result_retirement_repairs'})
HOLD_FIELDS = frozenset({'tracking', 'held', 'releasing', 'past_due', 'next_eligible', 'oldest_taken_at_ms'})
CUSTODY_FIELDS = frozenset({'live', 'context_bytes', 'max_live_context_bytes', 'retained_until_ms',
                           'awaiting_verifier', 'legacy_no_context', 'ended_by_update_unrecorded'})
DIRECTORY_REPAIR = {
    'reason': 'An unexpected directory is blocking result cleanup.',
    'action': 'Inspect and remove the unexpected directory in this project service store, then run learning-status again.',
    'operator_needed': True,
}
RETRY_REPAIR = {
    'reason': 'Result cleanup is waiting for a retry.',
    'action': 'Wait for the next cleanup pass; if this persists, check storage permissions and free space.',
    'operator_needed': False,
}
OPERATOR_REPAIR = {
    'reason': 'Result cleanup requires operator attention.',
    'action': 'Inspect this project service store and resolve the cleanup obstruction, then run learning-status again.',
    'operator_needed': True,
}


def _closed(value, fields):
    return type(value) is dict and value.keys() == fields


def _timestamp(value):
    return value is None or valid_integer(value)


def checked_holds_report(value):
    """Reject open fields, private text, bool-as-int and malformed nested values before presentation."""
    def require(condition):
        if not condition:
            raise HistoryError('history_invalid_response')

    require(_closed(value, REPORT_FIELDS))
    holds, custody, repairs = (value[name] for name in ('project_holds', 'result_custody', 'result_retirement_repairs'))
    require(_closed(holds, set(HOLD_KINDS)))
    for kind, counts in holds.items():
        require(_closed(counts, HOLD_FIELDS))
        require(counts['tracking'] == ('not_tracked' if kind in UNTRACKED_KINDS else 'tracked'))
        require(all(valid_integer(counts[key], maximum=1 << 32) for key in ('held', 'releasing')))
        require(all(_timestamp(counts[key]) for key in ('next_eligible', 'oldest_taken_at_ms')))
        require((counts['held'] == 0) == (counts['oldest_taken_at_ms'] is None))
        if kind in TIMER_KINDS:
            require(valid_integer(counts['past_due'], maximum=counts['held']))
            require(counts['next_eligible'] is not None or counts['past_due'] == 0)
        else:
            require(counts['past_due'] is None and counts['next_eligible'] is None)
        if kind in UNTRACKED_KINDS:
            require(counts['held'] == counts['releasing'] == 0 and counts['next_eligible'] is None)
    require(_closed(custody, CUSTODY_FIELDS))
    require(all(valid_integer(custody[key], maximum=1 << 32) for key in
                ('live', 'awaiting_verifier', 'legacy_no_context', 'ended_by_update_unrecorded')))
    require(type(custody['max_live_context_bytes']) is int and custody['max_live_context_bytes'] == CONTEXT_BYTES_LIMIT)
    require(valid_integer(custody['context_bytes'], maximum=CONTEXT_BYTES_LIMIT))
    require(_timestamp(custody['retained_until_ms']))
    require(custody['awaiting_verifier'] <= custody['live'] and custody['legacy_no_context'] <= custody['live'])
    require(_closed(repairs, {'entries', 'truncated'}) and type(repairs['truncated']) is bool)
    require(type(repairs['entries']) is list and len(repairs['entries']) <= MAX_REPAIR_ENTRIES)
    retry_seen = False
    for entry in repairs['entries']:
        require(_closed(entry, {'reason', 'action', 'operator_needed'}) and type(entry['operator_needed']) is bool)
        require(entry in (DIRECTORY_REPAIR, OPERATOR_REPAIR) if entry['operator_needed'] else entry == RETRY_REPAIR)
        require(not (retry_seen and entry['operator_needed']))
        retry_seen |= not entry['operator_needed']
    return value
