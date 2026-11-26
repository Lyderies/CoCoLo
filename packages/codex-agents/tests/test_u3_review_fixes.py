"""U3 review fixes (reviews/040-u3-r1): fail-closed duplicate guards and old/new admission query equivalence.

LOW-1: U3 guards the run-identity JSON expression with json_valid so an index build and a keyed seek never
trip over a corrupt row. A corrupt dispatch_intent, provider_usage or dispatch_result row then silently failed
to match the "already dispatched/recorded" checks, where the unguarded read raised. Those duplicate guards must
refuse instead, as they did before U3.

LOW-2: the candidate-seeking admission capture query must return exactly the rows, in the order, that the
original full-scan query returned. The original query is kept here as the reference implementation.

Integration stage: the bridge ledger and original-open fixtures (their set-up starts processes).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests import test_bridge as bridge_tests
from tests.learning_support import result
from tests import test_admission_history_growth as growth_tests
from test_framework import requirements

from codex_agents import account_service
from codex_agents.learning_store import LedgerError

DAY_MS = growth_tests.DAY_MS
USAGE = {
    "requested_model": "claude-opus-5-5", "models": [], "helper_models": [],
    "input_tokens": 10, "cached_input_tokens": 0, "cache_creation_input_tokens": 0,
    "output_tokens": 2, "reasoning_output_tokens": None,
}


class DuplicateGuardsFailClosedTests(bridge_tests.BridgeTestCase):
    def malformed(self, kind, source):
        """An unreadable retained row of `kind`, as ledger tampering or corruption would leave it."""
        connection = self.ledger._connection
        with self.ledger._lock, connection:
            top = connection.execute("SELECT MAX(source_sequence) FROM records WHERE project_id=?",
                                     (self.project_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
                "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,1,1,NULL,?,?,0)",
                (self.project_id, f"{kind}:malformed", kind, source, top + 1, b'{"run_id": "r", broken',
                 hashlib.sha256(kind.encode()).hexdigest()))

    def forget(self, kind):
        with self.ledger._lock, self.ledger._connection:
            self.ledger._connection.execute("DELETE FROM records WHERE project_id=? AND record_id=?",
                                            (self.project_id, f"{kind}:malformed"))

    def claim(self, route, assigned, handle):
        return self.ledger.record_dispatch_intent(
            self.project_id, run_handle=handle, assignment=assigned, task=self.task(), route=route,
            deadline_seconds=60)

    @requirements("U3", why_not_scenario="Ledger tampering needs a direct malformed row in a disposable ledger.")
    def test_U3_a_malformed_dispatch_intent_blocks_the_already_dispatched_check(self):
        route, assigned, handle = self.claude_run()
        self.malformed("dispatch_intent", "lifecycle")
        with self.assertRaisesRegex(LedgerError, "malformed dispatch_intent"):
            self.ledger.record_dispatch_intent(
                self.project_id, run_handle=handle, assignment=assigned, task=self.task(), route=route,
                deadline_seconds=1, claim=False)
        with self.assertRaisesRegex(LedgerError, "malformed dispatch_intent"):
            self.claim(route, assigned, handle)
        self.forget("dispatch_intent")
        self.assertIn("run_id", self.claim(route, assigned, handle))

    @requirements("U3", why_not_scenario="Ledger tampering needs a direct malformed row in a disposable ledger.")
    def test_U3_b_a_malformed_provider_usage_blocks_the_already_recorded_check(self):
        route, assigned, handle = self.claude_run()
        self.claim(route, assigned, handle)
        self.malformed("provider_usage", "provider")
        with self.assertRaisesRegex(LedgerError, "malformed provider_usage"):
            self.ledger.record_provider_usage(self.project_id, run_handle=handle, provider="claude", usage=USAGE)
        self.forget("provider_usage")
        self.assertEqual("provider_usage", self.ledger.record_provider_usage(
            self.project_id, run_handle=handle, provider="claude", usage=USAGE)["record_type"])

    @requirements("U3", why_not_scenario="Ledger tampering needs a direct malformed row in a disposable ledger.")
    def test_U3_c_a_malformed_dispatch_result_blocks_the_already_recorded_check(self):
        route, assigned, handle = self.claude_run()
        intent = self.claim(route, assigned, handle)
        self.malformed("dispatch_result", "lifecycle")
        arguments = {"run_handle": handle, "bridge_run_id": intent["bridge_run_id"], "result": result(),
                     "fenced": True, "reviewed": True, "terminal_state": "failed"}
        with self.assertRaisesRegex(LedgerError, "malformed dispatch_result"):
            self.ledger.record_dispatch_result(self.project_id, **arguments)
        self.forget("dispatch_result")
        self.ledger.record_dispatch_result(self.project_id, **arguments)
        with self.assertRaisesRegex(LedgerError, "already recorded"):
            self.ledger.record_dispatch_result(self.project_id, **arguments)

    @requirements("U3", why_not_scenario="Plan assertion needs the disposable ledger connection.")
    def test_U3_d_the_malformed_row_check_seeks_the_run_index(self):
        plan = " ".join(row[3] for row in self.ledger._connection.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM records WHERE project_id=? "
            "AND CASE WHEN json_valid(CAST(content AS TEXT)) THEN json_extract(CAST(content AS TEXT),'$.run_id') END IS NULL "
            "AND record_type=? AND NOT json_valid(CAST(content AS TEXT)) LIMIT 1", (self.project_id, "dispatch_intent")))
        self.assertIn("records_run", plan)
        self.assertNotIn("SCAN", plan)


# The admission capture query before U3: every candidate rule applied to the whole records table.
_ORIGINAL_ADMISSION_INPUT_JOIN = (
    " FROM (SELECT record_rowid FROM (SELECT record_rowid FROM temp.capture_candidates "
    "WHERE consumer=:provider AND project_id=:p ORDER BY record_rowid LIMIT :limit) "
    "UNION ALL SELECT record_rowid FROM (SELECT r.rowid AS record_rowid FROM records r WHERE r.record_type IN "
    "('provider_identity','admission_decision','probe_admission','provider_billing','provider_headroom') "
    "AND " + account_service._ADMISSION_INPUT_SCOPE + " ORDER BY r.rowid LIMIT :limit)) c CROSS JOIN records r "
    "LEFT JOIN owner_attestations a ON a.project_id=r.project_id AND a.subject_record_id=r.record_id "
    "WHERE r.rowid=c.record_rowid ORDER BY c.record_rowid LIMIT :limit"
)


class AdmissionQueryEquivalenceTests(unittest.TestCase):
    setUp = growth_tests.AdmissionHistoryGrowthTests.setUp
    copies = growth_tests.AdmissionHistoryGrowthTests.copies
    captured = growth_tests.AdmissionHistoryGrowthTests.captured

    def put(self, project, kind, source, age_ms, change=None, record_id=None, content=None):
        """One `kind` record observed `age_ms` ago (negative: in the future) in `project`, from its latest copy."""
        connection = self.ledger._connection
        template = connection.execute(
            "SELECT content FROM records WHERE record_type=? ORDER BY observed_at_ms DESC LIMIT 1", (kind,)).fetchone()
        sequence = connection.execute("SELECT COALESCE(MAX(source_sequence),0) FROM records WHERE project_id=?",
                                      (project,)).fetchone()[0] + 1
        now = self.ledger._clock_ms()
        observed = now - age_ms
        record = json.loads(bytes(template["content"]))
        identifier = record_id or f"{kind}:{hashlib.sha256(f'{project}-{kind}-{source}-{age_ms}-{sequence}'.encode()).hexdigest()}"
        record.update(record_id=identifier, observed_at_ms=observed)
        if change is not None:
            change(record, now)
        body = content if content is not None else json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
            "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
            (project, identifier, kind, source, sequence, observed, observed, observed + 90 * DAY_MS, body,
             hashlib.sha256(body).hexdigest()))
        connection.commit()
        return identifier

    def other_project(self):
        root = Path(tempfile.mkdtemp(dir=tests.support.TEMP_ROOT))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "other").mkdir()
        return self.ledger.register_project(root / "other", now_ms=1)

    @requirements("U3", why_not_scenario="Compares two SQL texts on one disposable ledger, which needs its connection.")
    def test_U3_the_candidate_query_returns_the_rows_and_order_of_the_original_full_scan(self):
        mine = self.open.reference.base.scope.project_id
        other = self.other_project()
        def claude(account):
            return lambda record, now: record.update(provider="claude", account_fingerprint=account * 64)
        def passing(account):
            return lambda record, now: record.update(
            provider="claude", decision="pass", account_fingerprint=account * 64)
        def live(record, now):
            return record.update(windows=[{
            "resets_at": now + 3_600_000, "scope": None, "used_percent": 100.0, "window_minutes": 10080}])
        for project in (mine, other):
            self.put(project, "provider_identity", "provider", 10 * 60_000)
            self.put(project, "provider_identity", "provider", 40 * 60_000)  # past its 30 minutes
            self.put(project, "provider_identity", "provider", -60_000)  # from the future
            self.put(project, "admission_decision", "lifecycle", 3_600_000)
            self.put(project, "admission_decision", "lifecycle", 6 * 3_600_000)  # past five hours
            self.put(project, "admission_decision", "hook", 30 * DAY_MS)  # foreign decision, any age
            self.put(project, "admission_decision", "lifecycle", 40 * DAY_MS, passing("a"))  # first pass of account a
            self.put(project, "admission_decision", "lifecycle", 30 * DAY_MS, passing("a"))  # a later pass: dropped
            self.put(project, "admission_decision", "lifecycle", 35 * DAY_MS, passing("b"))
            self.put(project, "admission_decision", "lifecycle", 20 * DAY_MS, claude("a"))  # not a pass
            for kind in ("provider_billing", "provider_headroom"):
                self.put(project, kind, "provider", 3_600_000)
                self.put(project, kind, "provider", -3_600_000)
                self.put(project, kind, "provider", 3 * DAY_MS, claude("a"))  # latest older, tied below
                self.put(project, kind, "provider", 3 * DAY_MS, claude("a"))
                self.put(project, kind, "provider", 5 * DAY_MS, claude("a"))  # superseded
                self.put(project, kind, "provider", 4 * DAY_MS, claude("b"))
                self.put(project, kind, "provider", 4 * DAY_MS, claude("b"), content=b"{not json")  # invalid, tied
                self.put(project, kind, "provider", 9 * DAY_MS)  # no account, old
            self.put(project, "provider_headroom", "provider", 6 * DAY_MS, live)  # live window, old
        rows = {}
        for name, join in (("new", account_service._ADMISSION_INPUT_JOIN), ("old", _ORIGINAL_ADMISSION_INPUT_JOIN)):
            with mock.patch.object(account_service, "_ADMISSION_INPUT_JOIN", join):
                ids, snapshot = self.captured()
                rows[name] = [tuple(row) for row in snapshot.rows]
        self.assertGreater(len(rows["old"]), 20)
        self.assertEqual(rows["old"], rows["new"])
