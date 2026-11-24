"""Weighted event retention: kept by the weight of their event code, expired on their own, summarised first.

Raw hook events are most of a busy project's records (191,000 of 192,000 in one
0.2.x ledger) and were kept 90 days, removed only by the owner's
learning-expire. Each event code now has a weight in the installed retention
policy; the collector removes expired raw events in small batches by itself,
after rolling them into one summary per run and day.
"""

import copy
import json
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from tests import test_learning_store as legacy
from tests.learning_support import event_record
from tests.support import ORCHESTRATION_ROOT
from codex_agents import learning_store
from codex_agents.contracts import ContractError, LedgerError
from codex_agents.learning_contracts import EVENT_CODES, validate_learning_record

DAY = 24 * 60 * 60 * 1000
POLICY = ORCHESTRATION_ROOT / "config" / "retention-policy.json"


def event(identifier, code, *, observed_at_ms, run_id="run_1"):
    record = event_record(identifier, observed_at_ms=observed_at_ms)
    record["event_code"], record["run_id"] = code, run_id
    return record


class RetentionPolicyTests(unittest.TestCase):
    def test_the_installed_policy_weights_every_event_code_once(self):
        policy = learning_store.load_retention_policy(POLICY)
        self.assertEqual(set(EVENT_CODES), set(policy.event_keep_ms))
        days = {code: keep // DAY for code, keep in policy.event_keep_ms.items()}
        self.assertEqual(7, days["tool_started"])
        self.assertEqual(7, days["tool_output_observed"])
        self.assertEqual(30, days["session_started"])
        self.assertEqual(30, days["turn_completed"])
        self.assertEqual(90, days["no_progress"])
        self.assertEqual(90, days["escalation"])
        self.assertEqual(90, policy.summary_keep_ms // DAY)

    def test_a_policy_that_misses_repeats_or_stretches_a_code_is_refused(self):
        raw = json.loads(POLICY.read_text())
        broken = []
        missing = copy.deepcopy(raw)
        missing["events"]["low"].remove("tool_started")
        broken.append(missing)
        repeated = copy.deepcopy(raw)
        repeated["events"]["high"].append("tool_started")
        broken.append(repeated)
        unknown = copy.deepcopy(raw)
        unknown["events"]["low"].append("not_an_event")
        broken.append(unknown)
        for days in (0, 366, 7.5, True):
            stretched = copy.deepcopy(raw)
            stretched["weights"]["low"]["keep_days"] = days
            broken.append(stretched)
        extra = copy.deepcopy(raw)
        extra["weights"]["urgent"] = {"keep_days": 1}
        broken.append(extra)
        version = copy.deepcopy(raw)
        version["version"] = 2
        broken.append(version)
        # A weight with no codes still needs a valid period, and a code must be a string.
        empty = copy.deepcopy(raw)
        empty["events"]["high"], empty["events"]["medium"] = [], empty["events"]["medium"] + empty["events"]["high"]
        empty["weights"]["high"]["keep_days"] = "x"
        broken.append(empty)
        unhashable = copy.deepcopy(raw)
        unhashable["events"]["low"].append(["tool_started"])
        broken.append(unhashable)
        for number, value in enumerate(broken):
            path = Path(self.enterContext(_temporary())) / f"policy-{number}.json"
            path.write_text(json.dumps(value))
            with self.subTest(number=number), self.assertRaises(LedgerError):
                learning_store.load_retention_policy(path)


def _temporary():
    import tempfile
    return tempfile.TemporaryDirectory()


class EventRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_a_id
        self.connection = self.ledger._connection

    def append(self, record, *, producer="hook", now_ms):
        return self.ledger.append(self.project, record, producer=producer, now_ms=now_ms)

    def expiry(self, record_id):
        return self.connection.execute("SELECT expires_at_ms,ingested_at_ms FROM records WHERE project_id=? "
                                       "AND record_id=?", (self.project, record_id)).fetchone()

    def test_each_event_is_kept_as_long_as_its_weight_says(self):
        now = 1_000_000_000_000
        for identifier, code, producer, days in (("low", "tool_started", "hook", 7),
                                                 ("medium", "session_started", "hook", 30),
                                                 ("high", "escalation", "hook", 90),
                                                 # Host lifecycle events live as long as their run's outcome.
                                                 ("host", "subagent_started", "host", 90)):
            self.append(event(identifier, code, observed_at_ms=now), producer=producer, now_ms=now)
            expires, ingested = self.expiry(identifier)
            with self.subTest(code=code, producer=producer):
                self.assertEqual(ingested + days * DAY, expires)

    def test_expired_raw_events_are_summarised_then_removed_in_small_batches(self):
        start = datetime(2026, 10, 1, 9, tzinfo=timezone.utc).timestamp() * 1000
        start = int(start)
        for number in range(30):
            self.append(event(f"tool-{number:02d}", "tool_started", observed_at_ms=start + number),
                        now_ms=start + number)
        self.append(event("tool-out", "tool_output_observed", observed_at_ms=start + 100), now_ms=start + 100)
        self.append(event("other-run", "tool_started", observed_at_ms=start + 200, run_id="run_2"),
                    now_ms=start + 200)
        self.append(event("kept", "escalation", observed_at_ms=start + 300), now_ms=start + 300)
        later = start + 8 * DAY
        # A recent event of the same code: hook delivery status must not change.
        self.append(event("recent", "tool_started", observed_at_ms=later - DAY), now_ms=later - DAY)
        before = self.connection.execute(
            "SELECT last_accepted_at_ms FROM hook_ingestion WHERE project_id=? AND event_code='tool_started'",
            (self.project,)).fetchone()
        backups = self.ledger.create_sanitized_backup(self.project, file_id="kept_backup",
                                                      expires_at_ms=start + 60 * DAY)
        removed = self.ledger.sweep_event_retention(now_ms=later, batch=20)
        self.assertEqual(20, removed)
        while self.ledger.sweep_event_retention(now_ms=later, batch=20):
            pass
        events = {row[0] for row in self.connection.execute(
            "SELECT record_id FROM records WHERE project_id=? AND record_type='event'", (self.project,))}
        self.assertNotIn("tool-00", events)
        self.assertNotIn("tool-out", events)
        self.assertEqual({"kept", "recent"}, events)  # Not due yet: high weight, and recent.
        summaries = [json.loads(row[0]) for row in self.connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='event_summary'", (self.project,))]
        totals = {}
        for summary in summaries:
            validate_learning_record(summary)
            self.assertEqual("2026-10-01", summary["day"])
            for code, count in summary["event_counts"].items():
                totals[(summary["run_id"], code)] = totals.get((summary["run_id"], code), 0) + count
            self.assertEqual(summary["last_observed_at_ms"] - summary["first_observed_at_ms"], summary["span_ms"])
            self.assertEqual(sum(summary["event_counts"].values()), summary["event_count"])
        self.assertEqual({("run_1", "tool_started"): 30, ("run_1", "tool_output_observed"): 1,
                          ("run_2", "tool_started"): 1}, totals)
        expires = {row[0] for row in self.connection.execute(
            "SELECT expires_at_ms - ingested_at_ms FROM records WHERE project_id=? AND record_type='event_summary'",
            (self.project,))}
        self.assertEqual({90 * DAY}, expires)
        # The owner's backup is kept for its own lifetime; hook delivery status is unchanged.
        self.assertTrue(backups.exists())
        self.assertEqual(tuple(before), tuple(self.connection.execute(
            "SELECT last_accepted_at_ms FROM hook_ingestion WHERE project_id=? AND event_code='tool_started'",
            (self.project,)).fetchone()))

    def test_linked_events_and_existing_expiries_are_left_to_the_owners_expiry(self):
        now = 1_000_000_000_000
        self.append(event("linked", "tool_started", observed_at_ms=now), now_ms=now)
        self.append(event("free", "tool_started", observed_at_ms=now), now_ms=now)
        self.connection.execute("INSERT INTO record_links VALUES(?,?,?,?)",
                                (self.project, "linked", "free", "evidence")).close()
        # A record kept from an earlier version keeps the expiry it was given.
        self.connection.execute("UPDATE records SET expires_at_ms=? WHERE project_id=? AND record_id='free'",
                                (now + 90 * DAY, self.project)).close()
        self.assertEqual(0, self.ledger.sweep_event_retention(now_ms=now + 10 * DAY))
        self.assertEqual({"linked", "free"}, {row[0] for row in self.connection.execute(
            "SELECT record_id FROM records WHERE project_id=? AND record_type='event'", (self.project,))})

    def _summaries(self):
        return [json.loads(bytes(row[0])) for row in self.connection.execute(
            "SELECT content FROM records WHERE project_id=? AND record_type='event_summary' ORDER BY record_id",
            (self.project,))]

    def _raw_ids(self):
        return {row[0] for row in self.connection.execute(
            "SELECT record_id FROM records WHERE project_id=? AND record_type='event'", (self.project,))}

    def test_learning_expire_summarises_the_raw_events_it_removes_before_removing_them(self):
        # L10.3 (T7 S3): raw events go, summaries stay, through the owner's real learning-expire.
        start = int(datetime(2026, 10, 1, 9, tzinfo=timezone.utc).timestamp() * 1000)
        for number in range(5):
            self.append(event(f"e{number}", "tool_started", observed_at_ms=start + number), now_ms=start + number)
        self.append(event("o", "tool_output_observed", observed_at_ms=start + 9, run_id="run_2"), now_ms=start + 9)
        self.assertEqual([], self._summaries())
        self.assertGreaterEqual(self.ledger.expire(self.project, now_ms=start + 8 * DAY), 6)
        self.assertEqual(set(), self._raw_ids())
        summaries = self._summaries()
        totals = {}
        for summary in summaries:
            validate_learning_record(summary)
            self.assertEqual("2026-10-01", summary["day"])
            for code, count in summary["event_counts"].items():
                totals[(summary["run_id"], code)] = count
        self.assertEqual({("run_1", "tool_started"): 5, ("run_2", "tool_output_observed"): 1}, totals)

    def test_learning_expire_and_the_sweep_never_summarise_the_same_events_twice(self):
        start = int(datetime(2026, 10, 1, 9, tzinfo=timezone.utc).timestamp() * 1000)
        for number in range(4):
            self.append(event(f"s{number}", "tool_started", observed_at_ms=start + number), now_ms=start + number)
        later = start + 8 * DAY
        self.assertEqual(2, self.ledger.sweep_event_retention(now_ms=later, batch=2))
        self.assertEqual(2, len(self._raw_ids()))
        self.ledger.expire(self.project, now_ms=later)
        self.assertEqual(set(), self._raw_ids())
        self.assertEqual(4, sum(summary["event_count"] for summary in self._summaries()))
        before = self._summaries()
        self.ledger.expire(self.project, now_ms=later)
        self.assertEqual(0, self.ledger.sweep_event_retention(now_ms=later))
        self.assertEqual(before, self._summaries())

    def test_a_summary_is_only_written_with_the_removal_it_describes(self):
        now = 1_000_000_000_000
        for number in range(5):
            self.append(event(f"e{number}", "tool_started", observed_at_ms=now + number), now_ms=now + number)
        # A summary the ledger refuses keeps its events: nothing is removed undescribed.
        with mock.patch.object(learning_store.LearningLedger, "_append_checked_locked", autospec=True,
                               side_effect=LedgerError("summary refused")):
            self.assertEqual(0, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY))
        self.assertEqual(5, self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=? AND record_type='event'", (self.project,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=? AND record_type='event_summary'",
            (self.project,)).fetchone()[0])

    def test_a_failure_part_way_leaves_every_summary_and_event_as_they_were(self):
        # The ledger connection autocommits, so the sweep must own one explicit
        # transaction: a removal that fails in a later group undoes every summary.
        now = 1_000_000_000_000
        for run, count in (("run_1", 3), ("run_2", 3)):
            for number in range(count):
                self.append(event(f"{run}-{number}", "tool_started", observed_at_ms=now + number, run_id=run),
                            now_ms=now + number)
        self.connection.execute("CREATE TEMP TRIGGER stop_removal BEFORE DELETE ON records "
                                "WHEN OLD.record_id='run_2-1' BEGIN SELECT RAISE(ABORT,'stopped'); END").close()
        with self.assertRaises(Exception):
            self.ledger.sweep_event_retention(now_ms=now + 8 * DAY)
        self.assertFalse(self.connection.in_transaction)
        self.assertEqual(6, self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=? AND record_type='event'", (self.project,)).fetchone()[0])
        self.assertEqual(0, self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE project_id=? AND record_type='event_summary'",
            (self.project,)).fetchone()[0])
        self.connection.execute("DROP TRIGGER stop_removal").close()
        self.assertEqual(6, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY))

    def test_an_event_a_stored_file_or_attestation_names_is_left_to_the_owners_expiry(self):
        # Removing it would cascade into the file's registration and leave the file behind.
        now = 1_000_000_000_000
        self.append(event("exported", "tool_started", observed_at_ms=now), now_ms=now)
        self.append(event("plain", "tool_started", observed_at_ms=now), now_ms=now)
        path = self.ledger.write_sanitized_record_file(
            self.project, file_id="event_export", kind="export",
            record=json.loads(self.connection.execute(
                "SELECT content FROM records WHERE record_id='exported'").fetchone()[0]))
        self.assertEqual(1, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY))
        self.assertIsNotNone(self.expiry("exported"))
        self.assertIsNone(self.expiry("plain"))
        self.assertTrue(path.exists())
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM stored_files WHERE file_id='event_export'").fetchone()[0])

    def test_a_malformed_group_does_not_hold_back_the_others(self):
        now = 1_000_000_000_000
        self.append(event("good", "tool_started", observed_at_ms=now, run_id="run_good"), now_ms=now)
        self.append(event("bad", "tool_started", observed_at_ms=now + 1, run_id="run_bad"), now_ms=now + 1)
        real = learning_store.LearningLedger._append_checked_locked

        def refuse_bad(ledger, project_id, checked, **options):
            if checked.get("run_id") == "run_bad":
                raise ContractError("summary refused")
            return real(ledger, project_id, checked, **options)

        with mock.patch.object(learning_store.LearningLedger, "_append_checked_locked", autospec=True,
                               side_effect=refuse_bad):
            self.assertEqual(1, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY))
        self.assertIsNone(self.expiry("good"))
        self.assertIsNotNone(self.expiry("bad"))

    def test_unreadable_rows_never_use_up_a_sweep(self):
        # Rows that cannot be summarised (left for learning-expire) may sort first;
        # the sweep reads past them instead of stalling every project behind them.
        now = 1_000_000_000_000
        sequence = self.connection.execute("SELECT COALESCE(MAX(source_sequence),0)+1 FROM records "
                                           "WHERE project_id=?", (self.project,)).fetchone()[0]
        self.connection.execute(
            "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.project, "early", "event", "app_server", sequence, now, now, now + DAY, b"not json",
             "0" * 64, 0)).close()
        for number in range(5):
            self.append(event(f"good-{number}", "tool_started", observed_at_ms=now + 1 + number),
                        now_ms=now + 1 + number)
        self.assertEqual(1, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY, batch=1))
        while self.ledger.sweep_event_retention(now_ms=now + 8 * DAY, batch=1):
            pass
        self.assertEqual([None] * 5, [self.expiry(f"good-{number}") for number in range(5)])
        self.assertIsNotNone(self.expiry("early"))

    def test_a_database_error_in_one_summary_leaves_only_that_group(self):
        import sqlite3
        now = 1_000_000_000_000
        self.append(event("good", "tool_started", observed_at_ms=now, run_id="run_good"), now_ms=now)
        self.append(event("bad", "tool_started", observed_at_ms=now + 1, run_id="run_bad"), now_ms=now + 1)
        real = learning_store.LearningLedger._append_checked_locked

        def refuse_bad(ledger, project_id, checked, **options):
            if checked.get("run_id") == "run_bad":
                raise sqlite3.OperationalError("summary write failed")
            return real(ledger, project_id, checked, **options)

        with mock.patch.object(learning_store.LearningLedger, "_append_checked_locked", autospec=True,
                               side_effect=refuse_bad):
            self.assertEqual(1, self.ledger.sweep_event_retention(now_ms=now + 8 * DAY))
        self.assertIsNone(self.expiry("good"))
        self.assertIsNotNone(self.expiry("bad"))

    def test_a_summary_record_is_closed_and_consistent(self):
        summary = {"schema_version": 1, "record_type": "event_summary", "record_id": "evsum-" + "a" * 32,
                   "run_id": "run_1", "day": "2025-10-01", "observed_at_ms": 1_759_309_200_100,
                   "first_observed_at_ms": 1_759_309_200_000, "last_observed_at_ms": 1_759_309_200_100,
                   "span_ms": 100, "event_count": 2, "event_counts": {"tool_started": 2},
                   "source_counts": {"hook": 2}}
        validate_learning_record(summary)
        for change in ({"event_count": 3}, {"span_ms": 5}, {"day": "2025-10-02"},
                       {"event_counts": {"not_an_event": 2}}, {"event_counts": {"tool_started": 0}},
                       {"source_counts": {"owner": 2}}, {"extra": 1}):
            with self.subTest(change=sorted(change)), self.assertRaises(ContractError):
                validate_learning_record({**summary, **change})


class CollectorSweepTests(unittest.TestCase):
    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_the_collector_applies_retention_by_itself(self):
        calls = []
        ledger = self.fixture.ledger
        with mock.patch.object(learning_store, "WORKER_RESULT_SWEEP_SECONDS", 0.01), \
                mock.patch.object(ledger, "sweep_event_retention", side_effect=lambda: calls.append(1) or 0):
            server = learning_store.CollectorServer(ledger)
            server.start()
            try:
                end = time.monotonic() + 10
                while len(calls) < 2 and time.monotonic() < end:
                    time.sleep(0.01)
            finally:
                server.stop()
        self.assertGreaterEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
