from __future__ import annotations

import json
import subprocess
import sys
import unittest

from tests.support import ORCHESTRATION_ROOT
from tests.learning_support import verification_outcome


class LearningAnalysisTests(unittest.TestCase):
    def test_engine_and_core_reexport_the_same_analyzer_and_minimum(self):
        from codex_agents import learning_analysis, learning_cycle_core, learning_engine

        self.assertIs(learning_engine.analyze_outcomes, learning_analysis.analyze_outcomes)
        self.assertIs(learning_cycle_core.analyze_outcomes, learning_analysis.analyze_outcomes)
        self.assertEqual(20, learning_analysis.COMPARISON_MIN_CASES)
        self.assertEqual(
            learning_analysis.COMPARISON_MIN_CASES, learning_engine.COMPARISON_MIN_CASES,
        )

    def test_fresh_nonempty_core_calculation_blocks_owner_imports_and_io(self):
        outcome = verification_outcome()
        outcome.update(
            evaluation_candidate_digest="d" * 64, evaluation_case_digest="e" * 64,
        )
        script = r'''
import importlib.abc
import json
import sys

sys.path.insert(0, sys.argv[1])
record = json.loads(sys.argv[2])
allowed = {
    "codex_agents", "codex_agents.contracts", "codex_agents.learning_contracts",
    "codex_agents.learning_analysis", "codex_agents.learning_cycle_core",
}
def forbidden(name):
    return (
        (name.startswith("codex_agents.") and name not in allowed)
        or name.split(".")[0] in {"sqlite3", "_sqlite3", "openai", "anthropic"}
    )
class PureImportsOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if forbidden(fullname):
            raise AssertionError("owner/provider import: " + fullname)
sys.meta_path.insert(0, PureImportsOnly())
from codex_agents import learning_cycle_core as core
from codex_agents.contracts import ContractError

assert not any(forbidden(name) for name in sys.modules)
def deny_io(event, args):
    if event == "open" or event.startswith((
        "os.", "subprocess.", "socket.", "ctypes.", "mmap.",
    )):
        raise AssertionError("calculation attempted I/O: " + event)
sys.addaudithook(deny_io)

section = {
    "observations": {
        "trials": 1, "accepted": 1, "terminal_failures": 0, "repairs": 0,
        "verifier_disagreements": 0, "mean_quality_micros": 1_000_000,
        "workflow_token_volume": 150, "workflow_model_calls": 2,
        "workflow_latency_ms": 100, "workflow_tool_calls": 1,
        "delegation_overhead_tokens": 0, "no_progress_events": 0,
    },
    "evidence_flags": {
        "usage_complete": True, "stale": False, "latency_complete": True,
        "tool_usage_complete": True, "insufficient_sample": True,
        "version_confounding": False,
    },
    "diagnoses": ["small_sample"],
    "alternative_explanations": ["task_mix_variance"],
}
expected = {
    "analysis": {
        "schema_version": 1, "as_of_ms": 10, "owner_feedback_excluded": 0,
        "slices": {"verification-learning": {
            "configurations": {record["configuration_digest"]: section},
        }},
    },
    "proposal_status": "complete", "candidate_specs": [],
}
before = json.dumps(record, sort_keys=True)
actual = core.plan_learning_cycle([record], all_cases=[], as_of_ms=10, max_age_ms=100)
assert actual == expected, actual
assert json.dumps(record, sort_keys=True) == before
source = dict(record, evaluation_candidate_digest=None, evaluation_case_digest=None)
assert core.select_mining_outcomes([source], existing_cases=[]) == [source]

record["coverage"] = "partial"
record["usage"] = {name: None for name in record["usage"]}
record["usage_sources"] = {
    name: {"source": "unavailable", "coverage": "unknown"} for name in record["usage"]
}
for name in (
    "workflow_token_volume", "workflow_model_calls", "workflow_latency_ms",
    "workflow_tool_calls",
):
    section["observations"][name] = None
section["evidence_flags"].update(
    usage_complete=False, stale=True, latency_complete=False, tool_usage_complete=False,
)
section["diagnoses"] = ["telemetry_incomplete", "stale_evidence", "small_sample"]
section["alternative_explanations"] = [
    "client_coverage_or_collection_gap",
    "historical_behavior_may_not_match_current_package", "task_mix_variance",
]
expected["analysis"]["as_of_ms"] = 1000
assert core.plan_learning_cycle(
    [record], all_cases=[], as_of_ms=1000, max_age_ms=10,
) == expected
excluded = core.plan_learning_cycle(
    [record], all_cases=[], as_of_ms=1000, max_age_ms=10,
    owner_invalidated_run_ids=[record["run_id"]],
)
assert excluded == {
    "analysis": {
        "schema_version": 1, "as_of_ms": 1000,
        "owner_feedback_excluded": 1, "slices": {},
    },
    "proposal_status": "complete", "candidate_specs": [],
}
invalid = dict(record, configuration_digest="0" * 64)
try:
    core.plan_learning_cycle([invalid], all_cases=[], as_of_ms=10, max_age_ms=100)
except ContractError:
    pass
else:
    raise AssertionError("nonempty outcome was not validated")
assert not any(forbidden(name) for name in sys.modules)
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script,
             str(ORCHESTRATION_ROOT), json.dumps(outcome)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stderr)
