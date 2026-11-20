"""C12: the account service and the collector share one ledger connection.

A read scope refuses a statement from any other thread, so every scope is taken
under the ledger lock, and a database refusal while serving one collector request
never stops the shared collector. Synthetic projects and providers only.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

import tests.support  # noqa: F401
from pathlib import Path
from tests.learning_support import event_record
from tests.support import TEMP_ROOT
from tests import test_account_original_open as open_tests
from tests import test_learning_store as store_tests

from codex_agents import account_original_open, learning_store
from codex_agents.learning_contracts import external_id
from codex_agents.account_api import decode_account_response, encode_account_request
from codex_agents.learning_store import CollectorClient, CollectorError, CollectorServer
from codex_agents.sql_read_budget import SQLReadError, SQLReadLimits


class RecordingLock:
    """The connection's shared lock, recording each acquisition timeout (C12g)."""

    def __init__(self, lock):
        self.lock, self.timeouts = lock, []

    def acquire(self, *args, **kwargs):
        self.timeouts.append(kwargs.get("timeout"))
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()


def record_lock_timeouts(test, connection):
    recording = RecordingLock(connection._sql_owner_lock)
    connection._sql_owner_lock = recording
    test.addCleanup(setattr, connection, "_sql_owner_lock", recording.lock)
    return recording


class CollectorRefusalTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger

    def serve(self):
        server = CollectorServer(self.ledger)
        server.start()
        self.addCleanup(server.stop)
        return server, CollectorClient(self.store.state.socket, timeout=0.5)

    def bind(self, client):
        return client.bind(self.store.project_a, "session", "hook", "codex-hooks-v1")

    def test_a_passing_refusal_during_a_bind_is_answered_and_the_collector_keeps_serving(self):
        server, client = self.serve()
        coded = []
        for code in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY, 517):  # 517: SQLITE_BUSY_SNAPSHOT
            error = sqlite3.OperationalError("database is busy")
            error.sqlite_errorcode = code
            coded.append(error)
        refusals = [SQLReadError(reason) for reason in
                    ("sql_read_busy", "sql_read_deadline", "sql_read_work_limit", "sql_read_value_limit")]
        for refusal in [*refusals, *coded]:
            with self.subTest(type(refusal).__name__ + ":" + str(refusal)
                              + ":" + str(getattr(refusal, "sqlite_errorcode", ""))):
                with mock.patch.object(self.ledger, "bind_session", side_effect=refusal):
                    with self.assertRaisesRegex(CollectorError, "operation_rejected"):
                        self.bind(client)
                self.assertTrue(server._thread.is_alive())
                self.assertIsNone(server.thread_error)
        self.assertTrue(self.bind(client).startswith("bnd_"))

    def test_a_defect_or_broken_connection_still_stops_the_collector_and_is_recorded(self):
        unknown = sqlite3.OperationalError("no such table: x")
        unknown.sqlite_errorcode = sqlite3.SQLITE_ERROR
        for failure in (SQLReadError("sql_read_wrong_owner"), SQLReadError("sql_read_unavailable"),
                        sqlite3.ProgrammingError("Incorrect number of bindings"),
                        sqlite3.InterfaceError("Error binding parameter"),
                        sqlite3.DatabaseError("database disk image is malformed"), unknown,
                        KeyError("defect")):
            with self.subTest(type(failure).__name__ + ":" + str(failure)):
                server, client = self.serve()
                try:
                    with (mock.patch.object(self.ledger, "bind_session", side_effect=failure),
                          mock.patch.object(threading, "excepthook")):
                        with self.assertRaises(CollectorError):
                            self.bind(client)
                    server._thread.join(2)
                    self.assertFalse(server._thread.is_alive())
                    self.assertIs(type(failure), type(server.thread_error))
                finally:
                    server.stop()  # Each case starts its own collector.

    def test_an_unusable_connection_stops_the_collector(self):
        # After a failed scope restore the connection is marked unusable and closed.
        server, client = self.serve()
        self.ledger._connection._sql_unusable = True
        with mock.patch.object(threading, "excepthook"):
            with self.assertRaises(CollectorError):
                self.bind(client)
        server._thread.join(2)
        self.assertFalse(server._thread.is_alive())
        self.assertIs(SQLReadError, type(server.thread_error))
        self.ledger._connection._sql_unusable = False


    def test_every_ingest_outcome_uses_up_its_binding_token(self):
        server, client = self.serve()
        token = self.bind(client)
        record = event_record("evt_refused")
        record["run_id"] = external_id(self.store.state.digest_key(), "run",
                                       "codex-hooks-v1\x00session\x00root")
        with mock.patch.object(self.ledger, "append", side_effect=SQLReadError("sql_read_busy")):
            with self.assertRaisesRegex(CollectorError, "operation_rejected"):
                client.ingest(token, record)
        with self.assertRaisesRegex(CollectorError, "binding_invalid"):
            client.ingest(token, record)
        self.assertTrue(server._thread.is_alive())


class PassingRefusalTests(unittest.TestCase):
    def test_subclasses_of_a_passing_refusal_still_pass(self):
        class Busy(SQLReadError):
            pass

        class Locked(sqlite3.OperationalError):
            pass

        locked = Locked("database table is locked")
        locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        self.assertTrue(learning_store._passing_refusal(Busy("sql_read_busy")))
        self.assertTrue(learning_store._passing_refusal(locked))
        self.assertFalse(learning_store._passing_refusal(Busy("sql_read_wrong_owner")))
        self.assertFalse(learning_store._passing_refusal(sqlite3.OperationalError("no code")))

    def test_a_real_busy_database_is_a_passing_refusal(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as folder:
            path = Path(folder) / "busy.sqlite"
            writer = sqlite3.connect(path, timeout=0, isolation_level=None)
            other = sqlite3.connect(path, timeout=0, isolation_level=None)
            try:
                writer.execute("BEGIN IMMEDIATE")
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    other.execute("BEGIN IMMEDIATE")
                self.assertEqual(sqlite3.SQLITE_BUSY, raised.exception.sqlite_errorcode & 255)
                self.assertTrue(learning_store._passing_refusal(raised.exception))
            finally:
                writer.close()
                other.close()


class HostOpenPublishLockTests(unittest.TestCase):
    def setUp(self):
        self.jobs = open_tests.OriginalAccountOpenJobTests(methodName='runTest')
        self.jobs.setUp()
        self.addCleanup(self.jobs.doCleanups)

    def test_the_publish_check_holds_the_lock_so_a_concurrent_statement_waits_and_succeeds(self):
        t, ledger = self.jobs, self.jobs.ledger
        service, _ = t.wire_service()
        real = account_original_open.current_open_request
        outcomes, threads = [], []

        def collector_statement(attempting):
            # What every collector request does: take the lock, then query.
            try:
                attempting.set()
                with ledger._lock:
                    ledger._connection.execute("SELECT 1").fetchone()
            except Exception as error:
                outcomes.append(f"{type(error).__name__}: {error}")
            else:
                outcomes.append("ok")

        def checked(*args, **kwargs):
            attempting = threading.Event()
            thread = threading.Thread(target=collector_statement, args=(attempting,))
            thread.start()
            threads.append(thread)
            # Without the lock the competing statement runs at once, inside the scope.
            self.assertTrue(attempting.wait(5))
            thread.join(0.2)
            return real(*args, **kwargs)

        request = t.wire_command()
        with mock.patch.object(account_original_open, "current_open_request", side_effect=checked):
            raw = t.pump_wire(service, service.begin(encode_account_request(request), t.token))
        for thread in threads:
            thread.join(5)
        self.assertEqual("opened", decode_account_response(raw, request)["state"])
        self.assertTrue(threads)
        self.assertEqual(["ok"] * len(threads), outcomes)


    def test_the_publish_check_waits_for_the_lock_no_longer_than_its_budget(self):
        t, ledger = self.jobs, self.jobs.ledger
        service, _ = t.wire_service()
        real_guard = service._views._guard
        release, holders = threading.Event(), []
        self.addCleanup(release.set)

        def hold():
            with ledger._lock:
                held.set()
                release.wait(5)

        held = threading.Event()

        def guard():
            real_guard()
            # The guard right before the publish check: another thread now holds the lock.
            # Only the guard right before the publish check: take_claim_reply has imported
            # current_open_request by then. Mock adds frames of its own, so look a few up.
            frame, publishing = sys._getframe(1), False
            for _ in range(8):
                if frame is None:
                    break
                # M4b E1a: take_claim_reply now sets the request deadline and runs _take_claim_reply.
                publishing = publishing or (frame.f_code.co_name in {"take_claim_reply", "_take_claim_reply"}
                                            and "current_open_request" in frame.f_locals)
                frame = frame.f_back
            if publishing and not holders:
                holder = threading.Thread(target=hold)
                holder.start()
                holders.append(holder)
                self.assertTrue(held.wait(5))

        request = t.wire_command()
        recording = record_lock_timeouts(self, ledger._connection)
        started = time.monotonic()
        with mock.patch.object(service._views, "_guard", side_effect=guard):
            raw = t.pump_wire(service, service.begin(encode_account_request(request), t.token))
        waited = time.monotonic() - started
        release.set()
        for holder in holders:
            holder.join(5)
        self.assertTrue(holders)
        with self.assertRaisesRegex(Exception, "open_account_busy"):  # S1-U10b (was open_unavailable)
            decode_account_response(raw, request)
        # The work limits allow 1 s; M4b E1a-1 caps every wait for the ledger lock at
        # LEDGER_LOCK_WAIT_MS (750 ms), or less inside a request's deadline, so the publish read
        # waited at most that (requirement-driven change, recorded in PLAN). The holder would keep
        # the lock for 5 s. Earlier reads in the open take it uncontended.
        from codex_agents.ledger_waits import LEDGER_LOCK_WAIT_MS
        self.assertLessEqual(recording.timeouts[-1], LEDGER_LOCK_WAIT_MS / 1000)
        self.assertTrue(all(timeout <= LEDGER_LOCK_WAIT_MS / 1000 for timeout in recording.timeouts))
        self.assertLess(waited, 5)


class LedgerReadScopeLockTests(unittest.TestCase):
    def setUp(self):
        self.store = store_tests.LearningStoreTests(methodName='runTest')
        self.store.setUp()
        self.addCleanup(self.store.tearDown)
        self.ledger = self.store.ledger
        self.limits = SQLReadLimits(100_000, 1000, 1_048_576)

    def read(self):
        return self.ledger._connection.execute("SELECT 1").fetchone()[0]

    def test_a_read_scope_waits_while_another_thread_holds_the_lock(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.ledger._lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(release.set)
        held.wait(5)
        # The scope itself must run on the connection's own thread, so the main
        # thread reads and a timer releases the holder.
        timer = threading.Timer(0.2, release.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        self.assertEqual(1, self.ledger._connection.read_call(self.limits, self.read))
        self.assertGreaterEqual(time.monotonic() - started, 0.15)

    def test_another_threads_locked_statement_waits_for_a_scope_and_succeeds(self):
        outcomes = []

        def collector_statement():
            try:
                with self.ledger._lock:
                    outcomes.append(self.ledger._connection.execute("SELECT 2").fetchone()[0])
            except Exception as error:
                outcomes.append(f"{type(error).__name__}: {error}")

        other = threading.Thread(target=collector_statement)

        def scope():
            other.start()
            other.join(0.1)
            self.assertTrue(other.is_alive())
            return self.read()

        self.assertEqual(1, self.ledger._connection.read_call(self.limits, scope))
        other.join(5)
        self.assertEqual([2], outcomes)
        self.assertIsNone(self.ledger._connection._sql_scope)

    def test_a_transaction_read_takes_the_lock_itself(self):
        outcomes, attempting = [], threading.Event()

        def collector_statement():
            attempting.set()
            with self.ledger._lock:
                outcomes.append(self.ledger._connection.execute("SELECT 3").fetchone()[0])

        other = threading.Thread(target=collector_statement)

        def scope():
            other.start()
            self.assertTrue(attempting.wait(5))
            other.join(0.2)
            self.assertTrue(other.is_alive())
            return self.read()

        # No outer lock: the connection itself must hold it for the scope.
        self.assertFalse(self.ledger._lock._is_owned())
        self.ledger._connection.execute("BEGIN")
        try:
            self.assertEqual(1, self.ledger._connection.transaction_read_call(self.limits, scope))
        finally:
            self.ledger._connection.execute("ROLLBACK")
        other.join(5)
        self.assertEqual([3], outcomes)

    def test_an_interrupt_once_the_lock_is_taken_leaves_it_as_it_was(self):
        lock = self.ledger._lock
        if not hasattr(lock, "_recursion_count"):
            self.skipTest("this Python cannot report how deeply an RLock is held")
        for outer in (0, 1):
            with self.subTest(outer_holds=outer):
                for _ in range(outer):
                    lock.acquire()
                try:
                    raised = []

                    def local(frame, event, arg):
                        # The first line run while this call holds one more level.
                        if event == "line" and not raised and lock._recursion_count() == outer + 1:
                            raised.append(frame.f_lineno)
                            raise KeyboardInterrupt
                        return local

                    def calls(frame, event, arg):
                        return local if frame.f_code.co_name == "_read_call" else None

                    sys.settrace(calls)
                    try:
                        with self.assertRaises(KeyboardInterrupt):
                            self.ledger._connection.read_call(self.limits, self.read)
                    finally:
                        sys.settrace(None)
                    self.assertTrue(raised)
                    self.assertEqual(outer, lock._recursion_count())
                    self.assertIsNone(self.ledger._connection._sql_scope)
                finally:
                    for _ in range(outer):
                        lock.release()

    def test_an_invalid_call_is_refused_as_invalid_even_while_the_lock_is_busy(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.ledger._lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        with self.assertRaisesRegex(SQLReadError, "sql_read_invalid_limits"):
            self.ledger._connection.read_call(SQLReadLimits(100_000, 100, 1_048_576), None)
        with self.assertRaisesRegex(SQLReadError, "sql_read_invalid_limits"):
            self.ledger._connection.read_call(object(), self.read)

    def test_a_read_mode_owner_waits_for_the_lock_no_longer_than_its_budget(self):
        from codex_agents.account_billing_admission import AccountBillingAdmission
        from codex_agents.account_observation import AccountObservationOwner

        held, release = threading.Event(), threading.Event()

        def hold():
            with self.ledger._lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        stand_in = SimpleNamespace(_ledger=self.ledger, _limits=SQLReadLimits(100_000, 100, 1_048_576))
        for owner in (AccountBillingAdmission, AccountObservationOwner):
            with self.subTest(owner.__name__):
                recording = record_lock_timeouts(self, self.ledger._connection)
                started = time.monotonic()
                with self.assertRaisesRegex(SQLReadError, "sql_read_busy"):
                    owner._read(stand_in, self.read)
                self.assertEqual([0.1], recording.timeouts)
                self.assertLess(time.monotonic() - started, 5)
                self.ledger._connection._sql_owner_lock = recording.lock

    def test_waiting_for_the_lock_is_bounded_by_the_read_budget(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.ledger._lock:
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        recording = record_lock_timeouts(self, self.ledger._connection)
        started = time.monotonic()
        with self.assertRaisesRegex(SQLReadError, "sql_read_busy"):
            self.ledger._connection.read_call(SQLReadLimits(100_000, 100, 1_048_576), self.read)
        self.assertEqual([0.1], recording.timeouts)
        self.assertLess(time.monotonic() - started, 5)
        self.ledger._connection._sql_owner_lock = recording.lock
        self.assertIsNone(self.ledger._connection._sql_scope)
        release.set()
        holder.join(5)
        self.assertEqual(1, self.ledger._connection.read_call(self.limits, self.read))

    def test_choosing_the_read_mode_happens_under_the_lock(self):
        # The collector holds the lock in a transaction. It commits only once the account
        # thread has looked at in_transaction, or has waited a while without looking (it is
        # blocked on the lock). The account read must then see no transaction and succeed.
        from codex_agents.account_billing_admission import AccountBillingAdmission
        from codex_agents.account_observation import AccountObservationOwner

        connection_type = type(self.ledger._connection)
        main = threading.current_thread()
        for owner in (AccountBillingAdmission, AccountObservationOwner):
            with self.subTest(owner.__name__):
                began, looked = threading.Event(), threading.Event()

                def in_transaction(connection):
                    value = sqlite3.Connection.in_transaction.__get__(connection)
                    if threading.current_thread() is main:
                        looked.set()  # Only once the value is read (C12g).
                    return value

                def collector_transaction():
                    with self.ledger._lock:
                        self.ledger._connection.execute("BEGIN")
                        began.set()
                        looked.wait(0.3)
                        self.ledger._connection.execute("COMMIT")

                collector = threading.Thread(target=collector_transaction)
                with mock.patch.object(connection_type, "in_transaction", property(in_transaction)):
                    collector.start()
                    self.assertTrue(began.wait(5))
                    stand_in = SimpleNamespace(_ledger=self.ledger, _limits=self.limits)
                    self.assertEqual(1, owner._read(stand_in, self.read))
                    collector.join(5)
                self.assertFalse(self.ledger._connection.in_transaction)

    def test_the_lock_is_set_once(self):
        with self.assertRaisesRegex(SQLReadError, "sql_read_unavailable"):
            self.ledger._connection.share_with_lock(threading.RLock())


if __name__ == "__main__":
    unittest.main()
