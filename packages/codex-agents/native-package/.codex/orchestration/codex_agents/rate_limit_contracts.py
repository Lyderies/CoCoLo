"""One Claude rate-limit event of a registered run, sent to the account owner.

The project host forwards the child's `rate_limit_info` exactly as the stream
carried it; the account derives the fingerprint, time and run from its own
admission and keeps only events more restrictive than the run's earlier ones.
The reply says only what the bridge must act on: how many records were kept
and whether one of them stops the run.
"""
from dataclasses import dataclass
import math
import re

from .contracts import ContractError
from .history_contracts import _RUN, matches
from .learning_contracts import canonical_json

MAX_RATE_LIMIT_INFO_BYTES = 4_096
RATE_LIMIT_STOPS = frozenset({'paid_usage_observed', 'provider_rejected'})


_FIELDS = ('status', 'rateLimitType', 'utilization', 'resetsAt', 'isUsingOverage', 'overageStatus',
           'overageDisabledReason')
_WINDOW_FIELDS = ('status', 'utilization', 'resetsAt')
MAX_WINDOWS = 8
MAX_TEXT = 64  # Per string field; with these bounds the reduced event stays well under 4 KiB.
# Plain printable ASCII without quotes or backslashes, so one character is one encoded byte.
_TEXT = re.compile(r'[ !#-\[\]-~]*')


class RateLimitError(ContractError):
    pass


def _scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if -(1 << 53) <= value <= 1 << 53 else None
    if type(value) is float:
        return value if math.isfinite(value) else None
    return value if type(value) is str and len(value) <= MAX_TEXT and _TEXT.fullmatch(value) else None


def bounded_rate_limit_info(info):
    """Only the fields the account reads, so an event's size never decides whether it is kept.

    A field it cannot carry becomes unknown, which the account treats as
    restrictive (unknown paid usage denies admission; an unknown status is a lost
    reading). A window beyond the limit is dropped only after its rejection, if
    any, is carried by the event's own status.
    """
    if type(info) is not dict:
        return {}
    facts = {name: _scalar(info.get(name)) for name in _FIELDS if name in info}
    windows = info.get('unifiedWindows')
    if type(windows) is dict:
        kept = {}
        for scope in sorted(windows, key=str):
            entry = windows[scope]
            entry = {n: _scalar(entry.get(n)) for n in _WINDOW_FIELDS if n in entry} if type(entry) is dict else None
            if (len(kept) < MAX_WINDOWS and type(scope) is str and len(scope) <= MAX_TEXT // 2
                    and _TEXT.fullmatch(scope)):
                kept[scope] = entry
            elif (entry is None or entry.get('status') != 'allowed') and facts.get('status') != 'rejected':
                # Never less restrictive than what was dropped.
                facts['status'] = 'rejected' if (entry or {}).get('status') == 'rejected' else None
        facts['unifiedWindows'] = kept
    if len(canonical_json(facts)) > MAX_RATE_LIMIT_INFO_BYTES:  # Defence in depth; the bounds above prevent it.
        rejected = facts.get('status') == 'rejected' or any(
            (entry or {}).get('status') == 'rejected' for entry in facts.get('unifiedWindows', {}).values())
        facts = {'status': 'rejected' if rejected else None,
                 **{n: facts[n] for n in ('isUsingOverage', 'overageStatus', 'overageDisabledReason') if n in facts}}
    return facts


_STATUS_RANK = {'allowed': 0, 'allowed_warning': 1, None: 2, 'rejected': 3}
_USING_RANK = {False: 0, None: 1, True: 2}
_OVERAGE_RANK = {'rejected': 0, None: 1, 'allowed_warning': 2, 'allowed': 3}


def _worst(values, rank):
    return max((v if v in rank else None for v in values), key=lambda v: rank[v])


def merged_restriction(events):
    """One event at least as restrictive as every one given, for events that cannot each be sent.

    The status, overage use and overage status are the most restrictive seen; the
    disabled reason is dropped, so the account cannot read paid usage as unusable.
    """
    events = list(events)
    windows = [entry for event in events for entry in (event.get('unifiedWindows') or {}).values()]
    return {'status': _worst([e.get('status') for e in events] + [(w or {}).get('status', 'allowed') for w in windows],
                             _STATUS_RANK),
            'isUsingOverage': _worst([e.get('isUsingOverage') for e in events], _USING_RANK),
            'overageStatus': _worst([e.get('overageStatus') for e in events], _OVERAGE_RANK),
            'overageDisabledReason': None}


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
