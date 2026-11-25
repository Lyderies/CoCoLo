"""Temporary, owner-fenced checkpoints for existing hold schema upgrades.

The target version excludes older executables. The progress table withholds
publication until the owner validates and drops it in its final transaction.
Small upgrades keep their original single-transaction rollback behavior.
"""
from __future__ import annotations

BATCH_SIZE = 64
TABLE = 'hold_migration_progress'
PENDING_MESSAGE = 'This store has a pending hold migration. Resume the drained upgrade. Run: ./setup.sh --update'
SCHEMA = '''CREATE TABLE hold_migration_progress(
store TEXT NOT NULL,class TEXT NOT NULL,cursor INTEGER,done INTEGER NOT NULL CHECK(done IN (0,1)),
started_at_ms INTEGER NOT NULL,clock_reference_ns INTEGER,PRIMARY KEY(store,class))'''


def pending(connection):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is not None


class HoldMigration:
    def __init__(self, connection, store, classes, now_ms, target_version, refusal, *, clock_reference_ns=None,
                 before_commit=None):
        self.connection, self.store, self.classes = connection, store, tuple(classes)
        self.refusal = refusal
        self.before_commit = before_commit
        self.active = pending(connection)
        if self.active:
            self.validate()
            self.now_ms, self.clock_reference_ns = connection.execute(
                f'SELECT started_at_ms,clock_reference_ns FROM {TABLE} LIMIT 1').fetchone()
        else:
            self.now_ms = now_ms
            self.clock_reference_ns = clock_reference_ns
            # Bounded probes, never COUNT(*) over historical tombstones.
            remaining = BATCH_SIZE
            for _, table in self.classes:
                rows = connection.execute(f'SELECT rowid FROM {table} LIMIT ?', (remaining + 1,)).fetchall()
                remaining -= len(rows)
                if remaining < 0:
                    self.active = True
                    break
            if self.active:
                connection.execute(SCHEMA)
                connection.executemany(f'INSERT INTO {TABLE} VALUES(?,?,NULL,0,?,?)',
                    [(store, name, now_ms, clock_reference_ns) for name, _ in self.classes])
        if self.active:
            # An exclusion fence only. A current owner must handle TABLE before
            # it can validate or return an admission/backup interface.
            connection.execute(f'PRAGMA user_version={target_version}')

    def validate(self):
        connection = self.connection
        schema = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        if schema is None or ' '.join(schema[0].split()) != ' '.join(SCHEMA.split()):
            raise self.refusal()
        rows = [tuple(row) for row in connection.execute(f'SELECT * FROM {TABLE}')]
        if (len(rows) != len(self.classes) or {row[1] for row in rows} != {name for name, _ in self.classes}
                or any(row[0] != self.store or (row[2] is not None and type(row[2]) is not int)
                       or type(row[3]) is not int or row[3] not in (0, 1)
                       or type(row[4]) is not int or row[4] < 0
                       or (self.store == 'account' and (type(row[5]) is not int or row[5] < 0))
                       or (self.store == 'project' and row[5] is not None) for row in rows)
                or len({(row[4], row[5]) for row in rows}) != 1):
            raise self.refusal()

    def batches(self, name):
        """Yield at most 64 rowids; checkpoint only after the caller's writes.

        Only migration code uses this while holding the original owner context.
        Every COMMIT/BEGIN goes through the same S2 fenced connection.
        """
        connection = self.connection
        table = dict(self.classes)[name]
        row = connection.execute(f'SELECT cursor,done FROM {TABLE} WHERE store=? AND class=?',
                                 (self.store, name)).fetchone()
        cursor, done = row
        while not done:
            condition = '' if cursor is None else ' WHERE rowid>?'
            parameters = () if cursor is None else (cursor,)
            rows = connection.execute(f'SELECT rowid FROM {table}{condition} ORDER BY rowid LIMIT ?',
                                      (*parameters, BATCH_SIZE)).fetchall()
            if not rows:
                connection.execute(f'UPDATE {TABLE} SET done=1 WHERE store=? AND class=?', (self.store, name))
                return
            end = rows[-1][0]
            yield cursor, end, [row[0] for row in rows]
            done = int(len(rows) < BATCH_SIZE)
            connection.execute(f'UPDATE {TABLE} SET cursor=?,done=? WHERE store=? AND class=?',
                               (end, done, self.store, name))
            if self.before_commit is not None:
                self.before_commit()
            connection.commit()
            connection.execute('BEGIN IMMEDIATE')
            cursor = end

    def adopt_sql(self, name, sql, alias, parameters=None):
        parameters = dict(parameters or {})
        if not self.active:
            self.connection.execute(sql, parameters)
            return
        for cursor, end, _ in self.batches(name):
            # Fixed compiled SQL only: the predicate is on the source alias,
            # after any class-specific eligibility predicate, never its subqueries.
            bounded = f'{alias}.rowid<=:migration_end'
            if cursor is not None:
                bounded += f' AND {alias}.rowid>:migration_after'
            self.connection.execute(sql + ' AND ' + bounded,
                {**parameters, 'migration_end': end, 'migration_after': cursor})

    def finish(self):
        if self.active:
            self.validate()
            if self.connection.execute(f'SELECT 1 FROM {TABLE} WHERE done=0 LIMIT 1').fetchone() is not None:
                raise self.refusal()
            self.connection.execute(f'DROP TABLE {TABLE}')
