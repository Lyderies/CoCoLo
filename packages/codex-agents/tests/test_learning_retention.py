"""O0c P7/P8: the learning cycle and case review read a retention window, not all time, and the
product's own loops remove unpromoted cases and candidates past it (the 0.3.4 ResultLimitTests shape:
limits patched small before anything opens, then more records over time than the limit).

Before this, `records` never lost a case or a candidate: cases + candidates were counted against
30,000 rows and 64 MiB (the project store) or 20,000 cases (the ledger) for good, and a project that
reached them got `learning_capacity` on every cycle for ever. The retention is one constant,
`learning_cycle_core.LEARNING_RETENTION_MS` (90 days, as for outcomes and operational QA).
"""

from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_store as legacy
from tests.learning_support import candidate, verification_outcome
from tests.support import TEMP_ROOT
from tests.test_project_learning_live import KEY, fixture_record, project_store, run_cycle, stored

from codex_agents import learning_cycle_core, learning_store
from codex_agents import project_learning_live as live
from codex_agents import project_learning_owner as owner
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_engine import review_evaluation_case

DAY = 24 * 60 * 60 * 1000


def case_ids(store):
    return [json.loads(content)["record_id"] for content in stored(store, "evaluation_case")]


def candidate_ids(store):
    return [json.loads(content)["record_id"] for content in stored(store, "candidate")]


class ProjectCycleWindowTests(unittest.TestCase):
    """P7: the live cycle reads the window; a project does not reach `learning_capacity` by age."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.store = project_store(Path(self.temporary.name) / "history")
        self.addCleanup(self.store.close)
        self.clock = [0]
        self.learning = live.ProjectLearning(clock_ms=lambda: self.clock[0])
        self.addCleanup(self.learning.close)

    def small(self, cases, outcomes=10_000):
        for patcher in (mock.patch.object(live, "MAX_CASES", cases),
                        mock.patch.object(live, "MAX_OUTCOMES", outcomes),
                        mock.patch.object(learning_cycle_core, "MAX_CASES", cases)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def outcome(self, name, day):
        return fixture_record(self.store, verification_outcome(name), "verifier", day * DAY)

    def cycle(self, request, day):
        self.clock[0] = day * DAY
        return run_cycle(self.learning, self.store, request)

    def test_P7_more_cases_mined_over_the_retention_than_the_case_limit_never_fail_the_cycle(self):
        """MAX_CASES 3; one case mined every 40 days, five in all (160 days, more than the 90-day
        retention). Under 0.4.0 the fourth cycle read all four cases and failed learning_capacity."""
        self.small(cases=3)
        for index in range(5):
            self.outcome(f"outcome-{index}", index * 40)
            with self.subTest(cycle=index):
                view = self.cycle(f"cycle-{index}", index * 40)
                self.assertEqual("ready", view["state"], view)
                self.assertEqual(1, view["summary"]["mined_case_count"])
        self.assertEqual(5, len(case_ids(self.store)))  # The cycle reads a window; the purge is the cleanup's.

    def test_P7_candidates_past_the_retention_do_not_count_against_the_corpus_limit(self):
        """Cases and candidates share one bound (MAX_CASES + MAX_OUTCOMES rows): here 2 + 1. Four old
        candidates would fill it for good; the window leaves them out."""
        self.small(cases=2, outcomes=1)
        for index in range(4):
            fixture_record(self.store, candidate([f"{index + 1:064x}"]), "evaluator", 0)
        self.assertEqual(4, len(candidate_ids(self.store)))
        self.outcome("fresh", 200)
        view = self.cycle("cycle", 200)
        self.assertEqual("ready", view["state"], view)
        self.assertEqual(1, view["summary"]["mined_case_count"])

    def test_P7_a_promoted_old_case_stays_in_the_window(self):
        """A reviewed (controlled) case is policy: it is read however old, and counts for its lineage."""
        self.outcome("source", 0)
        self.cycle("first", 0)
        (case,) = [json.loads(content) for content in stored(self.store, "evaluation_case")]
        reviewed = review_evaluation_case(case, digest_key=KEY, review_digest="d" * 64,
                                          similarity_group_digest="e" * 64, destination="promotion_holdout",
                                          freeze=True)
        fixture_record(self.store, reviewed, "evaluator", 0)
        self.small(cases=1)
        self.outcome("later", 200)
        view = self.cycle("later", 200)
        self.assertEqual("ready", view["state"], view)  # The promoted case + one new one, never the old development case.
        self.assertEqual(1, view["summary"]["mined_case_count"])


class ProjectRetentionFixture(unittest.TestCase):
    """Five old development cases, one of them promoted (so also superseded), one referenced by a
    recent outcome, a young case, and four old candidates (one promoted, one referenced)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.store = project_store(Path(self.temporary.name) / "history")
        self.addCleanup(self.store.close)
        self.clock = [0]
        self.learning = live.ProjectLearning(clock_ms=lambda: self.clock[0])
        self.addCleanup(self.learning.close)
        for index in range(5):
            fixture_record(self.store, verification_outcome(f"old-{index}"), "verifier", 0)
        self.cycle("old", 0)
        self.old = case_ids(self.store)
        self.assertEqual(5, len(self.old))
        cases = {json.loads(content)["source_outcome_id"]: json.loads(content)
                 for content in stored(self.store, "evaluation_case")}
        # Promoted: old-0's case is reviewed; the review is controlled and the development case it replaced is named by it.
        self.reviewed = review_evaluation_case(cases["old-0"], digest_key=KEY, review_digest="d" * 64,
                                               similarity_group_digest="e" * 64,
                                               destination="promotion_holdout", freeze=True)
        fixture_record(self.store, self.reviewed, "evaluator", 0)
        self.superseded = cases["old-0"]["record_id"]
        self.referenced = cases["old-2"]["record_id"]
        self.free = [cases[f"old-{index}"]["record_id"] for index in (1, 3, 4)]
        self.proposals = []
        for index in range(4):
            record = candidate([f"{index + 1:064x}"])
            if index == 3:
                record = {**record, "status": "promoted"}
            fixture_record(self.store, record, "evaluator", 0)
            self.proposals.append(record)
        # A recent outcome that names a candidate and a case by their digests (an evaluation run).
        evaluation = verification_outcome("evaluation")
        evaluation.update(
            evaluation_candidate_digest=self.proposals[0]["candidate_digest"],
            evaluation_case_digest=self.store._connection.execute(
                "SELECT content_digest FROM records WHERE project_id=? AND record_id=?",
                (self.store.project_id, self.referenced)).fetchone()[0])
        fixture_record(self.store, evaluation, "verifier", 95 * DAY)
        fixture_record(self.store, verification_outcome("young"), "verifier", 100 * DAY)
        self.cycle("young", 100)
        self.young = [identifier for identifier in case_ids(self.store)
                      if identifier not in {*self.old, self.reviewed["record_id"]}]

    def cycle(self, request, day):
        self.clock[0] = day * DAY
        return run_cycle(self.learning, self.store, request)

    def surviving(self, kind):
        return set(case_ids(self.store) if kind == "case" else candidate_ids(self.store))


class ProjectPurgeTests(ProjectRetentionFixture):
    """P7: the project service's cleanup pass removes unpromoted cases and candidates past the retention."""

    NOW = 100 * DAY + 1

    def test_P7_the_cleanup_pass_removes_unpromoted_cases_and_candidates_past_the_retention(self):
        with mock.patch.object(self.store, "_result_now", return_value=self.NOW):
            self.store.expire_results(limit=10)
        self.assertEqual({self.superseded, self.referenced, self.reviewed["record_id"], *self.young},
                         self.surviving("case"))
        # Kept: the promoted candidate and the one a recent outcome names; gone: the two nothing needs.
        self.assertEqual({self.proposals[0]["record_id"], self.proposals[3]["record_id"]},
                         self.surviving("candidate"))

    def test_P7_nothing_is_removed_within_the_retention(self):
        before = (self.surviving("case"), self.surviving("candidate"))
        with mock.patch.object(self.store, "_result_now", return_value=90 * DAY):
            self.store.expire_results(limit=10)
        self.assertEqual(before, (self.surviving("case"), self.surviving("candidate")))

    def test_P7_a_promoted_case_and_a_referenced_record_survive_the_purge(self):
        removed = owner.purge_old_learning(self.store, self.NOW, 100)
        self.assertEqual(len(self.free) + 2, removed)  # Three development cases, two candidates.
        survivors = self.surviving("case")
        self.assertIn(self.reviewed["record_id"], survivors)  # Promoted.
        self.assertIn(self.superseded, survivors)  # Named by the promoted case.
        self.assertIn(self.referenced, survivors)  # Named by a recent outcome.
        self.assertTrue(set(self.free).isdisjoint(survivors))
        self.assertIn(self.proposals[0]["record_id"], self.surviving("candidate"))  # Named by a recent outcome.
        self.assertIn(self.proposals[3]["record_id"], self.surviving("candidate"))  # Not proposed any more.
        self.assertEqual(0, owner.purge_old_learning(self.store, self.NOW, 100))

    def test_P7_purge_batches_are_bounded_and_every_pass_makes_progress(self):
        passes = []
        while True:
            removed = owner.purge_old_learning(self.store, self.NOW, 2)
            passes.append(removed)
            if not removed or len(passes) > 10:
                break
        self.assertEqual([2, 2, 1, 0], passes)  # Five records go, never more than two at a time.
        self.assertEqual({self.superseded, self.referenced, self.reviewed["record_id"], *self.young},
                         self.surviving("case"))
        self.assertEqual({self.proposals[0]["record_id"], self.proposals[3]["record_id"]},
                         self.surviving("candidate"))

    def test_P7_the_cleanup_pass_removes_at_most_its_batch(self):
        before = len(self.surviving("case")) + len(self.surviving("candidate"))
        with mock.patch.object(owner, "LEARNING_PURGE_BATCH", 1), \
                mock.patch.object(self.store, "_result_now", return_value=self.NOW):
            self.store.expire_results(limit=10)
        self.assertEqual(before - 1, len(self.surviving("case")) + len(self.surviving("candidate")))

    def test_P7_case_review_reads_a_window_not_all_cases(self):
        """MAX_CASES 3 against seven stored cases: the review reads the young case and the promoted one."""
        with mock.patch.object(owner, "MAX_CASES", 3):
            reviewed = owner.review_case(
                self.store, self.young[0], review_digest="f" * 64, similarity_group_digest="a" * 64,
                destination="promotion_holdout", freeze=False, now_ms=100 * DAY)
        self.assertTrue(reviewed["stored"])

    def test_P7_the_purge_never_removes_a_record_a_cycle_has_not_outlived(self):
        """Purged cases are not mined again: the outcomes behind them are as old as the cases."""
        owner.purge_old_learning(self.store, self.NOW, 100)
        view = self.cycle("after", 100)
        self.assertEqual("ready", view["state"])
        self.assertEqual(0, view["summary"]["mined_case_count"])


class LegacyLearningWindowTests(unittest.TestCase):
    """P8: the same, for the legacy ledger's cycle of a project not yet cut over."""

    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.connection = self.ledger._connection

    def outcome(self, name, day):
        self.fixture._admit_outcome(self.project, verification_outcome(name), now_ms=day * DAY)

    def cycle(self, day):
        return self.ledger.run_learning_cycle(self.project, as_of_ms=day * DAY)

    def test_P8_more_cases_mined_over_the_retention_than_the_case_limit_never_fail_the_cycle(self):
        with mock.patch.object(learning_store, "CYCLE_MAX_CASES", 3):
            for index in range(5):
                self.outcome(f"outcome-{index}", index * 40)
                with self.subTest(cycle=index):
                    self.assertEqual(1, len(self.cycle(index * 40)["mined_case_ids"]))

    def test_P8_more_outcomes_over_the_retention_than_the_outcome_limit_never_fail_the_cycle(self):
        with mock.patch.object(learning_store, "CYCLE_MAX_OUTCOMES", 2):
            for index in range(4):
                self.outcome(f"outcome-{index}", index * 50)
                with self.subTest(cycle=index):
                    self.assertEqual(1, len(self.cycle(index * 50)["mined_case_ids"]))

    def count(self, kind, project=None):
        return self.connection.execute("SELECT COUNT(*) FROM records WHERE project_id=? AND record_type=?",
                                       (project or self.project, kind)).fetchone()[0]

    def add_candidate(self, number, *, status="proposed"):
        record = {**candidate([f"{number + 1:064x}"]), "status": status}
        content = canonical_json(record)
        sequence = self.connection.execute(
            "SELECT MAX(source_sequence)+1 FROM records WHERE project_id=?", (self.project,)).fetchone()[0]
        self.connection.execute(
            "INSERT INTO records VALUES(?,?,?,?,?,?,?,NULL,?,?,0)",
            (self.project, record["record_id"], "candidate", "evaluator", sequence, 0, 0, content,
             record["candidate_digest"])).close()
        return record

    def build_corpus(self):
        for index in range(5):
            self.outcome(f"old-{index}", 0)
        mined = self.cycle(0)["mined_case_ids"]
        self.assertEqual(5, len(mined))
        with self.ledger.repository(self.project) as repository:
            cases = {record["source_outcome_id"]: record for record in (
                repository.record(identifier, "evaluation_case") for identifier in mined)}
        reviewed = review_evaluation_case(
            cases["old-0"], digest_key=self.fixture.state.digest_key(), review_digest="d" * 64,
            similarity_group_digest="e" * 64, destination="promotion_holdout", freeze=True)
        self.ledger.append(self.project, reviewed, producer="evaluator", now_ms=0)
        self.reviewed, self.superseded = reviewed["record_id"], cases["old-0"]["record_id"]
        self.referenced = cases["old-2"]["record_id"]
        self.free = {cases[f"old-{index}"]["record_id"] for index in (1, 3, 4)}
        # A later record names old-2's case and the first candidate (the ledger's own links).
        self.connection.execute("INSERT INTO record_links VALUES(?,?,?,?)",
                                (self.project, self.referenced, self.reviewed, "evidence")).close()
        self.proposals = [self.add_candidate(0), self.add_candidate(1), self.add_candidate(2)]
        self.connection.execute("INSERT INTO record_links VALUES(?,?,?,?)",
                                (self.project, self.proposals[0]["record_id"], self.reviewed, "evaluates")).close()
        self.outcome("young", 100)
        self.assertEqual(1, len(self.cycle(100)["mined_case_ids"]))

    def surviving(self, kind):
        return {row[0] for row in self.connection.execute(
            "SELECT record_id FROM records WHERE project_id=? AND record_type=?", (self.project, kind))}

    def test_P8_the_collector_sweep_removes_unpromoted_cases_and_candidates_past_the_retention(self):
        self.build_corpus()
        now = 100 * DAY + 1
        passes = []
        while True:
            removed = self.ledger.sweep_learning_retention(now_ms=now, batch=2)
            passes.append(removed)
            if not removed or len(passes) > 10:
                break
        self.assertEqual([2, 2, 1, 0], passes)  # Three cases and two candidates; never more than two at a time.
        cases = self.surviving("evaluation_case")
        self.assertTrue(self.free.isdisjoint(cases))
        self.assertTrue({self.reviewed, self.superseded, self.referenced} <= cases)  # Promoted, named by it, referenced.
        self.assertEqual(4, len(cases))  # Those three and the young case.
        self.assertEqual({self.proposals[0]["record_id"]}, self.surviving("candidate"))
        # Within the retention nothing goes.
        self.assertEqual(0, self.ledger.sweep_learning_retention(now_ms=10 * DAY, batch=100))

    def test_P8_a_project_whose_learning_moved_to_its_store_is_not_swept(self):
        self.build_corpus()
        before = self.count("evaluation_case")
        self.connection.execute("INSERT INTO additive_migrations VALUES(?)",
                                (learning_store.LEARNING_MOVED_KEY + self.project,)).close()
        self.assertEqual(0, self.ledger.sweep_learning_retention(now_ms=100 * DAY + 1, batch=100))
        self.assertEqual(before, self.count("evaluation_case"))

    def test_P8_the_running_collector_sweeps_by_itself(self):
        cycle = inspect.getsource(learning_store.CollectorServer._sweep_cycle)
        self.assertIn("self.ledger.sweep_learning_retention", cycle)


if __name__ == "__main__":
    unittest.main()
