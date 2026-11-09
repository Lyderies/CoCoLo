"""Closed account/project registration metadata, never native caller authority.

Only independently enrolled project/account services may exchange these values.
The local project must durably consume the original registration before its
actual fixed writer return can issue process custody. Parsed data or a MAC alone
cannot issue a ticket, reconstruct an execution or release account capacity.
"""
from dataclasses import asdict, dataclass
import secrets

from .account_claim_contracts import ClaimError, _BRIDGE, _DIGEST, _HANDLE, _REQUEST_ID, matches
from .history_contracts import AttemptBinding, valid_integer
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json

MAX_EXECUTION_GRANT_BYTES = 8192


@dataclass(frozen=True)
class ExecutionRegistrationInput:
    run_handle: str
    bridge_run_id: str
    claim_digest: str
    claim_request_id: str
    identity_digest: str

    def __post_init__(self):
        if not all(matches(value, pattern) for value, pattern in (
            (self.run_handle, _HANDLE), (self.bridge_run_id, _BRIDGE), (self.claim_digest, _DIGEST),
            (self.claim_request_id, _REQUEST_ID), (self.identity_digest, _DIGEST),
        )):
            raise ClaimError("claim_execution_invalid")

    def payload(self):
        self.__post_init__()
        return asdict(self)


def decode_execution_registration(payload):
    invalid = False
    try:
        if type(payload) is not dict:
            raise ValueError()
        value = ExecutionRegistrationInput(**payload)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_execution_invalid")
    return value


@dataclass(frozen=True)
class ExecutionGrant:
    binding: AttemptBinding
    handle_digest: str
    assignment_digest: str
    task_digest: str
    route_digest: str
    configuration_digest: str
    lease_digest: str
    clock_owner_digest: str
    clock_reference_ns: int
    deadline_ns: int
    claimed_at_ms: int
    deadline_at_ms: int
    registration_digest: str

    def __post_init__(self):
        if (type(self.binding) is not AttemptBinding
                or any(not matches(value, _DIGEST) for value in (
                    self.handle_digest, self.assignment_digest, self.task_digest, self.route_digest,
                    self.configuration_digest, self.lease_digest,
                    self.clock_owner_digest, self.registration_digest))
                or not valid_integer(self.clock_reference_ns)
                or not valid_integer(self.deadline_ns, minimum=self.clock_reference_ns + 1)
                or not valid_integer(self.claimed_at_ms)
                or not valid_integer(self.deadline_at_ms, minimum=self.claimed_at_ms + 1)
                or self.deadline_ns - self.clock_reference_ns != (self.deadline_at_ms - self.claimed_at_ms) * 1_000_000):
            raise ClaimError("claim_execution_invalid")
        self.binding.as_data()

    def as_data(self):
        self.__post_init__()
        return asdict(self)

    def encode(self):
        raw = canonical_json({"protocol_version": 1, "grant": self.as_data()})
        if len(raw) > MAX_EXECUTION_GRANT_BYTES:
            raise ClaimError("claim_execution_invalid")
        return raw


def checked_execution_grant(data):
    invalid = False
    try:
        if type(data) is not dict:
            raise ValueError()
        fields = dict(data)
        fields["binding"] = AttemptBinding(**fields["binding"])
        value = ExecutionGrant(**fields)
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_execution_invalid")
    return value


def registration_digest(key, material):
    return keyed_digest(key, canonical_json({"domain": "account-project-execution-v1", **material}))


def sign_execution_grant(key, **fields):
    material = {**fields, "binding": fields["binding"].as_data()}
    return ExecutionGrant(**fields, registration_digest=registration_digest(key, material))


def verify_execution_grant(key, binding, raw):
    invalid = False
    try:
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_EXECUTION_GRANT_BYTES or type(binding) is not AttemptBinding:
            raise ValueError()
        binding.as_data()
        data = parse_bounded_json(raw, max_bytes=MAX_EXECUTION_GRANT_BYTES)
        if type(data) is not dict or set(data) != {"protocol_version", "grant"} or type(data["protocol_version"]) is not int or data["protocol_version"] != 1:
            raise ValueError()
        value = checked_execution_grant(data["grant"])
        material = value.as_data()
        digest = material.pop("registration_digest")
        invalid = (value.binding != binding or raw != value.encode()
                   or not secrets.compare_digest(digest, registration_digest(key, material))
                   or not secrets.compare_digest(value.handle_digest, keyed_digest(key, binding.run_handle)))
    except Exception:
        invalid = True
    if invalid:
        raise ClaimError("claim_execution_invalid")
    return value


def checked_execution_reply(value):
    if type(value) is not dict or set(value) != {"grant", "known"} or type(value["known"]) is not bool:
        raise ClaimError("claim_execution_invalid")
    return {"grant": checked_execution_grant(value["grant"]).as_data(), "known": value["known"]}
