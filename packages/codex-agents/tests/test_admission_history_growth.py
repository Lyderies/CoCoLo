"""Admission keeps working as provider history grows, and keeps every restriction.

Every observation and admission adds records. Admission reads only the history
that can still change a decision, so an install with months of observations
opens runs as it did on its first day. A restriction that still applies, however
old its record, still denies; and a capture that cannot fit says so instead of
"account unavailable".
"""
import hashlib
import json
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_account_original_open as original_open

from codex_agents.account_api import AccountError, decode_account_response, encode_account_request
from codex_agents.admission_evidence_schema import check_admission_evidence_coverage, install_admission_evidence_schema
from codex_agents.admission_inputs import AdmissionCaptureError, AdmissionCaptureLimits
from codex_agents.learning_contracts import canonical_json

DAY_MS = 24 * 60 * 60 * 1000


class AdmissionHistoryGrowthTests(unittest.TestCase):
    def setUp(self):
        self.open = original_open.OriginalAccountOpenJobTests(methodName="runTest")
        self.open.setUp()
        self.addCleanup(self.open.doCleanups)
        self.ledger, self.jobs = self.open.ledger, self.open.jobs

    def outcome(self, reply):
        """The denial reason of an open, or None when it opened."""
        if reply.reason is not None:
            return reply.reason
        if reply.result["state"] == "denied":
            return reply.result["reason_code"]
        self.assertEqual("opened", reply.result["state"])
        return None

    def copies(self, kind, count, *, age_ms, change=None):
        """Add `count` copies of this project's latest `kind` record, observed `age_ms` and more ago."""
        connection = self.ledger._connection
        template = connection.execute(
            "SELECT project_id, source, content FROM records WHERE record_type=? ORDER BY observed_at_ms DESC LIMIT 1",
            (kind,)).fetchone()
        sequence = connection.execute("SELECT MAX(source_sequence) FROM records").fetchone()[0]
        now = self.ledger._clock_ms()
        for index in range(count):
            record = json.loads(bytes(template["content"]))
            observed = now - age_ms - index
            record.update(record_id=f"{kind}:{hashlib.sha256(f'{kind}-{age_ms}-{index}'.encode()).hexdigest()}",
                          observed_at_ms=observed)
            if change is not None:
                change(record, now)
            content = canonical_json(record)
            sequence += 1
            connection.execute(
                "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
                "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                (template["project_id"], record["record_id"], kind, template["source"], sequence, observed, observed,
                 observed + 90 * DAY_MS, content, hashlib.sha256(content).hexdigest()))
        connection.commit()

    def test_an_open_succeeds_with_much_more_old_history_than_one_capture_holds(self):
        # Two days old: past every freshness and evidence horizon, well within record retention.
        self.copies("provider_billing", 400, age_ms=2 * DAY_MS)
        self.copies("provider_headroom", 400, age_ms=2 * DAY_MS)
        self.copies("provider_identity", 400, age_ms=2 * DAY_MS)
        self.assertIsNone(self.outcome(self.open.pump(self.open.submit())))

    def test_an_old_headroom_record_whose_window_has_not_reset_still_denies(self):
        def exhausted(record, now):
            record["windows"] = [{"resets_at": now + 60 * 60 * 1000, "scope": None, "used_percent": 100.0,
                                  "window_minutes": 10080}]
        self.copies("provider_headroom", 600, age_ms=2 * DAY_MS)
        self.copies("provider_headroom", 1, age_ms=5 * DAY_MS, change=exhausted)
        self.assertEqual("provider_exhausted", self.outcome(self.open.pump(self.open.submit())))

    def test_paid_usage_from_the_last_day_still_denies_among_old_history(self):
        def paid(record, now):
            record["paid_usage_usable"] = True
        self.copies("provider_billing", 600, age_ms=2 * DAY_MS)
        self.copies("provider_billing", 1, age_ms=DAY_MS - 60_000, change=paid)
        self.assertEqual("paid_usage_usable", self.outcome(self.open.pump(self.open.submit())))

    def test_a_capture_that_does_not_fit_is_reported_as_such_and_a_retry_opens_once(self):
        service, _ = self.open.wire_service()
        command = self.open.wire_command()
        raw = encode_account_request(command)
        bindings = "SELECT COUNT(*) FROM host_run_bindings"
        before = self.ledger._connection.execute(bindings).fetchone()[0]
        with (mock.patch.object(self.jobs._views, "capture_admission_inputs",
                                side_effect=AdmissionCaptureError("admission_capture_limit")),
              self.assertRaisesRegex(AccountError, "^admission_capture_limit$")):
            decode_account_response(self.open.pump_wire(service, service.begin(raw, self.open.token)), command)
        opened = decode_account_response(self.open.pump_wire(service, service.begin(raw, self.open.token)), command)
        self.assertEqual("opened", opened["state"])
        self.assertEqual(before + 1, self.ledger._connection.execute(bindings).fetchone()[0])
        again = decode_account_response(self.open.pump_wire(service, service.begin(raw, self.open.token)), command)
        self.assertEqual({**opened, "known": True}, again)
        self.assertEqual(before + 1, self.ledger._connection.execute(bindings).fetchone()[0])


    def captured(self, *, anchor_ms=None):
        """Record ids one capture keeps, at the ledger clock (or against an earlier capture's anchor)."""
        views, ledger = self.jobs._views, self.ledger
        project = self.open.reference.base.scope.project_id
        with ledger._lock:
            snapshot = views._capture_admission_rows_locked(
                project, "codex", views._claim_policy("codex"), AdmissionCaptureLimits(4096, 64 * 1_048_576),
                ledger._current_signer_bytes(), anchor_ms)
        return {row[2] for row in snapshot.rows}, snapshot

    def latest(self, kind):
        """The most recently observed record of `kind`."""
        return self.ledger._connection.execute(
            "SELECT record_id FROM records WHERE record_type=? ORDER BY observed_at_ms DESC, rowid DESC LIMIT 1",
            (kind,)).fetchone()[0]

    def oldest(self, kind):
        return self.ledger._connection.execute(
            "SELECT record_id FROM records WHERE record_type=? ORDER BY observed_at_ms, rowid LIMIT 1",
            (kind,)).fetchone()[0]

    def test_the_first_passing_claude_decision_of_each_account_is_kept_at_any_age(self):
        def claude_pass(record, now):
            record.update(provider="claude", decision="pass", account_fingerprint="c" * 64)
        self.copies("admission_decision", 1, age_ms=30 * DAY_MS, change=claude_pass)
        first = self.oldest("admission_decision")
        self.copies("admission_decision", 1, age_ms=20 * DAY_MS, change=claude_pass)
        second = self.ledger._connection.execute(
            "SELECT record_id FROM records WHERE record_type='admission_decision' AND observed_at_ms<? "
            "ORDER BY observed_at_ms DESC LIMIT 1", (self.ledger._clock_ms() - 19 * DAY_MS,)).fetchone()[0]
        kept, _ = self.captured()
        self.assertIn(first, kept)
        self.assertNotIn(second, kept)

    def test_claudes_latest_older_billing_and_headroom_per_account_are_kept(self):
        def claude(record, now):
            record.update(provider="claude", account_fingerprint="d" * 64)
        for kind in ("provider_billing", "provider_headroom"):
            with self.subTest(kind=kind):
                self.copies(kind, 3, age_ms=10 * DAY_MS, change=claude)
                newest = self.ledger._connection.execute(
                    "SELECT record_id FROM records WHERE record_type=? AND observed_at_ms<? "
                    "ORDER BY observed_at_ms DESC LIMIT 1", (kind, self.ledger._clock_ms() - 9 * DAY_MS)).fetchone()[0]
                kept, _ = self.captured()
                self.assertIn(newest, kept)
                older = {row[0] for row in self.ledger._connection.execute(
                    "SELECT record_id FROM records WHERE record_type=? AND observed_at_ms<? AND record_id<>?",
                    (kind, self.ledger._clock_ms() - 9 * DAY_MS, newest))}
                self.assertTrue(older)
                self.assertFalse(older & kept)

    def test_a_recapture_with_a_clock_earlier_than_its_anchor_is_refused(self):
        _, snapshot = self.captured()
        with self.assertRaisesRegex(AdmissionCaptureError, "^admission_capture_unavailable$"):
            self.captured(anchor_ms=snapshot.captured_at_ms + 60_000)
        again, later = self.captured(anchor_ms=snapshot.anchor_ms)
        self.assertEqual(snapshot.anchor_ms, later.anchor_ms)

    def test_history_keeps_growing_and_every_open_still_fits(self):
        for _ in range(3):
            self.copies("provider_billing", 300, age_ms=3 * DAY_MS + _ * DAY_MS)
            self.copies("provider_headroom", 300, age_ms=3 * DAY_MS + _ * DAY_MS)
            kept, _snapshot = self.captured()
            self.assertLess(len(kept), 64)
        self.assertIsNone(self.outcome(self.open.pump(self.open.submit())))

    def test_the_upgrade_fills_the_index_exactly_as_the_trigger_does(self):
        def exhausted(record, now):
            record["windows"] = [{"resets_at": now + 60 * 60 * 1000, "scope": None, "used_percent": 100.0,
                                  "window_minutes": 10080}]
        self.copies("provider_headroom", 50, age_ms=5 * DAY_MS, change=exhausted)
        self.copies("provider_billing", 50, age_ms=5 * DAY_MS)
        connection = self.ledger._connection
        written = connection.execute("SELECT * FROM admission_evidence ORDER BY project_id,record_id").fetchall()
        self.assertTrue(check_admission_evidence_coverage(connection))
        connection.execute("BEGIN")
        connection.execute("DROP TRIGGER admission_evidence_insert")
        connection.execute("DROP TABLE admission_evidence")
        install_admission_evidence_schema(connection)
        connection.execute("COMMIT")
        self.assertEqual([tuple(row) for row in written], [tuple(row) for row in connection.execute(
            "SELECT * FROM admission_evidence ORDER BY project_id,record_id")])
        self.assertEqual("provider_exhausted", self.outcome(self.open.pump(self.open.submit())))


if __name__ == "__main__":
    unittest.main()
