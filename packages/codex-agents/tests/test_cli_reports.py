from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "native-package/.codex/orchestration/codex_agents/cli_reports.py"
SPEC = importlib.util.spec_from_file_location("standalone_cli_reports", SOURCE)
reports = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reports)


def cycle(groups=1):
    return {
        "analysis": {
            "schema_version": 1, "as_of_ms": 101, "owner_feedback_excluded": 2,
            "slices": {"CANARY-slice": {"configurations": {
                str(index): {"diagnoses": ["small_sample", "telemetry_incomplete"]}
                for index in range(groups)
            }}},
        },
        "mined_case_ids": ["case:CANARY"], "proposed_candidate_digests": [],
        "proposal_status": "complete",
    }


class CliReportMemoryTests(unittest.TestCase):
    def test_only_incidental_cycle_changes_and_input_is_not_mutated(self):
        original = {
            "ready": True, "learning_cycle": cycle(), "result": {"body": "CANARY-result"},
            "host_outcome_digest": "abc", "check_receipt": "receipt", "outcome_commit": {"duplicate": True},
            "outcome": {"accepted": False, "status": "rejected", "execution_state": "completed",
                        "semantic_verdict": "not-ready", "result_validation_reason_code": "missing_evidence"},
            "event": {"record_id": "event"}, "qa": {"accepted": False, "verifier_disagreement": True},
        }
        before = copy.deepcopy(original)
        projected = reports.command_report("learning-record-host-result", original)
        self.assertEqual(before, original)
        self.assertEqual(original.keys(), projected.keys())
        for key in original.keys() - {"learning_cycle"}:
            self.assertIs(original[key], projected[key])
        self.assertNotIn("analysis", projected["learning_cycle"])
        self.assertEqual("compact-learning-cycle-v1", projected["learning_cycle"]["format"])

    def test_full_mode_is_the_exact_legacy_object_for_every_supported_command(self):
        original = {"ready": True, "learning_cycle": cycle()}
        for command in reports.COMPACT_CYCLE_COMMANDS:
            self.assertIs(original, reports.command_report(command, original, full=True))
            self.assertEqual(
                json.dumps(original, sort_keys=True, indent=2),
                json.dumps(reports.command_report(command, original, full=True), sort_keys=True, indent=2),
            )
            self.assertEqual("available", reports.command_report(command, original)["learning_cycle"]["summary_state"])

    def test_dispatch_result_and_explicit_analytics_are_never_projected(self):
        original = {"ready": False, "result": {"evidence": ["CANARY"]}, "learning_cycle": cycle()}
        for command in ("dispatch", "validate-result", "learning-run-cycle", "learning-analyze", "other"):
            self.assertIs(original, reports.command_report(command, original))
        self.assertIs(original["result"], reports.command_report("dispatch", original)["result"])
        without_cycle = {"ready": False, "error": "fixed-code"}
        self.assertIs(without_cycle, reports.command_report("learning-record-host-result", without_cycle))

    def test_counts_are_explicit_and_no_history_names_or_identifiers_are_printed(self):
        value = cycle(3)
        value["analysis"]["slices"]["CANARY-slice"]["configurations"]["0"]["diagnoses"].append("CANARY-new-diagnosis")
        compact = reports.learning_cycle_summary(value)
        self.assertEqual(1, compact["mined_case_count"])
        self.assertEqual(0, compact["proposed_candidate_count"])
        summary = compact["analysis_summary"]
        self.assertEqual((101, 2, 1, 3), tuple(summary[key] for key in (
            "as_of_ms", "owner_feedback_excluded", "slice_count", "configuration_count",
        )))
        self.assertEqual(3, summary["diagnosis_counts"]["small_sample"])
        self.assertEqual(3, summary["diagnosis_counts"]["telemetry_incomplete"])
        self.assertEqual(1, summary["other_diagnosis_count"])
        self.assertNotIn("CANARY", json.dumps(compact))
        self.assertEqual("--full-report", compact["full_report_option"])
        self.assertEqual(["analysis.slices", "mined_case_ids", "proposed_candidate_digests"], compact["omitted_fields"])

    def test_large_history_and_bulk_lists_have_a_fixed_size_summary(self):
        value = cycle(reports.MAX_ANALYSIS_GROUPS)
        value["mined_case_ids"] = ["case:" + "a" * 64] * reports.MAX_CYCLE_RECORDS
        value["proposed_candidate_digests"] = ["b" * 64] * reports.MAX_CYCLE_RECORDS
        compact = reports.learning_cycle_summary(value)
        self.assertEqual("available", compact["summary_state"])
        self.assertEqual(reports.MAX_ANALYSIS_GROUPS, compact["analysis_summary"]["configuration_count"])
        self.assertEqual(reports.MAX_CYCLE_RECORDS, compact["mined_case_count"])
        self.assertLess(len((json.dumps(compact, sort_keys=True, indent=2) + "\n").encode()), 2048)
        self.assertGreater(len(json.dumps(value)), 1_000_000)

    def test_unavailable_metadata_does_not_invent_success_or_zero_counts(self):
        variants = [None, [], {}, cycle(), cycle(), cycle(), cycle(), cycle(), cycle()]
        variants[3]["analysis"]["schema_version"] = True
        variants[4]["analysis"]["as_of_ms"] = False
        variants[5]["analysis"]["owner_feedback_excluded"] = -1
        variants[6]["analysis"]["slices"] = []
        variants[7]["proposal_status"] = "CANARY"
        variants[8]["unexpected"] = "CANARY"
        for value in variants:
            original = {"ready": True, "outcome": {"accepted": False}, "learning_cycle": value}
            projected = reports.command_report("learning-record-operational-qa", original)
            self.assertIs(projected["ready"], True)
            self.assertIs(projected["outcome"]["accepted"], False)
            summary = projected["learning_cycle"]
            self.assertEqual("unavailable", summary["summary_state"])
            self.assertEqual("learning_summary_unavailable", summary["reason_code"])
            for key in ("analysis_summary", "mined_case_count", "proposed_candidate_count", "proposal_status"):
                self.assertIsNone(summary[key])
            self.assertNotIn("CANARY", json.dumps(summary))
            self.assertIs(original, reports.command_report("learning-record-operational-qa", original, full=True))

    def test_genuine_empty_analysis_counts_as_zero(self):
        value = cycle(0)
        value["analysis"]["slices"] = {}
        value["mined_case_ids"] = []
        compact = reports.learning_cycle_summary(value)
        self.assertEqual("available", compact["summary_state"])
        self.assertEqual(0, compact["analysis_summary"]["slice_count"])
        self.assertEqual(0, compact["analysis_summary"]["configuration_count"])
        self.assertEqual(0, compact["mined_case_count"])

    def test_limits_refuse_summary_without_touching_authoritative_payload(self):
        value = cycle(2)
        original = {"learning_cycle": value, "result": {"body": "CANARY"}}
        with mock.patch.object(reports, "MAX_ANALYSIS_GROUPS", 1):
            summary = reports.command_report("learning-record-host-result", original)
        self.assertEqual("unavailable", summary["learning_cycle"]["summary_state"])
        self.assertIs(original["result"], summary["result"])
        for key in ("mined_case_ids", "proposed_candidate_digests"):
            oversized = cycle()
            oversized[key] = ["x"] * (reports.MAX_CYCLE_RECORDS + 1)
            self.assertEqual("unavailable", reports.learning_cycle_summary(oversized)["summary_state"])

    def test_unknown_group_diagnosis_or_identifier_shapes_remain_unavailable(self):
        variants = [cycle() for _ in range(6)]
        variants[0]["analysis"]["slices"]["CANARY-slice"] = []
        variants[1]["analysis"]["slices"]["CANARY-slice"]["configurations"]["0"] = None
        variants[2]["analysis"]["slices"]["CANARY-slice"]["configurations"]["0"]["diagnoses"] = [True]
        variants[3]["analysis"]["slices"]["CANARY-slice"]["configurations"]["0"]["diagnoses"] = ["x" * 81]
        variants[4]["mined_case_ids"] = [True]
        variants[5]["proposed_candidate_digests"] = ["x" * 161]
        for value in variants:
            summary = reports.learning_cycle_summary(value)
            self.assertEqual("unavailable", summary["summary_state"])
            self.assertIsNone(summary["analysis_summary"])


class CliReportIntegrationTests(unittest.TestCase):
    """Real parser/main presentation; persistence/auth are mocked, not proven."""

    def invoke(self, command, full=False):
        import tests.support  # noqa: F401 -- source CLI import path, no real state
        import cli

        ledger = mock.MagicMock()
        committed = {"host_outcome_digest": "exact-digest", "outcome": {"accepted": False},
                     "qa": {"accepted": False, "verifier_disagreement": True},
                     "outcome_commit": {"stored": True, "duplicate": False}}
        ledger.commit_operational_qa.return_value = committed
        ledger.run_learning_cycle.return_value = cycle()
        ledger.append.return_value = {"stored": True, "duplicate": False, "source_sequence": 1}
        arguments = ["cli.py", command, "--state-root", "unused-state", "--project-root", "unused-project"]
        if command == "learning-record-operational-qa":
            for field in ("host-outcome-digest", "verifier-host-outcome-digest", "evaluator-assignment",
                          "evaluator-result", "check-receipt", "verifier-assignment", "verifier-result"):
                arguments += ["--" + field, "fixture"]
        else:
            arguments += ["--case-id", "fixture", "--review-digest", "fixture",
                          "--similarity-group-digest", "fixture", "--destination", "canary", "--freeze"]
        if full:
            arguments.append("--full-report")
        with mock.patch.object(sys, "argv", arguments), \
                mock.patch.object(cli, "_require_protected_runtime"), \
                mock.patch.object(cli, "_require_own_roots"), \
                mock.patch.object(cli, "_learning_ledger", return_value=(ledger, "project")), \
                mock.patch.object(cli, "_json_input", return_value={}), \
                mock.patch.object(cli, "review_evaluation_case", return_value={"record_id": "case:exact"}), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = cli.main()
        self.assertEqual(0, code)
        ledger.run_learning_cycle.assert_called_once()
        if command == "learning-record-operational-qa":
            ledger.commit_operational_qa.assert_called_once()
        else:
            ledger.append.assert_called_once()
        return json.loads(stdout.getvalue()), committed

    def test_actual_qa_parser_and_main_preserve_negative_qa_in_both_formats(self):
        for full in (False, True):
            report, committed = self.invoke("learning-record-operational-qa", full)
            for key, value in committed.items():
                self.assertEqual(value, report[key])
            self.assertTrue(report["ready"])
            self.assertFalse(report["qa"]["accepted"])
            self.assertEqual(cycle() if full else reports.learning_cycle_summary(cycle()), report["learning_cycle"])

    def test_actual_review_parser_and_main_choose_full_or_compact(self):
        for full in (False, True):
            report, _ = self.invoke("learning-review-case", full)
            self.assertEqual({"record_id": "case:exact"}, report["record"])
            self.assertTrue(report["stored"])
            self.assertEqual(cycle() if full else reports.learning_cycle_summary(cycle()), report["learning_cycle"])


if __name__ == "__main__":
    unittest.main()
