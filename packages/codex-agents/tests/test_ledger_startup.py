from __future__ import annotations

import errno
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import learning_store
from codex_agents.sql_read_budget import SQLReadConnection


class LedgerStartupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def state(self, name: str) -> learning_store.LearningState:
        return learning_store.LearningState(self.base / name)

    def failed_owner(self) -> learning_store.LearningLedger:
        owner = learning_store.LearningLedger.__new__(learning_store.LearningLedger)

        def cleanup():
            connection = getattr(owner, "_connection", None)
            if connection is not None:
                connection.close()
            descriptor = getattr(owner, "_writer_descriptor", None)
            if descriptor is not None:
                os.close(descriptor)
                owner._writer_descriptor = None

        self.addCleanup(cleanup)
        return owner

    def assert_closed(self, connection: sqlite3.Connection) -> None:
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def assert_fresh_owner(self, state: learning_store.LearningState) -> None:
        owner = learning_store.LearningLedger(state)
        try:
            self.assertEqual(1, owner._connection.execute("SELECT 1").fetchone()[0])
            self.assertIsNone(owner._connection.execute(
                "SELECT name FROM sqlite_master WHERE name='startup_canary'"
            ).fetchone())
        finally:
            owner.close()

    def test_connection_setup_failures_close_actual_connection(self) -> None:
        stages = (
            "row_factory", "PRAGMA foreign_keys = ON", "PRAGMA journal_mode = WAL",
            "PRAGMA synchronous = FULL", "PRAGMA secure_delete = ON",
            "PRAGMA busy_timeout = 5000", "harden",
        )
        native_connect = sqlite3.connect
        for index, stage in enumerate(stages):
            for failure_type in (OSError, MemoryError, KeyboardInterrupt, SystemExit):
                with self.subTest(stage=stage, failure=failure_type.__name__):
                    state = self.state(f"setup-{index}-{failure_type.__name__}")
                    failure = failure_type("synthetic connection setup failure")
                    allocated = []

                    class FailingConnection(SQLReadConnection):
                        def __setattr__(self, name, value):
                            if name == "row_factory" and stage == name:
                                raise failure
                            super().__setattr__(name, value)

                        def execute(self, statement, parameters=(), /):
                            if statement == stage:
                                raise failure
                            return super().execute(statement, parameters)

                    def connect(*args, **kwargs):
                        self.assertIs(SQLReadConnection, kwargs["factory"])
                        kwargs["factory"] = FailingConnection
                        connection = native_connect(*args, **kwargs)
                        allocated.append(connection)
                        return connection

                    def harden():
                        if stage == "harden":
                            raise failure
                        original_harden()

                    original_harden = state.harden_database_files
                    owner = self.failed_owner()
                    try:
                        with mock.patch.object(learning_store.sqlite3, "connect", side_effect=connect), \
                                mock.patch.object(state, "harden_database_files", side_effect=harden):
                            with self.assertRaises(failure_type) as caught:
                                owner.__init__(state)
                        self.assertIs(failure, caught.exception)
                        self.assertEqual(1, len(allocated))
                        self.assert_closed(allocated[0])
                        self.assert_fresh_owner(state)
                    finally:
                        for connection in allocated:
                            connection.close()

    def test_post_connect_failures_close_and_roll_back_before_reopening(self) -> None:
        for stage in ("_migrate", "_record_first_owner_key", "_reconcile_managed_files"):
            for failure_type in (OSError, MemoryError, KeyboardInterrupt, SystemExit):
                with self.subTest(stage=stage, failure=failure_type.__name__):
                    state = self.state(f"{stage}-{failure_type.__name__}")
                    failure = failure_type("synthetic startup failure")
                    owners = []
                    failed_owner = self.failed_owner()

                    def fail(owner):
                        owners.append(owner)
                        owner._connection.execute("BEGIN IMMEDIATE")
                        owner._connection.execute("CREATE TABLE startup_canary(value)")
                        raise failure

                    try:
                        with mock.patch.object(learning_store.LearningLedger, stage, fail):
                            with self.assertRaises(failure_type) as caught:
                                failed_owner.__init__(state)
                        self.assertIs(failure, caught.exception)
                        self.assertEqual(1, len(owners))
                        self.assert_closed(owners[0]._connection)
                        self.assertIsNone(owners[0]._writer_descriptor)
                        self.assert_fresh_owner(state)
                    finally:
                        for owner in owners:
                            owner.close()

    @unittest.skipIf(learning_store.fcntl is None, "writer flock unavailable")
    def test_lock_setup_failures_close_actual_descriptor(self) -> None:
        native_open = os.open
        native_fchmod = os.fchmod
        native_flock = learning_store.fcntl.flock
        for stage in ("fchmod", "flock"):
            for failure_type in (OSError, MemoryError, KeyboardInterrupt, SystemExit):
                with self.subTest(stage=stage, failure=failure_type.__name__):
                    state = self.state(f"lock-{stage}-{failure_type.__name__}")
                    failure = failure_type("synthetic lock setup failure")
                    descriptors = []

                    def open_lock(*args, **kwargs):
                        descriptor = native_open(*args, **kwargs)
                        descriptors.append(descriptor)
                        return descriptor

                    def fchmod(descriptor, mode):
                        native_fchmod(descriptor, mode)
                        if stage == "fchmod":
                            raise failure

                    def flock(descriptor, operation):
                        native_flock(descriptor, operation)
                        if stage == "flock":
                            raise failure

                    try:
                        with mock.patch.object(learning_store.os, "open", side_effect=open_lock), \
                                mock.patch.object(learning_store.os, "fchmod", side_effect=fchmod), \
                                mock.patch.object(learning_store.fcntl, "flock", side_effect=flock):
                            if failure_type is OSError:
                                with self.assertRaisesRegex(learning_store.LedgerError,
                                                            "another learning writer") as caught:
                                    learning_store.LearningLedger(state)
                                self.assertIs(failure, caught.exception.__cause__)
                            else:
                                with self.assertRaises(failure_type) as caught:
                                    learning_store.LearningLedger(state)
                                self.assertIs(failure, caught.exception)
                        self.assertEqual(1, len(descriptors))
                        with self.assertRaises(OSError) as closed:
                            os.fstat(descriptors[0])
                        self.assertEqual(errno.EBADF, closed.exception.errno)
                        self.assert_fresh_owner(state)
                    finally:
                        for descriptor in descriptors:
                            try:
                                os.close(descriptor)
                            except OSError as error:
                                if error.errno != errno.EBADF:
                                    raise

    def test_connect_failure_releases_writer_lock(self) -> None:
        for failure_type in (sqlite3.OperationalError, MemoryError, KeyboardInterrupt, SystemExit):
            with self.subTest(failure=failure_type.__name__):
                state = self.state(f"connect-{failure_type.__name__}")
                failure = failure_type("synthetic connection allocation failure")
                owner = self.failed_owner()
                with mock.patch.object(learning_store.sqlite3, "connect", side_effect=failure):
                    with self.assertRaises(failure_type) as caught:
                        owner.__init__(state)
                self.assertIs(failure, caught.exception)
                self.assert_fresh_owner(state)

    @unittest.skipIf(learning_store.fcntl is None, "writer flock unavailable")
    def test_successful_owner_still_excludes_another_writer(self) -> None:
        state = self.state("successful")
        owner = learning_store.LearningLedger(state)
        try:
            with self.assertRaisesRegex(learning_store.LedgerError, "another learning writer"):
                learning_store.LearningLedger(state)
            self.assertEqual(1, owner._connection.execute("SELECT 1").fetchone()[0])
        finally:
            owner.close()
        self.assert_fresh_owner(state)
