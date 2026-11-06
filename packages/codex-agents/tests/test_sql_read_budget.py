from __future__ import annotations

import os
import sqlite3
import threading
import tempfile
import time
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT

from codex_agents.sql_read_budget import (
    MAX_SQL_READ_CALLBACKS, MAX_SQL_READ_CURSORS, MAX_SQL_READ_DURATION_MS,
    MAX_SQL_READ_VALUE_BYTES, SQLReadConnection, SQLReadError, SQLReadLimits,
)

LONG_QUERY = "WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM numbers WHERE n<100000) SELECT sum(n) FROM numbers"


class SQLReadBudgetTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", factory=SQLReadConnection, isolation_level=None,
                                          check_same_thread=False)
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE data(value BLOB)")
        self.connection.execute("INSERT INTO data VALUES(?)", (b"CANARY" * 1000,))
        self.limits = SQLReadLimits(10, 1000, 65_536)

    def assert_fixed(self, reason, action):
        with self.assertRaises(SQLReadError) as caught:
            action()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def run_query(self, sql="SELECT 1", limits=None):
        return self.connection.read_call(self.limits if limits is None else limits,
                                         lambda: self.connection.execute(sql).fetchall())

    def settings(self):
        return (self.connection.execute("PRAGMA query_only").fetchone()[0],
                self.connection.execute("PRAGMA busy_timeout").fetchone()[0],
                self.connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))

    def test_limits_are_explicit_exact_finite_integers(self):
        for values in ((True, 1, 1), (1, False, 1), (1, 1, True), (0, 1, 1), (1, 0, 1), (1, 1, 0),
                       (1.0, 1, 1), (1, "CANARY", 1), (MAX_SQL_READ_CALLBACKS + 1, 1, 1),
                       (1, MAX_SQL_READ_DURATION_MS + 1, 1), (1, 1, MAX_SQL_READ_VALUE_BYTES + 1)):
            with self.subTest(values=values):
                self.assert_fixed("sql_read_invalid_limits", lambda: SQLReadLimits(*values))
        self.assert_fixed("sql_read_invalid_limits", lambda: self.run_query(limits={}))
        self.assertEqual("sql_read_unavailable", str(SQLReadError("CANARY-PRIVATE")))

    def test_small_read_restores_exact_previous_settings_and_closes_cursors(self):
        self.connection.execute("PRAGMA busy_timeout=123")
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100_000)
        before = self.settings()
        cursors = []

        def operation():
            self.assertEqual((1, 0, 65_536), self.settings())
            cursor = self.connection.execute("SELECT value FROM data")
            cursors.append(cursor)
            self.assertEqual(b"CANARY" * 1000, cursor.fetchone()[0])

        self.connection.read_call(self.limits, operation)
        self.assertEqual(before, self.settings())
        with self.assertRaises(sqlite3.ProgrammingError):
            cursors[0].fetchone()
        self.connection.execute("INSERT INTO data VALUES(?)", (b"AFTER",))
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])

    def test_existing_value_limit_is_never_widened(self):
        self.connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 10_000)
        observed = self.connection.read_call(self.limits, lambda: self.connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
        self.assertEqual(10_000, observed)
        self.assertEqual(10_000, self.connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))

    def test_existing_query_only_mode_remains_enabled(self):
        self.connection.execute("PRAGMA query_only=1")
        self.assertEqual([(1,)], self.run_query())
        self.assertEqual(1, self.connection.execute("PRAGMA query_only").fetchone()[0])

    def test_foreign_progress_callback_is_not_replaced_or_lost(self):
        calls = []
        self.connection.set_progress_handler(lambda: calls.append(1) or 0, 256)
        self.assert_fixed("sql_read_unavailable", self.run_query)
        self.connection.execute(LONG_QUERY).fetchall()
        self.assertGreater(len(calls), 0)
        self.connection.set_progress_handler(None, 0)
        self.assertEqual([(1,)], self.run_query())

    def test_large_scan_aborts_then_another_scope_succeeds(self):
        before = self.settings()
        self.assert_fixed("sql_read_work_limit", lambda: self.run_query(LONG_QUERY))
        self.assertEqual(before, self.settings())
        self.assertEqual([(1,)], self.run_query())

    def test_callbacks_accumulate_across_statements_and_latch_refusal(self):
        observed = []

        def operation():
            scope = self.connection._sql_scope
            first = "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<50) SELECT sum(x) FROM n"
            self.connection.execute(first).fetchall()
            observed.append((scope, scope.callbacks))
            self.connection.execute(LONG_QUERY).fetchall()

        self.assert_fixed("sql_read_work_limit", lambda: self.connection.read_call(self.limits, operation))
        self.assertEqual(1, len(observed))
        scope, first_count = observed[0]
        self.assertGreater(first_count, 0)
        self.assertEqual(self.limits.max_callbacks, scope.callbacks)
        self.assertEqual("sql_read_work_limit", scope.reason)
        self.assertEqual(1, scope.progress())

    def test_write_refusal_has_no_data_change_and_restores_owner_write_access(self):
        self.assert_fixed("sql_read_unavailable", lambda: self.run_query("INSERT INTO data VALUES('CANARY')"))
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])
        self.connection.execute("INSERT INTO data VALUES('AFTER')")

    def test_outer_transaction_and_nested_scope_refuse_without_resetting_outer_state(self):
        self.connection.execute("BEGIN")
        try:
            self.assert_fixed("sql_read_unavailable", self.run_query)
            self.assertTrue(self.connection.in_transaction)
        finally:
            self.connection.rollback()
        observations = []

        def operation():
            outer = self.connection._sql_scope
            self.assert_fixed("sql_read_unavailable", self.run_query)
            observations.append((outer, self.connection._sql_scope,
                                 self.connection.execute("PRAGMA query_only").fetchone()[0]))

        self.connection.read_call(self.limits, operation)
        self.assertEqual(1, len(observations))
        self.assertIs(observations[0][0], observations[0][1])
        self.assertEqual(1, observations[0][2])

    def test_scope_cannot_replace_the_owned_handler_or_run_script_or_batch(self):
        def operation():
            for action in (lambda: self.connection.set_progress_handler(None, 0),
                           lambda: self.connection.executescript("SELECT 1;"),
                           lambda: self.connection.executemany("SELECT ?", [(1,)])):
                self.assert_fixed("sql_read_unavailable", action)

        self.connection.read_call(self.limits, operation)

    def test_monotonic_deadline_without_a_progress_callback_is_checked_at_exit(self):
        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=[0, 0, 0, 1_000_000_000]):
            self.assert_fixed("sql_read_deadline", self.run_query)
        self.assertEqual([(1,)], self.run_query())

    def test_clock_failure_and_regression_are_fixed_and_settings_restore(self):
        before = self.settings()
        for values in ([RuntimeError("CANARY-PATH")], [2, 1], [0, 0, RuntimeError("CANARY-PATH")]):
            with self.subTest(values=len(values)), mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=values):
                self.assert_fixed("sql_read_unavailable", self.run_query)
            self.assertEqual(before, self.settings())

    def test_callback_clock_failure_regression_and_deadline_abort_actual_scans(self):
        before = self.settings()
        for values, reason in (([0, 0, 0, RuntimeError("CANARY-PATH")], "sql_read_unavailable"),
                               ([2, 2, 2, 1], "sql_read_unavailable"),
                               ([0, 0, 0, 1_000_000_000], "sql_read_deadline")):
            with self.subTest(reason=reason), mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=values):
                self.assert_fixed(reason, lambda: self.run_query(LONG_QUERY))
            self.assertEqual(before, self.settings())

    def test_partial_setup_failure_restores_settings_before_any_operation(self):
        before = self.settings()
        original = self.connection._pragma_integer
        called = []

        def fail_after_query_only(name, value=None):
            result = original(name, value)
            if name == "query_only" and value == 1:
                raise RuntimeError("CANARY-PATH")
            return result

        with mock.patch.object(self.connection, "_pragma_integer", side_effect=fail_after_query_only):
            self.assert_fixed("sql_read_unavailable", lambda: self.connection.read_call(
                self.limits, lambda: called.append(True)))
        self.assertEqual([], called)
        self.assertEqual(before, self.settings())
        self.assertEqual([(1,)], self.run_query())

    def test_locked_database_refuses_without_the_original_five_second_busy_wait(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            path = os.path.join(directory, "owned-lock-fixture.sqlite3")
            owner = sqlite3.connect(path, factory=SQLReadConnection, isolation_level=None, timeout=5)
            locker = sqlite3.connect(path, isolation_level=None)
            try:
                owner.execute("CREATE TABLE example(value INTEGER)")
                owner.execute("INSERT INTO example VALUES(1)")
                locker.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                self.assert_fixed("sql_read_busy", lambda: owner.read_call(
                    self.limits, lambda: owner.execute("SELECT * FROM example").fetchall()))
                elapsed = time.monotonic() - started
                locker.rollback()
                self.assertLess(elapsed, 2)
                self.assertEqual(5000, owner.execute("PRAGMA busy_timeout").fetchone()[0])
                self.assertEqual([(1,)], owner.read_call(
                    self.limits, lambda: owner.execute("SELECT * FROM example").fetchall()))
            finally:
                locker.close()
                owner.close()

    def test_keyboard_interrupt_propagates_after_restoration(self):
        before = self.settings()

        def operation():
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.connection.read_call(self.limits, operation)
        self.assertEqual(before, self.settings())
        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.run_query()
        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=[0, 0, 0, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_query(LONG_QUERY)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(before, self.settings())

    def test_latched_failure_prevents_a_subsequent_short_statement(self):
        statements = []
        self.connection.set_trace_callback(statements.append)

        def operation():
            try:
                self.connection.execute(LONG_QUERY).fetchall()
            except sqlite3.DatabaseError:
                pass
            self.connection.execute("SELECT 'CANARY-LATE'").fetchone()

        try:
            self.assert_fixed("sql_read_work_limit", lambda: self.connection.read_call(self.limits, operation))
        finally:
            self.connection.set_trace_callback(None)
        self.assertFalse(any("CANARY-LATE" in statement for statement in statements))
        self.assertEqual([(1,)], self.run_query())

    def test_backend_errors_and_oversized_values_are_sanitized(self):
        self.assert_fixed("sql_read_unavailable", lambda: self.run_query("SELECT * FROM CANARY_PRIVATE_TABLE"))
        self.assert_fixed("sql_read_value_limit", lambda: self.run_query("SELECT value FROM data",
                          limits=SQLReadLimits(10, 1000, 1000)))
        self.assertEqual([(1,)], self.run_query())

    def test_cursor_capacity_is_bounded_and_scope_owned_cursors_close(self):
        cursors = []

        def operation():
            for _ in range(MAX_SQL_READ_CURSORS + 1):
                cursors.append(self.connection.cursor())

        self.assert_fixed("sql_read_work_limit", lambda: self.connection.read_call(self.limits, operation))
        self.assertEqual(MAX_SQL_READ_CURSORS, len(cursors))
        for cursor in cursors:
            with self.assertRaises(sqlite3.ProgrammingError):
                cursor.execute("SELECT 1")

    def test_restoration_failure_closes_the_exact_connection_and_refuses_reuse(self):
        with mock.patch.object(self.connection, "_restore_read_settings", side_effect=RuntimeError("CANARY-PATH")):
            self.assert_fixed("sql_read_unavailable", self.run_query)
        self.assert_fixed("sql_read_unavailable", lambda: self.connection.execute("SELECT 1"))
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(self.connection, "SELECT 1")

    def test_wrong_process_and_thread_refuse_before_sqlite_access(self):
        with mock.patch("codex_agents.sql_read_budget.os.getpid", return_value=os.getpid() + 1):
            self.assert_fixed("sql_read_wrong_owner", self.run_query)
        failures = []

        def attempt():
            try:
                self.run_query()
            except Exception as error:
                failures.append(error)

        thread = threading.Thread(target=attempt)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(failures))
        self.assertIs(type(failures[0]), SQLReadError)
        self.assertEqual("sql_read_wrong_owner", failures[0].reason_code)
