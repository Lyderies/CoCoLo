from __future__ import annotations

import unittest
from copy import deepcopy
from unittest import mock

import tests.support  # noqa: F401 -- bootstrap the native package import path
from codex_agents import bridge
from codex_agents.contracts import (
    RESULT_VALIDATION_REASON_CODES,
    ContractError,
    ResultContractError,
)
from codex_agents.learning_contracts import canonical_json, validate_learning_record
from codex_agents.learning_store import MAX_WORKER_RESULT_BYTES
from tests.learning_support import assignment, result


class ResultDiagnosticTests(unittest.TestCase):
    def test_actual_invalid_results_preserve_every_typed_reason(self) -> None:
        valid = result()
        evidence = valid["evidence"][0]
        missing = dict(valid)
        del missing["summary"]
        extra_criterion = assignment()
        extra_criterion["required_evidence"].append({"id": "second", "description": "second"})
        cases = {
            "assignment_mismatch": ({**valid, "assignment_id": "other"}, assignment()),
            "duplicate_criterion": ({**valid, "evidence": [evidence, evidence]}, assignment()),
            "failed_completed_evidence": (
                {**valid, "evidence": [{**evidence, "outcome": "fail"}]}, assignment()
            ),
            "invalid_evidence": ({**valid, "evidence": ["CANARY-REJECTED"]}, assignment()),
            "invalid_shape": ({**valid, "summary": {"CANARY-FIELD": "CANARY-REJECTED"}}, assignment()),
            "invalid_status": ({**valid, "status": "CANARY-REJECTED"}, assignment()),
            "invalid_usage": ({**valid, "usage": {"cost": "CANARY-REJECTED"}}, assignment()),
            "missing_evidence": ({**valid, "evidence": []}, assignment()),
            "missing_required_evidence": (valid, extra_criterion),
            "missing_required_fields": (missing, assignment()),
            "unknown_criterion": (
                {**valid, "evidence": [{**evidence, "criterion_id": "CANARY-REJECTED"}]}, assignment()
            ),
            "unknown_fields": ({**valid, "CANARY-FIELD": "CANARY-REJECTED"}, assignment()),
            "inconsistent_axes": ({**valid, "execution_state": "failed"}, assignment()),
        }
        self.assertEqual(RESULT_VALIDATION_REASON_CODES, set(cases))
        for reason, (invalid, assigned) in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(("invalid_result", reason), bridge._unrecordable(invalid, assigned))

    def test_generic_and_malformed_errors_use_only_the_fixed_fallback(self) -> None:
        errors = [ContractError("CANARY-EXCEPTION", field="CANARY-FIELD")]
        generic = ContractError("CANARY-EXCEPTION")
        generic.reason_code = "missing_evidence"
        errors.append(generic)
        for injected in ("CANARY-REJECTED", None, [], {}, 42):
            error = ResultContractError("missing_evidence", "CANARY-EXCEPTION")
            error.reason_code = injected
            errors.append(error)
        missing = ResultContractError("missing_evidence", "CANARY-EXCEPTION")
        del missing.reason_code
        errors.append(missing)
        for error in errors:
            with self.subTest(error=type(error).__name__), mock.patch.object(
                bridge, "validate_result", side_effect=error
            ):
                self.assertEqual(
                    ("invalid_result", "contract_validation_failed"),
                    bridge._unrecordable(result(), assignment()),
                )

    def test_valid_and_oversized_results_have_no_contract_rejection_reason(self) -> None:
        self.assertEqual((None, None), bridge._unrecordable(result(), assignment()))
        oversized = {**result(), "summary": "x" * MAX_WORKER_RESULT_BYTES}
        self.assertEqual(("result_too_large", None), bridge._unrecordable(oversized, assignment()))

    def test_versioned_diagnostic_record_keeps_legacy_bytes_and_unknown_reasons(self) -> None:
        legacy = {
            "schema_version": 1, "record_type": "dispatch_result", "record_id": "dispatch_result:old",
            "run_id": "run-old", "bridge_run_id": "brg_" + "a" * 32, "result_digest": "b" * 64,
            "fenced": True, "reviewed": True, "terminal_state": "failed", "observed_at_ms": 5,
        }
        original_bytes = canonical_json(legacy)
        self.assertEqual(original_bytes, canonical_json(validate_learning_record(deepcopy(legacy))))
        self.assertNotIn("result_validation_reason_code", validate_learning_record(legacy))
        modern = {**legacy, "schema_version": 2, "result_validation_reason_code": "missing_evidence"}
        self.assertEqual(modern, validate_learning_record(modern))
        for changes in (
            {"result_validation_reason_code": "CANARY-REJECTED"},
            {"result_validation_reason_code": []},
            {"result_validation_reason_code": True},
            {"terminal_state": "timed_out"},
            {"fenced": False, "terminal_state": None},
            {"schema_version": 3},
            {"CANARY-FIELD": "CANARY-REJECTED"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_learning_record({**modern, **changes})
        for reason in (*RESULT_VALIDATION_REASON_CODES, "contract_validation_failed", None):
            validate_learning_record({**modern, "result_validation_reason_code": reason})
        with self.assertRaises(ContractError):
            validate_learning_record({**legacy, "result_validation_reason_code": "missing_evidence"})
        with self.assertRaises(ContractError):
            validate_learning_record({**legacy, "schema_version": 2})


if __name__ == "__main__":
    unittest.main()
