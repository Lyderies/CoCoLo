"""One Claude rate-limit event of a registered run, sent to the account owner.

The project host forwards the child's `rate_limit_info` exactly as the stream
carried it; the account derives the fingerprint, time and run from its own
admission and keeps only events more restrictive than the run's earlier ones.
The reply says only what the bridge must act on: how many records were kept
and whether one of them stops the run.
"""
from dataclasses import dataclass

from .contracts import ContractError
from .history_contracts import _RUN, matches
from .learning_contracts import canonical_json

MAX_RATE_LIMIT_INFO_BYTES = 4_096
RATE_LIMIT_STOPS = frozenset({'paid_usage_observed', 'provider_rejected'})


class RateLimitError(ContractError):
    pass


@dataclass(frozen=True)
class RateLimitEventInput:
    run_handle: str
    rate_limit_info: dict

    def __post_init__(self):
        if not matches(self.run_handle, _RUN) or type(self.rate_limit_info) is not dict:
            raise RateLimitError('rate limit event is invalid')
        try:
            size = len(canonical_json(self.rate_limit_info))
        except (ContractError, TypeError, ValueError, RecursionError):
            raise RateLimitError('rate limit event is invalid') from None
        if size > MAX_RATE_LIMIT_INFO_BYTES:
            raise RateLimitError('rate limit event is invalid')

    def payload(self):
        self.__post_init__()
        return {'run_handle': self.run_handle, 'rate_limit_info': self.rate_limit_info}


def decode_rate_limit_event(value):
    if type(value) is not dict or value.keys() != {'run_handle', 'rate_limit_info'}:
        raise RateLimitError('rate limit event is invalid')
    return RateLimitEventInput(value['run_handle'], value['rate_limit_info'])


def rate_limit_reply(kept):
    """The owner's summary of the records it kept for one event."""
    stop = None
    if any(record['record_type'] == 'provider_billing'
           and (record['paid_usage_usable'] is True or record['is_using_overage'] is True) for record in kept):
        stop = 'paid_usage_observed'
    elif any(record['record_type'] == 'provider_headroom' and record['status'] == 'rejected' for record in kept):
        stop = 'provider_rejected'
    return {'kept': len(kept), 'stop': stop}


def checked_rate_limit_reply(value):
    if (type(value) is not dict or value.keys() != {'kept', 'stop'} or type(value['kept']) is not int
            or not 0 <= value['kept'] <= 4 or (value['stop'] is not None and value['stop'] not in RATE_LIMIT_STOPS)
            or (value['stop'] is not None and value['kept'] == 0)):
        raise RateLimitError('rate limit reply is invalid')
    return value
