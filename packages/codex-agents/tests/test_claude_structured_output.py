"""O10 VF: a Claude child's result is held to the result schema, as a Codex child's is (PLAN VF-1 to VF-5).

Integration stage: the bridge with a fake Claude client that replays frames, as in test_bridge. The
frames follow a real `claude -p --json-schema` run (2026-11-20, Claude Code 2.1.311): the output comes
as a `StructuredOutput` tool call, and the final result frame carries `structured_output`.
"""
import json
import unittest

from tests.test_bridge import RATE_LIMIT, RESULT_SCHEMA, BridgeTestCase, init_frame, result, result_frame

from codex_agents.bridge import strict_output_schema

STRUCTURED = ["StructuredOutput", "Read", "Glob", "Grep"]


def output_call(value):
    return {"type": "assistant", "message": {"model": "claude-opus-5-5", "content": [
        {"type": "tool_use", "id": "toolu_1", "name": "StructuredOutput", "input": value}]}}


class StructuredOutputTests(BridgeTestCase):
    def schema_argument(self):
        child = [i for i in self.invocations("claude") if "-p" in i["argv"]][-1]["argv"]
        return json.loads(child[child.index("--json-schema") + 1])

    def test_VF_1_a_claude_child_gets_the_same_strict_schema_as_a_codex_child(self):
        route, assigned, handle = self.claude_run()
        self.frames("claude", [init_frame(tools=STRUCTURED), RATE_LIMIT, output_call(result()), result_frame()])
        output = self.dispatch(route, assigned, handle)
        self.assertEqual(strict_output_schema(json.loads(RESULT_SCHEMA)), self.schema_argument())
        self.assertTrue(output["bridge_report"]["reviewed"], output["bridge_report"])

    def test_VF_5_the_result_is_the_validated_structured_output(self):
        route, assigned, handle = self.claude_run()
        valid = result()
        final = {**result_frame("not the result"), "structured_output": valid}
        self.frames("claude", [init_frame(tools=STRUCTURED), RATE_LIMIT, output_call(valid), final])
        output = self.dispatch(route, assigned, handle)
        self.assertTrue(output["bridge_report"]["reviewed"], output["bridge_report"])
        self.assertEqual(valid["summary"], output["result"]["summary"])

    def test_VF_2_a_run_without_validated_output_has_its_own_reason(self):
        for final in ({**result_frame(), "structured_output": None},
                      {**result_frame(), "subtype": "error_max_structured_output_retries", "is_error": True}):
            with self.subTest(subtype=final["subtype"]):
                final.pop("structured_output", None) if final["subtype"] != "success" else None
                route, assigned, handle = self.claude_run()
                self.frames("claude", [init_frame(tools=STRUCTURED), RATE_LIMIT, final])
                report = self.dispatch(route, assigned, handle)["bridge_report"]
                self.assertEqual("invalid_result", report["outcome"])
                self.assertEqual("structured_output_missing", report["result_validation_reason_code"])
                self.assertFalse(report["reviewed"])

    def test_VF_5_only_the_output_tool_is_added(self):
        route, assigned, handle = self.claude_run()
        self.frames("claude", [init_frame(tools=[*STRUCTURED, "Bash"]), {"sleep": 30}, result_frame()])
        self.assertEqual("init_tool_not_allowed", self.dispatch(route, assigned, handle)["bridge_report"]["outcome"])

    def test_VF_3_the_rules_name_the_exact_evidence_fields(self):
        from codex_agents.contracts import EVIDENCE_FIELDS_RULE, result_preflight_rules
        self.assertIn(EVIDENCE_FIELDS_RULE, result_preflight_rules())
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
