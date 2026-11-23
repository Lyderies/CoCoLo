"""Internal project-history values; validation is not caller authentication.

These values cross the supervisor/persistence boundary after service authorization.
They grant neither admission nor launch permission. No native endpoint is enabled.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PROJECT = re.compile(r"prj_[0-9a-f]{32}")
_ORCHESTRATION = re.compile(r"orc_[0-9a-f]{48}")
_RUN = re.compile(r"hrn_[0-9a-f]{48}")
MAX_SEQUENCE = (1 << 63) - 1
MAX_ATTEMPT = (1 << 31) - 1
HISTORY_REASON_CODES = frozenset({
    "history_invalid_input", "history_unsafe_path", "history_writer_busy",
    "history_unavailable", "history_incompatible", "history_wrong_owner",
    "history_conflict", "history_not_found", "history_sequence_exhausted",
    "history_invalid_request", "history_request_too_large", "history_invalid_response",
    "history_not_authorized", "history_service_unavailable",
    "history_result_capacity", "history_result_capacity_busy", "history_result_expired", "history_result_unavailable",
    "history_result_not_reserved", "history_result_not_ready",
    "history_execution_consumed", "history_execution_not_ready",
    "history_orchestration_unavailable", "history_orchestration_state",
    "history_orchestration_pending", "history_orchestration_full",
    # R8a: opened under another session, service or version (an update changes the service's scope).
    "history_orchestration_other_scope",
    # R7b: a service that would not take the connection said why (service_transport.SERVICE_REFUSALS).
    "service_refused_late", "service_refused_enrollment", "service_refused_busy", "service_refused_unavailable",
    "history_learning_capacity", "history_learning_incomplete", "history_learning_reconciliation",
    # D1 (RS-D M1): a result kept from before format 18, whose record context only an earlier service's memory held.
    "history_worker_context_unavailable",
    # D2 (RS-D-5): a verifier is not opened on a worker that will not outlast it by the QA margin.
    "history_worker_result_expiring",
})


class HistoryError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in HISTORY_REASON_CODES:
            reason_code = "history_invalid_input"
        self.reason_code = reason_code
        super().__init__(reason_code)


def matches(value: object, pattern: re.Pattern[str]) -> bool:
    return type(value) is str and pattern.fullmatch(value) is not None


def valid_integer(value: object, *, minimum: int = 0, maximum: int = MAX_SEQUENCE) -> bool:
    return type(value) is int and minimum <= value <= maximum


@dataclass(frozen=True)
class AttemptBinding:
    project_id: str
    primary_session_id: str
    runtime_digest: str
    supervisor_instance_id: str
    supervisor_generation: int
    orchestration_handle: str
    run_handle: str
    run_id: str
    attempt: int
    claim_id: str
    claim_digest: str

    def __post_init__(self) -> None:
        for value, pattern in (
            (self.project_id, _PROJECT), (self.primary_session_id, _ID),
            (self.runtime_digest, _DIGEST), (self.supervisor_instance_id, _ID),
            (self.orchestration_handle, _ORCHESTRATION), (self.run_handle, _RUN),
            (self.run_id, _ID), (self.claim_id, _ID), (self.claim_digest, _DIGEST),
        ):
            if not matches(value, pattern):
                raise HistoryError("history_invalid_input")
        if not valid_integer(self.supervisor_generation, minimum=1) or not valid_integer(
            self.attempt, minimum=1, maximum=MAX_ATTEMPT,
        ):
            raise HistoryError("history_invalid_input")

    def as_data(self) -> dict:
        self.__post_init__()
        return asdict(self)


@dataclass(frozen=True)
class HistoryEntry:
    project_id: str
    record_id: str
    source: str
    source_sequence: int
    observed_at_ms: int
    ingested_at_ms: int
    content: bytes
    content_digest: str
    late: bool


@dataclass(frozen=True)
class TerminalDelivery:
    delivery_sequence: int
    binding: AttemptBinding
    record_id: str
    record_content_digest: str
    result_digest: str

    def __post_init__(self) -> None:
        if (
            not valid_integer(self.delivery_sequence, minimum=1, maximum=MAX_SEQUENCE - 1)
            or type(self.binding) is not AttemptBinding or not matches(self.record_id, _ID)
            or not matches(self.record_content_digest, _DIGEST) or not matches(self.result_digest, _DIGEST)
        ):
            raise HistoryError("history_invalid_input")
        self.binding.as_data()

    def as_data(self) -> dict:
        self.__post_init__()
        return asdict(self)
