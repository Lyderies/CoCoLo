"""Runs an earlier version left in the records must not hold capacity, or the learning move, for ever.

Upgrading a 0.2.x ledger turns every historical run into an unreleased
`legacy_unknown` reservation (there is no proof its cleanup was confirmed). Those
rows had no deadline, so nothing could ever count them as finished: two of them
blocked every new child, and any of them blocked the move of the project's
learning history (`migration_not_drained`). A 0.2.x child ran inside its own CLI
command under a route deadline of at most six hours, so one claimed more than
LEGACY_RUN_MAX_MS ago cannot still be running; it stops counting, while its row
stays unreleased, exactly like a crashed claim of this version.
"""

import json
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_store as legacy
from tests.test_account_launch_claims import downgrade_ledger_to_six
from codex_agents import active_run_capacity, learning_store
from codex_agents.collector_client import CollectorClient, CollectorError

HOUR = 60 * 60 * 1000


class LegacyRunsFixture:
    """A ledger upgraded from 0.2.x with runs of chosen ages."""

    def setUp(self):
        self.fixture = legacy.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.project_a_id

    def upgrade_with_runs(self, *ages_ms, client_version="codex-host-v1"):
        ledger = self.fixture.ledger
        downgrade_ledger_to_six(ledger)
        now = time.time_ns() // 1_000_000
        for number, age in enumerate(ages_ms):
            ledger._connection.execute(
                "INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,"
                "route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.project, f"{number:064x}", f"legacy-run-{number}", "b" * 64, "c" * 64, "d" * 64,
                 "e" * 64, client_version, now - age, now - age + 90 * 24 * HOUR,
                 "consumed" if number % 2 else "pending")).close()
        ledger.close()
        self.fixture.ledger = learning_store.LearningLedger(
            self.fixture.state, client_capabilities=self.fixture.clients,
            allow_schema_upgrade=True, backup_before_upgrade=True)
        return self.fixture.ledger

    def counted(self, ledger):
        crashed, parameters = active_run_capacity.crashed_claim(
            "", ledger._clock_ms(), ledger._capacity_clock_reference())
        return ledger._connection.execute(
            f"SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL AND NOT {crashed}",
            parameters).fetchone()[0]


class LegacyCapacityUpgradeTests(LegacyRunsFixture, unittest.TestCase):
    def test_old_runs_from_an_earlier_version_neither_hold_capacity_nor_block_the_move(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, 3 * 24 * HOUR, 2 * learning_store.LEGACY_RUN_MAX_MS)
        rows = ledger._connection.execute(
            "SELECT COUNT(*) FROM active_run_capacity WHERE kind='legacy_unknown' AND released_at_ms IS NULL"
        ).fetchone()[0]
        self.assertEqual(3, rows)  # Kept, unreleased: no cleanup is invented.
        self.assertEqual(0, self.counted(ledger))
        self.assertTrue(ledger.drained())

    def test_a_recent_run_from_an_earlier_version_still_counts_until_it_cannot_be_running(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, HOUR)
        self.assertEqual(1, self.counted(ledger))
        self.assertFalse(ledger.drained())
        later = ledger._clock_ms() + learning_store.LEGACY_RUN_MAX_MS
        ledger._clock_ms = lambda: later
        self.assertEqual(0, self.counted(ledger))
        self.assertTrue(ledger.drained())

    def test_the_bound_is_a_cautious_day(self):
        # Not a proof: a native 0.2.x child had no enforced deadline, and a bridged one
        # could outlive a dispatch process that was killed outright.
        self.assertEqual(24 * HOUR, learning_store.LEGACY_RUN_MAX_MS)

    def test_bridged_and_native_runs_from_an_earlier_version_count_for_a_day(self):
        for client_version in ("bridge-v1", "codex-host-v1"):
            with self.subTest(client_version=client_version):
                if client_version != "bridge-v1":
                    self.fixture.tearDown()
                    self.setUp()
                ledger = self.upgrade_with_runs(21 * HOUR, client_version=client_version)
                self.assertEqual(1, self.counted(ledger))
                later = ledger._clock_ms() + 3 * HOUR + 60_000
                ledger._clock_ms = lambda: later
                self.assertEqual(0, self.counted(ledger))

    def test_replacing_the_collector_does_not_wait_for_runs_an_earlier_version_left(self):
        # They ran inside their own 0.2.x commands, never under a collector or a
        # project service, so stopping the collector cannot affect them.
        ledger = self.upgrade_with_runs(HOUR)
        self.assertFalse(ledger.drained())
        self.assertTrue(ledger.drained(children_only=True))
        server = learning_store.CollectorServer.__new__(learning_store.CollectorServer)
        server.ledger = ledger
        server._stop = mock.Mock()
        frame = json.dumps({"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "shutdown_if_drained"})

        class Connection:
            sent = [(frame + "\n").encode(), b""]

            def settimeout(self, seconds):
                pass

            def recv(self, size):
                return self.sent.pop(0)
        self.assertEqual({"ok": True}, server._handle(Connection()))
        server._stop.set.assert_called_once()



class EarlierVersionRunsStatusTests(LegacyRunsFixture, unittest.TestCase):
    """Status says how many slots runs from an earlier version hold, and until when (F2)."""

    def test_the_ledger_says_how_many_earlier_runs_hold_slots_and_when_the_last_stops(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, HOUR, 3 * HOUR)
        claimed = sorted(row[0] for row in ledger._connection.execute(
            "SELECT claimed_at_ms FROM active_run_capacity WHERE kind='legacy_unknown'"))
        self.assertEqual({"holding": 2, "finished_by_ms": claimed[-1] + learning_store.LEGACY_RUN_MAX_MS},
                         ledger.earlier_version_runs())

    def test_with_no_earlier_runs_holding_a_slot_there_is_no_time(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR)
        self.assertEqual({"holding": 0, "finished_by_ms": None}, ledger.earlier_version_runs())

    def test_the_collector_reports_it_with_the_learning_move_state(self):
        answer = {"ok": True, "moved": False, "has_learning": True, "drained": False, "activation_pending": False,
                  "earlier_version_runs": {"holding": 2, "finished_by_ms": 1_800_000_000_000}}
        client = CollectorClient(Path("/srv/collector.sock"))
        with mock.patch.object(client, "_request", return_value=answer):
            self.assertEqual({"holding": 2, "finished_by_ms": 1_800_000_000_000},
                             client.learning_migration(Path("/srv/project"))["earlier_version_runs"])
        older = {key: value for key, value in answer.items() if key != "earlier_version_runs"}
        with mock.patch.object(client, "_request", return_value=older):
            # A collector of an earlier release does not report it: unknown, not an error.
            self.assertIsNone(client.learning_migration(Path("/srv/project"))["earlier_version_runs"])
        for bad in ({"holding": -1, "finished_by_ms": None}, {"holding": 1, "finished_by_ms": None},
                    {"holding": True, "finished_by_ms": None}, {"holding": 0}, "two"):
            with self.subTest(bad=bad), mock.patch.object(
                    client, "_request", return_value={**answer, "earlier_version_runs": bad}), \
                    self.assertRaises(CollectorError):
                client.learning_migration(Path("/srv/project"))


if __name__ == "__main__":
    unittest.main()


class EarlierVersionRunsStatusTests(LegacyRunsFixture, unittest.TestCase):
    """Status says how many slots runs from an earlier version hold, and until when (F2)."""

    def test_the_ledger_says_how_many_earlier_runs_hold_slots_and_when_the_last_stops(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR, 3 * HOUR, 20 * HOUR)
        claimed = sorted(row[0] for row in ledger._connection.execute(
            "SELECT claimed_at_ms FROM active_run_capacity WHERE kind='legacy_unknown'"))
        self.assertEqual({"holding": 2, "finished_by_ms": claimed[-1] + learning_store.LEGACY_RUN_MAX_MS},
                         ledger.earlier_version_runs())

    def test_with_no_earlier_runs_holding_a_slot_there_is_no_time(self):
        ledger = self.upgrade_with_runs(30 * 24 * HOUR)
        self.assertEqual({"holding": 0, "finished_by_ms": None}, ledger.earlier_version_runs())

    def test_the_collector_reports_it_with_the_learning_move_state(self):
        answer = {"ok": True, "moved": False, "has_learning": True, "drained": False, "activation_pending": False,
                  "earlier_version_runs": {"holding": 2, "finished_by_ms": 1_800_000_000_000}}
        client = CollectorClient(Path("/srv/collector.sock"))
        with mock.patch.object(client, "_request", return_value=answer):
            self.assertEqual({"holding": 2, "finished_by_ms": 1_800_000_000_000},
                             client.learning_migration(Path("/srv/project"))["earlier_version_runs"])
        for bad in ({"holding": -1, "finished_by_ms": None}, {"holding": 1, "finished_by_ms": None},
                    {"holding": True, "finished_by_ms": None}, {"holding": 0}, "two"):
            with self.subTest(bad=bad), mock.patch.object(
                    client, "_request", return_value={**answer, "earlier_version_runs": bad}), \
                    self.assertRaises(CollectorError):
                client.learning_migration(Path("/srv/project"))
