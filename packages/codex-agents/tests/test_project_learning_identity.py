from __future__ import annotations

import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT
from tests.learning_support import candidate, verification_outcome
from tests import test_account_claim_storage as account_fixtures

from codex_agents.account_api import AccountError
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import mine_evaluation_case
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity

PROJECT = "prj_" + "a" * 32
KEY = b"K" * 32
DOMAIN = b"project-learning-key-domain-v1\0"


def expected_case(key, outcome):
    criteria = {name: outcome[name] for name in (
        "deterministic_pass", "security_pass", "required_criteria_total", "required_criteria_passed",
    )}
    return mine_evaluation_case(
        digest_key=key, outcome=outcome, fixture_digest=outcome["fixture_digest"],
        criteria_digest=content_digest(criteria), lineage_digest=keyed_digest(key, outcome["run_id"]),
    )


def fixture_outcome(store, outcome, timestamp):
    """Synthetic trusted-owner fixture; no generic verified-write or QA authority."""
    raw = canonical_json(outcome)
    checked = store._checked_record(raw, "verifier", timestamp)
    with store._transaction():
        return store._append_locked(checked, raw, "verifier", timestamp)


class ProjectLearningIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "history"
        self.directory.mkdir(mode=0o700)
        self.store = ProjectHistory(
            self.directory, PROJECT, result_key=KEY,
            result_capacity=ResultCapacity(1, 1_048_576, 120_000, 120_000),
        )
        self.addCleanup(self.store.close)
        self.outcome = verification_outcome()
        entry = fixture_outcome(self.store, self.outcome, 10)
        self.reference = (entry.record_id, entry.content_digest)
        self.domain = keyed_digest(KEY, DOMAIN + PROJECT.encode("ascii"))

    def prepare(self, references=None, domain=None):
        return self.store.prepare_learning_case_records(
            [self.reference] if references is None else references,
            expected_domain_digest=self.domain if domain is None else domain,
        )

    def test_existing_owner_key_prepares_byte_identical_legacy_case_without_persisting(self):
        self.assertEqual(self.domain, self.store.learning_key_domain_digest())
        before = [tuple(row) for row in self.store._connection.execute("SELECT * FROM records")]
        actual = self.prepare()
        self.assertEqual(canonical_json([expected_case(KEY, self.outcome)]), canonical_json(actual))
        self.assertEqual(actual, self.prepare())
        self.assertEqual(before, [tuple(row) for row in self.store._connection.execute("SELECT * FROM records")])
        self.assertEqual(2, self.store._connection.execute("SELECT next_source_sequence FROM history_identity").fetchone()[0])

    def test_wrong_key_domain_stale_content_or_duplicate_reference_is_refused(self):
        for refs, domain in (([self.reference], "f" * 64),
                             ([(self.reference[0], "f" * 64)], self.domain),
                             ([self.reference, self.reference], self.domain)):
            with self.subTest(references=len(refs)), self.assertRaises(HistoryError):
                self.prepare(refs, domain)

    def test_caller_cannot_replace_the_installed_key_check(self):
        self.store._result_key = b"x" * 32
        changed_domain = keyed_digest(self.store._result_key, DOMAIN + PROJECT.encode("ascii"))
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.prepare(domain=changed_domain)

    def test_preparation_never_runs_under_a_writer_transaction(self):
        with self.store._transaction(), self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.prepare()

    def test_batch_or_aggregate_byte_exhaustion_refuses_without_partial_results(self):
        from codex_agents import project_learning_identity as identity
        with self.assertRaises(HistoryError):
            self.prepare([self.reference] * (identity.MAX_CASE_PREPARATION + 1))
        with mock.patch.object(identity, "MAX_PREPARATION_BYTES", 1), self.assertRaises(HistoryError):
            self.prepare()

    def test_unverified_or_evaluation_outcome_is_not_a_mining_source(self):
        for record in (
            {**self.outcome, "record_id": "unverified", "status": "unverified", "accepted": False, "admission_state": "unverified"},
            {**self.outcome, "record_id": "evaluation", "evaluation_candidate_digest": "d" * 64, "evaluation_case_digest": "e" * 64},
        ):
            entry = fixture_outcome(self.store, record, 20)
            with self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.prepare([(entry.record_id, entry.content_digest)])

    def test_foreign_owner_thread_cannot_read_or_prepare_keyed_metadata(self):
        errors = []

        def other():
            for call in (self.store.learning_key_domain_digest, self.prepare):
                try:
                    call()
                except HistoryError as error:
                    errors.append(error.reason_code)

        worker = threading.Thread(target=other)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(["history_wrong_owner"] * 2, errors)

    def test_candidate_input_is_byte_bounded_and_closed_before_any_outcome_read(self):
        with mock.patch.object(self.store, "record", side_effect=AssertionError("read malformed candidate")):
            for spec in (b"CANARY", b"[]", b"x" * 65_537, {}, canonical_json({"scope": "project"})):
                with self.subTest(kind=type(spec).__name__), self.assertRaisesRegex(HistoryError, "history_invalid_input") as caught:
                    self.store.prepare_learning_candidate_record(
                        spec, [self.reference], expected_domain_digest=self.domain,
                    )
                self.assertNotIn("CANARY", str(caught.exception))

    def test_malformed_nested_candidate_values_have_fixed_errors_and_no_append(self):
        from codex_agents import project_learning_identity as identity
        proposal = candidate([self.reference[1]])
        spec = {name: proposal[name] for name in identity._CANDIDATE_FIELDS}
        spec["scope"] = "project"
        missing_value = deepcopy(spec)
        missing_value["change_manifest"][0].pop("after_value")
        for changed in ({**spec, "champion_configuration": []}, missing_value):
            with self.subTest(champion_type=type(changed["champion_configuration"]).__name__), \
                    self.assertRaisesRegex(HistoryError, "history_invalid_input"):
                self.store.prepare_learning_candidate_record(
                    canonical_json(changed), [self.reference], expected_domain_digest=self.domain,
                )
        self.assertEqual(1, self.store._connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def test_staged_outcomes_remain_unavailable_even_with_the_same_key_domain_marker(self):
        from codex_agents import project_learning_corpus as corpus
        self.store.close()
        self.store = ProjectHistory(
            self.directory, PROJECT, result_key=KEY,
            result_capacity=ResultCapacity(1, 1_048_576, 120_000, 120_000), allow_schema_upgrade=True,
        )
        self.addCleanup(self.store.close)
        outcome = verification_outcome("staged-only")
        raw = canonical_json(outcome)
        record = {"content": raw, "source": "host", "source_sequence": 1,
                  "observed_at_ms": 1, "ingested_at_ms": 1, "expires_at_ms": None, "late": False}
        manifest = {"schema_version": 1, "scope": "learning-record-staging-v1", "project_id": PROJECT,
                    "source_identity_digest": "d" * 64, "key_domain_digest": self.domain,
                    "record_count": 1, "attestation_count": 0, "total_bytes": len(raw),
                    "chain_digest": corpus.next_chain_digest(corpus.EMPTY_CHAIN_DIGEST, corpus.chunk_digest([record], []))}
        self.store.begin_learning_import("staged", manifest, now_ms=100, before_commit=lambda: None)
        self.store.stage_learning_import_chunk("staged", 0, [record], [], now_ms=101, before_commit=lambda: None)
        receipt = self.store.finish_learning_import("staged", now_ms=102, before_commit=lambda: None)
        self.assertIs(False, receipt["learning_ready"])
        with self.assertRaisesRegex(HistoryError, "history_not_found"):
            self.prepare([(outcome["record_id"], content_digest(outcome))])


class ExistingAccountKeyDomainTests(unittest.TestCase):
    """Real store/AccountService cryptographic checks; synthetic frontend, no model."""

    def setUp(self):
        self.fixture = account_fixtures.AccountClaimStorageTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_matching_existing_project_key_admits_storage_and_preserves_legacy_case_identity(self):
        source = self.fixture
        source.arm()
        self.assertEqual("claimed", source.call()["state"])
        outcome = verification_outcome("identity-source")
        outcome["run_id"] = source.scope.run_id
        entry = fixture_outcome(source.store, outcome, source.base.stamp)
        domain = keyed_digest(source.ledger.state.digest_key(), DOMAIN + source.project.encode("ascii"))
        actual = source.store.prepare_learning_case_records(
            [(entry.record_id, entry.content_digest)], expected_domain_digest=domain,
        )
        self.assertEqual([expected_case(source.ledger.state.digest_key(), outcome)], actual)

    def test_actual_different_project_key_cannot_admit_storage_or_match_legacy_learning_domain(self):
        source = self.fixture
        directory = source.directory.parent / "wrong-key-history"
        directory.mkdir(mode=0o700)
        wrong = ProjectHistory(
            directory, source.project, result_key=b"x" * 32, result_capacity=source.store._result_capacity,
            result_clock_ms=lambda: source.base.stamp, allow_legacy_orchestration_reference=True,
        )
        self.addCleanup(wrong.close)
        wrong.reserve_result_lease(source.scope, source.assigned)
        wrong.arm_result_lease(source.scope)
        source.evidence = wrong.attest_result_lease(source.scope)
        with self.assertRaisesRegex(AccountError, "claim_storage_invalid"):
            source.call()
        source.no_consumption()
        legacy_domain = keyed_digest(source.ledger.state.digest_key(), DOMAIN + source.project.encode("ascii"))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            wrong.prepare_learning_case_records([("missing", "a" * 64)], expected_domain_digest=legacy_domain)
