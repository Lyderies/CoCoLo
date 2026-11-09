"""Project result-admission values; no SQLite, keys or caller authority.

Native enrollment and endpoint authentication remain separate activation gates.
Clients name an already durable terminal, never supply a replacement result,
repair receipt, observation, accepted outcome or execution configuration.
"""
from dataclasses import asdict, dataclass

from .history_contracts import AttemptBinding, HistoryError, _DIGEST, _ID, matches
from .learning_contracts import canonical_json, parse_bounded_json
from .result_contracts import MAX_ASSIGNMENT_CANONICAL_BYTES

MAX_HOST_RESULT_CONTEXT_BYTES = 3 * MAX_ASSIGNMENT_CANONICAL_BYTES


@dataclass(frozen=True, repr=False)
class WorkerResultContext:
    """Independent original worker scope and protected prospective verifier task.

    This exists before the verifier is opened. Native adapters must establish
    current primary permission and original task/scope custody independently.
    Neither a caller-selected task nor this value is that authentication.
    """
    binding: AttemptBinding
    task: dict

    def snapshot(self):
        invalid = False
        try:
            if type(self.binding) is not AttemptBinding or type(self.task) is not dict:
                raise ValueError()
            raw = canonical_json({'binding': self.binding.as_data(), 'task': self.task})
            if not 0 < len(raw) <= MAX_HOST_RESULT_CONTEXT_BYTES:
                raise ValueError()
            value = parse_bounded_json(raw, max_bytes=MAX_HOST_RESULT_CONTEXT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError('history_not_authorized')
        return raw, value


@dataclass(frozen=True)
class HostResultContext:
    """Original inputs supplied by a trusted server adapter, never request JSON.

    Hashes are checked against the actual consumed account registration. Native
    caller/session/generation/revocation and original input custody must still
    be independently proved by that adapter. This type proves neither.
    """
    binding: AttemptBinding
    assignment: dict
    task: dict
    route: dict
    client_capability: dict

    def snapshot(self):
        invalid = False
        try:
            fields = ("assignment", "task", "route", "client_capability")
            if (type(self.binding) is not AttemptBinding
                    or any(type(getattr(self, name)) is not dict for name in fields)):
                raise ValueError()
            raw = canonical_json({"binding": self.binding.as_data(), **{name: getattr(self, name) for name in fields}})
            if not 0 < len(raw) <= MAX_HOST_RESULT_CONTEXT_BYTES:
                raise ValueError()
            value = parse_bounded_json(raw, max_bytes=MAX_HOST_RESULT_CONTEXT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise HistoryError("history_not_authorized")
        return raw, value


@dataclass(frozen=True)
class HostResultReference:
    binding: AttemptBinding
    terminal_record_id: str
    terminal_content_digest: str
    result_digest: str

    def __post_init__(self):
        if (type(self.binding) is not AttemptBinding
                or not matches(self.terminal_record_id, _ID)
                or not matches(self.terminal_content_digest, _DIGEST)
                or not matches(self.result_digest, _DIGEST)):
            raise HistoryError("history_invalid_input")
        self.binding.as_data()

    def as_data(self):
        self.__post_init__()
        return asdict(self)


def decode_host_result_reference(value):
    invalid = False
    try:
        if type(value) is not dict or value.keys() != {
            "binding", "terminal_record_id", "terminal_content_digest", "result_digest",
        } or type(value["binding"]) is not dict:
            raise ValueError()
        reference = HostResultReference(**{**value, "binding": AttemptBinding(**value["binding"])})
    except Exception:
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_input")
    return reference
