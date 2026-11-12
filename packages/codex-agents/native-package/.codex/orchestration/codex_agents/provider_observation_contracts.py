"""Closed non-spending observation values; no caller or launch authority."""
from dataclasses import asdict, dataclass

from .contracts import ContractError
from .history_contracts import _ID, _RUN, matches, valid_integer
from .learning_contracts import canonical_json, content_digest, validate_learning_record

MAX_OBSERVATION_RECORDS = 8
MAX_OBSERVATION_REPLY_BYTES = 12_288
OBSERVATION_REASONS = frozenset({
    'observation_invalid_request', 'observation_invalid_response', 'observation_not_authorized',
    'observation_unavailable', 'observation_conflict', 'observation_full',
})


class ObservationError(RuntimeError):
    def __init__(self, reason='observation_unavailable'):
        self.reason_code = reason if type(reason) is str and reason in OBSERVATION_REASONS else 'observation_unavailable'
        super().__init__(self.reason_code)


@dataclass(frozen=True)
class ObservationInput:
    provider: str
    run_handle: str | None = None
    phase: str | None = None

    def __post_init__(self):
        if (type(self.provider) is not str or self.provider not in {'codex', 'claude'}
                or ((self.run_handle is None) != (self.phase is None))
                or (self.run_handle is not None and
                    (not matches(self.run_handle, _RUN) or type(self.phase) is not str
                     or self.phase not in {'before', 'after'}))):
            raise ObservationError('observation_invalid_request')

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_observation_input(value):
    if type(value) is not dict or value.keys() != {'provider', 'run_handle', 'phase'}:
        raise ObservationError('observation_invalid_request')
    return ObservationInput(**value)


def checked_observation_reply(value, request):
    try:
        if (type(request) is not ObservationInput or type(value) is not dict
                or value.keys() not in ({'provider', 'run_handle', 'phase', 'run_id', 'observed_at_ms', 'known', 'records'},
                    {'provider', 'run_handle', 'phase', 'run_id', 'observed_at_ms', 'known', 'records', 'billing_waivers'})
                or any(value[name] != getattr(request, name) for name in ('provider', 'run_handle', 'phase'))
                or type(value['known']) is not bool or not valid_integer(value['observed_at_ms'])
                or (value['run_id'] is not None and not matches(value['run_id'], _ID))
                or ((request.run_handle is None) != (value['run_id'] is None))
                or type(value['records']) is not list or not 1 <= len(value['records']) <= MAX_OBSERVATION_RECORDS):
            raise ValueError()
        identities = 0
        for record in value['records']:
            checked = validate_learning_record(record)
            if (checked['record_type'] not in {'provider_identity', 'provider_billing', 'provider_headroom'}
                    or checked['provider'] != request.provider or checked['observed_at_ms'] != value['observed_at_ms']
                    or checked.get('run_id') != (value['run_id'] if 'run_id' in checked else None)
                    or checked.get('phase') != (request.phase if 'phase' in checked else None)
                    or checked['record_id'] != checked['record_type'] + ':' + content_digest(
                        {key: item for key, item in checked.items() if key != 'record_id'})):
                raise ValueError()
            identities += checked['record_type'] == 'provider_identity'
        if 'billing_waivers' in value:
            waivers = value['billing_waivers']
            billing = {r['record_id'] for r in value['records'] if r['record_type'] == 'provider_billing'}
            if (type(waivers) is not dict or waivers.keys() != billing
                    or any(type(v) is not bool for v in waivers.values())):
                raise ValueError()
        if identities != 1 or len(canonical_json(value)) > MAX_OBSERVATION_REPLY_BYTES:
            raise ValueError()
    except (ContractError, KeyError, TypeError, ValueError, RecursionError):
        raise ObservationError('observation_invalid_response') from None
    return value
