"""Closed original-run failure metadata; no launch or provider authority."""
from dataclasses import asdict, dataclass

from .contracts import ContractError
from .history_contracts import _ID, _RUN, matches, valid_integer
from .learning_contracts import (
    OBSERVATION_UNAVAILABLE_REASONS, canonical_json, content_digest, validate_observation_unavailable,
)
from .provider_observation_contracts import ObservationError


@dataclass(frozen=True)
class ObservationFailureInput:
    provider: str
    run_handle: str
    phase: str
    reason_code: str

    def __post_init__(self):
        if (type(self.provider) is not str or self.provider not in {'codex', 'claude'}
                or not matches(self.run_handle, _RUN) or type(self.phase) is not str
                or self.phase not in {'before', 'after'} or type(self.reason_code) is not str
                or self.reason_code not in OBSERVATION_UNAVAILABLE_REASONS):
            raise ObservationError('observation_invalid_request')

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_observation_failure(value):
    if type(value) is not dict or value.keys() != {'provider', 'run_handle', 'phase', 'reason_code'}:
        raise ObservationError('observation_invalid_request')
    return ObservationFailureInput(**value)


def checked_observation_failure_reply(value, request):
    try:
        if (type(request) is not ObservationFailureInput or type(value) is not dict
                or value.keys() != {'provider', 'run_handle', 'phase', 'reason_code', 'run_id',
                                    'observed_at_ms', 'known', 'record'}
                or any(value[name] != item for name, item in request.payload().items())
                or not matches(value['run_id'], _ID) or not valid_integer(value['observed_at_ms'])
                or type(value['known']) is not bool):
            raise ValueError()
        record = validate_observation_unavailable(value['record'])
        if (any(record[name] != value[name] for name in ('provider', 'run_id', 'phase', 'reason_code', 'observed_at_ms'))
                or record['record_id'] != 'observation_unavailable:' + content_digest(
                    {name: item for name, item in record.items() if name != 'record_id'})
                or len(canonical_json(value)) > 4096):
            raise ValueError()
    except (ContractError, KeyError, TypeError, ValueError, RecursionError):
        raise ObservationError('observation_invalid_response') from None
    return value
