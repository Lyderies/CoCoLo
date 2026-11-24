"""RS-E E2a-6: the closed `account.publish_wake` payload and reply.

A project service tells the collector when its store next needs a live service: `pending` (a write with an earlier
deadline may be in flight, so its state is unknown), `exact` (its committed next due) or `clean_stop` (it stopped
normally). A generation is (epoch, seq): the collector allocates each service instance's epoch (a null epoch asks for
the next one) and keeps only strictly newer generations.
"""
from dataclasses import dataclass

WAKE_STATES = ('pending', 'exact', 'clean_stop')
WAKE_OUTCOMES = ('accepted', 'duplicate')
WAKE_REASON_CODES = frozenset({'wake_stale'})
WAKE_FIELDS = frozenset({'epoch', 'seq', 'state', 'next_due_at_ms'})
WAKE_REPLY_FIELDS = frozenset({'epoch', 'seq', 'outcome'})
# Review round 1: a publication with a null epoch may name its service instance (32 hex, fresh per instance, bound by
# the request's MAC), so a retry after a lost acknowledgement is given the same epoch.
WAKE_INSTANCE_FIELD = 'instance'
# Review round 1: a project service that wrote with no acknowledged publication keeps a marker in its project
# folder (`<state>/projects/<project id>/`) until a publication is acknowledged; the collector's reconcile treats
# a clean stop with one as unknown. Decision 179: this is a folder of one file per service instance (named by its
# instance, locked while it lives); an acknowledgement removes its own and those of instances that are gone.
WAKE_MARKER = 'wake-unpublished'
_MAX = (1 << 63) - 1


def _integer(value, minimum):
    return type(value) is int and minimum <= value <= _MAX


@dataclass(frozen=True)
class WakePublishInput:
    epoch: int | None
    seq: int
    state: str
    next_due_at_ms: int | None
    instance: str | None = None


def _instance(payload):
    value = payload.get(WAKE_INSTANCE_FIELD)
    return (payload['epoch'] is None and type(value) is str and len(value) == 32
            and set(value) <= set('0123456789abcdef'))


def decode_wake_publish(payload) -> WakePublishInput:
    if (type(payload) is not dict
            or not (set(payload) == WAKE_FIELDS or (set(payload) == WAKE_FIELDS | {WAKE_INSTANCE_FIELD}
                                                    and _instance(payload)))
            or not (payload['epoch'] is None or _integer(payload['epoch'], 1))
            or not _integer(payload['seq'], 1)
            or type(payload['state']) is not str or payload['state'] not in WAKE_STATES
            or not (payload['next_due_at_ms'] is None or _integer(payload['next_due_at_ms'], 0))):
        raise ValueError('wake publication is invalid')
    return WakePublishInput(payload['epoch'], payload['seq'], payload['state'], payload['next_due_at_ms'],
                            payload.get(WAKE_INSTANCE_FIELD))


def checked_wake_reply(result, wake: WakePublishInput) -> dict:
    if (type(result) is not dict or set(result) != WAKE_REPLY_FIELDS or not _integer(result['epoch'], 1)
            or result['seq'] != wake.seq or type(result['seq']) is not int
            or (wake.epoch is not None and result['epoch'] != wake.epoch)
            or type(result['outcome']) is not str or result['outcome'] not in WAKE_OUTCOMES):
        raise ValueError('wake reply is invalid')
    return {'epoch': result['epoch'], 'seq': result['seq'], 'outcome': result['outcome']}
