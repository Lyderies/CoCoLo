"""Closed fixed billing-probe values; no native or execution authority."""
from .admission_metadata import _headroom_state
from .contracts import ContractError
from .history_contracts import valid_integer
from .learning_contracts import ADMISSION_REASON_CODES, canonical_json, content_digest, validate_learning_record

MAX_BILLING_REPLY_BYTES = 12_288
BILLING_PROBE_REASONS = frozenset({'probe_invalid_request', 'probe_invalid_response', 'probe_not_authorized',
    'probe_unavailable', 'probe_conflict', 'probe_full', 'probe_busy', 'probe_not_ready',
    # Which step stopped a probe before it launched (0.3.2): preparing it, its worker, another step.
    'probe_preparation_failed', 'probe_preparation_unknown', 'probe_worker_failed', 'probe_step_failed',
    # No probe worker can free without a restart (0.3.3, C9c).
    'probe_worker_unavailable'})
PROBE_STEP_REASONS = frozenset({'probe_preparation_failed', 'probe_preparation_unknown', 'probe_worker_failed',
    'probe_step_failed', 'probe_worker_unavailable'})
PROBE_COMPLETION_REASONS = frozenset({'probe_incomplete', 'probe_invalid_events', 'probe_no_events'})


class BillingProbeError(RuntimeError):
    def __init__(self, reason='probe_unavailable'):
        self.reason_code = reason if type(reason) is str and reason in BILLING_PROBE_REASONS else 'probe_unavailable'
        super().__init__(self.reason_code)


class ProbeAdmissionError(BillingProbeError):
    """Finite owner/service failure, defined without persistence dependencies."""


def decode_billing_probe_input(value):
    if type(value) is not dict or value:
        raise BillingProbeError('probe_invalid_request')
    return {}


def checked_billing_probe_reply(value):
    try:
        fields = {'probe', 'reason_code', 'records', 'known', 'observed_at_ms'}
        if (type(value) is not dict or value.keys() not in (fields, fields | {'billing_waivers'})
                or type(value['probe']) is not str or value['probe'] not in {'reused', 'denied', 'completed'}
                or type(value['known']) is not bool or not valid_integer(value['observed_at_ms'])
                or type(value['records']) is not list or len(value['records']) > 3):
            raise ValueError()
        state, reason, records = value['probe'], value['reason_code'], value['records']
        if state == 'denied':
            # A probe settled before its launch is denied with the failed step (0.3.3, C9).
            if (records or type(reason) is not str
                    or reason not in (ADMISSION_REASON_CODES-{'admitted'}) | PROBE_STEP_REASONS):
                raise ValueError()
        else:
            if ((state == 'reused' and reason is not None)
                    or reason is not None and (type(reason) is not str or reason not in PROBE_COMPLETION_REASONS)):
                raise ValueError()
            kinds, fingerprints = set(), set()
            for record in records:
                record = validate_learning_record(record)
                kind = record['record_type']
                if (kind not in {'provider_identity', 'provider_headroom', 'provider_billing'} or kind in kinds
                        or record['provider'] != 'claude' or record.get('run_id') is not None
                        or record.get('phase') is not None or not valid_integer(record['observed_at_ms'])
                        or record['record_id'] != kind+':'+content_digest(
                            {k: v for k, v in record.items() if k != 'record_id'})):
                    raise ValueError()
                if reason is not None and (
                        kind == 'provider_billing' and record['paid_usage_usable'] is False
                        or kind == 'provider_headroom' and record['status'] == 'allowed'
                        and _headroom_state('claude', record, record['observed_at_ms']) in {'ample', None}):
                    raise ValueError()
                kinds.add(kind)
                fingerprints.add(record['account_fingerprint'])
            if ('provider_identity' not in kinds or len(fingerprints) != 1
                    or (reason is None and state == 'completed' and kinds != {
                        'provider_identity', 'provider_headroom', 'provider_billing'})
                    or state == 'reused' and 'provider_billing' not in kinds):
                raise ValueError()
        if 'billing_waivers' in value:
            waivers = value['billing_waivers']
            billing = {r['record_id'] for r in records if r['record_type'] == 'provider_billing'}
            if type(waivers) is not dict or waivers.keys() != billing or any(type(v) is not bool for v in waivers.values()):
                raise ValueError()
        if len(canonical_json(value)) > MAX_BILLING_REPLY_BYTES:
            raise ValueError()
    except (ContractError, KeyError, TypeError, ValueError, RecursionError):
        raise BillingProbeError('probe_invalid_response') from None
    return value
