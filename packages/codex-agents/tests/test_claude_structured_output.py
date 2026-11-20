"""O10 VF: a Claude child's result is held to the result schema, as a Codex child's is (PLAN VF-1 to VF-6).

Integration stage: the bridge with a fake Claude client that replays frames, as in test_bridge. The
frames follow a real `claude -p --json-schema` run (2026-11-20, Claude Code 2.1.311): the output comes
as a `StructuredOutput` tool call, and the final result frame carries `structured_output`.
"""
import json
import unittest

from tests.test_bridge import (RATE_LIMIT, RESULT_SCHEMA, BridgeTestCase, init_frame, null_filled, result,
                               result_frame)

from codex_agents.bridge import strict_output_schema

STRUCTURED = ["StructuredOutput", "Read", "Glob", "Grep"]


def output_call(value, number=1):
    return {"type": "assistant", "message": {"id": f"msg_{number}", "model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": f"toolu_{number}", "name": "StructuredOutput", "input": value}]}}


def final(structured, **changes):
    frame = {**result_frame("text is never the result"), **changes}
    if structured is not ABSENT:
        frame["structured_output"] = structured
    return frame


ABSENT = object()


class StructuredOutputTests(BridgeTestCase):
    def schema_argument(self):
        child = [i for i in self.invocations("claude") if "-p" in i["argv"]][-1]["argv"]
        return json.loads(child[child.index("--json-schema") + 1])

    def run_frames(self, frames):
        route, assigned, handle = self.claude_run()
        self.frames("claude", frames)
        return self.dispatch(route, assigned, handle)

    def durable_reason(self):
        with self.ledger.repository(self.project_id) as repository:
            return repository.records("dispatch_result")[-1]["result_validation_reason_code"]

    def test_VF_1_a_claude_child_gets_the_same_strict_schema_as_a_codex_child(self):
        output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, output_call(result()),
                                  final(result())])
        self.assertEqual(strict_output_schema(json.loads(RESULT_SCHEMA)), self.schema_argument())
        self.assertEqual("result", output["bridge_report"]["outcome"], output["bridge_report"])

    def test_VF_5_the_result_is_the_final_structured_output_with_optional_nulls_removed(self):
        attempted = {**result(), "summary": "an attempt the client refused"}
        valid = result()
        output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, output_call(attempted, 1),
                                  output_call(null_filled(valid), 2), final(null_filled(valid))])
        self.assertEqual("result", output["bridge_report"]["outcome"], output["bridge_report"])
        self.assertEqual(valid, output["result"])  # neither the tool input nor the text, and no optional nulls

    def test_VF_5_the_full_validator_still_checks_the_structured_output(self):
        broken = {**result(), "evidence": [{**result()["evidence"][0], "severity": "high"}]}
        output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, final(broken)])
        self.assertEqual("invalid_result", output["bridge_report"]["outcome"])
        self.assertEqual("evidence_unknown_fields", output["bridge_report"]["result_validation_reason_code"])
        self.assertEqual("failed", output["result"]["execution_state"])

    def test_VF_2_a_run_without_validated_output_has_its_own_durable_reason(self):
        for label, frame in (("absent", final(ABSENT)), ("null", final(None)), ("not an object", final([1])),
                             ("retries exhausted", final(result(), subtype="error_max_structured_output_retries",
                                                         is_error=True))):
            with self.subTest(label):
                output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, frame])
                self.assertEqual("invalid_result", output["bridge_report"]["outcome"])
                self.assertEqual("structured_output_missing", output["bridge_report"]["result_validation_reason_code"])
                self.assertEqual("failed", output["result"]["execution_state"])
                self.assertEqual("structured_output_missing", self.durable_reason())

    def test_VF_2_a_stronger_stop_keeps_its_own_cause(self):
        output = self.run_frames([init_frame(tools=[*STRUCTURED, "Bash"]), {"sleep": 30}, final(ABSENT)])
        self.assertEqual("init_tool_not_allowed", output["bridge_report"]["outcome"])

    def test_VF_5_only_the_output_tool_is_added(self):
        call = {"type": "assistant", "message": {"id": "msg_9", "model": "claude-opus-5-5", "content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "true"}}]}}
        output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, call, {"sleep": 30}, final(result())])
        self.assertEqual("claude_tool_not_allowed", output["bridge_report"]["outcome"])

    def test_VF_6_a_client_too_old_to_hold_results_to_the_schema_is_refused_before_launch(self):
        from unittest import mock
        from codex_agents import bridge
        route, assigned, handle = self.claude_run()
        self.frames("claude", [init_frame(tools=STRUCTURED), RATE_LIMIT, final(result())])
        launched = len([i for i in self.invocations("claude") if "-p" in i["argv"]])
        with mock.patch.object(bridge, "CLAUDE_SCHEMA_MIN_VERSION", (999, 0, 0)):
            with self.assertRaisesRegex(bridge.BridgeRefused, "cannot hold its result to the schema"):
                self.dispatch(route, assigned, handle)
        self.assertEqual(launched, len([i for i in self.invocations("claude") if "-p" in i["argv"]]))

    def test_VF_6_a_child_whose_client_reports_an_older_version_stops_at_init(self):
        output = self.run_frames([init_frame(tools=STRUCTURED, claude_code_version="2.1.300"), {"sleep": 30},
                                  final(result())])
        self.assertEqual("init_client_unsupported", output["bridge_report"]["outcome"])
        self.assertEqual("failed", output["result"]["execution_state"])

    def test_VF_5_a_structured_result_is_still_held_to_the_size_limit(self):
        from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES
        huge = {**result(), "summary": "x" * MAX_RESULT_BODY_BYTES}
        output = self.run_frames([init_frame(tools=STRUCTURED), RATE_LIMIT, final(huge)])
        # The stream's own frame limit catches it first; either bound refuses it.
        self.assertIn(output["bridge_report"]["outcome"], {"stream_too_large", "result_too_large"})
        self.assertEqual("failed", output["result"]["execution_state"])

    def test_VF_3_the_child_is_told_to_give_its_result_as_the_structured_output(self):
        from codex_agents.bridge import child_prompt
        _, assigned, _ = self.claude_run()
        self.assertIn("structured output", child_prompt(assigned))  # verifier of 040-vf-design

    def test_VF_3_the_rules_name_the_exact_evidence_fields_for_the_primary_and_the_child(self):
        from codex_agents.bridge import child_prompt
        from codex_agents.contracts import EVIDENCE_FIELDS_RULE, result_preflight
        _, assigned, _ = self.claude_run()
        self.assertIn(EVIDENCE_FIELDS_RULE, result_preflight(assigned)["consistency_rules"])
        self.assertIn(EVIDENCE_FIELDS_RULE, child_prompt(assigned))
        for field in ("criterion_id", "type", "outcome", "reference", "details"):
            self.assertIn(field, EVIDENCE_FIELDS_RULE)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited bridge tests run in test_bridge."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(StructuredOutputTests):
        if name in StructuredOutputTests.__dict__:
            suite.addTest(StructuredOutputTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
