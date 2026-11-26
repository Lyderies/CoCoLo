"""TORN-1: the fence's epoch read happens under the connection's shared lock (deterministic, no race luck)."""
import sqlite3
import threading
import types
import unittest
from unittest import mock

import tests.support  # noqa: F401
from test_framework import requirements
from tests.test_synchronized_races import DuplicateHostResultRaceTests

from codex_agents import store_fence

def spied(spy):
    """store_fence's sqlite3 name with Connection.execute replaced (the C type itself is immutable)."""
    return mock.patch.object(store_fence, 'sqlite3', types.SimpleNamespace(
        Connection=types.SimpleNamespace(execute=spy), Error=sqlite3.Error, OperationalError=sqlite3.OperationalError))


EPOCH_SQL = 'SELECT epoch,new_digest FROM runtime_takeover WHERE singleton=1'


class TornReadTests(DuplicateHostResultRaceTests):
    @requirements('TORN-1', why_not_scenario='A deterministic lock-ownership contract on one connection; the real race is stress-proven in the unit notes.')
    def test_TORN_1_the_epoch_read_runs_under_the_connection_lock(self):
        seen = []
        real = sqlite3.Connection.execute

        def spy(connection, sql, *args):
            if sql == EPOCH_SQL:
                seen.append(self.ledger._lock._is_owned())
            return real(connection, sql, *args)

        with spied(spy):
            self.ledger._fence.check(self.ledger._connection)
        self.assertEqual([True], seen)

    @requirements('TORN-1', why_not_scenario='A deterministic lock-ownership contract on one connection; the real race is stress-proven in the unit notes.')
    def test_TORN_1_a_check_from_another_thread_waits_for_a_held_lock(self):
        held, release, done = threading.Event(), threading.Event(), threading.Event()
        reads = []
        real = sqlite3.Connection.execute

        def spy(connection, sql, *args):
            if sql == EPOCH_SQL:
                reads.append(release.is_set())  # was the holder still inside its lock?
            return real(connection, sql, *args)

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(30)

        def checker():
            self.ledger._fence.check(self.ledger._connection)
            done.set()
        first = threading.Thread(target=holder, daemon=True)
        first.start()
        self.addCleanup(first.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        with spied(spy):
            second = threading.Thread(target=checker, daemon=True)
            second.start()
            self.assertFalse(done.wait(0.3))
            release.set()
            self.assertTrue(done.wait(5))
            second.join(5)
        self.assertEqual([True], reads)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(TornReadTests):
        if name in TornReadTests.__dict__:
            suite.addTest(TornReadTests(name))
    return suite


if __name__ == '__main__':
    unittest.main()
