from __future__ import annotations

import inspect
import json
import subprocess
import sys
import unittest
from copy import deepcopy
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests import test_learning_store as legacy
from tests.learning_support import (
    DIGEST_A, champion_configuration, full_usage, generate_test_owner_key,
    heldout_cases, verification_outcome,
)

from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import generate_candidate, mine_evaluation_case, review_evaluation_case
from codex_agents.learning_store import DEFAULT_RETENTION_MS, LedgerError
from codex_agents import learning_cycle_core as core


class LearningCycleCoreTests(unittest.TestCase):
    def test_missing_owner_derived_mining_metadata_is_refused(self):
        with self.assertRaisesRegex(ContractError, "mining_metadata_missing"):
            core.plan_learning_cycle(
                [verification_outcome()], all_cases=[], as_of_ms=10,
                max_age_ms=100,
            )

    def test_owner_invalidation_excludes_analysis_and_mining(self):
        outcome = verification_outcome()
        invalidated = {outcome["run_id"]}
        self.assertEqual([], core.select_mining_outcomes(
            [outcome], existing_cases=[], owner_invalidated_run_ids=invalidated,
        ))
        plan = core.plan_learning_cycle(
            [outcome], all_cases=[], owner_invalidated_run_ids=invalidated,
            as_of_ms=10, max_age_ms=100,
        )
        self.assertEqual(1, plan["analysis"]["owner_feedback_excluded"])
        self.assertEqual({}, plan["analysis"]["slices"])
        self.assertEqual([], plan["candidate_specs"])

    def test_bounds_refuse_instead_of_truncating_or_returning_empty(self):
        with self.assertRaisesRegex(core.LearningCycleBoundsError, "outcome_limit"):
            core.select_mining_outcomes(
                [verification_outcome()] * 10_001, existing_cases=[],
            )
        with self.assertRaisesRegex(core.LearningCycleBoundsError, "case_limit"):
            core.plan_learning_cycle(
                [], all_cases=[{}] * 20_001, as_of_ms=10, max_age_ms=100,
            )
        with mock.patch.object(core, "MAX_SNAPSHOT_BYTES", 1), \
                self.assertRaisesRegex(core.LearningCycleBoundsError, "snapshot_bytes"):
            core.select_mining_outcomes([verification_outcome()], existing_cases=[])
        with mock.patch.object(core, "MAX_PLAN_BYTES", 1), \
                self.assertRaisesRegex(core.LearningCycleBoundsError, "plan_bytes"):
            core.plan_learning_cycle([], all_cases=[], as_of_ms=10, max_age_ms=100)

    def test_verified_outcome_supersedes_unverified_same_run_without_mutation(self):
        verified = verification_outcome("verified")
        unverified = deepcopy(verified)
        unverified.update(
            record_id="unverified", status="unverified", accepted=False,
            admission_state="unverified",
        )
        records = [unverified, verified]
        before = deepcopy(records)
        selected = core.select_mining_outcomes(records, existing_cases=[])
        self.assertEqual([verified], selected)
        self.assertEqual(before, records)

    def test_fresh_import_and_empty_plan_cannot_load_persistence_or_use_io(self):
        script = '''
import builtins, sys
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "sqlite3" or name.startswith("sqlite3.") or name.endswith("learning_store"):
        raise AssertionError("persistence import: " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from codex_agents.learning_cycle_core import plan_learning_cycle
def deny(event, args):
    if event in {"open", "os.listdir", "os.scandir", "subprocess.Popen", "socket.__new__"}:
        raise AssertionError("calculation attempted I/O: " + event)
sys.addaudithook(deny)
result = plan_learning_cycle([], all_cases=[], as_of_ms=10, max_age_ms=100)
assert result["analysis"]["slices"] == {}
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(ORCHESTRATION_ROOT)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for function in (core.select_mining_outcomes, core.plan_learning_cycle):
            self.assertNotIn("digest_key", inspect.signature(function).parameters)


class LearningCycleLegacyParityTests(unittest.TestCase):
    """Run existing fixture workflows against an untouched legacy oracle.

    Only the test owner holds the synthetic key and opens SQL. Its dry finalizer
    uses the real evidence/eligibility gates inside a rolled-back savepoint.
    """

    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        original = self.fixture.ledger.run_learning_cycle
        self.calls = 0

        def compare(project_id, *, as_of_ms, max_age_ms=DEFAULT_RETENTION_MS):
            predicted, records = self._predict(project_id, as_of_ms, max_age_ms)
            actual = original(project_id, as_of_ms=as_of_ms, max_age_ms=max_age_ms)
            self.assertEqual(canonical_json(actual), canonical_json(predicted))
            for record in records:
                row = self.fixture.ledger._connection.execute(
                    "SELECT content FROM records WHERE project_id=? AND record_id=?",
                    (project_id, record["record_id"]),
                ).fetchone()
                self.assertEqual(canonical_json(record), bytes(row[0]))
            self.calls += 1
            return actual

        self.fixture.ledger.run_learning_cycle = compare
        self.planned_spec_counts = []

    def _predict(self, project_id, as_of_ms, max_age_ms):
        ledger = self.fixture.ledger
        connection = ledger._connection

        def records(kind):
            return [json.loads(bytes(row[0])) for row in connection.execute(
                "SELECT content FROM records WHERE project_id=? AND record_type=? "
                "ORDER BY source_sequence", (project_id, kind),
            )]

        outcomes, cases = records("outcome"), records("evaluation_case")
        invalidated = ledger._owner_invalidated_run_ids_locked(project_id)
        existing = {item["candidate_digest"] for item in records("candidate")}
        new_cases = []
        for outcome in core.select_mining_outcomes(
            outcomes, existing_cases=cases, owner_invalidated_run_ids=invalidated,
        ):
            criteria = {key: outcome[key] for key in (
                "deterministic_pass", "security_pass", "required_criteria_total",
                "required_criteria_passed",
            )}
            new_cases.append(mine_evaluation_case(
                digest_key=self.fixture.state.digest_key(), outcome=outcome,
                fixture_digest=outcome["fixture_digest"], criteria_digest=content_digest(criteria),
                lineage_digest=keyed_digest(self.fixture.state.digest_key(), outcome["run_id"]),
            ))
        plan = core.plan_learning_cycle(
            outcomes, all_cases=cases + new_cases, owner_invalidated_run_ids=invalidated,
            as_of_ms=as_of_ms, max_age_ms=max_age_ms,
        )
        self.planned_spec_counts.append(len(plan["candidate_specs"]))
        proposed = []
        appended = list(new_cases)
        connection.execute("SAVEPOINT core_parity_prediction")
        try:
            for case in new_cases:
                ledger._validate_evidence_links_locked(project_id, case)
                ledger._append_checked_locked(
                    project_id, case, producer="evaluator", timestamp=as_of_ms,
                )
            for spec in plan["candidate_specs"]:
                record = generate_candidate(digest_key=self.fixture.state.digest_key(), **spec)
                if record["candidate_digest"] in existing:
                    continue
                try:
                    ledger._validate_evidence_links_locked(project_id, record)
                    ledger._append_checked_locked(
                        project_id, record, producer="evaluator", timestamp=as_of_ms,
                    )
                except LedgerError:
                    continue
                existing.add(record["candidate_digest"])
                proposed.append(record["candidate_digest"])
                appended.append(record)
        finally:
            connection.execute("ROLLBACK TO core_parity_prediction")
            connection.execute("RELEASE core_parity_prediction")
        return {
            "mined_case_ids": [item["record_id"] for item in new_cases],
            "proposed_candidate_digests": proposed,
            "analysis": plan["analysis"], "proposal_status": plan["proposal_status"],
        }, appended

    def test_dominant_proposal_and_exact_replay_match_unchanged_legacy(self):
        self.fixture.test_learning_cycle_mines_cases_and_generates_only_inert_dominant_proposal()
        self.assertEqual(2, self.calls)

    def test_protected_case_lineage_matches_unchanged_legacy(self):
        self.fixture.test_learning_cycle_excludes_frozen_case_lineage_from_proposals()
        self.assertEqual(2, self.calls)

    def test_configuration_limit_preserves_full_analysis(self):
        for index in range(65):
            outcome = verification_outcome(f"configuration-{index}")
            outcome["bounds_digest"] = f"{index + 1000:064x}"
            configuration = {key: outcome[key] for key in (
                "model", "effort", "role", "topology", "max_parallel",
                "verification_required", "verifier_digest", "bounds_digest", "package_digest",
                "policy_digest", "agent_digest", "skill_digests", "prompt_digest", "client_digest",
            )}
            outcome["configuration_digest"] = content_digest(configuration)
            self.fixture._admit_outcome(self.fixture.project_a_id, outcome, now_ms=index + 10)
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=100)
        self.assertEqual("configuration_limit", result["proposal_status"])
        self.assertEqual([], result["proposed_candidate_digests"])
        configurations = result["analysis"]["slices"]["verification-learning"]["configurations"]
        self.assertEqual(65, len(configurations))

    def test_missing_accepted_qa_still_rejects_candidate_at_owner_gate(self):
        self._dominant_cohort(accepted_qa=False)
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=400)
        self.assertEqual([1], self.planned_spec_counts)
        self.assertEqual([], result["proposed_candidate_digests"])
        self.assertEqual(40, len(result["mined_case_ids"]))

    def _dominant_cohort(self, *, accepted_qa, count=20):
        self.fixture._admit_reviewed_cases(self.fixture.project_a_id, heldout_cases())
        outcomes = []
        for index in range(count):
            for model, tokens in (("gpt-5.6-terra", 200), ("gpt-5.6-luna", 100)):
                outcome = verification_outcome(f"dominant-{index}-{model}")
                configuration = {**champion_configuration(), "model": model}
                qa_digest = content_digest({"qa": outcome["record_id"]})
                outcome.update(
                    **configuration, task_slice="coding-medium-low",
                    fixture_digest=f"{index + 2000:064x}",
                    configuration_digest=content_digest(configuration),
                    usage=full_usage(input_tokens=tokens, output_tokens=tokens, model_calls=2),
                    evidence_digests=[DIGEST_A, qa_digest],
                )
                self.fixture._admit_outcome(self.fixture.project_a_id, outcome, now_ms=100 + index)
                if accepted_qa:
                    self.fixture._accept_outcome_qa(
                        self.fixture.project_a_id, outcome, qa_digest, now_ms=100 + index,
                    )
                outcomes.append(outcome)
        return outcomes

    def test_same_cohort_with_accepted_qa_produces_proposal(self):
        self._dominant_cohort(accepted_qa=True)
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=400)
        self.assertEqual([1], self.planned_spec_counts)
        self.assertEqual(1, len(result["proposed_candidate_digests"]))

    def test_owner_feedback_remains_invalidating_after_key_replacement(self):
        self.fixture.test_operational_qa_supersedes_unverified_run_and_surfaces_no_owner_review()
        ledger = self.fixture.ledger
        invalidated = ledger._owner_invalidated_run_ids_locked(self.fixture.project_a_id)
        self.assertEqual(1, len(invalidated))
        replacement = self.fixture.state_root.parent / "replacement-owner"
        replacement.mkdir()
        _, public_key = generate_test_owner_key(replacement)
        ledger.replace_owner_key(public_key, now_ms=190)
        result = ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=200)
        self.assertEqual(invalidated, ledger._owner_invalidated_run_ids_locked(self.fixture.project_a_id))
        self.assertGreater(result["analysis"]["owner_feedback_excluded"], 0)
        self.assertEqual([], result["mined_case_ids"])

    def test_newly_mined_case_joins_existing_protected_lineage_before_proposals(self):
        outcomes = self._dominant_cohort(accepted_qa=True)
        protected_source = deepcopy(outcomes[0])
        protected_source["record_id"] = "protected-predecessor-source"
        self.fixture._admit_outcome(self.fixture.project_a_id, protected_source, now_ms=150)
        key = self.fixture.state.digest_key()
        criteria = {name: protected_source[name] for name in (
            "deterministic_pass", "security_pass", "required_criteria_total",
            "required_criteria_passed",
        )}
        case = mine_evaluation_case(
            digest_key=key, outcome=protected_source,
            fixture_digest=protected_source["fixture_digest"], criteria_digest=content_digest(criteria),
            lineage_digest=keyed_digest(key, protected_source["run_id"]),
        )
        reviewed = review_evaluation_case(
            case, digest_key=key, review_digest="d" * 64,
            similarity_group_digest="e" * 64, destination="promotion_holdout", freeze=True,
        )
        for record in (case, reviewed):
            self.fixture.ledger.append(
                self.fixture.project_a_id, record, producer="evaluator", now_ms=151,
            )
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=200)
        self.assertEqual([0], self.planned_spec_counts)
        self.assertEqual([], result["proposed_candidate_digests"])
        self.assertEqual(40, len(result["mined_case_ids"]))
        row = self.fixture.ledger._connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='evaluation_case' "
            "AND record_id IN (" + ",".join("?" for _ in result["mined_case_ids"]) + ")",
            (self.fixture.project_a_id, *result["mined_case_ids"]),
        ).fetchall()
        mined = [json.loads(bytes(item[0])) for item in row]
        joined = next(item for item in mined if item["source_outcome_id"] == outcomes[0]["record_id"])
        self.assertEqual(reviewed["lineage_digest"], joined["lineage_digest"])

    def test_unknown_usage_and_stale_evidence_match_unchanged_legacy(self):
        outcome = verification_outcome("unknown-usage")
        outcome["coverage"] = "partial"
        outcome["usage"] = {name: None for name in outcome["usage"]}
        outcome["usage_sources"] = {
            name: {"source": "unavailable", "coverage": "unknown"}
            for name in outcome["usage"]
        }
        self.fixture._admit_outcome(self.fixture.project_a_id, outcome, now_ms=10)
        result = self.fixture.ledger.run_learning_cycle(
            self.fixture.project_a_id, as_of_ms=1000, max_age_ms=10,
        )
        configurations = result["analysis"]["slices"]["verification-learning"]["configurations"]
        section = next(iter(configurations.values()))
        self.assertIsNone(section["observations"]["workflow_token_volume"])
        self.assertTrue(section["evidence_flags"]["stale"])
        self.assertIn("telemetry_incomplete", section["diagnoses"])

    def test_retry_duplicates_do_not_make_nineteen_independent_fixtures_eligible(self):
        outcomes = self._dominant_cohort(accepted_qa=True, count=19)
        for parent in outcomes[:2]:
            retried = deepcopy(parent)
            retried.update(
                record_id=parent["record_id"] + "-retry",
                run_id=parent["run_id"] + "-retry",
                assignment_id=parent["assignment_id"] + "-retry",
                retry_attempt=2, retry_of_assignment_id=parent["assignment_id"],
            )
            self.fixture._admit_outcome(self.fixture.project_a_id, retried, now_ms=150)
            self.fixture._accept_outcome_qa(
                self.fixture.project_a_id, retried, retried["evidence_digests"][1], now_ms=150,
            )
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=200)
        self.assertEqual([0], self.planned_spec_counts)
        self.assertEqual([], result["proposed_candidate_digests"])

    def test_proposals_select_last_thirty_two_fixtures_in_sorted_digest_order(self):
        outcomes = self._dominant_cohort(accepted_qa=True, count=40)
        result = self.fixture.ledger.run_learning_cycle(self.fixture.project_a_id, as_of_ms=200)
        self.assertEqual(1, len(result["proposed_candidate_digests"]))
        candidate, _ = self.fixture.ledger.lifecycle_records(
            self.fixture.project_a_id, result["proposed_candidate_digests"][0],
        )
        fixtures = {content_digest(outcome): outcome["fixture_digest"] for outcome in outcomes}
        self.assertEqual(64, len(candidate["evidence_digests"]))
        self.assertEqual(
            {f"{index + 2000:064x}" for index in range(8, 40)},
            {fixtures[digest] for digest in candidate["evidence_digests"]},
        )
