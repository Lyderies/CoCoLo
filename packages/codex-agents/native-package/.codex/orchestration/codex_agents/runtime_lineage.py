"""Read-only authority over original custody; never authorizes an old sender."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

from .collector_contracts import _version_tuple

MAX_LINEAGE_LINKS = 16
MAX_RELEASE_BYTES = 262144


def release_versions(*, project_root=None):
    """Read this build's shipped catalogue lazily, using only a local install binding."""
    from .compatibility_message import _local_install
    orchestration = Path(__file__).resolve().parent.parent
    clone_package = orchestration.parents[2]
    candidate = clone_package / 'runtime-releases.json'
    if clone_package.parts[-2:] != ('packages', 'codex-agents'):
        install = _local_install(project_root, orchestration)
        if install is None:
            return {}
        candidate = install[0] / 'packages/codex-agents/runtime-releases.json'
    try:
        if candidate.is_symlink():
            return {}
        with candidate.open('rb') as stream:
            raw = stream.read(MAX_RELEASE_BYTES + 1)
        if len(raw) > MAX_RELEASE_BYTES:
            return {}
        data = json.loads(raw)
        if type(data) is not dict:
            return {}
        versions = {}
        for version, digest in data.items():
            if (_version_tuple(version) is None or type(digest) is not str
                    or re.fullmatch('[0-9a-f]{64}', digest) is None or digest in versions):
                return {}
            versions[digest] = version
        return versions
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}


def verified_predecessor(identity, current_version, *, releases=None):
    releases = release_versions() if releases is None else releases
    try:
        version, current = _version_tuple(identity['version']), _version_tuple(current_version)
        return (version is not None and current is not None and version < current
                and releases.get(identity['digest']) == identity['version'])
    except (TypeError, KeyError):
        return False


def successor_custody(ledger, custody_digest, current_digest, *, releases=None, current_version=None,
                      project=False):
    """Follow at most 16 immutable, increasing, catalogue-verified custody links.

    Call only after ingress authenticated a current-runtime request. `ledger`
    may be its owner's connection (also used by the project owner).
    """
    if (type(custody_digest) is not str or type(current_digest) is not str
            or re.fullmatch('[0-9a-f]{64}', custody_digest) is None
            or re.fullmatch('[0-9a-f]{64}', current_digest) is None):
        return False
    if custody_digest == current_digest:
        return True
    if current_version is None:
        from .collector_client import PACKAGE_VERSION
        current_version = PACKAGE_VERSION
    releases = release_versions() if releases is None else releases
    current = _version_tuple(current_version)
    if current is None:
        return False
    connection = ledger if isinstance(ledger, sqlite3.Connection) else ledger._connection
    table = 'takeover_record' if project else 'runtime_lineage'
    digest, seen, prior = custody_digest, set(), None
    try:
        for _ in range(MAX_LINEAGE_LINKS):
            if digest in seen:
                return False
            seen.add(digest)
            rows = connection.execute('SELECT predecessor_version,successor_digest,successor_version FROM '
                + table + ' WHERE predecessor_digest=?', (digest,)).fetchall()
            if len(rows) != 1:
                return False
            old_text, next_digest, next_text = tuple(rows[0])
            old, next_version = _version_tuple(old_text), _version_tuple(next_text)
            if (old is None or next_version is None or releases.get(digest) != old_text or not old < next_version
                    or not old < current or (prior is not None and old != prior)
                    or type(next_digest) is not str or re.fullmatch('[0-9a-f]{64}', next_digest) is None):
                return False
            if next_digest == current_digest:
                return next_version == current
            if next_version >= current or releases.get(next_digest) != next_text:
                return False
            digest, prior = next_digest, next_version
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return False


def protected_lineage(edges, custody_digests, current_digest):
    """The edges live custody names: every path from a custody digest up to the current runtime.

    One visited set for all paths (a walk stops at an edge an earlier walk already kept, whose rest of the
    path that walk kept too), so this is linear in the edges however much the paths overlap (S14F-5).
    """
    protected = set()
    for digest in custody_digests:
        while digest in edges and digest not in protected and digest != current_digest:
            protected.add(digest)
            digest = edges[digest]
    return protected


def prune_lineage(db, custody_digests, current_digest, *, transaction=None):
    """Keep every path named by live custody, even beyond the verification limit.

    No per-digest cap applies to protected rows. Unreferenced edges cannot be
    used by any of the six successor-custody operations and are removed.
    """
    rows = db.execute('SELECT predecessor_digest,successor_digest FROM runtime_lineage').fetchall()
    edges = dict(rows)
    protected = protected_lineage(edges, custody_digests, current_digest)
    removed = 0
    context = db if transaction is None else transaction()
    with context:
        for digest in edges.keys() - protected:
            removed += db.execute('DELETE FROM runtime_lineage WHERE predecessor_digest=?', (digest,)).rowcount
    return removed


def live_ledger_custody(db, now):
    """Stored custody used by H3b: claimed runs, observed/unlaunched bindings.

    active_run_capacity carries a claimed runtime through terminal, cleanup,
    lost settlement and replay. launch_inputs carries the original runtime
    before a claim (including a denial or unlaunched closure). Their binding
    and run-hold rows keep them live; the existing open replay window keeps
    released inputs replayable. Never infer a runtime from a new sender.
    """
    from .learning_store import OPEN_REQUEST_RETENTION_MS
    cutoff = now - OPEN_REQUEST_RETENTION_MS
    digests = {row[0] for row in db.execute('SELECT runtime_digest FROM active_run_capacity '
        'WHERE runtime_digest IS NOT NULL AND (released_at_ms IS NULL OR released_at_ms>=?)', (cutoff,))}
    digests.update(row[0] for row in db.execute(
        'SELECT i.runtime_digest FROM launch_inputs i JOIN host_run_bindings h '
        'ON h.project_id=i.project_id AND h.handle_digest=i.handle_digest '
        'WHERE h.expires_at_ms>=? OR EXISTS(SELECT 1 FROM run_holds r '
        "WHERE r.project_id=i.project_id AND r.kind IN ('run_slot','binding') "
        "AND r.identity=i.handle_digest AND r.state<>'released')", (cutoff,)))
    return digests


CUSTODY_PASS_ROWS = 256  # S14F-1/-5: units of work one owner-thread pass may do (rows, edges, walk steps, deletes)

# S14F-6: a per-connection change watch. TEMP triggers (never in the stored schema, never copied by a backup) bump
# a TEMP counter whenever a change could ADD custody for a lineage edge key, or change the edges themselves.
# Deletes and release transitions only remove custody, so they never invalidate a cycle; a row that reappears
# (delete + reinsert, whatever its rowid) is an insert and is caught. The counter row is created by
# CREATE ... AS SELECT, which counts as no row change, so connection.total_changes watchers see nothing.
_WATCH = 'cocolo_custody_watch'
_EDGE_KEY = 'EXISTS(SELECT 1 FROM runtime_lineage l WHERE l.predecessor_digest={})'
_EDGE_INPUT = ('EXISTS(SELECT 1 FROM launch_inputs i JOIN runtime_lineage l ON l.predecessor_digest=i.runtime_digest '
               'WHERE i.project_id=NEW.project_id AND i.handle_digest={})')
_HOLD_LIVE = "{0}.kind IN ('run_slot','binding') AND {0}.state<>'released'"
_TRIGGERS = (
    ('lineage_insert', 'INSERT ON main.runtime_lineage', '1'),
    ('lineage_update', 'UPDATE ON main.runtime_lineage', '1'),
    ('run_insert', 'INSERT ON main.active_run_capacity', _EDGE_KEY.format('NEW.runtime_digest')),
    ('run_update', 'UPDATE ON main.active_run_capacity', _EDGE_KEY.format('NEW.runtime_digest')
        + ' AND NOT (OLD.runtime_digest IS NEW.runtime_digest AND NEW.released_at_ms IS NOT NULL'
        ' AND (OLD.released_at_ms IS NULL OR NEW.released_at_ms<=OLD.released_at_ms))'),
    ('input_insert', 'INSERT ON main.launch_inputs', _EDGE_KEY.format('NEW.runtime_digest')),
    ('input_update', 'UPDATE ON main.launch_inputs', _EDGE_KEY.format('NEW.runtime_digest')),
    ('binding_insert', 'INSERT ON main.host_run_bindings', _EDGE_INPUT.format('NEW.handle_digest')),
    ('binding_update', 'UPDATE ON main.host_run_bindings', _EDGE_INPUT.format('NEW.handle_digest')
        + ' AND NOT (OLD.project_id IS NEW.project_id AND OLD.handle_digest IS NEW.handle_digest'
        ' AND (NEW.expires_at_ms IS NULL OR (OLD.expires_at_ms IS NOT NULL AND NEW.expires_at_ms<=OLD.expires_at_ms)))'),
    ('hold_insert', 'INSERT ON main.run_holds', _HOLD_LIVE.format('NEW') + ' AND ' + _EDGE_INPUT.format('NEW.identity')),
    ('hold_update', 'UPDATE ON main.run_holds', _HOLD_LIVE.format('NEW') + ' AND ' + _EDGE_INPUT.format('NEW.identity')
        + ' AND NOT (' + _HOLD_LIVE.format('OLD') + ' AND OLD.project_id IS NEW.project_id AND OLD.identity IS NEW.identity)'),
)


def _install_watch(db):
    """Idempotent; separate statements (executescript would commit a caller's open transaction)."""
    db.execute('CREATE TEMP TABLE IF NOT EXISTS ' + _WATCH + ' AS SELECT 0 AS changes')
    for name, event, when in _TRIGGERS:
        db.execute('CREATE TEMP TRIGGER IF NOT EXISTS ' + _WATCH + '_' + name + ' AFTER ' + event
                   + ' WHEN ' + when + ' BEGIN UPDATE ' + _WATCH + ' SET changes=changes+1; END')


class CustodyScan:
    """S14F: live_ledger_custody and the lineage prune in bounded passes, for the sole owner thread.

    The retained history grows with released runs, so the sweep may not recompute it in one owner turn.
    A cycle reads the lineage edges (paged by key), then the history (rowid windows of active_run_capacity,
    then launch_inputs) keeping only digests that are edge keys (no other digest protects anything in
    `prune_lineage`), then walks the protected paths with one visited set, then deletes the rest, each
    pass doing at most `bound` units of work in total.

    Safety (custody may never be under-counted where it is used to prune):
    - one cutoff per cycle, fixed at its start (S14F-4). A later pass with a later clock judges against the
      earlier cutoff, which keeps more custody. A pass whose clock is before the cycle start restarts the
      cycle, since the start cutoff would keep less than the whole answer at that earlier time.
    - the change watch (S14F-6): a cycle continues only while the watch counter, PRAGMA data_version (a
      commit by another connection) and main.schema_version (VACUUM, a table rebuild) are unchanged and it
      runs on the same connection. Otherwise every change since its start could only have removed custody.
    """

    TABLES = ('active_run_capacity', 'launch_inputs')

    def __init__(self):
        self.visited = 0  # history rows visited over all passes
        self.work = 0  # units of work in the last pass
        self.cycles = 0  # completed prune cycles
        self.reset()

    def reset(self):
        self.db = self.generation = self.start = None
        self.stage, self.cursor, self.phase = 'edges', '', 0
        self.edges, self.order, self.found = {}, [], set()
        self.queue = self.at = None
        self.protected, self.index = set(), 0

    @staticmethod
    def _generation(db):
        return (db.execute('SELECT changes FROM temp.' + _WATCH).fetchone()[0],
                db.execute('PRAGMA data_version').fetchone()[0],
                db.execute('PRAGMA main.schema_version').fetchone()[0])

    def _begin(self, db, now):
        if self.start is not None and (db is not self.db or now < self.start
                                       or self._generation(db) != self.generation):
            self.reset()
        if self.start is None:
            _install_watch(db)
            self.db, self.start, self.generation = db, now, self._generation(db)

    def _scan(self, db, budget):
        """Edges then history rows; returns the budget left. Stage becomes 'walk' when both are read."""
        from .learning_store import OPEN_REQUEST_RETENTION_MS
        cutoff = self.start - OPEN_REQUEST_RETENTION_MS
        while budget > 0 and self.stage == 'edges':
            rows = db.execute('SELECT predecessor_digest,successor_digest FROM runtime_lineage '
                              'WHERE predecessor_digest>? ORDER BY predecessor_digest LIMIT ?',
                              (self.cursor, budget)).fetchall()
            self.edges.update(rows)
            self.order.extend(row[0] for row in rows)
            if len(rows) < budget:
                self.stage, self.cursor = ('rows' if self.edges else 'walk'), 0
            else:
                self.cursor = rows[-1][0]
            budget -= max(1, len(rows))
        while budget > 0 and self.stage == 'rows':
            if self.phase == len(self.TABLES):
                self.stage = 'walk'
                break
            table = self.TABLES[self.phase]
            first = db.execute('SELECT MIN(rowid) FROM ' + table + ' WHERE rowid>?', (self.cursor,)).fetchone()[0]
            if first is None:
                self.phase, self.cursor = self.phase + 1, 0
                budget -= 1
                continue
            last = first + budget - 1
            if self.phase == 0:
                rows = db.execute('SELECT runtime_digest FROM active_run_capacity WHERE rowid>=? AND rowid<=? '
                    'AND runtime_digest IS NOT NULL AND (released_at_ms IS NULL OR released_at_ms>=?)',
                    (first, last, cutoff)).fetchall()
            else:
                rows = db.execute('SELECT i.runtime_digest FROM launch_inputs i LEFT JOIN host_run_bindings h '
                    'ON h.project_id=i.project_id AND h.handle_digest=i.handle_digest WHERE i.rowid>=? AND i.rowid<=? '
                    'AND (h.expires_at_ms>=? OR EXISTS(SELECT 1 FROM run_holds r '
                    "WHERE r.project_id=i.project_id AND r.kind IN ('run_slot','binding') "
                    "AND r.identity=i.handle_digest AND r.state<>'released'))",
                    (first, last, cutoff)).fetchall()
            visited = db.execute('SELECT COUNT(*) FROM ' + table + ' WHERE rowid>=? AND rowid<=?',
                                 (first, last)).fetchone()[0]
            self.found.update(row[0] for row in rows if row[0] in self.edges)
            self.cursor = last
            budget -= max(1, visited)
            self.visited += max(1, visited)
        return budget

    def step(self, db, now, bound=CUSTODY_PASS_ROWS):
        """Return the custody digests (limited to lineage edge keys) when a scan cycle completes, else None."""
        self._begin(db, now)
        budget = self._scan(db, bound)
        self.work = bound - budget
        if self.stage != 'walk':
            return None
        found, valid = self.found, self._generation(db) == self.generation
        self.reset()
        return found if valid else None

    def advance(self, db, now, current_digest, *, transaction=None, bound=CUSTODY_PASS_ROWS):
        """One owner pass of the whole prune cycle; returns the edges it deleted (0 until the cycle prunes)."""
        self._begin(db, now)
        budget = bound
        if self.stage in ('edges', 'rows'):
            budget = self._scan(db, budget)
            if self.stage == 'walk' and self._generation(db) != self.generation:
                self.reset()
                self.work = bound - budget
                return 0
        if self.stage == 'walk':
            if self.queue is None:
                self.queue = list(self.found)
            while budget > 0 and (self.queue or self.at is not None):
                digest = self.queue.pop() if self.at is None else self.at
                if digest in self.edges and digest not in self.protected and digest != current_digest:
                    self.protected.add(digest)
                    self.at = self.edges[digest]
                else:
                    self.at = None
                budget -= 1
            if not self.queue and self.at is None:
                self.stage = 'delete'
        removed = 0
        if self.stage == 'delete' and budget > 0:
            # _begin checked this pass's clock and the watch; nothing has run on this connection since
            context = db if transaction is None else transaction()
            with context:
                while budget > 0 and self.index < len(self.order):
                    digest = self.order[self.index]
                    self.index += 1
                    budget -= 1
                    if digest not in self.protected:
                        removed += db.execute('DELETE FROM runtime_lineage WHERE predecessor_digest=?',
                                              (digest,)).rowcount
            if self.index >= len(self.order):
                self.cycles += 1
                self.reset()
        self.work = bound - budget
        return removed


def lineage_status(db):
    count = db.execute('SELECT COUNT(*) FROM runtime_lineage').fetchone()[0]
    if count <= 64:
        return None
    return (f'Update records: {count} links to earlier CoCoLo versions are kept because unfinished runs still need them. '
            'They clear by themselves as those runs settle. Nothing needs doing.')
