"""Persistence-owner epoch and lifetime-lock checks; no I/O at import.

This is a cooperative boundary for the fixed internal SQL API, not a sandbox for
native code with a raw SQLite/file-descriptor capability. Legacy executable
exclusion additionally requires the lifetime flock and a newer store version.
"""
from __future__ import annotations

import fcntl
import functools
import os
import re
import secrets
import sqlite3
import stat

EPOCH_COLUMNS = ('singleton', 'epoch', 'old_digest', 'new_digest')
EPOCH_SCHEMA = """
CREATE TABLE runtime_takeover(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL CHECK(epoch>=1),
 old_digest TEXT, new_digest TEXT NOT NULL);
"""
FENCE_MESSAGE = 'This store owner was replaced or lost its writer lock. Run: ./setup.sh --update'
BUSY_MESSAGE = 'Another store owner is still running. Run: ./setup.sh --update'


@functools.lru_cache(maxsize=1024)
def _classify_sql(sql):
    """(comment-stripped SQL, its first word upper-cased, whether it only reads). A pure function of the text."""
    # All SQL is fixed owner code. Strip comments so a leading comment cannot
    # accidentally turn a write/control statement into a read classification.
    clean = re.sub(r'/\*.*?\*/|--[^\n]*(?:\n|$)', ' ', sql, flags=re.S).strip()
    word = clean.split(None, 1)[0].rstrip(';').upper() if clean else ''
    database_pragma = word == 'PRAGMA' and (any(name in clean.lower() for name in
        ('wal_checkpoint', 'optimize', 'incremental_vacuum'))
        or ('journal_mode' in clean.lower() and ('=' in clean or '(' in clean)) or ('=' in clean and any(
        name in clean.lower() for name in ('user_version', 'application_id', 'schema_version',
                                          'journal_mode', 'writable_schema'))))
    reads = (word in {'SELECT', 'EXPLAIN'}
             or (word == 'WITH' and not re.search(r'\b(?:INSERT|UPDATE|DELETE|REPLACE)\b', sql, re.I))
             or (word == 'PRAGMA' and not database_pragma))
    return clean, word, reads


class StoreFence:
    def __init__(self, path, descriptor, digest, refusal, *, legacy=False):
        self.path, self.descriptor, self.digest, self.refusal = path, descriptor, digest, refusal
        self.pid = os.getpid()
        self.identity = self._identity(os.fstat(descriptor))
        self.epoch = None
        self.connection = None  # the actual connection, also when diagnostics wrap its public attribute
        self.legacy = legacy
        # Validate the protected inode before writing even the ephemeral stamp.
        # In particular, a hard link must not turn fencing into file truncation.
        try:
            for info in (os.fstat(descriptor), path.lstat()):
                if (self._identity(info) != self.identity or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1):
                    raise ValueError("unsafe lock")
        except (OSError, ValueError):
            raise self.refusal() from None
        self.stamp = secrets.token_hex(32).encode("ascii")
        os.ftruncate(descriptor, 0)
        if os.pwrite(descriptor, self.stamp, 0) != len(self.stamp):
            raise self.refusal()

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino

    def lock(self):
        """Never retake a lost lock. A separate open must be unable to take it."""
        probe = None
        try:
            if os.getpid() != self.pid:
                raise ValueError('inherited owner')
            actual, expected = os.fstat(self.descriptor), self.path.lstat()
            for info in (actual, expected):
                if (self._identity(info) != self.identity or not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_nlink != 1):
                    raise ValueError('changed lock')
            if os.pread(self.descriptor, 65, 0) != self.stamp:
                raise ValueError('replaced owner')
            probe = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW)
            if self._identity(os.fstat(probe)) != self.identity:
                raise ValueError('changed lock')
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                # The lock must also belong to this original descriptor. This
                # is idempotent for its held flock; it cannot take an exclusive
                # lock that the independent probe just found free (that path
                # refuses below). A successor also replaces the stamp first.
                fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if os.pread(self.descriptor, 65, 0) != self.stamp:
                    raise ValueError("replaced owner")
                return
            raise ValueError('released lock')
        except (OSError, ValueError):
            raise self.refusal() from None
        finally:
            if probe is not None:
                os.close(probe)

    def check(self, connection, *, full=True):
        connection = self.connection
        if connection is None:
            raise self.refusal()
        if full:
            self.lock()
        if self.epoch is not None:
            # TORN-1: the connection is shared between threads; read under its lock (RLock, re-entrant). The wait
            # is bounded like every ledger-lock wait (E1a-1) and a wait that runs out is today's busy refusal.
            lock, acquired = getattr(connection, '_sql_owner_lock', None), False
            if lock is not None:
                from .ledger_waits import wait_seconds
                from .sql_read_budget import SQLReadError
                acquired = lock.acquire(timeout=wait_seconds())
                if not acquired:
                    waits = getattr(connection, '_sql_lock_waits', None)
                    if waits is not None:
                        waits.refused('fence_check')
                    raise SQLReadError('sql_read_busy')
            try:
                try:
                    row = sqlite3.Connection.execute(connection,
                        'SELECT epoch,new_digest FROM runtime_takeover WHERE singleton=1').fetchone()
                finally:
                    if acquired:
                        lock.release()
                valid = row is not None and tuple(row) == (self.epoch, self.digest)
            except sqlite3.Error as error:
                if isinstance(error, sqlite3.OperationalError) and str(error) == 'interrupted':
                    raise
                valid = False
            if not valid:
                raise self.refusal()

    def claim(self, connection):
        """Called only inside the schema/owner transaction, before historical adoption."""
        connection = self.connection
        if connection is None:
            raise self.refusal()
        if not connection.in_transaction:
            raise self.refusal()
        from .collector_client import PACKAGE_VERSION
        from .takeover_schema import check_floor, raise_floor
        check_floor(connection, PACKAGE_VERSION, self.refusal)
        # BEGIN already performed the full lifetime-lock probe.
        if self.epoch is not None:
            self.check(connection, full=False)
            return
        exists = sqlite3.Connection.execute(connection,
            "SELECT 1 FROM sqlite_master WHERE name='runtime_takeover'").fetchone()
        if not exists and not self.legacy:
            raise self.refusal()
        if not exists:
            sqlite3.Connection.execute(connection, EPOCH_SCHEMA)
        columns = tuple(row[1] for row in sqlite3.Connection.execute(connection, 'PRAGMA table_info(runtime_takeover)'))
        if columns != EPOCH_COLUMNS:
            raise self.refusal()
        rows = sqlite3.Connection.execute(connection, 'SELECT * FROM runtime_takeover LIMIT 2').fetchall()
        if not rows and not self.legacy:
            raise self.refusal()
        old = None
        epoch = 1
        if rows:
            row = rows[0]
            if (len(rows) != 1 or row[0] != 1 or type(row[1]) is not int or not 1 <= row[1] < 2**63-1
                    or not isinstance(row[3], str) or re.fullmatch('[0-9a-f]{64}', row[3]) is None):
                raise self.refusal()
            epoch, old = row[1] + 1, row[3]
        raise_floor(connection, PACKAGE_VERSION, self.refusal)
        sqlite3.Connection.execute(connection,
            'INSERT OR REPLACE INTO runtime_takeover VALUES(1,?,?,?)', (epoch, old, self.digest))
        self.epoch = epoch


class FencedCursor(sqlite3.Cursor):
    def execute(self, sql, parameters=(), /):
        return self.connection._fenced(sql, lambda: super(FencedCursor, self).execute(sql, parameters))

    def executemany(self, sql, parameters, /):
        return self.connection._fenced(sql, lambda: super(FencedCursor, self).executemany(sql, parameters))

    def executescript(self, script, /):
        self.connection.executescript(script)
        return self


class FencedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        self._write_fence = None
        self._savepoints = []
        self._savepoint_transaction = False
        super().__init__(*args, **kwargs)

    def _check_fence(self, *, full=True):
        if self._write_fence is not None:
            self._write_fence.check(self, full=full)

    @staticmethod
    def _savepoint_name(sql):
        # Fixed owner SQL, including SQLite's quoted identifier spellings.
        tokens = re.findall(r'''"(?:[^"]|"")*"|'(?:[^']|'')*'|`(?:[^`]|``)*`|\[[^\]]*\]|[^\s;]+''', sql)
        name = tokens[-1] if tokens else ''
        if name[:1] in {'"', "'", '`', '['}:
            quote = name[0]
            name = name[1:-1].replace(quote * 2, quote)
        return name.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))

    def _fenced(self, sql, operation):
        if self._write_fence is None:
            return operation()
        if not self.in_transaction:
            self._savepoints.clear()
            self._savepoint_transaction = False
        clean, word, reads = _classify_sql(sql)
        if word in {'ROLLBACK', 'ABORT'}:
            result = operation()
            if self.in_transaction:  # ROLLBACK TO keeps the named savepoint.
                name = self._savepoint_name(clean)
                if name in self._savepoints:
                    index = len(self._savepoints) - 1 - self._savepoints[::-1].index(name)
                    del self._savepoints[index + 1:]
            return result
        if reads:
            return operation()
        if word in {'BEGIN', 'SAVEPOINT'}:
            # SAVEPOINT starts its own transaction when necessary. Preserve its
            # name and nesting; an automatic BEGIN/commit here would destroy it.
            automatic = not self.in_transaction
            result = operation()
            try:
                self._check_fence(full=automatic)
            except BaseException:
                sqlite3.Connection.rollback(self)
                raise
            if word == 'SAVEPOINT':
                if automatic:
                    self._savepoint_transaction = True
                self._savepoints.append(self._savepoint_name(clean))
            return result
        if word in {'COMMIT', 'END', 'RELEASE'}:
            # RELEASE may commit the outermost savepoint. Check before that
            # operation, and let SQLite retain any enclosing transaction.
            index = None
            if word == 'RELEASE':
                name = self._savepoint_name(clean)
                if name in self._savepoints:
                    index = len(self._savepoints) - 1 - self._savepoints[::-1].index(name)
            commits = word != 'RELEASE' or (self._savepoint_transaction and index == 0)
            try:
                self._check_fence(full=commits)
            except BaseException:
                sqlite3.Connection.rollback(self)
                raise
            # SQLite errors retain SQLite's own transaction state.
            result = operation()
            if index is not None:
                del self._savepoints[index:]
            return result
        maintenance = word in {'VACUUM', 'PRAGMA'}
        automatic = not self.in_transaction
        if automatic:
            sqlite3.Connection.execute(self, 'BEGIN IMMEDIATE')
        try:
            self._check_fence(full=automatic)
            if maintenance and automatic:
                # SQLite forbids VACUUM/journal changes in a transaction. The
                # epoch read is serialized first; the lifetime flock remains held
                # across the operation and is checked again on return.
                sqlite3.Connection.commit(self)
                # A fresh check immediately before the nontransactional change.
                self._check_fence()
            result = operation()
            if automatic:
                # Maintenance may already be durable here; refusal detects a
                # concurrent revocation but cannot undo VACUUM/journal changes.
                self._check_fence()
            if automatic and self.in_transaction:
                sqlite3.Connection.commit(self)
            return result
        except BaseException:
            if automatic:
                sqlite3.Connection.rollback(self)
            raise

    def execute(self, sql, parameters=(), /):
        # Connection.execute normally returns a bare cursor whose later execute
        # calls bypass the connection override. Keep that returned capability
        # fenced as well as explicitly requested cursors.
        cursor = sqlite3.Connection.cursor(self, FencedCursor)
        return self._fenced(sql, lambda: sqlite3.Cursor.execute(cursor, sql, parameters))

    def executemany(self, sql, parameters, /):
        cursor = sqlite3.Connection.cursor(self, FencedCursor)
        return self._fenced(sql, lambda: sqlite3.Cursor.executemany(cursor, sql, parameters))

    def executescript(self, script, /):
        # Unlike SQLite executescript, do not implicitly commit an owner's unit.
        # complete_statement handles trigger bodies and quoted semicolons.
        pending = ''
        result = None
        for character in script:
            pending += character
            if character == ';' and sqlite3.complete_statement(pending):
                result = self.execute(pending)
                pending = ''
        if pending.strip():
            result = self.execute(pending)
        return result

    def cursor(self, factory=FencedCursor):
        if self._write_fence is not None and factory is not FencedCursor:
            raise self._write_fence.refusal()
        return super().cursor(factory)

    def commit(self):
        return self._fenced('COMMIT', lambda: super(FencedConnection, self).commit())

    def __exit__(self, kind, value, traceback):
        if kind is not None:
            self.rollback()
        else:
            self.commit()
        return False
