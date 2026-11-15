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

    def test_transaction_read_preserves_outer_writes_settings_and_caller_rollback(self):
        before = self.settings()
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("INSERT INTO data VALUES(?)", (b"PENDING",))
        result = self.connection.transaction_read_call(
            self.limits, lambda: self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0],
        )
        self.assertEqual(2, result)
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(before, self.settings())
        self.connection.rollback()
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])

    def test_transaction_read_fault_does_not_commit_or_rollback_outer_writes(self):
        before = self.settings()
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("INSERT INTO data VALUES(?)", (b"PENDING",))
        self.assert_fixed("sql_read_work_limit", lambda: self.connection.transaction_read_call(
            self.limits, lambda: self.connection.execute(LONG_QUERY).fetchall(),
        ))
        self.assertTrue(self.connection.in_transaction)
        self.assertEqual(before, self.settings())
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])
        self.connection.rollback()
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])

    def test_transaction_read_requires_outer_transaction_and_cannot_write(self):
        self.assert_fixed("sql_read_unavailable", lambda: self.connection.transaction_read_call(self.limits, lambda: None))
        self.connection.execute("BEGIN IMMEDIATE")
        self.assert_fixed("sql_read_unavailable", self.run_query)
        self.assert_fixed("sql_read_unavailable", lambda: self.connection.transaction_read_call(
            self.limits, lambda: self.connection.execute("DELETE FROM data"),
        ))
        self.assertTrue(self.connection.in_transaction)
        self.connection.rollback()

    def test_transaction_read_cannot_end_or_reconfigure_the_outer_transaction(self):
        for command in ("COMMIT", "ROLLBACK", "SAVEPOINT nested", "PRAGMA query_only=OFF", "commit", "rollback", "context"):
            with self.subTest(command=command):
                connection = sqlite3.connect(":memory:", factory=SQLReadConnection, isolation_level=None)
                self.addCleanup(connection.close)
                connection.execute("CREATE TABLE data(value)")
                connection.execute("INSERT INTO data VALUES(1)")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO data VALUES(2)")

                def action():
                    if command in {"commit", "rollback"}:
                        getattr(connection, command)()
                    elif command == "context":
                        with connection:
                            pass
                    else:
                        connection.execute(command)

                self.assert_fixed("sql_read_unavailable", lambda: connection.transaction_read_call(self.limits, action))
                self.assertTrue(connection.in_transaction)
                self.assertEqual(2, connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])
                connection.rollback()
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM data").fetchone()[0])

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
        native = []

        def operation():
            self.assertEqual((1, 0, 65_536), self.settings())
            cursor = self.connection.execute("SELECT value FROM data")
            cursors.append(cursor)
            native.append(self.connection._sql_scope.cursors[-1])
            self.assertEqual(b"CANARY" * 1000, cursor.fetchone()[0])

        self.connection.read_call(self.limits, operation)
        self.assertEqual(before, self.settings())
        self.assert_fixed("sql_read_unavailable", cursors[0].fetchone)
        with self.assertRaises(sqlite3.ProgrammingError):
            native[0].fetchone()
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
        completed, observed = [], []

        def operation():
            value = self.connection.execute("SELECT 1").fetchall()
            observed.append((value, self.connection._sql_scope.callbacks))
            completed.append(True)

        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=lambda: 1_000_000_000 if completed else 0):
            self.assert_fixed("sql_read_deadline", lambda: self.connection.read_call(self.limits, operation))
        self.assertEqual([([(1,)], 0)], observed)
        self.assertEqual([(1,)], self.run_query())

    def test_clock_failure_and_regression_are_fixed_and_settings_restore(self):
        before = self.settings()
        for values in ([RuntimeError("CANARY-PATH")], [2, 1], [0, 0, RuntimeError("CANARY-PATH")]):
            with self.subTest(values=len(values)), mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=values):
                self.assert_fixed("sql_read_unavailable", self.run_query)
            self.assertEqual(before, self.settings())

    def test_callback_clock_failure_regression_and_deadline_abort_actual_scans(self):
        before = self.settings()
        for kind, reason in (("failure", "sql_read_unavailable"), ("regression", "sql_read_unavailable"),
                             ("deadline", "sql_read_deadline")):
            observed = []

            def clock():
                scope = self.connection._sql_scope
                if scope is not None and scope.callbacks:
                    observed.append(scope.callbacks)
                    if kind == "failure":
                        raise RuntimeError("CANARY-PATH")
                    return 1 if kind == "regression" else 1_000_000_000
                return 2 if kind == "regression" else 0

            with self.subTest(kind=kind), mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=clock):
                self.assert_fixed(reason, lambda: self.run_query(LONG_QUERY))
            self.assertTrue(observed)
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
        observed = []

        def callback_clock():
            scope = self.connection._sql_scope
            if scope is not None and scope.callbacks:
                observed.append(scope.callbacks)
                raise KeyboardInterrupt()
            return 0

        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=callback_clock):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.run_query(LONG_QUERY)
        self.assertIsNone(caught.exception.__context__)
        self.assertTrue(observed)
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
        native = []

        def operation():
            try:
                for _ in range(MAX_SQL_READ_CURSORS + 1):
                    cursors.append(self.connection.execute("SELECT 1"))
            finally:
                native.extend(self.connection._sql_scope.cursors)

        self.assert_fixed("sql_read_work_limit", lambda: self.connection.read_call(self.limits, operation))
        self.assertEqual(MAX_SQL_READ_CURSORS, len(cursors))
        for cursor in cursors:
            self.assert_fixed("sql_read_unavailable", cursor.fetchone)
        self.assertEqual(MAX_SQL_READ_CURSORS, len(native))
        for cursor in native:
            with self.assertRaises(sqlite3.ProgrammingError):
                cursor.execute("SELECT 1")

    def test_a_failure_in_each_actual_restore_step_closes_the_connection(self):
        for kind in ("query_only", "busy_timeout", "value_limit"):
            with self.subTest(kind=kind):
                connection = sqlite3.connect(":memory:", factory=SQLReadConnection, isolation_level=None)
                try:
                    original_pragma, original_limit = connection._pragma_integer, connection.setlimit
                    prior_limit = connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH)
                    reached = []

                    def pragma(name, value=None):
                        if (kind == "query_only" and name == kind and value == 0
                                or kind == "busy_timeout" and name == kind and value == 5000):
                            reached.append(name)
                            raise sqlite3.OperationalError("CANARY-RESTORE")
                        return original_pragma(name, value)

                    def limit(category, value):
                        if kind == "value_limit" and category == sqlite3.SQLITE_LIMIT_LENGTH and value == prior_limit:
                            reached.append(kind)
                            return original_limit(category, value - 1)
                        return original_limit(category, value)

                    with mock.patch.object(connection, "_pragma_integer", side_effect=pragma), \
                            mock.patch.object(connection, "setlimit", side_effect=limit):
                        self.assert_fixed("sql_read_unavailable", lambda: connection.read_call(
                            self.limits, lambda: connection.execute("SELECT 1").fetchall()))
                    self.assertEqual([kind], reached)
                    self.assertTrue(connection._sql_unusable)
                    with self.assertRaises(sqlite3.ProgrammingError):
                        sqlite3.Connection.execute(connection, "SELECT 1")
                finally:
                    connection.close()

    def test_fetch_views_check_scope_and_owner_before_backend_access(self):
        views, foreign_errors, owner_rows = [], [], []

        def operation():
            view = self.connection.execute("SELECT 1 UNION ALL SELECT 2")
            views.append(view)

            def foreign_fetch():
                try:
                    view.fetchone()
                except Exception as error:
                    foreign_errors.append(error)

            thread = threading.Thread(target=foreign_fetch)
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            with mock.patch("codex_agents.sql_read_budget.os.getpid", return_value=os.getpid() + 1):
                self.assert_fixed("sql_read_wrong_owner", view.fetchone)
            owner_rows.extend(view.fetchall())

        self.connection.read_call(self.limits, operation)
        self.assertEqual(1, len(foreign_errors))
        self.assertIs(type(foreign_errors[0]), SQLReadError)
        self.assertEqual("sql_read_wrong_owner", foreign_errors[0].reason_code)
        self.assertEqual([(1,), (2,)], owner_rows)
        self.assert_fixed("sql_read_unavailable", views[0].fetchone)

    def test_ordinary_legacy_cursor_statements_remain_available_outside_a_budget(self):
        cursor = self.connection.cursor()
        try:
            self.assertIsInstance(cursor, sqlite3.Cursor)
            self.assertEqual([(1,)], cursor.execute("SELECT 1").fetchall())
            cursor.executescript("CREATE TABLE legacy(value INTEGER);")
            cursor.executemany("INSERT INTO legacy VALUES(?)", [(1,), (2,)])
            self.assertEqual([(1,), (2,)], cursor.execute("SELECT value FROM legacy").fetchall())
        finally:
            cursor.close()
        self.assertEqual([(1,)], self.run_query())

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

    def test_returned_cursor_cannot_execute_after_a_caught_scan_abort(self):
        statements, seen = [], []
        self.connection.set_trace_callback(statements.append)

        def operation():
            view = self.connection.execute("SELECT 1")
            try:
                self.connection.execute(LONG_QUERY).fetchall()
            except sqlite3.DatabaseError:
                pass
            seen.extend(view.execute("SELECT 'CANARY-LATE-CURSOR'").fetchall())

        try:
            self.assert_fixed("sql_read_work_limit", lambda: self.connection.read_call(self.limits, operation))
        finally:
            self.connection.set_trace_callback(None)
        self.assertEqual([], seen)
        self.assertFalse(any("CANARY-LATE-CURSOR" in statement for statement in statements))

    def test_budget_refuses_cursor_creation_and_cursor_statement_execution(self):
        for operation in (lambda: self.connection.cursor(),
                          lambda: self.connection.execute("SELECT 1").execute("SELECT 2").fetchall()):
            with self.subTest(operation=operation):
                self.assert_fixed("sql_read_unavailable", lambda: self.connection.read_call(self.limits, operation))

    def test_fixed_error_inside_owner_sql_handler_has_no_raw_context(self):
        caught_sql = []

        def clock():
            return 1_000_000_000 if caught_sql else 0

        def operation():
            try:
                self.connection.execute("SELECT * FROM CANARY_MISSING_TABLE").fetchall()
            except sqlite3.Error:
                caught_sql.append(True)
                self.connection.execute("SELECT 1").fetchall()

        with mock.patch("codex_agents.sql_read_budget._clock_ns", side_effect=clock):
            self.assert_fixed("sql_read_deadline", lambda: self.connection.read_call(self.limits, operation))
        self.assertEqual([True], caught_sql)

    def test_restoration_failure_overrides_existing_fixed_refusal(self):
        def operation():
            try:
                self.connection.execute(LONG_QUERY).fetchall()
            except sqlite3.Error:
                pass
            self.connection.execute("SELECT 'CANARY-LATE'").fetchall()

        with mock.patch.object(self.connection, "_restore_read_settings", side_effect=RuntimeError("CANARY-RESTORE")):
            self.assert_fixed("sql_read_unavailable", lambda: self.connection.read_call(self.limits, operation))
        self.assertTrue(self.connection._sql_unusable)
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(self.connection, "SELECT 1")

    def test_fixed_read_cannot_leave_a_transaction_open(self):
        self.assert_fixed("sql_read_unavailable", lambda: self.connection.read_call(
            self.limits, lambda: self.connection.execute("BEGIN")))
        self.assertTrue(self.connection._sql_unusable)
        self.assert_fixed("sql_read_unavailable", lambda: self.connection.execute("SELECT 1"))
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(self.connection, "SELECT 1")

    def test_interrupt_at_callback_counter_increment_propagates_after_restore(self):
        class InterruptAtCount:
            def __add__(self, other):
                raise KeyboardInterrupt()

        before = self.settings()

        def operation():
            self.connection._sql_scope.callbacks = InterruptAtCount()
            self.connection.execute(LONG_QUERY).fetchall()

        with self.assertRaises(KeyboardInterrupt) as caught:
            self.connection.read_call(self.limits, operation)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(before, self.settings())
        self.assertEqual([(1,)], self.run_query())
