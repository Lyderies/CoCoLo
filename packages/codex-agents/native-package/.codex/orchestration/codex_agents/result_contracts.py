"""Internal result-storage values; none authenticate a caller or a launch claim."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .contracts import ContractError, result_axes, validate_assignment_shape, validate_result
from .history_contracts import MAX_SEQUENCE, AttemptBinding, HistoryError, _DIGEST, _ID, matches, valid_integer
from .learning_contracts import MAX_OUTCOME_EVIDENCE_DIGESTS, canonical_json, parse_bounded_json

MAX_RESULT_BODY_BYTES = 1_048_576
MAX_RESULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ASSIGNMENT_CANONICAL_BYTES = 6 * 1_048_576
RESULT_STATES = frozenset({"reserved", "committed", "retiring", "expired"})


@dataclass(frozen=True)
class ResultCapacity:
    """Explicit trusted policy, with no guessed deployment defaults.

    Every live reservation charges the complete supported result size, including
    an interrupted artifact write. Expired tombstones retain business identities
    and count against max_entries; no automatic eviction grants another launch.
    """

    max_entries: int
    max_reserved_bytes: int
    reservation_ms: int
    retention_ms: int

    def __post_init__(self) -> None:
        if (
            not valid_integer(self.max_entries, minimum=1, maximum=65_536)
            or not valid_integer(self.max_reserved_bytes, minimum=MAX_RESULT_BODY_BYTES)
            or not valid_integer(self.reservation_ms, minimum=1, maximum=MAX_RESULT_RETENTION_MS)
            or not valid_integer(self.retention_ms, minimum=1, maximum=MAX_RESULT_RETENTION_MS)
        ):
            raise HistoryError("history_invalid_input")


@dataclass(frozen=True)
class ResultReservation:
    binding: AttemptBinding
    assignment_digest: str
    created_at_ms: int
    hard_deadline_at_ms: int
    reserved_until_ms: int
    expires_at_ms: int
    state: str
    prepared_at_ms: int | None = None
    retained_until_ms: int | None = None
    committed_at_ms: int | None = None
    body_size: int | None = None
    result_digest: str | None = None
    terminal_record_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.binding) is not AttemptBinding or not matches(self.assignment_digest, _DIGEST)
            or not valid_integer(self.created_at_ms, maximum=MAX_SEQUENCE - MAX_RESULT_RETENTION_MS)
            or not valid_integer(self.hard_deadline_at_ms, minimum=self.created_at_ms + 1)
            or not valid_integer(self.reserved_until_ms, minimum=self.hard_deadline_at_ms + 1)
            or self.reserved_until_ms - self.created_at_ms > MAX_RESULT_RETENTION_MS
            or not valid_integer(self.expires_at_ms, minimum=self.created_at_ms + 1)
            or type(self.state) is not str or self.state not in RESULT_STATES
        ):
            raise HistoryError("history_invalid_input")
        self.binding.as_data()
        prepared = (
            valid_integer(self.prepared_at_ms, minimum=self.created_at_ms, maximum=self.reserved_until_ms - 1)
            and valid_integer(self.retained_until_ms, minimum=self.prepared_at_ms + 1)
            and self.retained_until_ms - self.prepared_at_ms <= MAX_RESULT_RETENTION_MS
        )
        unprepared = self.prepared_at_ms is None and self.retained_until_ms is None
        empty = (self.committed_at_ms is None and self.body_size is None
                 and self.result_digest is None and self.terminal_record_id is None)
        full = (
            prepared and valid_integer(self.committed_at_ms, minimum=self.prepared_at_ms,
                                       maximum=min(self.reserved_until_ms, self.retained_until_ms) - 1)
            and valid_integer(self.body_size, minimum=1, maximum=MAX_RESULT_BODY_BYTES)
            and matches(self.result_digest, _DIGEST) and matches(self.terminal_record_id, _ID)
        )
        expiry_valid = (
            self.expires_at_ms == self.reserved_until_ms if unprepared else
            prepared and self.expires_at_ms == (min(self.reserved_until_ms, self.retained_until_ms)
                                                if empty else self.retained_until_ms)
        )
        if ((self.state == "reserved" and not empty) or (self.state == "committed" and not full)
                or not (empty or full) or not (prepared or unprepared) or not expiry_valid):
            raise HistoryError("history_invalid_input")

    def as_data(self) -> dict:
        self.__post_init__()
        return asdict(self)


def assignment_bytes(assignment: object, binding: AttemptBinding) -> bytes:
    invalid = False
    try:
        checked = validate_assignment_shape(assignment)
        encoded = canonical_json(checked)
        invalid = (
            len(encoded) > MAX_ASSIGNMENT_CANONICAL_BYTES
            or checked.get("attempt", 1) != binding.attempt
        )
    except (ContractError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_input")
    return encoded


def checked_result_body(body: object, assignment: dict, terminal: dict) -> bytes:
    """Check supplied canonical bytes without repair or durable rejected text.

    A trusted service must separately prove the original assignment and actual
    bridge/fence/terminal facts. Validation does not create that authority.
    """
    invalid = False
    try:
        if type(body) is not bytes or not 0 < len(body) <= MAX_RESULT_BODY_BYTES:
            raise HistoryError("history_invalid_input")
        result = parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)
        validate_result(result, assignment=assignment)
        axes = result_axes(result, attempt=assignment.get("attempt", 1),
                           retry_of_assignment_id=assignment.get("retry_of_assignment_id"))
        invalid = (
            canonical_json(result) != body or axes["admission_state"] != "unverified"
            or len(result["evidence"]) + 2 > MAX_OUTCOME_EVIDENCE_DIGESTS
            or (terminal["fenced"] and axes["execution_state"] != terminal["terminal_state"])
            or (not terminal["fenced"] and axes["execution_state"] == "timed_out")
        )
    except (HistoryError, ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_input")
    return body
