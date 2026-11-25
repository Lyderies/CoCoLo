"""Cooperative read work on one trusted owner's existing SQLite connection.

This connection factory tracks Python progress-handler ownership from creation.
It is not a sandbox for arbitrary code holding the database capability. Direct
native/base-class calls can bypass that bookkeeping and confer no caller identity.
Callback counts and monotonic checks do not bound OS I/O, Python heap or every VM
instruction. Explicit limits are configuration, not measured installed defaults.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field

from .store_fence import FencedConnection

SQL_READ_PROGRESS_INTERVAL = 256
MAX_SQL_READ_CALLBACKS = 100_000
MAX_SQL_READ_DURATION_MS = 1000
MAX_SQL_READ_VALUE_BYTES = 64 * 1_048_576
MAX_SQL_READ_CURSORS = 16
SQL_READ_REASONS = frozenset({
    "sql_read_invalid_limits", "sql_read_wrong_owner", "sql_read_unavailable",
    "sql_read_work_limit", "sql_read_deadline", "sql_read_value_limit", "sql_read_busy",
})


class SQLReadError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or reason_code not in SQL_READ_REASONS:
            reason_code = "sql_read_unavailable"
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class SQLReadLimits:
    max_callbacks: int
    max_duration_ms: int
    max_value_bytes: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or not 1 <= value <= ceiling for value, ceiling in (
            (self.max_callbacks, MAX_SQL_READ_CALLBACKS),
            (self.max_duration_ms, MAX_SQL_READ_DURATION_MS),
            (self.max_value_bytes, MAX_SQL_READ_VALUE_BYTES),
        )):
            raise SQLReadError("sql_read_invalid_limits")


def _clock_ns() -> int:
    value = time.monotonic_ns()
    if type(value) is not int or value < 0:
        raise ValueError("invalid monotonic clock")
    return value


@dataclass
class _ReadScope:
    limits: SQLReadLimits
    last_ns: int
    deadline_ns: int
    callbacks: int = 0
    reason: str | None = None
    interruption: BaseException | None = None
    cursors: list[sqlite3.Cursor] = field(default_factory=list)
    transaction: bool = False

    def poll(self) -> None:
        if self.reason is not None:
            return
        try:
            now = _clock_ns()
            if now < self.last_ns:
                self.reason = "sql_read_unavailable"
            elif now >= self.deadline_ns:
                self.reason = "sql_read_deadline"
            self.last_ns = now
        except BaseException as error:
            self.reason = "sql_read_unavailable"
            if not isinstance(error, Exception):
                self.interruption = error

    def progress(self) -> int:
        # No SQL, connection mutation, external callback or async interrupt.
        try:
            self.callbacks += 1
            if self.reason is None and self.callbacks >= self.limits.max_callbacks:
                self.reason = "sql_read_work_limit"
            self.poll()
            return int(self.reason is not None)
        except BaseException as error:
            self.reason = "sql_read_unavailable"
            if not isinstance(error, Exception):
                self.interruption = error
            return 1


class _SQLReadView:
    """Fetch-only view for one fixed owner read; no cursor statement API."""

    __slots__ = ("_owner", "_scope", "_cursor")

    def __init__(self, owner: SQLReadConnection, scope: _ReadScope, cursor: sqlite3.Cursor) -> None:
        self._owner, self._scope, self._cursor = owner, scope, cursor

    def _check(self) -> None:
        self._owner._read_owner()
        if self._owner._sql_scope is not self._scope:
            raise SQLReadError("sql_read_unavailable")
        self._scope.poll()
        if self._scope.reason is not None:
            raise SQLReadError(self._scope.reason)

    def fetchone(self):
        self._check()
        return self._cursor.fetchone()

    def fetchall(self):
        self._check()
        return self._cursor.fetchall()

    def fetchmany(self, *args, **kwargs):
        self._check()
        return self._cursor.fetchmany(*args, **kwargs)

    def __iter__(self):
        return self

    def __next__(self):
        self._check()
        return next(self._cursor)

    def execute(self, *args, **kwargs):
        self._check()
        raise SQLReadError("sql_read_unavailable")

    executemany = execute
    executescript = execute

    def close(self) -> None:
        self._owner._read_owner()
        if self._owner._sql_scope is self._scope:
            self._cursor.close()


class SQLReadConnection(FencedConnection):
    """Trusted fresh factory; ordinary legacy operations keep their contract.

    A read scope requires the actual construction process/thread, no transaction,
    and no registered foreign progress handler. Its fixed owner function creates
    fresh reads through connection.execute and fetch-only views. Cursor creation
    and statement execution through views are refused. Previously retained raw
    cursors must not be used by that fixed function; holding or inspecting native
    database/cursor capabilities is outside this cooperative contract. Do not
    install native custom busy/progress handlers behind this factory's back.
    No listener or client-facing arbitrary SQL interface is provided here.
    """

    def __init__(self, *args, **kwargs) -> None:
        self._sql_origin = (os.getpid(), threading.current_thread())
        self._sql_foreign_progress = False
        self._sql_scope: _ReadScope | None = None
        self._sql_unusable = False
        self._sql_sole_owner = False
        # The lock that keeps other threads off this connection (set by its owner).
        # A scope refuses every other thread's statement, so every scope holds it:
        # another thread's locked statement then waits instead of being refused.
        self._sql_owner_lock = None
        self._sql_lock_waits = None  # M4b E1a-3: where a refused wait is counted
        super().__init__(*args, **kwargs)

    def share_with_lock(self, lock, *, waits=None) -> None:
        """Every read scope then holds `lock` (reentrant) for its whole length; `waits` counts the
        scopes refused for want of it (M4b E1a-3)."""
        if self._sql_owner_lock is not None or type(lock) is not type(threading.RLock()):
            raise SQLReadError("sql_read_unavailable")
        self._sql_owner_lock = lock
        self._sql_lock_waits = waits

    def require_owner(self) -> None:
        """Opt the collector's existing connection into sole-thread ownership."""
        self._read_owner()
        self._sql_sole_owner = True

    def _usable(self) -> None:
        if self._sql_sole_owner and (self._sql_origin[0] != os.getpid()
                                    or self._sql_origin[1] is not threading.current_thread()):
            raise SQLReadError("sql_read_wrong_owner")
        if self._sql_unusable:
            raise SQLReadError("sql_read_unavailable")

    def _read_owner(self) -> None:
        if self._sql_origin[0] != os.getpid() or self._sql_origin[1] is not threading.current_thread():
            raise SQLReadError("sql_read_wrong_owner")
        self._usable()

    def set_progress_handler(self, callback, n, /) -> None:
        self._usable()
        if self._sql_scope is not None:
            raise SQLReadError("sql_read_unavailable")
        if type(n) is not int:
            raise TypeError("progress interval must be an integer")
        super().set_progress_handler(callback, n)
        self._sql_foreign_progress = callback is not None and n > 0

    def _track(self, cursor: sqlite3.Cursor) -> sqlite3.Cursor:
        scope = self._sql_scope
        if scope is not None:
            if len(scope.cursors) >= MAX_SQL_READ_CURSORS:
                cursor.close()
                scope.reason = "sql_read_work_limit"
                raise SQLReadError("sql_read_work_limit")
            scope.cursors.append(cursor)
        return cursor

    def _before_statement(self, sql=None) -> None:
        self._usable()
        scope = self._sql_scope
        if scope is not None:
            self._read_owner()
            if scope.transaction and sql is not None:
                # Fixed trusted SELECTs only. This prevents accidental COMMIT,
                # savepoint or PRAGMA changes through the ordinary API; it is
                # not a parser/sandbox for arbitrary native SQL capabilities.
                words = sql.lstrip().split(None, 1) if type(sql) is str else ()
                if not words or words[0].upper() not in {"SELECT", "WITH"}:
                    raise SQLReadError("sql_read_unavailable")
            scope.poll()
            if scope.reason is None and len(scope.cursors) >= MAX_SQL_READ_CURSORS:
                scope.reason = "sql_read_work_limit"
            if scope.reason is not None:
                raise SQLReadError(scope.reason)

    def execute(self, sql, parameters=(), /):
        self._before_statement(sql)
        cursor = self._track(super().execute(sql, parameters))
        scope = self._sql_scope
        return cursor if scope is None else _SQLReadView(self, scope, cursor)

    def _transaction_control(self):
        self._usable()
        if self._sql_scope is not None and self._sql_scope.transaction:
            raise SQLReadError("sql_read_unavailable")

    def commit(self):
        self._transaction_control()
        return super().commit()

    def rollback(self):
        self._transaction_control()
        return super().rollback()

    def __enter__(self):
        self._transaction_control()
        return super().__enter__()

    def __exit__(self, *args):
        self._transaction_control()
        return super().__exit__(*args)

    def cursor(self, *args, **kwargs):
        self._before_statement()
        if self._sql_scope is not None:
            raise SQLReadError("sql_read_unavailable")
        return super().cursor(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._usable()
        if self._sql_scope is not None:
            raise SQLReadError("sql_read_unavailable")
        return super().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        self._usable()
        if self._sql_scope is not None:
            raise SQLReadError("sql_read_unavailable")
        return super().executescript(*args, **kwargs)

    def _pragma_integer(self, name: str, value: int | None = None) -> int:
        # Names are fixed owner code, never request fields or arbitrary SQL.
        if name not in {"query_only", "busy_timeout"}:
            raise ValueError("unknown owner setting")
        cursor = super().execute("PRAGMA " + name + ("=" + str(value) if value is not None else ""))
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        # Assigning query_only returns no row; verify with an actual fresh read.
        if value is not None:
            return self._pragma_integer(name)
        if row is None or type(row[0]) is not int:
            raise ValueError("invalid owner setting")
        return row[0]

    def _restore_read_settings(self, prior: tuple[int, int, int], scope: _ReadScope, *, transaction=False) -> None:
        # Finish every scope-owned cursor before removing the handler, so a
        # returned cursor cannot later resume this job with another job's budget.
        for cursor in scope.cursors:
            cursor.close()
        super().set_progress_handler(None, 0)
        if self.in_transaction is not transaction:
            raise ValueError("read operation changed transaction state")
        query_only, busy_timeout, value_limit = prior
        self.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, value_limit)
        if self.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) != value_limit:
            raise ValueError("value limit was not restored")
        if self._pragma_integer("query_only", query_only) != query_only:
            raise ValueError("query mode was not restored")
        if self._pragma_integer("busy_timeout", busy_timeout) != busy_timeout:
            raise ValueError("busy timeout was not restored")

    def read_call(self, limits: SQLReadLimits, operation):
        """Run a fixed trusted owner function, never an RPC-supplied callback.

        Keeping this boundary outside context-manager exception propagation lets
        fixed backend errors have neither a raw cause nor a raw context.
        """
        return self._read_call(limits, operation, transaction=False)

    def transaction_read_call(self, limits: SQLReadLimits, operation):
        """Fixed owner SELECT work inside its already-open write transaction.

        The caller owns commit/rollback. This cooperative scope neither starts
        nor ends that transaction, and it is not an arbitrary SQL/callback RPC.
        Native/base-class capabilities remain outside this trusted contract.
        """
        return self._read_call(limits, operation, transaction=True)

    def read_in_current_mode(self, limits, operation):
        """`read_call`, or `transaction_read_call` inside an open transaction.

        The mode is chosen while holding the owner's lock, so another thread cannot
        commit between the choice and the read (C12c, C12f).
        """
        return self._read_call(limits, operation, transaction=None)

    def _read_call(self, limits, operation, *, transaction):
        self._read_owner()
        # Every existing refusal of an invalid call comes before any wait (C12f).
        if type(limits) is not SQLReadLimits or not callable(operation):
            raise SQLReadError("sql_read_invalid_limits")
        limits.__post_init__()
        lock = self._sql_owner_lock
        if lock is None:
            mode = self.in_transaction if transaction is None else transaction
            return self._scoped_read(limits, operation, transaction=mode)
        # Waiting for the lock is bounded by the read's own budget (C12c). Taking it is
        # inside the try, so an exception once it is held releases exactly this hold.
        acquired = False
        try:
            # M4b E1a-1: never longer than the ledger's own limit or the request's deadline either.
            from .ledger_waits import wait_seconds
            acquired = lock.acquire(timeout=wait_seconds(limits.max_duration_ms / 1000))
            if not acquired:
                if self._sql_lock_waits is not None:
                    self._sql_lock_waits.refused("read_call")
                raise SQLReadError("sql_read_busy")
            mode = self.in_transaction if transaction is None else transaction
            return self._scoped_read(limits, operation, transaction=mode)
        finally:
            if acquired:
                lock.release()

    def _scoped_read(self, limits, operation, *, transaction):
        if type(limits) is not SQLReadLimits or not callable(operation):
            raise SQLReadError("sql_read_invalid_limits")
        limits.__post_init__()
        reason = None
        scope = None
        prior = None
        failure = None
        traceback = None
        sqlite_failure = False
        sqlite_code = None
        fixed_reason = None
        result = None
        try:
            if self._sql_scope is not None or self._sql_foreign_progress or self.in_transaction is not transaction:
                raise SQLReadError("sql_read_unavailable")
            now = _clock_ns()
            scope = _ReadScope(limits, now, now + limits.max_duration_ms * 1_000_000, transaction=transaction)
            prior = (self._pragma_integer("query_only"), self._pragma_integer("busy_timeout"),
                     self.getlimit(sqlite3.SQLITE_LIMIT_LENGTH))
            if prior[0] not in {0, 1} or prior[1] < 0 or prior[2] < 1:
                raise ValueError("invalid previous read settings")
            self._sql_scope = scope
            if self._pragma_integer("busy_timeout", 0) != 0 or self._pragma_integer("query_only", 1) != 1:
                raise ValueError("read settings did not apply")
            effective = min(prior[2], limits.max_value_bytes)
            self.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, effective)
            if self.getlimit(sqlite3.SQLITE_LIMIT_LENGTH) != effective:
                raise ValueError("value limit did not apply")
            super().set_progress_handler(scope.progress, SQL_READ_PROGRESS_INTERVAL)
            scope.poll()
        except SQLReadError as error:
            reason = error.reason_code
        except BaseException as error:
            reason = "sql_read_unavailable"
            if not isinstance(error, Exception):
                failure, traceback = error, error.__traceback__
        else:
            if scope.reason is None:
                try:
                    result = operation()
                except BaseException as error:
                    if isinstance(error, SQLReadError):
                        # Retain only a validated reason, never a raised fixed
                        # error's pre-existing raw exception context/traceback.
                        fixed_reason = SQLReadError(error.reason_code).reason_code
                    elif isinstance(error, sqlite3.Error):
                        sqlite_failure = True
                        sqlite_code = getattr(error, "sqlite_errorcode", None)
                    else:
                        failure, traceback = error, error.__traceback__
            scope.poll()
            reason = scope.reason if scope.reason is not None else fixed_reason
        finally:
            if scope is not None and self._sql_scope is scope:
                try:
                    if transaction:
                        self._restore_read_settings(prior, scope, transaction=True)
                    else:
                        self._restore_read_settings(prior, scope)
                except BaseException as error:
                    self._sql_unusable = True
                    reason = "sql_read_unavailable"
                    try:
                        super().close()
                    except Exception:
                        pass
                    if not isinstance(error, Exception) and failure is None:
                        failure, traceback = error, error.__traceback__
                finally:
                    self._sql_scope = None
        if scope is not None and scope.interruption is not None:
            raise scope.interruption
        if failure is not None:
            raise failure.with_traceback(traceback)
        if reason is None and sqlite_failure:
            reason = ("sql_read_value_limit" if sqlite_code == sqlite3.SQLITE_TOOBIG else
                      "sql_read_busy" if type(sqlite_code) is int and sqlite_code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                      else "sql_read_unavailable")
        if reason is not None:
            raise SQLReadError(reason)
        return result
