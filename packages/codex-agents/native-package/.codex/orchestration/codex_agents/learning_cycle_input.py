"""Immutable record references; owner capture is structural, wire work is worker-only.

No completeness, authority, persistence, native policy or activation is implied.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
import threading
import time

from .contracts import ContractError
from .learning_contracts import (
    MAX_RECORD_BYTES, canonical_json, parse_bounded_json,
    validate_evaluation_case, validate_outcome,
)
from .learning_cycle_core import (
    MAX_CASES, MAX_INVALIDATED_RUNS, MAX_OUTCOMES, MAX_SNAPSHOT_BYTES,
)

MAX_INPUT_BYTES = MAX_SNAPSHOT_BYTES + 1_048_576
_MAX_NS = (1 << 63) - 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class LearningInputError(ValueError):
    def __init__(self, finished_ns=None):
        self.finished_ns = finished_ns
        super().__init__("learning_input_invalid")


class LearningInputStopped(RuntimeError):
    def __init__(self, suffix, finished_ns):
        self.suffix = suffix
        self.finished_ns = finished_ns
        super().__init__("learning_input_" + suffix)


@dataclass(frozen=True, repr=False, slots=True)
class LearningCycleInput:
    outcomes: tuple
    cases: tuple
    owner_invalidated_run_ids: tuple = ()
    as_of_ms: int | None = None
    max_age_ms: int | None = None


def _matches(value, pattern, maximum):
    return type(value) is str and 1 <= len(value) <= maximum and pattern.fullmatch(value) is not None


def capture_learning_input(value, *, operation):
    """Recapture known exact immutable fields without decoding or scanning bytes."""
    if (type(value) is not LearningCycleInput or type(operation) is not str
            or operation not in {"select_mining", "plan"}):
        raise LearningInputError()
    try:
        outcomes, cases = value.outcomes, value.cases
        invalidated = value.owner_invalidated_run_ids
        as_of_ms, max_age_ms = value.as_of_ms, value.max_age_ms
    except AttributeError:
        raise LearningInputError() from None
    if operation == "select_mining":
        if as_of_ms is not None or max_age_ms is not None:
            raise LearningInputError()
    elif any(type(clock) is not int or not 0 <= clock <= _MAX_NS for clock in (as_of_ms, max_age_ms)):
        raise LearningInputError()
    if type(invalidated) is not tuple or len(invalidated) > MAX_INVALIDATED_RUNS:
        raise LearningInputError()
    invalidation_bytes = 0
    for identifier in invalidated:
        if type(identifier) is not str or not 1 <= len(identifier) <= 256:
            raise LearningInputError()
        invalidation_bytes += 12 * len(identifier) + 3
    captured = []
    record_bytes = record_count = 0
    for records, maximum in ((outcomes, MAX_OUTCOMES), (cases, MAX_CASES)):
        if type(records) is not tuple or len(records) > maximum:
            raise LearningInputError()
        seen, entries = set(), []
        for entry in records:
            if type(entry) is not tuple or len(entry) != 3:
                raise LearningInputError()
            identifier, digest, raw = entry
            if (not _matches(identifier, _ID, 160) or not _matches(digest, _DIGEST, 64)
                    or type(raw) is not bytes or not 0 < len(raw) <= MAX_RECORD_BYTES
                    or identifier in seen):
                raise LearningInputError()
            seen.add(identifier)
            record_bytes += len(raw)
            record_count += 1
            if record_bytes + invalidation_bytes > MAX_SNAPSHOT_BYTES:
                raise LearningInputError()
            entries.append((identifier, digest, raw))
        captured.append(tuple(entries))
    charge = record_bytes + record_count + invalidation_bytes + 1_048_576
    if record_bytes + invalidation_bytes > MAX_SNAPSHOT_BYTES or charge > MAX_INPUT_BYTES:
        raise LearningInputError()
    return LearningCycleInput(
        captured[0], captured[1], tuple(item for item in invalidated), as_of_ms, max_age_ms,
    ), charge


class _Clock:
    def __init__(self, deadline_ns, cancelled):
        self.deadline_ns, self.cancelled, self.last_ns = deadline_ns, cancelled, None

    def sample(self):
        try:
            now = time.monotonic_ns()
        except BaseException:
            raise LearningInputStopped("clock_failure", None) from None
        if (type(now) is not int or not 0 <= now <= _MAX_NS
                or self.last_ns is not None and now < self.last_ns):
            raise LearningInputStopped("clock_failure", None)
        self.last_ns = now
        if self.cancelled.is_set():
            raise LearningInputStopped("cancelled", now)
        if now >= self.deadline_ns:
            raise LearningInputStopped("expired", now)
        return now


def _verify(entry, kind, validate):
    identifier, digest, raw = entry
    try:
        record = parse_bounded_json(raw, max_bytes=MAX_RECORD_BYTES)
        if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
            raise LearningInputError()
        validate(record)
        if (record["record_id"] != identifier or record["record_type"] != kind
                or canonical_json(record) != raw or hashlib.sha256(raw).hexdigest() != digest):
            raise LearningInputError()
    except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
        raise LearningInputError() from None


def _encode(value, *, operation, binding_digest, clock):
    clock.sample()
    captured, charge = capture_learning_input(value, operation=operation)
    clock.sample()
    for entries, kind, validate in (
        (captured.outcomes, "outcome", validate_outcome),
        (captured.cases, "evaluation_case", validate_evaluation_case),
    ):
        for entry in entries:
            clock.sample()
            invalid = False
            try:
                _verify(entry, kind, validate)
            except LearningInputError:
                invalid = True
            clock.sample()
            if invalid:
                raise LearningInputError()
    for identifier in captured.owner_invalidated_run_ids:
        clock.sample()
        invalid = False
        try:
            # The core counts UTF-8 bytes. JSON escaping alone also accepts
            # unpaired surrogates, which cannot reach that valid core input.
            identifier.encode('utf-8')
        except UnicodeError:
            invalid = True
        clock.sample()
        if invalid:
            raise LearningInputError()
    clock.sample()
    # Only short allowlisted scalars are encoded here. Corpus bytes were checked
    # individually above and are never decoded/re-encoded as a whole snapshot.
    fields = []
    if operation == "plan":
        fields.append(b'"as_of_ms":' + canonical_json(captured.as_of_ms))
    fields.extend((
        b'"binding_digest":' + canonical_json(binding_digest),
        b'"cases":[' + b",".join(entry[2] for entry in captured.cases) + b"]",
    ))
    if operation == "plan":
        fields.append(b'"max_age_ms":' + canonical_json(captured.max_age_ms))
    fields.extend((
        b'"operation":' + canonical_json(operation),
        b'"outcomes":[' + b",".join(entry[2] for entry in captured.outcomes) + b"]",
        b'"owner_invalidated_run_ids":[' + b",".join(
            canonical_json(item) for item in captured.owner_invalidated_run_ids) + b"]",
        b'"schema_version":1',
    ))
    raw = b"{" + b",".join(fields) + b"}"
    clock.sample()
    if len(raw) > MAX_INPUT_BYTES or len(raw) > charge:
        raise LearningInputError()
    return raw


def encode_learning_input(value, *, operation, binding_digest, deadline_ns, cancelled):
    """Verify records on a worker, retaining the original deadline and event."""
    if (not _matches(binding_digest, _DIGEST, 64)
            or type(deadline_ns) is not int or not 1 <= deadline_ns <= _MAX_NS
            or type(cancelled) is not threading.Event):
        raise LearningInputError()
    clock = _Clock(deadline_ns, cancelled)
    try:
        return _encode(value, operation=operation, binding_digest=binding_digest, clock=clock)
    except LearningInputError:
        # A refusal is also sampled; a stop/clock failure takes precedence.
        raise LearningInputError(clock.sample()) from None
