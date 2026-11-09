"""Closed history-owner contracts and client; no database or native authority.

Endpoint discovery, transport identity and caller enrollment are separate gates.
Project/runtime fields are claims. The client neither starts an owner nor falls
back to SQLite, and never retries an ambiguously delivered mutation automatically.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError, TASK_FAMILIES
from .history_contracts import (
    HISTORY_REASON_CODES, MAX_SEQUENCE, AttemptBinding, HistoryError, TerminalDelivery,
    _DIGEST, _ID, _PROJECT, matches, valid_integer,
)
from .host_result_contracts import HostResultReference, decode_host_result_reference
from .host_open_contracts import OpenProvenanceInput, decode_host_open_submission
from .learning_contracts import SOURCES, canonical_json, parse_bounded_json, validate_learning_record
from .result_contracts import MAX_RESULT_BODY_BYTES
from .service_transport import MAX_FRAME_BYTES, MAX_TIMEOUT_SECONDS, ServiceTransportError, exchange_frame

HISTORY_PROTOCOL_VERSION = 1
MAX_HISTORY_REPLY_BYTES = 16_384
_RECORD_OPERATIONS = {
    "history.record_event": "event", "history.record_intent": "dispatch_intent",
    "history.record_outcome": "outcome", "history.bind_verifier": "verification_binding",
    "history.commit_terminal": "dispatch_result",
}
HISTORY_OPERATIONS = frozenset({
    *_RECORD_OPERATIONS, "history.pending_terminals", "history.acknowledge_terminal", "history.read_record",
    "history.admit_host_result", "history.record_host_open", "history.worker_reference", "history.qa_scope",
    "history.issue_qa_checks",
})
HISTORY_RECORD_TYPES = frozenset({*_RECORD_OPERATIONS.values(), "host_result_admission",
                                  "provider_usage", "observation_unavailable", "execution_provenance", "host_open_provenance"})
_REQUEST_FIELDS = {"protocol_version", "operation", "request_id", "project_id", "runtime_digest", "payload"}
_RESPONSE_FIELDS = {"protocol_version", "project_id", "runtime_digest", "request_id", "request_digest", "ready", "result", "reason_code"}
_METADATA_FIELDS = {"record_id", "source", "source_sequence", "observed_at_ms", "ingested_at_ms", "content_digest", "late"}


@dataclass(frozen=True)
class HistoryRequest:
    protocol_version: int
    operation: str
    request_id: str
    project_id: str
    runtime_digest: str
    payload: dict

    def __post_init__(self) -> None:
        encode_history_request(self)


def _closed(value: object, fields: set[str]) -> bool:
    return type(value) is dict and value.keys() == fields


def _checked_history_record(value: object, *, expected_type: str | None = None) -> dict:
    # Protocol-v1 history values describe the new typed store, not legacy import.
    # The existing shared legacy reader and its canonical bytes remain unchanged.
    if (
        type(value) is not dict or type(value.get("schema_version")) is not int
        or type(value.get("record_type")) is not str or value["record_type"] not in HISTORY_RECORD_TYPES
        or (expected_type is not None and value["record_type"] != expected_type)
    ):
        raise HistoryError("history_invalid_input")
    return validate_learning_record(value)


def _validate_request(request: HistoryRequest) -> None:
    if (
        type(request) is not HistoryRequest or type(request.protocol_version) is not int
        or request.protocol_version != HISTORY_PROTOCOL_VERSION
        or type(request.operation) is not str or request.operation not in HISTORY_OPERATIONS
        or not matches(request.request_id, _ID) or not matches(request.project_id, _PROJECT)
        or not matches(request.runtime_digest, _DIGEST)
    ):
        raise HistoryError("history_invalid_request")
    payload, operation = request.payload, request.operation
    if operation in _RECORD_OPERATIONS:
        fields = {"record", "binding"} if operation == "history.commit_terminal" else {"record"}
        if not _closed(payload, fields):
            raise HistoryError("history_invalid_request")
        record = _checked_history_record(payload["record"], expected_type=_RECORD_OPERATIONS[operation])
        identity = record["record_id"]
        if operation == "history.commit_terminal":
            if type(payload["binding"]) is not dict:
                raise HistoryError("history_invalid_request")
            binding = AttemptBinding(**payload["binding"])
            if (
                binding.project_id != request.project_id or binding.run_id != record["run_id"]
                or binding.runtime_digest != request.runtime_digest
            ):
                raise HistoryError("history_invalid_request")
            identity = binding.claim_id
        if request.request_id != identity:
            raise HistoryError("history_invalid_request")
    elif operation in {'history.qa_scope', 'history.issue_qa_checks'}:
        if not _closed(payload, {'worker_binding', 'verifier_binding', 'host_outcome_digest', 'verifier_host_outcome_digest'}):
            raise HistoryError('history_invalid_request')
        for name in ('worker_binding', 'verifier_binding'):
            if type(payload[name]) is not dict:
                raise HistoryError('history_invalid_request')
            binding = AttemptBinding(**payload[name])
            if binding.project_id != request.project_id or binding.runtime_digest != request.runtime_digest:
                raise HistoryError('history_invalid_request')
        if (not all(matches(payload[name], _DIGEST) for name in ('host_outcome_digest', 'verifier_host_outcome_digest'))
                or (operation == 'history.qa_scope'
                    and request.request_id != hashlib.sha256(canonical_json(payload)).hexdigest())):
            raise HistoryError('history_invalid_request')
    elif operation == 'history.worker_reference':
        if not _closed(payload, {'binding', 'host_outcome_digest'}) or type(payload['binding']) is not dict:
            raise HistoryError('history_invalid_request')
        binding = AttemptBinding(**payload['binding'])
        if (binding.project_id != request.project_id or binding.runtime_digest != request.runtime_digest
                or not matches(payload['host_outcome_digest'], _DIGEST)
                or request.request_id != payload['host_outcome_digest']):
            raise HistoryError('history_invalid_request')
    elif operation == 'history.record_host_open':
        opened, _ = decode_host_open_submission(payload, request.project_id, request.runtime_digest)
        if request.request_id != opened.run_handle:
            raise HistoryError('history_invalid_request')
    elif operation == "history.admit_host_result":
        reference = decode_host_result_reference(payload)
        if (reference.binding.project_id != request.project_id or reference.binding.runtime_digest != request.runtime_digest
                or request.request_id != reference.binding.claim_id):
            raise HistoryError("history_invalid_request")
    elif operation == "history.acknowledge_terminal":
        if (
            not _closed(payload, {"claim_id", "record_content_digest", "receipt_digest"})
            or request.request_id != payload["claim_id"] or not matches(payload["claim_id"], _ID)
            or not all(matches(payload[key], _DIGEST) for key in ("record_content_digest", "receipt_digest"))
        ):
            raise HistoryError("history_invalid_request")
    elif operation == "history.pending_terminals":
        if (
            not _closed(payload, {"after_sequence", "limit"})
            or not valid_integer(payload["after_sequence"])
            or not valid_integer(payload["limit"], minimum=1, maximum=100)
        ):
            raise HistoryError("history_invalid_request")
    elif not _closed(payload, {"record_id"}) or not matches(payload["record_id"], _ID):
        raise HistoryError("history_invalid_request")


def encode_history_request(request: HistoryRequest) -> bytes:
    invalid = False
    try:
        _validate_request(request)
        encoded = canonical_json({name: getattr(request, name) for name in _REQUEST_FIELDS})
    except (HistoryError, ContractError, AttributeError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_request")
    if len(encoded) + 1 > MAX_FRAME_BYTES:
        raise HistoryError("history_request_too_large")
    return encoded


def decode_history_request(payload: bytes) -> HistoryRequest:
    if type(payload) is not bytes:
        raise HistoryError("history_invalid_request")
    if len(payload) + 1 > MAX_FRAME_BYTES:
        raise HistoryError("history_request_too_large")
    invalid = False
    try:
        data = parse_bounded_json(payload, max_bytes=MAX_FRAME_BYTES - 1)
        if not _closed(data, _REQUEST_FIELDS):
            raise HistoryError("history_invalid_request")
        request = HistoryRequest(**data)
    except (HistoryError, ContractError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_request")
    return request


def history_request_digest(request: HistoryRequest) -> str:
    return hashlib.sha256(encode_history_request(request)).hexdigest()


def _validate_metadata(value: object) -> None:
    if (
        not _closed(value, _METADATA_FIELDS) or not matches(value["record_id"], _ID)
        or type(value["source"]) is not str or value["source"] not in SOURCES
        or not valid_integer(value["source_sequence"], minimum=1, maximum=MAX_SEQUENCE - 1)
        or not valid_integer(value["observed_at_ms"]) or not valid_integer(value["ingested_at_ms"])
        or not matches(value["content_digest"], _DIGEST) or type(value["late"]) is not bool
    ):
        raise HistoryError("history_invalid_response")


def delivery_from_data(value: object) -> TerminalDelivery:
    invalid = False
    try:
        delivery = _decoded_delivery(value)
    except (HistoryError, ContractError, KeyError, TypeError, ValueError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_response")
    return delivery


def _decoded_delivery(value: object) -> TerminalDelivery:
    fields = {"delivery_sequence", "binding", "record_id", "record_content_digest", "result_digest"}
    if not _closed(value, fields | {"dispatch_result"}):
        raise HistoryError("history_invalid_response")
    if type(value["binding"]) is not dict:
        raise HistoryError("history_invalid_response")
    delivery = TerminalDelivery(**{**{name: value[name] for name in fields}, "binding": AttemptBinding(**value["binding"])})
    record = _checked_history_record(value["dispatch_result"], expected_type="dispatch_result")
    if (
        record["record_type"] != "dispatch_result" or record["record_id"] != delivery.record_id
        or record["run_id"] != delivery.binding.run_id or record["result_digest"] != delivery.result_digest
        or hashlib.sha256(canonical_json(record)).hexdigest() != delivery.record_content_digest
    ):
        raise HistoryError("history_invalid_response")
    return delivery


def _validate_host_reference(result, binding, digest, *, verification):
    if (not _closed(result, {'binding', 'host_outcome_digest', 'provider', 'task_family', 'host_open_record_id',
                'host_open_content_digest', 'result_digest', 'body_size', 'terminal_record_id', 'expires_at_ms'})
                or result['binding'] != binding or result['host_outcome_digest'] != digest
                or type(result['provider']) is not str or result['provider'] not in {'codex', 'claude'}
                or type(result['task_family']) is not str or result['task_family'] not in TASK_FAMILIES
                or (result['task_family'] == 'verification') != verification
                or not all(matches(result[n], _DIGEST) for n in ('result_digest', 'host_open_content_digest'))
                or not all(matches(result[n], _ID) for n in ('host_open_record_id', 'terminal_record_id'))
                or not valid_integer(result['body_size'], minimum=1, maximum=MAX_RESULT_BODY_BYTES)
                or not valid_integer(result['expires_at_ms'])):
        raise HistoryError('history_invalid_response')
    # Dict equality alone treats JSON true as integer 1. Validate every nested
    # scope field before accepting equality to the independently sent binding.
    if type(result['binding']) is not dict:
        raise HistoryError('history_invalid_response')
    AttemptBinding(**result['binding'])


def _validate_result(request: HistoryRequest, result: object) -> None:
    operation = request.operation
    if operation == 'history.issue_qa_checks':
        if (not _closed(result, {'check_receipt', 'check_report_digest', 'checks_passed', 'expires_at_ms', 'known'})
                or type(result['check_receipt']) is not str
                or re.fullmatch(r'oqc_[0-9a-f]{48}', result['check_receipt']) is None
                or not matches(result['check_report_digest'], _DIGEST)
                or type(result['checks_passed']) is not bool or type(result['known']) is not bool
                or not valid_integer(result['expires_at_ms'])):
            raise HistoryError('history_invalid_response')
    elif operation == 'history.qa_scope':
        if not _closed(result, {'worker', 'verifier'}):
            raise HistoryError('history_invalid_response')
        for name, key in (('worker', 'host_outcome_digest'), ('verifier', 'verifier_host_outcome_digest')):
            _validate_host_reference(result[name], request.payload[name + '_binding'], request.payload[key],
                                     verification=name == 'verifier')
    elif operation == 'history.worker_reference':
        _validate_host_reference(result, request.payload['binding'], request.payload['host_outcome_digest'], verification=False)
        if result['provider'] != 'codex':
            raise HistoryError('history_invalid_response')
    elif operation == 'history.record_host_open':
        if not _closed(result, {'record', 'metadata'}):
            raise HistoryError('history_invalid_response')
        _, proof = decode_host_open_submission(request.payload, request.project_id, request.runtime_digest)
        record = _checked_history_record(result['record'], expected_type='host_open_provenance')
        metadata = result['metadata']
        _validate_metadata(metadata)
        if (record['provenance'] != proof or record['run_id'] != proof['provenance']['binding']['run_id']
                or metadata['source'] != 'host' or metadata['record_id'] != record['record_id']
                or metadata['observed_at_ms'] != record['observed_at_ms']
                or metadata['content_digest'] != hashlib.sha256(canonical_json(record)).hexdigest()):
            raise HistoryError('history_invalid_response')
    elif operation == "history.admit_host_result":
        if not _closed(result, {"admission", "admission_commit", "event_commit", "outcome_commit"}):
            raise HistoryError("history_invalid_response")
        reference = decode_host_result_reference(request.payload)
        receipt = _checked_history_record(result["admission"], expected_type="host_result_admission")
        terminal = receipt["dispatch_result"]
        if (receipt["run_id"] != reference.binding.run_id or terminal["bridge_run_id"] != reference.binding.claim_id
                or terminal["record_id"] != reference.terminal_record_id
                or terminal["result_digest"] != reference.result_digest
                or receipt["terminal_content_digest"] != reference.terminal_content_digest):
            raise HistoryError("history_invalid_response")
        for label, identity, digest in (
            ("admission_commit", receipt["record_id"], hashlib.sha256(canonical_json(receipt)).hexdigest()),
            ("event_commit", receipt["event_record_id"], receipt["event_content_digest"]),
            ("outcome_commit", receipt["outcome_record_id"], receipt["host_outcome_digest"]),
        ):
            metadata = result[label]
            _validate_metadata(metadata)
            if (metadata["source"] != "host" or metadata["record_id"] != identity or metadata["content_digest"] != digest
                    or metadata["observed_at_ms"] != receipt["observed_at_ms"]):
                raise HistoryError("history_invalid_response")
    elif operation in _RECORD_OPERATIONS and operation != "history.commit_terminal":
        _validate_metadata(result)
        record = request.payload["record"]
        if (
            result["record_id"] != record["record_id"] or result["observed_at_ms"] != record["observed_at_ms"]
            or result["content_digest"] != hashlib.sha256(canonical_json(record)).hexdigest()
        ):
            raise HistoryError("history_invalid_response")
    elif operation == "history.commit_terminal":
        delivery = delivery_from_data(result)
        if (
            delivery.binding.as_data() != request.payload["binding"]
            or delivery.record_id != request.payload["record"]["record_id"]
            or delivery.result_digest != request.payload["record"]["result_digest"]
            or delivery.record_content_digest != hashlib.sha256(canonical_json(request.payload["record"])).hexdigest()
        ):
            raise HistoryError("history_invalid_response")
    elif operation == "history.pending_terminals":
        if not _closed(result, {"deliveries", "next_after_sequence"}) or type(result["deliveries"]) is not list:
            raise HistoryError("history_invalid_response")
        if len(result["deliveries"]) > request.payload["limit"]:
            raise HistoryError("history_invalid_response")
        sequence = request.payload["after_sequence"]
        for data in result["deliveries"]:
            delivery = delivery_from_data(data)
            if delivery.binding.project_id != request.project_id or delivery.delivery_sequence <= sequence:
                raise HistoryError("history_invalid_response")
            sequence = delivery.delivery_sequence
        expected = sequence if result["deliveries"] else None
        if result["next_after_sequence"] != expected or (
            expected is not None and type(result["next_after_sequence"]) is not int
        ):
            raise HistoryError("history_invalid_response")
    elif operation == "history.acknowledge_terminal":
        if not _closed(result, {"recorded"}) or result["recorded"] is not True:
            raise HistoryError("history_invalid_response")
    else:
        if not _closed(result, {"record", "metadata"}) or type(result["record"]) is not dict:
            raise HistoryError("history_invalid_response")
        _validate_metadata(result["metadata"])
        record, metadata = _checked_history_record(result["record"]), result["metadata"]
        if (
            record["record_id"] != request.payload["record_id"] or metadata["record_id"] != record["record_id"]
            or metadata["observed_at_ms"] != record["observed_at_ms"]
            or metadata["content_digest"] != hashlib.sha256(canonical_json(record)).hexdigest()
        ):
            raise HistoryError("history_invalid_response")


def decode_history_response(raw: bytes, request: HistoryRequest) -> dict:
    invalid = False
    try:
        _validate_request(request)
        limit = MAX_FRAME_BYTES if request.operation == "history.read_record" else MAX_HISTORY_REPLY_BYTES
        if type(raw) is not bytes or len(raw) + 1 > limit:
            raise HistoryError("history_invalid_response")
        response = parse_bounded_json(raw, max_bytes=limit - 1)
        if (
            not _closed(response, _RESPONSE_FIELDS) or type(response["protocol_version"]) is not int
            or response["protocol_version"] != HISTORY_PROTOCOL_VERSION
            or response["project_id"] != request.project_id or response["runtime_digest"] != request.runtime_digest
            or response["request_id"] != request.request_id or type(response["ready"]) is not bool
            or response["request_digest"] != history_request_digest(request)
        ):
            raise HistoryError("history_invalid_response")
        if response["ready"]:
            if response["reason_code"] is not None:
                raise HistoryError("history_invalid_response")
            _validate_result(request, response["result"])
        elif (
            response["result"] is not None or type(response["reason_code"]) is not str
            or response["reason_code"] not in HISTORY_REASON_CODES
        ):
            raise HistoryError("history_invalid_response")
    except (HistoryError, ContractError, AttributeError, TypeError, ValueError, KeyError, OverflowError, RecursionError):
        invalid = True
    if invalid:
        raise HistoryError("history_invalid_response")
    if not response["ready"]:
        raise HistoryError(response["reason_code"])
    return response["result"]


class HistoryClient:
    """A bounded, non-retrying client for an already trusted local endpoint.

    Runtime/project labels alone do not authenticate that endpoint or its caller.
    A transport/parse failure may follow a commit; explicitly retry the same
    immutable business identity after reconciling availability.
    """

    def __init__(self, endpoint: Path, project_id: str, runtime_digest: str, *, timeout: float) -> None:
        if (
            not isinstance(endpoint, Path) or not endpoint.is_absolute()
            or not matches(project_id, _PROJECT) or not matches(runtime_digest, _DIGEST)
            or type(timeout) not in (int, float) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
        ):
            raise HistoryError("history_invalid_input")
        self._endpoint, self._project, self._runtime, self._timeout = endpoint, project_id, runtime_digest, timeout

    def _call(self, operation: str, identity: str, payload: dict) -> dict:
        request = HistoryRequest(HISTORY_PROTOCOL_VERSION, operation, identity, self._project, self._runtime, payload)
        encoded = encode_history_request(request)
        request = decode_history_request(encoded)
        failure = False
        try:
            raw = exchange_frame(self._endpoint, encoded, timeout=self._timeout,
                                 response_limit=MAX_FRAME_BYTES if operation == "history.read_record" else MAX_HISTORY_REPLY_BYTES)
        except ServiceTransportError:
            failure = True
        if failure:
            raise HistoryError("history_service_unavailable")
        return decode_history_response(raw, request)

    def _record_call(self, operation: str, record: dict) -> dict:
        if type(record) is not dict:
            raise HistoryError("history_invalid_input")
        return self._call(operation, record.get("record_id"), {"record": record})

    def record_event(self, record: dict) -> dict:
        return self._record_call("history.record_event", record)

    def record_intent(self, record: dict) -> dict:
        return self._record_call("history.record_intent", record)

    def record_outcome(self, record: dict) -> dict:
        return self._record_call("history.record_outcome", record)

    def record_host_open(self, request: OpenProvenanceInput, provenance: dict) -> dict:
        if type(request) is not OpenProvenanceInput:
            raise HistoryError('history_invalid_input')
        try:
            payload = {'open_request': request.payload(), 'provenance': provenance}
        except Exception:
            raise HistoryError('history_invalid_input') from None
        return self._call('history.record_host_open', request.run_handle,
                          payload)

    def admit_host_result(self, reference: HostResultReference) -> dict:
        if type(reference) is not HostResultReference:
            raise HistoryError("history_invalid_input")
        return self._call("history.admit_host_result", reference.binding.claim_id, reference.as_data())

    def verification_worker_reference(self, binding: AttemptBinding, host_outcome_digest: str) -> dict:
        """Resolve metadata before verifier open; private bytes use ResultReadClient.

        The native adapter independently authorizes the protected verifier task
        and original worker scope. Request labels alone grant no result access.
        """
        if type(binding) is not AttemptBinding:
            raise HistoryError('history_invalid_input')
        return self._call('history.worker_reference', host_outcome_digest,
            {'binding': binding.as_data(), 'host_outcome_digest': host_outcome_digest})

    def operational_qa_scope(self, worker: AttemptBinding, verifier: AttemptBinding,
                             host_outcome_digest: str, verifier_host_outcome_digest: str) -> dict:
        """Resolve protected originals before checks; no result bodies or QA booleans."""
        if type(worker) is not AttemptBinding or type(verifier) is not AttemptBinding:
            raise HistoryError('history_invalid_input')
        payload = {'worker_binding': worker.as_data(), 'verifier_binding': verifier.as_data(),
                   'host_outcome_digest': host_outcome_digest, 'verifier_host_outcome_digest': verifier_host_outcome_digest}
        return self._call('history.qa_scope', hashlib.sha256(canonical_json(payload)).hexdigest(), payload)

    def issue_operational_check_receipt(self, worker: AttemptBinding, verifier: AttemptBinding,
                                       host_outcome_digest: str, verifier_host_outcome_digest: str, *, request_id: str) -> dict:
        """Name original pair/report identity; the trusted report stays server-side."""
        if type(worker) is not AttemptBinding or type(verifier) is not AttemptBinding:
            raise HistoryError('history_invalid_input')
        return self._call('history.issue_qa_checks', request_id,
            {'worker_binding': worker.as_data(), 'verifier_binding': verifier.as_data(),
             'host_outcome_digest': host_outcome_digest, 'verifier_host_outcome_digest': verifier_host_outcome_digest})

    def bind_verifier(self, record: dict) -> dict:
        return self._record_call("history.bind_verifier", record)

    def commit_terminal(self, record: dict, binding: AttemptBinding) -> dict:
        if type(binding) is not AttemptBinding:
            raise HistoryError("history_invalid_input")
        return self._call("history.commit_terminal", binding.claim_id, {"record": record, "binding": binding.as_data()})

    def pending_terminals(self, *, after_sequence: int = 0, limit: int = 10) -> dict:
        return self._call("history.pending_terminals", "pending", {"after_sequence": after_sequence, "limit": limit})

    def acknowledge_terminal(self, claim_id: str, record_content_digest: str, receipt_digest: str) -> dict:
        return self._call("history.acknowledge_terminal", claim_id,
                          {"claim_id": claim_id, "record_content_digest": record_content_digest, "receipt_digest": receipt_digest})

    def read_record(self, record_id: str) -> dict:
        return self._call("history.read_record", "read", {"record_id": record_id})
