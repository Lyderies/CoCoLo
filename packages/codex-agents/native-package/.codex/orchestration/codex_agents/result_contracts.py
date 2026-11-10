"""Internal result-storage values; none authenticate a caller or a launch claim."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .contracts import ContractError, result_axes, validate_assignment_shape, validate_result
from .history_contracts import (
    MAX_ATTEMPT, MAX_SEQUENCE, AttemptBinding, HistoryError, _DIGEST, _ID, _ORCHESTRATION,
    _PROJECT, _RUN, matches, valid_integer,
)
from .learning_contracts import (
    DISPATCH_RESULT_SCHEMA_VERSION, MAX_OUTCOME_EVIDENCE_DIGESTS, MAX_RECORD_BYTES, SUPPORTED_MODELS,
    canonical_json, parse_bounded_json, validate_learning_record,
)

MAX_RESULT_BODY_BYTES = 1_048_576
MAX_RESULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
MAX_ASSIGNMENT_CANONICAL_BYTES = 6 * 1_048_576
RESULT_STATES = frozenset({"reserved", "committed", "retiring", "expired"})
LEASE_SCOPE_FIELDS = ("project_id", "primary_session_id", "runtime_digest", "supervisor_instance_id",
                      "supervisor_generation", "orchestration_handle", "run_handle", "run_id", "attempt")

CODEX_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


@dataclass(frozen=True)
class CodexUsageObservation:
    """Actual fixed-worker client counts, never model-reported result usage.

    The writer must separately prove custody of the selected packet. None means
    unavailable, not zero. These values establish no billing/admission authority.
    """
    requested_model: str
    usage: dict | None

    def __post_init__(self):
        self.as_data()

    def as_data(self):
        if (type(self.requested_model) is not str or self.requested_model not in SUPPORTED_MODELS
                or not self.requested_model.startswith("gpt-")):
            raise HistoryError("history_invalid_input")
        if self.usage is not None and (type(self.usage) is not dict or self.usage.keys() != set(CODEX_USAGE_FIELDS)
                                      or any(v is not None and not valid_integer(v) for v in self.usage.values())):
            raise HistoryError("history_invalid_input")
        return {"requested_model": self.requested_model,
                "usage": None if self.usage is None else {name: self.usage[name] for name in CODEX_USAGE_FIELDS}}


@dataclass(frozen=True)
class ResultLeaseBinding:
    """Pre-claim storage identity, without an invented or consumed launch claim.

    The request fields name the original account claim request. They are not
    caller authentication, an account receipt or permission to launch anything.
    """
    project_id: str
    primary_session_id: str
    runtime_digest: str
    supervisor_instance_id: str
    supervisor_generation: int
    orchestration_handle: str
    run_handle: str
    run_id: str
    attempt: int
    claim_request_id: str
    claim_request_digest: str

    def __post_init__(self):
        for value, pattern in (
            (self.project_id, _PROJECT), (self.primary_session_id, _ID),
            (self.runtime_digest, _DIGEST), (self.supervisor_instance_id, _ID),
            (self.orchestration_handle, _ORCHESTRATION), (self.run_handle, _RUN),
            (self.run_id, _ID), (self.claim_request_id, _ID), (self.claim_request_digest, _DIGEST),
        ):
            if not matches(value, pattern):
                raise HistoryError("history_invalid_input")
        if (not valid_integer(self.supervisor_generation, minimum=1)
                or not valid_integer(self.attempt, minimum=1, maximum=MAX_ATTEMPT)):
            raise HistoryError("history_invalid_input")

    def as_data(self):
        self.__post_init__()
        return asdict(self)

    def matches_attempt(self, binding):
        self.__post_init__()
        if type(binding) is not AttemptBinding:
            return False
        binding.as_data()
        return all(getattr(self, name) == getattr(binding, name) for name in LEASE_SCOPE_FIELDS)


@dataclass(frozen=True)
class ResultLease:
    """Bounded durable metadata, never an account or native authorization grant."""
    binding: ResultLeaseBinding
    assignment_digest: str
    created_at_ms: int
    reserved_until_ms: int
    state: str
    lease_digest: str
    claim_id: str | None = None
    claim_digest: str | None = None
    hard_deadline_at_ms: int | None = None
    claim_binding_digest: str | None = None
    cleanup_reservation_digest: str | None = None

    def __post_init__(self):
        if (type(self.binding) is not ResultLeaseBinding or not matches(self.assignment_digest, _DIGEST)
                or not matches(self.lease_digest, _DIGEST)
                or not valid_integer(self.created_at_ms, maximum=MAX_SEQUENCE - MAX_RESULT_RETENTION_MS)
                or not valid_integer(self.reserved_until_ms, minimum=self.created_at_ms + 1)
                or self.reserved_until_ms - self.created_at_ms > MAX_RESULT_RETENTION_MS
                or type(self.state) is not str or self.state not in {"reserved", "claim_pending", "bound", "expired"}):
            raise HistoryError("history_invalid_input")
        self.binding.as_data()
        if self.cleanup_reservation_digest is not None and not matches(self.cleanup_reservation_digest, _DIGEST):
            raise HistoryError("history_invalid_input")
        if self.state == "bound":
            if (not matches(self.claim_id, _ID) or not matches(self.claim_digest, _DIGEST)
                    or not valid_integer(self.hard_deadline_at_ms, minimum=self.created_at_ms + 1,
                                         maximum=self.reserved_until_ms - 1)
                    or not matches(self.claim_binding_digest, _DIGEST)):
                raise HistoryError("history_invalid_input")
        elif any(value is not None for value in (self.claim_id, self.claim_digest,
                                                 self.hard_deadline_at_ms, self.claim_binding_digest)):
            raise HistoryError("history_invalid_input")


@dataclass(frozen=True)
class ResultObservation:
    """Captured executor facts, never a caller capability or parsed authority.

    The trusted executor must capture these once from the actual execution. The
    service must separately prove its custody, original assignment and complete
    claim. In particular, the observation time is not regenerated on delivery
    retry. Private digest keys and caller-selected record IDs are absent.
    """

    bridge_run_id: str
    observed_at_ms: int
    fenced: bool
    reviewed: bool
    terminal_state: str | None
    result_validation_reason_code: str | None = None
    client_diagnostics: dict | None = None

    def __post_init__(self) -> None:
        self.as_data()

    def as_data(self) -> dict:
        invalid = False
        try:
            if (
                type(self) is not ResultObservation or type(self.bridge_run_id) is not str
                or not valid_integer(self.observed_at_ms)
                or type(self.fenced) is not bool or type(self.reviewed) is not bool
                or (self.client_diagnostics is not None and type(self.client_diagnostics) is not dict)
            ):
                raise HistoryError("history_invalid_input")
            material = {name: getattr(self, name) for name in (
                "bridge_run_id", "observed_at_ms", "fenced", "reviewed", "terminal_state",
                "result_validation_reason_code", "client_diagnostics",
            )}
            # Reuse the exact existing terminal/fence/diagnostic validator. These
            # fixed placeholders check values only, not a business record or ID.
            validate_learning_record({
                **material, "schema_version": DISPATCH_RESULT_SCHEMA_VERSION,
                "record_type": "dispatch_result", "record_id": "observation-validation",
                "run_id": "observation-validation", "result_digest": "0" * 64,
            })
            encoded = canonical_json(material)
            invalid = len(encoded) > MAX_RECORD_BYTES
            if not invalid:
                snapshot = parse_bounded_json(encoded, max_bytes=MAX_RECORD_BYTES)
        except (HistoryError, ContractError, AttributeError, KeyError, TypeError, ValueError,
                OverflowError, RecursionError):
            invalid = True
        if invalid:
            raise HistoryError("history_invalid_input")
        return snapshot


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
            or type(checked.get("attempt", 1)) is not int
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
