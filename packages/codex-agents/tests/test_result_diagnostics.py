from __future__ import annotations

import importlib.abc
import json
import sys
import unittest
from copy import deepcopy
from unittest import mock

import tests.support  # noqa: F401 -- bootstrap the native package import path
from codex_agents import bridge
from codex_agents.contracts import (
    RESULT_VALIDATION_REASON_CODES,
    ContractError,
    ResultContractError,
    result_preflight,
    validate_result,
)
from codex_agents.learning_contracts import (
    CLIENT_ERROR_CODES,
    CLIENT_EXIT_CODE_MAX,
    CLIENT_EXIT_CODE_MIN,
    CLIENT_RESULT_SUBTYPES,
    canonical_json,
    validate_client_diagnostics,
    validate_learning_record,
)
from codex_agents.learning_store import MAX_WORKER_RESULT_BYTES
from tests.learning_support import assignment, result


class ResultDiagnosticTests(unittest.TestCase):
    def test_codex_abort_labels_use_only_fixed_protocol_names(self) -> None:
        class HostileString(str):
            def __hash__(self):
                raise AssertionError("untrusted string subclass was hashed")

        for value in (None, True, 1, [], {}, "", "CANARY_EVENT_SECRET", "thread.surprise",
                      "mcp_tool_call" + "x" * 100_000, "\N{SNOWMAN}", HostileString("mcp_tool_call")):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual("unknown", bridge._event_label(value))
        for value in ("mcp_tool_call", "web_search", "collab_tool_call"):
            self.assertEqual(value, bridge._event_label(value))

    def test_result_validation_never_imports_a_persistence_owner(self) -> None:
        blocked = {"sqlite3", "_sqlite3", "codex_agents.learning_store", "codex_agents.project_history"}

        class Guard(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name in blocked:
                    raise AssertionError("result validation imported persistence")

        # QA deliberately forbids process creation. Fresh module imports still
        # exercise the boundary, even when earlier tests imported the owners.
        # Restore both caches so later tests keep their original class identity.
        with mock.patch.dict(sys.modules), mock.patch.object(sys, "meta_path", [Guard(), *sys.meta_path]):
            for name in tuple(sys.modules):
                if name in blocked or name == "codex_agents" or name.startswith("codex_agents."):
                    del sys.modules[name]
            for name in sorted(blocked):
                with self.assertRaisesRegex(AssertionError, "result validation imported persistence"):
                    importlib.import_module(name)
            fresh_bridge = importlib.import_module("codex_agents.bridge")
            body_limit = importlib.import_module("codex_agents.result_contracts").MAX_RESULT_BODY_BYTES
            assigned, value = assignment(), result()
            observed = {
                "valid": fresh_bridge._unrecordable(value, assigned),
                "invalid": fresh_bridge._unrecordable({**value, "assignment_id": "different"}, assigned),
                "oversized": fresh_bridge._unrecordable({**value, "summary": "x" * body_limit}, assigned),
                "sqlite_loaded": "sqlite3" in sys.modules,
            }
        self.assertEqual({"valid": (None, None), "invalid": ("invalid_result", "assignment_mismatch"),
                          "oversized": ("result_too_large", None), "sqlite_loaded": False}, observed)

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
            # 0.4.0 item 0.5 (VF-INV-b, decision 26 branch split): `inconsistent_axes` is now one code per branch.
            "axes_status_verdict_mismatch": ({**valid, "execution_state": "failed"}, assignment()),
            "axes_verdict_without_completion": (
                {**valid, "status": "failed", "execution_state": "failed"}, assignment()),
            "axes_admission_without_completion": (
                {**valid, "status": "failed", "execution_state": "failed", "semantic_verdict": "not-applicable",
                 "admission_state": "accepted"}, assignment()),
            "axes_retry_lineage": (
                {**valid, "retry": {"attempt": 1, "retry_of_assignment_id": "earlier"}}, assignment()),
            "child_set_admission": ({**valid, "admission_state": "accepted"}, assignment()),
            "child_set_timeout": (
                {**valid, "status": "failed", "execution_state": "timed_out", "semantic_verdict": "not-applicable"},
                assignment()),
            # RV1: each evidence rule's own code, all content-free.
            "evidence_missing_fields": ({**valid, "evidence": [{k: v for k, v in evidence.items() if k != "details"}]},
                                        assignment()),
            "evidence_unknown_fields": ({**valid, "evidence": [{**evidence, "CANARY-FIELD": "x"}]}, assignment()),
            "evidence_unsupported_type": ({**valid, "evidence": [{**evidence, "type": "CANARY-REJECTED"}]}, assignment()),
            "evidence_empty_criterion": ({**valid, "evidence": [{**evidence, "criterion_id": ""}]}, assignment()),
            "evidence_empty_reference": ({**valid, "evidence": [{**evidence, "reference": ""}]}, assignment()),
            "evidence_unsupported_outcome": ({**valid, "evidence": [{**evidence, "outcome": "CANARY-REJECTED"}]},
                                             assignment()),
            "evidence_details_not_text": ({**valid, "evidence": [{**evidence, "details": ["CANARY-REJECTED"]}]},
                                          assignment()),
        }
        # Corrected for O10 VF-2 (requirement): structured_output_missing is named by the bridge when the
        # client gave no result to validate (test_claude_structured_output), never by the validator.
        self.assertEqual(RESULT_VALIDATION_REASON_CODES - {"structured_output_missing"}, set(cases))
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

    def test_malformed_result_axes_return_a_typed_reason_without_an_exception(self) -> None:
        for field in ("execution_state", "semantic_verdict", "admission_state"):
            for value in (["CANARY-AXIS"], {"CANARY-AXIS": "CANARY-VALUE"}, True, 42, None):
                with self.subTest(field=field, value=value):
                    self.assertEqual(
                        ("invalid_result", "invalid_status"),
                        bridge._unrecordable({**result(), field: value}, assignment()),
                    )

    def test_valid_and_oversized_results_have_no_contract_rejection_reason(self) -> None:
        self.assertEqual((None, None), bridge._unrecordable(result(), assignment()))
        oversized = {**result(), "summary": "x" * MAX_WORKER_RESULT_BYTES}
        self.assertEqual(("result_too_large", None), bridge._unrecordable(oversized, assignment()))

    def test_child_axes_cannot_claim_host_owned_admission_or_timeout(self) -> None:
        # Owner decision 26 (VF-INV-d): this rejection holds for every role except a verifier, whose
        # admission the host resets and records (VerifierHostSetFieldsDispatchTests, VerifierInvalidResultTests).
        # A child's timeout claim is rejected for every role. Reason codes follow VF-INV-b.
        for changes, reason in (
            ({"admission_state": "accepted"}, "child_set_admission"),
            ({"admission_state": "rejected"}, "child_set_admission"),
            ({"admission_state": "pending"}, "child_set_admission"),
            ({"status": "failed", "execution_state": "timed_out", "semantic_verdict": "not-applicable"},
             "child_set_timeout"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    ("invalid_result", reason),
                    bridge._unrecordable({**result(), **changes}, assignment()),
                )

    def test_unrepresentable_usage_is_a_typed_rejection(self) -> None:
        self.assertEqual(
            ("invalid_result", "invalid_usage"),
            bridge._unrecordable({**result(), "usage": {"cost": 10 ** 400}}, assignment()),
        )

    def test_host_metadata_capacity_reserves_assignment_and_result_digests(self) -> None:
        for count, expected in ((62, (None, None)), (63, ("result_too_large", None))):
            assigned = assignment()
            assigned["required_evidence"] = [
                {"id": f"criterion-{index}", "description": "fixture"} for index in range(count)
            ]
            candidate = {**result(), "evidence": [
                {**result()["evidence"][0], "criterion_id": criterion["id"]}
                for criterion in assigned["required_evidence"]
            ]}
            with self.subTest(count=count):
                self.assertEqual(expected, bridge._unrecordable(candidate, assigned))

    def test_result_and_stream_parser_failures_stay_bounded(self) -> None:
        self.assertIsNone(bridge._result_object("\ud800"))
        with mock.patch.object(bridge.json, "loads", side_effect=RecursionError("CANARY-PARSER")):
            self.assertIsNone(bridge._result_object('{"nested":[]}'))
            callback = mock.Mock()
            with self.assertRaises(bridge._Abort) as captured:
                bridge._deliver(b'{"nested":[]}', callback)
            self.assertEqual("unparseable_stream", str(captured.exception))
            callback.assert_not_called()

    def test_result_fence_syntax_is_preserved(self) -> None:
        value = {"marker": "fixture"}
        raw = json.dumps(value)
        for wrapped in (raw, "```" + raw + "```", "```json" + raw + "```",
                        " \n```json\n " + raw + " \n```\n "):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(value, bridge._result_object(wrapped))
        for malformed in ("```json" + " " * 4000 + "x", "``` json " + raw + "```", "```JSON" + raw + "```"):
            with self.subTest(malformed=malformed[:20]):
                self.assertIsNone(bridge._result_object(malformed))

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
        self.assertEqual(canonical_json(modern), canonical_json(validate_learning_record(deepcopy(modern))))
        self.assertNotIn("client_diagnostics", validate_learning_record(modern))
        current = {**modern, "schema_version": 3, "client_diagnostics": None}
        self.assertEqual(current, validate_learning_record(current))
        for bad_version in (3.0, 4, [3], {"version": 3}):
            with self.subTest(bad_version=bad_version), self.assertRaises(ContractError):
                validate_learning_record({**current, "schema_version": bad_version})
        for changes in (
            {"result_validation_reason_code": "CANARY-REJECTED"},
            {"result_validation_reason_code": []},
            {"result_validation_reason_code": True},
            {"terminal_state": "timed_out"},
            {"fenced": False, "terminal_state": None},
            {"schema_version": 3},
            {"schema_version": 2.0},
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
        # Preserve the exact legacy reader semantics and signed/canonical bytes;
        # only the newly introduced v2 record requires an integer representation.
        for version in (True, 1.0):
            old = {**legacy, "schema_version": version}
            self.assertEqual(canonical_json(old), canonical_json(validate_learning_record(deepcopy(old))))

    def test_client_observation_codes_are_closed_and_do_not_echo_unknown_values(self) -> None:
        for allowed, fallback in ((CLIENT_ERROR_CODES, "unrecognized_error"),
                                  (CLIENT_RESULT_SUBTYPES, "unrecognized_result")):
            for code in allowed:
                self.assertEqual(code, bridge._client_observation_code(code, allowed, fallback))
            for malformed in ("CANARY-CODE", None, True, 1, [], {"CANARY-FIELD": "CANARY-VALUE"}):
                self.assertEqual(fallback, bridge._client_observation_code(malformed, allowed, fallback))

    def test_process_exit_observations_are_bounded_strict_integers(self) -> None:
        state = {"result_frame_seen": False, "result_subtype": None, "client_error_code": None}
        for code in (CLIENT_EXIT_CODE_MIN, -9, 0, 1, CLIENT_EXIT_CODE_MAX):
            observed = bridge._client_diagnostics({"returncode": code}, state)
            self.assertEqual(code, observed["exit_code"])
            validate_client_diagnostics(observed)
        for code in (None, False, True, 1.0, "CANARY-EXIT", [], {},
                     CLIENT_EXIT_CODE_MIN - 1, CLIENT_EXIT_CODE_MAX + 1):
            observed = bridge._client_diagnostics({"returncode": code}, state)
            self.assertIsNone(observed["exit_code"])
            validate_client_diagnostics(observed)

    def test_durable_client_observations_reject_arbitrary_content_and_inconsistent_shapes(self) -> None:
        valid = {"exit_code": 1, "result_frame_seen": True,
                 "result_subtype": "error_during_execution", "error_code": "server_error"}
        self.assertEqual(valid, validate_client_diagnostics(valid))
        for changes in (
            {"exit_code": True}, {"exit_code": 1.0}, {"exit_code": CLIENT_EXIT_CODE_MAX + 1},
            {"result_frame_seen": 1}, {"result_frame_seen": False}, {"result_subtype": None},
            {"result_subtype": "CANARY-SUBTYPE"}, {"result_subtype": []},
            {"error_code": "CANARY-ERROR"}, {"error_code": {"CANARY-FIELD": "value"}},
            {"CANARY-FIELD": "CANARY-VALUE"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ContractError):
                validate_client_diagnostics({**valid, **changes})
        for code in CLIENT_ERROR_CODES:
            validate_client_diagnostics({**valid, "error_code": code})
        for subtype in CLIENT_RESULT_SUBTYPES:
            validate_client_diagnostics({**valid, "result_subtype": subtype})
        legacy = {"schema_version": 3, "record_type": "dispatch_result", "record_id": "dispatch_result:client",
                  "run_id": "run-client", "bridge_run_id": "brg_" + "a" * 32, "result_digest": "b" * 64,
                  "fenced": False, "reviewed": True, "terminal_state": None, "observed_at_ms": 5,
                  "result_validation_reason_code": None, "client_diagnostics": valid}
        # An observed client error is not an acceptance decision or validator reason.
        self.assertEqual(legacy, validate_learning_record(legacy))
        for version in (1, 2):
            old = {**legacy, "schema_version": version}
            if version == 1:
                old.pop("result_validation_reason_code")
            with self.assertRaises(ContractError):
                validate_learning_record(old)


class ResultPromptContractTests(unittest.TestCase):
    @staticmethod
    def prompt_objects(assigned, **options):
        prompt = bridge.child_prompt(assigned, **options)
        decoder = json.JSONDecoder()
        guidance, _ = decoder.raw_decode(prompt.split("Contract guidance (not result fields):\n", 1)[1])
        template, _ = decoder.raw_decode(prompt.split("filling in this template:\n", 1)[1])
        return guidance, template

    def test_prompt_includes_the_preflight_vocabularies_and_rules(self) -> None:
        assigned = assignment()
        preflight = result_preflight(assigned)
        guidance, template = self.prompt_objects(assigned)
        for name in ("execution_states", "semantic_verdicts", "admission_states",
                     "evidence_types", "evidence_outcomes", "consistency_rules"):
            with self.subTest(name=name):
                self.assertEqual(preflight[name], guidance[name])
                self.assertNotIn(name, template)
        self.assertEqual(preflight["template"], template)
        self.assertEqual(set(preflight["semantic_verdicts"]), set(guidance["completed_status_by_verdict"]))
        self.assertEqual("completed", guidance["completed_status_by_verdict"]["not-ready"])
        self.assertEqual("blocked", guidance["completed_status_by_verdict"]["blocked"])

    def test_CR2_the_prompt_says_a_finished_review_is_completed_whatever_its_verdict(self) -> None:
        prompt = " ".join(bridge.child_prompt(assignment()).split())
        self.assertIn("A review you finished is status completed and execution_state completed, whatever its "
                      "verdict, a negative one included; failed means you could not do the work.", prompt)
        self.assertIn("Set execution_state, semantic_verdict and admission_state; never leave them null.", prompt)

    def test_prompt_examples_validate_without_weakening_negative_fences(self) -> None:
        assigned = assignment()
        guidance, template = self.prompt_objects(assigned)
        for verdict in ("pass", "not-ready", "blocked"):
            value = deepcopy(template)
            value.update(semantic_verdict=verdict, status=guidance["completed_status_by_verdict"][verdict],
                         summary="Synthetic prompt-contract example only.")
            for item in value["evidence"]:
                item.update(reference="synthetic-prompt-contract", details="Synthetic structural validation only.",
                            outcome="observed" if verdict == "pass" else "fail")
            with self.subTest(verdict=verdict):
                validate_result(value, assignment=assigned)
                self.assertEqual((None, None), bridge._unrecordable(value, assigned))
                # Copying guidance into the result does not extend its schema.
                self.assertEqual(("invalid_result", "unknown_fields"), bridge._unrecordable(
                    {**value, "semantic_verdicts": guidance["semantic_verdicts"]}, assigned,
                ))
                for field in guidance["string_array_fields"]:
                    self.assertIn(field, template)
                    self.assertEqual(("invalid_result", "invalid_shape"), bridge._unrecordable(
                        {**value, field: [{"severity": "synthetic-finding"}]}, assigned,
                    ))
                if verdict != "pass":
                    wrong = "blocked" if value["status"] == "completed" else "completed"
                    self.assertEqual(("invalid_result", "axes_status_verdict_mismatch"), bridge._unrecordable(
                        {**value, "status": wrong}, assigned,
                    ))

    def test_verifier_template_keeps_its_own_assignment_and_retry(self) -> None:
        assigned = {**assignment(), "assignment_id": "verifier-2", "attempt": 2,
                    "retry_of_assignment_id": "verifier-1"}
        guidance, template = self.prompt_objects(
            assigned, worker_result_path=".orchestration-input/worker-result.json",
        )
        self.assertEqual("verifier-2", template["assignment_id"])
        self.assertEqual({"attempt": 2, "retry_of_assignment_id": "verifier-1"}, template["retry"])
        self.assertEqual("unverified", template["admission_state"])
        self.assertNotIn("assignment_id", guidance)



class VerifierInvalidResultTests(unittest.TestCase):
    """0.4.0 item 0.5 (VF-INV-b, -c, -d): split reasons, a narrowed child schema, host-set fields."""

    VERIFIER = "codex_agents_verification_worker"

    def verifier_assignment(self) -> dict:
        return {**assignment(), "role": self.VERIFIER}

    def axes(self, **changes) -> dict:
        return {**result(), "execution_state": "completed", "semantic_verdict": "pass",
                "admission_state": "unverified", **changes}

    def test_each_inconsistent_axes_branch_has_its_own_fixed_reason(self) -> None:
        retry_first = {"attempt": 1, "retry_of_assignment_id": "earlier"}
        retry_second = {"attempt": 2, "retry_of_assignment_id": None}
        later = {**assignment(), "attempt": 2}
        cases = [
            ("axes_status_verdict_mismatch",
             self.axes(semantic_verdict="not-ready", status="blocked"), assignment()),
            ("axes_verdict_without_completion",
             self.axes(execution_state="failed", status="failed", semantic_verdict="pass"), assignment()),
            ("axes_admission_without_completion",
             self.axes(execution_state="failed", status="failed", semantic_verdict="not-applicable",
                       admission_state="accepted"), assignment()),
            ("axes_retry_lineage", self.axes(retry=retry_first), assignment()),
            ("axes_retry_lineage", self.axes(retry=retry_second), later),
            ("child_set_admission", self.axes(admission_state="accepted"), assignment()),
            ("child_set_timeout",
             self.axes(status="failed", execution_state="timed_out", semantic_verdict="not-applicable"),
             assignment()),
        ]
        for reason, value, assigned in cases:
            with self.subTest(reason=reason):
                self.assertEqual(("invalid_result", reason), bridge._unrecordable(value, assigned))
        self.assertNotIn("inconsistent_axes", RESULT_VALIDATION_REASON_CODES)

    def test_split_reasons_are_in_the_observation_schema_and_old_records_replay(self) -> None:
        from codex_agents.learning_contracts import content_digest
        from tests.support import ORCHESTRATION_ROOT

        listed = json.loads((ORCHESTRATION_ROOT / "schemas" / "learning-host-observation.schema.json").read_text())
        codes = set()

        def walk(node):
            if isinstance(node, dict):
                if node.get("enum") and "inconsistent_axes" in node["enum"]:
                    codes.update(node["enum"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(listed)
        for code in ("axes_status_verdict_mismatch", "axes_verdict_without_completion",
                     "axes_admission_without_completion", "axes_retry_lineage",
                     "child_set_admission", "child_set_timeout", "inconsistent_axes"):
            self.assertIn(code, codes)
        for code in ("inconsistent_axes", "axes_retry_lineage", "child_set_timeout"):
            material = {
                "schema_version": 3, "record_type": "dispatch_result", "run_id": "run_1",
                "bridge_run_id": "brg_" + "0" * 32, "result_digest": "1" * 64, "fenced": True,
                "reviewed": True, "terminal_state": "failed", "result_validation_reason_code": code,
                "client_diagnostics": None, "observed_at_ms": 5,
            }
            with self.subTest(code=code):
                record = {**material, "record_id": f"dispatch_result:{content_digest(material)}"}
                self.assertEqual(code, validate_learning_record(record)["result_validation_reason_code"])

    def narrowed(self):
        from tests.support import ORCHESTRATION_ROOT
        original = json.loads((ORCHESTRATION_ROOT / "schemas" / "result.schema.json").read_text())
        return original, bridge.narrow_verifier_schema(deepcopy(original))

    def test_the_verifier_schema_only_offers_what_the_host_would_accept(self) -> None:
        original, narrowed = self.narrowed()
        properties = narrowed["properties"]
        self.assertEqual(["unverified"], properties["admission_state"]["enum"])
        self.assertEqual([state for state in original["properties"]["execution_state"]["enum"]
                          if state != "timed_out"], properties["execution_state"]["enum"])
        for name in ("status", "semantic_verdict", "evidence", "retry"):
            self.assertEqual(original["properties"][name], properties[name])
        self.assertEqual(original["required"], narrowed["required"])
        strict = bridge.strict_output_schema(narrowed)
        self.assertEqual(["unverified"], strict["properties"]["admission_state"]["anyOf"][0]["enum"])
        self.assertNotIn("timed_out", strict["properties"]["execution_state"]["anyOf"][0]["enum"])
        # No root-level anyOf: strict clients do not accept one.
        self.assertEqual(set(original), set(narrowed))

    def test_the_narrowed_schema_agrees_with_the_host_on_every_axis_combination(self) -> None:
        from codex_agents.contracts import (
            ADMISSION_STATES, EXECUTION_STATES, SEMANTIC_VERDICTS, legacy_result_status,
        )
        original, narrowed = self.narrowed()
        checked = 0
        for execution in sorted(EXECUTION_STATES):
            for verdict in sorted(SEMANTIC_VERDICTS):
                for admission in sorted(ADMISSION_STATES):
                    candidate = self.axes(execution_state=execution, semantic_verdict=verdict,
                                          admission_state=admission,
                                          status=legacy_result_status(execution, verdict))
                    try:
                        validate_result(candidate, assignment=assignment())
                    except ContractError:
                        continue  # a cross-field rule the schema cannot state; the host rejects it
                    checked += 1
                    offered = (execution in narrowed["properties"]["execution_state"]["enum"]
                               and admission in narrowed["properties"]["admission_state"]["enum"])
                    host = bridge._unrecordable(candidate, assignment())[0] is None
                    with self.subTest(execution=execution, verdict=verdict, admission=admission):
                        self.assertEqual(host, offered)
        self.assertGreater(checked, 20)

    def test_only_a_verification_assignment_gets_the_narrowed_schema(self) -> None:
        from tests.support import ORCHESTRATION_ROOT
        raw = (ORCHESTRATION_ROOT / "schemas" / "result.schema.json").read_bytes()
        worker = json.loads(bridge.result_schema_for(raw, assignment()))
        verifier = json.loads(bridge.result_schema_for(raw, self.verifier_assignment()))
        self.assertEqual(json.loads(raw), worker)
        self.assertEqual(["unverified"], verifier["properties"]["admission_state"]["enum"])
        security = {**assignment(), "role": "codex_agents_security_reviewer"}
        self.assertEqual(json.loads(raw), json.loads(bridge.result_schema_for(raw, security)))

    def prepared(self, value: dict, assigned: dict | None = None):
        report = {"outcome": "result", "rerouted": False}
        return bridge.prepare_dispatch_result(assigned or self.verifier_assignment(), value, report)

    def test_a_verifier_status_that_disagrees_with_its_verdict_is_repaired_and_recorded(self) -> None:
        for wrong, verdict, right in (("blocked", "not-ready", "completed"), ("failed", "pass", "completed"),
                                      ("completed", "blocked", "blocked")):
            with self.subTest(status=wrong, verdict=verdict):
                finished, report, fenced, _, terminal, reason = self.prepared(
                    self.axes(status=wrong, semantic_verdict=verdict))
                self.assertEqual((False, None, None, "result"), (fenced, terminal, reason, report["outcome"]))
                self.assertEqual(right, finished["status"])
                self.assertEqual(verdict, finished["semantic_verdict"])
                self.assertIn({"field": "status", "was": wrong, "now": right}, report["host_set_fields"])

    def test_a_verifier_admission_is_set_by_the_host_and_recorded(self) -> None:
        for claimed in ("accepted", "rejected", "pending"):
            with self.subTest(admission=claimed):
                finished, report, fenced, *_ = self.prepared(self.axes(admission_state=claimed))
                self.assertFalse(fenced)
                self.assertEqual("unverified", finished["admission_state"])
                self.assertEqual([{"field": "admission_state", "was": claimed, "now": "unverified"}],
                                 report["host_set_fields"])

    def test_a_clean_verifier_result_records_no_host_set_fields(self) -> None:
        finished, report, fenced, *_ = self.prepared(self.axes())
        self.assertFalse(fenced)
        self.assertNotIn("host_set_fields", report)

    def test_the_host_never_fills_in_a_verdict_or_a_child_timeout(self) -> None:
        value = self.axes()
        value["semantic_verdict"] = None
        _, _, fenced, _, terminal, reason = self.prepared(value)
        self.assertEqual(("failed", "missing_required_fields", True), (terminal, reason, fenced))
        timed_out = self.axes(status="failed", execution_state="timed_out", semantic_verdict="not-applicable")
        self.assertEqual("child_set_timeout", self.prepared(timed_out)[5])
        failed_with_verdict = self.axes(execution_state="failed", semantic_verdict="pass", status="completed")
        _, _, fenced, _, _, reason = self.prepared(failed_with_verdict)
        self.assertEqual(("axes_verdict_without_completion", True), (reason, fenced))

    def test_host_set_fields_hold_only_fixed_values(self) -> None:
        value = self.axes(admission_state={"CANARY-AXIS": "CANARY-VALUE"}, status="CANARY-REJECTED")
        _, report, _, _, _, reason = self.prepared(value)
        self.assertEqual("invalid_status", reason)
        self.assertNotIn("host_set_fields", report)
        self.assertNotIn("CANARY", json.dumps(report))

    def test_worker_and_other_review_results_get_no_host_set_fields(self) -> None:
        for role in ("codex_agents_researcher", "codex_agents_implementation_worker",
                     "codex_agents_security_reviewer"):
            assigned = {**assignment(), "role": role}
            for changes, reason in (({"admission_state": "accepted"}, "child_set_admission"),
                                    ({"status": "blocked", "semantic_verdict": "not-ready"},
                                     "axes_status_verdict_mismatch")):
                with self.subTest(role=role, reason=reason):
                    _, report, fenced, _, terminal, code = self.prepared(self.axes(**changes), assigned)
                    self.assertEqual((True, "failed", reason), (fenced, terminal, code))
                    self.assertNotIn("host_set_fields", report)


if __name__ == "__main__":
    unittest.main()
