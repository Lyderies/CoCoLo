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


def prune_lineage(db, custody_digests, current_digest, *, transaction=None):
    """Keep every path named by live custody, even beyond the verification limit.

    No per-digest cap applies to protected rows. Unreferenced edges cannot be
    used by any of the six successor-custody operations and are removed.
    """
    rows = db.execute('SELECT predecessor_digest,successor_digest FROM runtime_lineage').fetchall()
    edges = dict(rows)
    protected = set()
    for digest in custody_digests:
        seen = set()
        while digest in edges and digest not in seen and digest != current_digest:
            seen.add(digest)
            protected.add(digest)
            digest = edges[digest]
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


CUSTODY_PASS_ROWS = 256  # S14F-1: rows of history one owner-thread maintenance pass may visit


class CustodyScan:
    """S14F-1: live_ledger_custody in bounded passes, for the sole owner thread.

    The retained history grows with released runs, so the sweep may not recompute it in one owner turn.
    Each `step` visits at most `bound` rows (rowid windows of active_run_capacity, then launch_inputs) and
    keeps only the digests that name a lineage edge: a custody digest that is no edge's predecessor protects
    nothing in `prune_lineage`, so this is exactly the same answer. The answer is released only when one
    cycle has seen every row. A cycle is thrown away (never used to prune) when the edge set changed or a
    source table's highest rowid fell (a deleted last row may let a new row reuse a visited rowid), so a
    cycle can only miss a run that appeared behind its cursor, and runs of a predecessor runtime cannot
    appear once that runtime has a lineage edge (takeover fences it). Each pass uses its own clock; an
    earlier pass saw an earlier cutoff, which can only keep more custody, never less.
    """

    TABLES = ('active_run_capacity', 'launch_inputs')

    def __init__(self):
        self.visited = 0
        self.reset()

    def reset(self):
        self.keys = None
        self.phase = 0
        self.cursor = 0
        self.found = set()
        self.maxes = None

    def _maxes(self, db):
        return tuple(db.execute('SELECT MAX(rowid) FROM ' + table).fetchone()[0] or 0 for table in self.TABLES)

    def step(self, db, now, bound=CUSTODY_PASS_ROWS):
        """Return the custody digests (limited to lineage edge keys) when a cycle completes, else None."""
        from .learning_store import OPEN_REQUEST_RETENTION_MS
        cutoff = now - OPEN_REQUEST_RETENTION_MS
        keys = frozenset(row[0] for row in db.execute('SELECT predecessor_digest FROM runtime_lineage'))
        if self.keys is None or self.keys != keys:
            self.reset()
            self.keys = keys
            self.maxes = self._maxes(db)
        if not keys:
            self.reset()
            return set()
        budget = bound
        while budget > 0 and self.phase < len(self.TABLES):
            table = self.TABLES[self.phase]
            first = db.execute('SELECT MIN(rowid) FROM ' + table + ' WHERE rowid>?', (self.cursor,)).fetchone()[0]
            if first is None:
                self.phase, self.cursor = self.phase + 1, 0
                continue
            last = first + budget - 1
            if self.phase == 0:
                rows = db.execute('SELECT runtime_digest FROM active_run_capacity WHERE rowid>=? AND rowid<=? '
                    'AND runtime_digest IS NOT NULL AND (released_at_ms IS NULL OR released_at_ms>=?)',
                    (first, last, cutoff)).fetchall()
                visited = db.execute('SELECT COUNT(*) FROM active_run_capacity WHERE rowid>=? AND rowid<=?',
                                     (first, last)).fetchone()[0]
            else:
                rows = db.execute('SELECT i.runtime_digest FROM launch_inputs i LEFT JOIN host_run_bindings h '
                    'ON h.project_id=i.project_id AND h.handle_digest=i.handle_digest WHERE i.rowid>=? AND i.rowid<=? '
                    'AND (h.expires_at_ms>=? OR EXISTS(SELECT 1 FROM run_holds r '
                    "WHERE r.project_id=i.project_id AND r.kind IN ('run_slot','binding') "
                    "AND r.identity=i.handle_digest AND r.state<>'released'))",
                    (first, last, cutoff)).fetchall()
                visited = db.execute('SELECT COUNT(*) FROM launch_inputs WHERE rowid>=? AND rowid<=?',
                                     (first, last)).fetchone()[0]
            self.found.update(row[0] for row in rows if row[0] in keys)
            self.cursor = last
            budget -= max(1, visited)
            self.visited += max(1, visited)
        if self.phase < len(self.TABLES):
            return None
        done, maxes = self.found, self._maxes(db)
        stale = any(now_max < start_max for now_max, start_max in zip(maxes, self.maxes))
        self.reset()
        return None if stale else done


def lineage_status(db):
    count = db.execute('SELECT COUNT(*) FROM runtime_lineage').fetchone()[0]
    if count <= 64:
        return None
    return (f'Update records: {count} links to earlier CoCoLo versions are kept because unfinished runs still need them. '
            'They clear by themselves as those runs settle. Nothing needs doing.')
