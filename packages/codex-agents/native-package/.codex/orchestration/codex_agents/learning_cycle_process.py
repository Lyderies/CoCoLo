"""Fixed keyless calculation helper and bounded private wire codec.

Only a trusted parent may launch this entry point with its reviewed OS policy,
original process/descriptor custody, bounded channels and one wall deadline.
The parent retains cleanup and scheduling responsibility. A ready marker, input
digest or successful calculation grants no native identity, corpus activation,
admission, persistence or release authority. No service invokes this entry yet.

Large codec operations belong outside the persistence writer. The writer uses
bounded source references/candidate pages and retains every authority, revision
and eligibility gate. This helper receives approved metadata, never owner keys,
SQL connections, signatures, paths, callbacks or model tools.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import resource
import stat
import struct
import sys

from .contracts import ContractError
from .learning_contracts import (
    MAX_RECORD_BYTES, candidate_base_digest, canonical_json, content_digest,
    parse_bounded_json, validate_execution_configuration,
)
from .learning_analysis import COMPARISON_MIN_CASES
from .learning_cycle_core import (
    MAX_OUTCOMES, MAX_PLAN_BYTES, MAX_SNAPSHOT_BYTES, plan_learning_cycle, select_mining_outcomes,
)

LEARNING_PROCESS_READY = b"LCRDY\x00\x00\x01"
READY_BYTES = 17
MAX_INPUT_BYTES = MAX_SNAPSHOT_BYTES + 1_048_576
MAX_OUTPUT_BYTES = MAX_PLAN_BYTES + 1_048_576
MAX_CPU_SECONDS = 120
DATA_LIMIT_BYTES = 512 * 1_048_576
MAX_STRUCTURE_WEIGHT = 512 * 1_048_576
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
if sys.platform == "darwin":
    # macOS introduces this public startup setting. Set its canonical value in
    # the trusted launcher; never inherit an arbitrary owner encoding/value.
    _ENVIRONMENT["__CF_USER_TEXT_ENCODING"] = f"0x{os.getuid():X}:0x0:0x0"
_OPERATIONS = frozenset({"select_mining", "plan"})
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_REQUEST_FIELDS = frozenset({
    "schema_version", "operation", "binding_digest", "outcomes", "cases", "owner_invalidated_run_ids",
})
_REPLY_FIELDS = frozenset({"schema_version", "operation", "binding_digest", "input_digest", "result"})
_OBSERVATIONS = frozenset({
    "trials", "accepted", "terminal_failures", "repairs", "verifier_disagreements", "mean_quality_micros",
    "workflow_token_volume", "workflow_model_calls", "workflow_latency_ms", "workflow_tool_calls",
    "delegation_overhead_tokens", "no_progress_events",
})
_FLAGS = frozenset({
    "usage_complete", "stale", "latency_complete", "tool_usage_complete", "insufficient_sample", "version_confounding",
})
_SPEC_FIELDS = frozenset({
    "scope", "task_slice", "target", "effect", "base_digest", "champion_configuration",
    "change_manifest", "evidence_digests", "eligibility_receipt",
})
_RECEIPT_FIELDS = frozenset({
    "task_slice", "evidence_set_digest", "independent_case_count", "usage_complete",
    "version_unconfounded", "task_mix_matched", "retry_deduplicated", "verifier_evidence_complete",
})


class LearningProcessError(RuntimeError):
    def __init__(self):
        super().__init__("learning_process_unavailable")


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _closed(value, fields):
    if type(value) is not dict or set(value) != fields:
        raise LearningProcessError()
    return value


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or maximum is not None and value > maximum:
        raise LearningProcessError()


def _analysis(value):
    _closed(value, {"schema_version", "as_of_ms", "owner_feedback_excluded", "slices"})
    _integer(value["schema_version"], 1, 1)
    _integer(value["as_of_ms"])
    _integer(value["owner_feedback_excluded"], 0, MAX_OUTCOMES)
    slices = value["slices"]
    if type(slices) is not dict or len(slices) > MAX_OUTCOMES:
        raise LearningProcessError()
    total = value["owner_feedback_excluded"]
    for task_slice, item in slices.items():
        if not _matches(task_slice, _ID):
            raise LearningProcessError()
        configurations = _closed(item, {"configurations"})["configurations"]
        if type(configurations) is not dict or not 1 <= len(configurations) <= MAX_OUTCOMES:
            raise LearningProcessError()
        for digest, section in configurations.items():
            if not _matches(digest, _DIGEST):
                raise LearningProcessError()
            _closed(section, {"observations", "evidence_flags", "diagnoses", "alternative_explanations"})
            observations = _closed(section["observations"], _OBSERVATIONS)
            flags = _closed(section["evidence_flags"], _FLAGS)
            if any(type(flag) is not bool for flag in flags.values()):
                raise LearningProcessError()
            trials = observations["trials"]
            _integer(trials, 1, MAX_OUTCOMES)
            total += trials
            if total > MAX_OUTCOMES:
                raise LearningProcessError()
            for name in ("accepted", "terminal_failures", "repairs", "verifier_disagreements"):
                _integer(observations[name], 0, trials)
            _integer(observations["mean_quality_micros"], 0, 1_000_000)
            _integer(observations["delegation_overhead_tokens"], 0, trials * 10**12)
            _integer(observations["no_progress_events"], 0, trials * 10_000)
            for name, complete, multiplicity in (
                ("workflow_token_volume", "usage_complete", 2), ("workflow_model_calls", "usage_complete", 1),
                ("workflow_latency_ms", "latency_complete", 1), ("workflow_tool_calls", "tool_usage_complete", 1),
            ):
                number = observations[name]
                if flags[complete]:
                    _integer(number, 0, trials * multiplicity * (2**63 - 1))
                elif number is not None:
                    raise LearningProcessError()
            if (flags["insufficient_sample"] != (trials < COMPARISON_MIN_CASES)
                    or flags["version_confounding"] != (len(configurations) > 1)):
                raise LearningProcessError()
            diagnoses, alternatives = [], []
            for condition, diagnosis, alternative in (
                (not all(flags[name] for name in ("usage_complete", "latency_complete", "tool_usage_complete")),
                 "telemetry_incomplete", "client_coverage_or_collection_gap"),
                (flags["stale"], "stale_evidence", "historical_behavior_may_not_match_current_package"),
                (flags["insufficient_sample"], "small_sample", "task_mix_variance"),
                (flags["version_confounding"], "version_confounding", "configuration_change"),
            ):
                if condition:
                    diagnoses.append(diagnosis)
                    alternatives.append(alternative)
            if section["diagnoses"] != diagnoses or section["alternative_explanations"] != alternatives:
                raise LearningProcessError()


def _candidate_spec(value):
    # Shape and internal binding only. No record ID/key, durable evidence or QA
    # authority is manufactured by decoding an inert suggestion.
    _closed(value, _SPEC_FIELDS)
    if (value["scope"] != "project" or value["target"] != "model_effort" or value["effect"] != "optimize"
            or not _matches(value["task_slice"], _ID) or len(canonical_json(value)) > MAX_RECORD_BYTES):
        raise LearningProcessError()
    champion = validate_execution_configuration(value["champion_configuration"])
    if value["base_digest"] != candidate_base_digest(champion):
        raise LearningProcessError()
    manifest = value["change_manifest"]
    if type(manifest) is not list or len(manifest) != 1:
        raise LearningProcessError()
    change = _closed(manifest[0], {"path_code", "before_digest", "after_digest", "before_value", "after_value"})
    before = _closed(change["before_value"], {"model", "effort"})
    after = _closed(change["after_value"], {"model", "effort"})
    if (change["path_code"] != "routing.model_effort" or before == after
            or before != {name: champion[name] for name in ("model", "effort")}
            or change["before_digest"] != content_digest(before) or change["after_digest"] != content_digest(after)):
        raise LearningProcessError()
    validate_execution_configuration({**champion, **after})
    evidence = value["evidence_digests"]
    if (type(evidence) is not list or not 40 <= len(evidence) <= 64
            or any(not _matches(digest, _DIGEST) for digest in evidence) or evidence != sorted(set(evidence))):
        raise LearningProcessError()
    receipt = _closed(value["eligibility_receipt"], _RECEIPT_FIELDS)
    count = receipt["independent_case_count"]
    _integer(count, COMPARISON_MIN_CASES, 32)
    if (receipt["task_slice"] != value["task_slice"] or count * 2 != len(evidence)
            or receipt["evidence_set_digest"] != content_digest(evidence)
            or any(receipt[name] is not True for name in _RECEIPT_FIELDS - {
                "task_slice", "evidence_set_digest", "independent_case_count",
            })):
        raise LearningProcessError()


def _result(value, operation):
    if operation == "select_mining":
        refs = _closed(value, {"mining_references"})["mining_references"]
        if type(refs) is not list or len(refs) > MAX_OUTCOMES:
            raise LearningProcessError()
        seen = set()
        for ref in refs:
            if (type(ref) is not list or len(ref) != 2 or not _matches(ref[0], _ID)
                    or not _matches(ref[1], _DIGEST) or ref[0] in seen):
                raise LearningProcessError()
            seen.add(ref[0])
    else:
        _closed(value, {"analysis", "proposal_status", "candidate_specs"})
        _analysis(value["analysis"])
        specs = value["candidate_specs"]
        if (type(value["proposal_status"]) is not str or value["proposal_status"] not in {"complete", "configuration_limit"}
                or type(specs) is not list or len(specs) > 64 * 63
                or value["proposal_status"] == "configuration_limit" and specs):
            raise LearningProcessError()
        for spec in specs:
            _candidate_spec(spec)
    return value


def process_arguments(arguments):
    if (type(arguments) is not list or len(arguments) != 2
            or any(type(value) is not str for value in arguments)
            or arguments[0] not in _OPERATIONS):
        raise LearningProcessError()
    number = arguments[1]
    if not 1 <= len(number) <= 3 or not number.isascii() or not number.isdecimal():
        raise LearningProcessError()
    seconds = int(number)
    if not 1 <= seconds <= MAX_CPU_SECONDS or str(seconds) != number:
        raise LearningProcessError()
    return arguments[0], seconds


def _bounded_json(raw, maximum):
    if type(raw) is not bytes or not 0 < len(raw) <= maximum or not raw.isascii():
        raise LearningProcessError()
    # Charge syntax even inside strings: overcounting refuses safely. This caps
    # allocation-heavy hostile structure BEFORE json.loads constructs objects.
    # The weight is a conservative syntax budget, not a physical RSS claim.
    weight = (4 * len(raw) + 256 * raw.count(b"{") + 96 * raw.count(b"[")
              + 128 * raw.count(b":") + 64 * raw.count(b","))
    if weight > MAX_STRUCTURE_WEIGHT:
        raise LearningProcessError()
    return parse_bounded_json(raw, max_bytes=maximum)


def _request(raw, operation):
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_INPUT_BYTES
            or type(operation) is not str or operation not in _OPERATIONS):
        raise LearningProcessError()
    value = _bounded_json(raw, MAX_INPUT_BYTES)
    fields = _REQUEST_FIELDS | ({"as_of_ms", "max_age_ms"} if operation == "plan" else set())
    if (set(value) != fields or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["operation"] != operation or not _matches(value["binding_digest"], _DIGEST)
            or any(type(value[name]) is not list for name in ("outcomes", "cases", "owner_invalidated_run_ids"))
            or canonical_json(value) != raw):
        raise LearningProcessError()
    return value


def calculate_learning_request(raw, *, operation):
    """Calculate a complete result within explicit envelopes; never truncate."""
    try:
        value = _request(raw, operation)
        if operation == "select_mining":
            sources = select_mining_outcomes(
                value["outcomes"], existing_cases=value["cases"],
                owner_invalidated_run_ids=value["owner_invalidated_run_ids"],
            )
            result = {"mining_references": [[item["record_id"], content_digest(item)] for item in sources]}
        else:
            result = plan_learning_cycle(
                value["outcomes"], all_cases=value["cases"], as_of_ms=value["as_of_ms"],
                max_age_ms=value["max_age_ms"], owner_invalidated_run_ids=value["owner_invalidated_run_ids"],
            )
        _result(result, operation)
        encoded = canonical_json({
            "schema_version": 1, "operation": operation, "binding_digest": value["binding_digest"],
            "input_digest": hashlib.sha256(raw).hexdigest(), "result": result,
        })
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise LearningProcessError()
        return encoded
    except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
        raise LearningProcessError() from None


def decode_calculation_reply(raw, *, input_digest, operation, binding_digest):
    """Check framing/binding outside the writer; this is not native provenance.

    Returned plans remain untrusted suggestions requiring actual owner evidence
    and eligibility checks. The caller must separately confirm original helper
    custody, successful exit, deadline and cleanup before using any result.
    """
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_OUTPUT_BYTES
            or not _matches(input_digest, _DIGEST) or not _matches(binding_digest, _DIGEST)
            or type(operation) is not str or operation not in _OPERATIONS):
        raise LearningProcessError()
    try:
        value = _bounded_json(raw, MAX_OUTPUT_BYTES)
        if (set(value) != _REPLY_FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["operation"] != operation or value["binding_digest"] != binding_digest
                or value["input_digest"] != input_digest or canonical_json(value) != raw
                or type(value["result"]) is not dict):
            raise LearningProcessError()
        return _result(value["result"], operation)
    except (ContractError, ValueError, TypeError, RecursionError, OverflowError):
        raise LearningProcessError() from None


def _require_environment():
    if dict(os.environ) != _ENVIRONMENT:
        raise LearningProcessError()


def _require_descriptors():
    # List the fixed kernel descriptor directory, not any supplied filesystem path.
    # listdir's own directory descriptor is closed before we inspect this snapshot.
    names = os.listdir("/dev/fd")
    if len(names) > 64:
        raise LearningProcessError()
    for name in names:
        if not name.isascii() or not name.isdecimal():
            raise LearningProcessError()
        descriptor = int(name)
        if descriptor <= 2:
            continue
        try:
            os.fstat(descriptor)
        except OSError as error:
            if error.errno == errno.EBADF:
                continue
            raise LearningProcessError() from None
        raise LearningProcessError()
    if any(not stat.S_ISFIFO(os.fstat(descriptor).st_mode) for descriptor in (0, 1, 2)):
        raise LearningProcessError()


def _apply_limits(seconds):
    # The tested Darwin kernel rejects finite RLIMIT_DATA settings. Structural,
    # byte, record and output envelopes remain enforced there; the native parent's
    # memory/process policy is still an activation gate. RLIMIT_DATA elsewhere
    # bounds a data segment, never total RSS.
    limits = [(resource.RLIMIT_CPU, seconds), (resource.RLIMIT_FSIZE, 0),
              (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 64)]
    if sys.platform != "darwin":
        limits.append((resource.RLIMIT_DATA, DATA_LIMIT_BYTES))
    for kind, desired in limits:
        _, hard = resource.getrlimit(kind)
        maximum = desired if hard == resource.RLIM_INFINITY else min(desired, hard)
        resource.setrlimit(kind, (maximum, maximum))


def _require_fork_denial():
    try:
        pid = os.fork()
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES):
            return
        raise LearningProcessError() from None
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    raise LearningProcessError()


def _refuse_calculation_io(event, arguments):
    if (event == "open" or event.startswith(("os.", "subprocess.", "socket.", "ctypes.", "mmap.", "sqlite3."))
            or event in {"resource.setrlimit", "resource.prlimit"}):
        raise LearningProcessError()


def _write(raw):
    remaining = memoryview(raw)
    while remaining:
        count = os.write(1, remaining)
        if count <= 0:
            raise LearningProcessError()
        remaining = remaining[count:]


def _run(operation):
    mode = b"S" if operation == "select_mining" else b"P"
    _write(LEARNING_PROCESS_READY + mode + struct.pack(">Q", os.getpid()))
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    sys.addaudithook(_refuse_calculation_io)
    _write(calculate_learning_request(raw, operation=operation))


def main(arguments=None):
    try:
        operation, seconds = process_arguments(sys.argv[1:] if arguments is None else arguments)
        _require_environment()
        _require_descriptors()
        _apply_limits(seconds)
        _require_fork_denial()
        _run(operation)
    except LearningProcessError:
        return 2
    except BaseException:
        # Fence faults only at this fixed private process boundary, never print
        # input bytes, raw diagnostics, stack traces or untrusted messages.
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
