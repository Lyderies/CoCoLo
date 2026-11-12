"""Bounded keyed record preparation by the existing project persistence owner.

Account storage verification already checks project seals using the account's
legacy digest key. Compatible original admission and a verified migration/key
domain remain requirements; a caller-provided digest authenticates nothing.
This helper checks compatibility with the owner's installed key and reads exact
durable outcomes. It transfers no key to a client or calculation worker.

Prepared records are inert. Full evidence/QA/feedback eligibility, revision and
authority rechecks still precede any append. Staged imports are not read here.
No normal API exposes these helpers and no transaction may enclose preparation.
"""

from __future__ import annotations

import secrets

from .contracts import ContractError
from .history_contracts import HistoryError, _DIGEST, _ID, matches
from .learning_contracts import (
    MAX_RECORD_BYTES, canonical_json, content_digest, keyed_digest, parse_bounded_json,
    validate_execution_configuration,
)
from .learning_engine import generate_candidate, mine_evaluation_case

LEARNING_KEY_DOMAIN = b"project-learning-key-domain-v1\0"
MAX_CASE_PREPARATION = 64
MAX_PREPARATION_BYTES = 262_144
_CANDIDATE_FIELDS = frozenset({
    "scope", "task_slice", "target", "effect", "base_digest", "champion_configuration",
    "change_manifest", "evidence_digests", "eligibility_receipt",
})


def _key(history):
    history._require_owner()
    if history._connection.in_transaction:
        raise HistoryError("history_result_unavailable")
    key = history._result_key
    if type(key) is not bytes or len(key) != 32 or history._result_capacity is None:
        raise HistoryError("history_result_unavailable")
    row = history._connection.execute(
        "SELECT key_check FROM result_identity WHERE project_id=?", (history.project_id,),
    ).fetchone()
    if (row is None or not matches(row["key_check"], _DIGEST)
            or not secrets.compare_digest(row["key_check"], keyed_digest(key, "project-result-key-v1:" + history.project_id))):
        raise HistoryError("history_incompatible")
    return key


def key_domain_digest(history):
    key = _key(history)
    return keyed_digest(key, LEARNING_KEY_DOMAIN + history.project_id.encode("ascii"))


def _compatible_key(history, expected):
    key = _key(history)
    if not matches(expected, _DIGEST):
        raise HistoryError("history_invalid_input")
    actual = keyed_digest(key, LEARNING_KEY_DOMAIN + history.project_id.encode("ascii"))
    if not secrets.compare_digest(expected, actual):
        raise HistoryError("history_conflict")
    return key


def _references(references):
    if type(references) not in (list, tuple) or not 1 <= len(references) <= MAX_CASE_PREPARATION:
        raise HistoryError("history_invalid_input")
    captured, identifiers, digests = [], set(), set()
    for pair in references:
        if (type(pair) not in (list, tuple) or len(pair) != 2
                or not matches(pair[0], _ID) or not matches(pair[1], _DIGEST)
                or pair[0] in identifiers or pair[1] in digests):
            raise HistoryError("history_invalid_input")
        identifiers.add(pair[0])
        digests.add(pair[1])
        captured.append((pair[0], pair[1]))
    return tuple(captured)


def _outcomes(history, references):
    result, size = [], 0
    for identity, digest in references:
        # Primary-key lookups, never a digest/JSON predicate scan of the corpus.
        entry = history.record(identity)
        if entry.content_digest != digest:
            raise HistoryError("history_conflict")
        size += len(entry.content)
        if size > MAX_PREPARATION_BYTES:
            raise HistoryError("history_learning_capacity")
        record = parse_bounded_json(entry.content)
        if record["record_type"] != "outcome":
            raise HistoryError("history_invalid_input")
        result.append(record)
    return result


def prepare_cases(history, references, *, expected_domain_digest):
    key = _compatible_key(history, expected_domain_digest)
    references = _references(references)
    outcomes = _outcomes(history, references)
    cases = []
    for outcome in outcomes:
        if outcome["status"] == "unverified" or outcome["evaluation_candidate_digest"] is not None:
            raise HistoryError("history_invalid_input")
        criteria = {name: outcome[name] for name in (
            "deterministic_pass", "security_pass", "required_criteria_total", "required_criteria_passed",
        )}
        cases.append(mine_evaluation_case(
            digest_key=key, outcome=outcome, fixture_digest=outcome["fixture_digest"],
            criteria_digest=content_digest(criteria), lineage_digest=keyed_digest(key, outcome["run_id"]),
        ))
    return cases


def prepare_candidate(history, spec, references, *, expected_domain_digest):
    key = _compatible_key(history, expected_domain_digest)
    references = _references(references)
    if type(spec) is not bytes or not 0 < len(spec) <= MAX_RECORD_BYTES:
        raise HistoryError("history_invalid_input")
    try:
        raw, spec = spec, parse_bounded_json(spec)
        if (type(spec) is not dict or set(spec) != _CANDIDATE_FIELDS or canonical_json(spec) != raw
                or spec["scope"] != "project" or spec["target"] != "model_effort" or spec["effect"] != "optimize"
                or type(spec["change_manifest"]) is not list or len(spec["change_manifest"]) != 1
                or type(spec["change_manifest"][0]) is not dict
                or set(spec["change_manifest"][0]) != {
                    "path_code", "before_digest", "after_digest", "before_value", "after_value",
                }
                or spec["change_manifest"][0].get("path_code") != "routing.model_effort"
                or spec["evidence_digests"] != sorted(digest for _, digest in references)):
            raise HistoryError("history_invalid_input")
        validate_execution_configuration(spec["champion_configuration"])
        _outcomes(history, references)
        # Reuse the exact legacy generator; no eligibility claim is upgraded here.
        record = generate_candidate(digest_key=key, **spec)
        if len(canonical_json(record)) > MAX_RECORD_BYTES:
            raise HistoryError("history_learning_capacity")
        return record
    except (ContractError, ValueError, TypeError, OverflowError, RecursionError):
        raise HistoryError("history_invalid_input") from None
