"""HM-3: observe hold/source drift without acquiring a writer or releasing a hold.

The open/restore validators remain the authority. ValidatorPages applies their exact
predicates to bounded keyset windows of each driving table; correlated source reads
still seek their existing keys. No OFFSET, adoption, schema upgrade or repair runs.
Each predicate has its own cursor, so even a permanently broken row cannot starve
later rows. Readers have a SQLite progress deadline as well as bounded row counts.

Fault state belongs to the project process. Its D13 shutdown calls the service-side
part below after giving the driver its final step, before successor settlement.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import logging
from pathlib import Path
import re
import sqlite3
import time

from .health_monitor import Check, Finding

READ_PAGE_ROWS = 64
SQL_PROGRESS_OPS = 1000
_ROOT = re.compile(r'\bFROM ([a-z_]+)(?: ([a-z])\b)?(?: INDEXED BY [a-z_]+)?')


class _Reader(sqlite3.Connection):
    """A reader's stable file generation, used only for cross-page byte totals."""


def _file_epoch(path):
    records = []
    for source in (path, Path(str(path) + '-wal')):
        try:
            info = source.stat()
            records.append((info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            records.append(None)
    return tuple(records)


def open_reader(path):
    """Open an existing database only; no writer setup, migration or file creation."""
    path = Path(path)
    before = _file_epoch(path)
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.05, factory=_Reader)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')  # one consistent read snapshot, never a writer transaction
    connection.execute('PRAGMA schema_version').fetchone()  # pin that snapshot between the two observations
    connection.holds_epoch = before if before == _file_epoch(path) else None
    return connection


class ValidatorPages:
    """Connection facade for the existing validators, with per-statement rowid seeks.

    Pages are bounded *before* the mismatch filter: LIMIT 1 on a mismatch query alone
    would examine the entire healthy history. Rowid is SQLite's existing primary index.
    The outer source's forced partial index is removed only for this bounded rowid
    range; every original predicate and correlated primary-key lookup stays intact.
    """

    def __init__(self, connection, cursors, context, project_id=None):
        self.connection, self.cursors, self.context = connection, cursors, context
        self.project_id = project_id
        self.plans = []
        self.completed = set()
        self.executed = set()
        connection.set_progress_handler(lambda: int(context.expired()), SQL_PROGRESS_OPS)

    def create_function(self, *args, **kwargs):
        return self.connection.create_function(*args, **kwargs)

    def execute(self, sql, parameters=()):
        if self.context.expired():
            raise TimeoutError()
        if sql.startswith('PRAGMA '):
            return self.connection.execute(sql, parameters)
        self.executed.add(sql)
        root = _ROOT.search(sql)
        if root is None:
            raise ValueError('validator must have a driving source')
        table, alias = root.group(1), root.group(2) or root.group(1)
        live = (table in ('run_holds', 'project_holds')
                and f"WHERE {alias}.state<>'released'" in sql)
        first = ('', -1, 0) if live else (0,)
        after, total, earlier_epoch = self.cursors.get(sql, (first, 0, None))
        epoch = getattr(self.connection, 'holds_epoch', (id(self.connection), self.connection.total_changes))
        if 'SUM(context_size)' in sql and (epoch is None or epoch != earlier_epoch):
            # Never combine old byte sizes with a changed store's next page. The
            # aggregate restarts on change; relationship predicates keep progressing.
            after, total = first, 0
        if live:
            select = (f'SELECT rowid,state,next_check_ms FROM {table} INDEXED BY {table}_due '
                      "WHERE state<>'released' AND (state,next_check_ms,rowid)>(?,?,?) "
                      'ORDER BY state,next_check_ms,rowid LIMIT ?')
        else:
            select = f'SELECT rowid FROM {table} WHERE rowid>? ORDER BY rowid LIMIT ?'
        self.plans.append(self.connection.execute('EXPLAIN QUERY PLAN ' + select,
                                                 (*after, READ_PAGE_ROWS)).fetchall())
        rows = self.connection.execute(select, (*after, READ_PAGE_ROWS)).fetchall()
        if not rows:
            self.completed.add(sql)
        if not rows and after != first:
            after, total = first, 0
            rows = self.connection.execute(select, (*after, READ_PAGE_ROWS)).fetchall()
        last = ((rows[-1][1], rows[-1][2], rows[-1][0]) if live else (rows[-1][0],)) if rows else first
        self.cursors[sql] = (last, total, epoch)
        # The validators use one top-level WHERE, then optionally LIMIT 1. Their
        # subqueries follow that WHERE. Preserve parentheses around OR predicates.
        bounded = sql[:root.start()] + f'FROM {table}' + (f' {alias}' if alias != table else '') + sql[root.end():]
        start = bounded.find(' WHERE ', root.start())
        if start < 0:
            bounded += ' WHERE 1'
            start = bounded.index(' WHERE ', root.start())
        suffix = ' LIMIT 1' if bounded.endswith(' LIMIT 1') else ''
        end = len(bounded) - len(suffix) if suffix else len(bounded)
        selection = ','.join('?' for _ in rows) or 'NULL'
        project_filter = ''
        if self.project_id:
            project = self.project_id.replace("'", "''")
            project_filter = f" AND {alias}.project_id='{project}'"
        bounded = (bounded[:start] + f' WHERE {alias}.rowid IN ({selection}){project_filter} AND ('
                   + bounded[start + 7:end] + ')' + suffix)
        if self.project_id:
            # The project's identity is already known to this check. The original
            # scalar singleton read becomes a primary-key lookup rather than a scan.
            project = self.project_id.replace("'", "''")
            bounded = bounded.replace('SELECT project_id FROM history_identity',
                                      "SELECT project_id FROM history_identity WHERE project_id='" + project + "'")
        # Bind the window before the predicate's own parameters.
        args = (*[row[0] for row in rows], *parameters)
        self.plans.append(self.connection.execute('EXPLAIN QUERY PLAN ' + bounded, args).fetchall())
        result = self.connection.execute(bounded, args)
        if 'SUM(context_size)' in sql:
            total += result.fetchone()[0]
            if rows:
                self.cursors[sql] = (last, total, epoch)
            return _Scalar(total)
        return result


class _Scalar:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


def validate_project(pages, key):
    """Run all register predicates even after a mismatch, then the custody validator."""
    from . import project_holds_schema as schema
    register = [pages.execute(sql).fetchone() is None for sql in schema._REGISTER_INCONSISTENT]
    return all(register), schema.custody_consistent(pages, key)


def _finding(project_id, reason, text, subject='holds'):
    return Finding('holds', 'project' if project_id else 'machine', subject, 'reported', reason, text, project_id)


def _legacy_deadline():
    """The installed legacy predicate, with ambiguity decided after two indexed matches.

    One retained matching intent may prove a deadline; two or more prove ambiguity.
    Additional history cannot change that answer and must not be enumerated.
    """
    from .legacy_slots import deadline_sql
    source = ("(SELECT * FROM records INDEXED BY records_run WHERE project_id=a.project_id "
              "AND CASE WHEN json_valid(CAST(content AS TEXT)) THEN "
              "json_extract(CAST(content AS TEXT),'$.run_id') END=a.run_id "
              "AND record_type='dispatch_intent' AND source='lifecycle' LIMIT 2) d")
    return deadline_sql('a').replace('records d', source)


class HoldsCheck(Check):
    name = 'holds'

    def __init__(self, state, *, sweep_interval_seconds):
        self.state = state
        self.interval_ms = int(sweep_interval_seconds * 1000)
        self._project_after = ''
        self._cursors = {}
        self._seen = set()
        self._observed = {}
        self._latched = {}
        self._clean_pages = {}
        self._account_latched = {}
        self._account_clean = {}
        self._monotonic = time.monotonic

    def check_project(self, connection, project_id, context, key):
        if context.expired():
            return []
        cursors = self._cursors.setdefault(('project', project_id), {})
        pages = ValidatorPages(connection, cursors, context, project_id)
        register, custody = validate_project(pages, key)
        findings = []
        if not register:
            findings.append(_finding(project_id, 'hold_source_mismatch',
                                     'A held resource and its project record disagree; the supervisor must reconcile it.'))
        if not custody:
            findings.append(_finding(project_id, 'custody_mismatch',
                                     'A saved result and its held storage disagree; the supervisor must reconcile it.'))
        latched = self._latched.setdefault(project_id, {})
        clean = self._clean_pages.setdefault(project_id, set())
        if findings:
            latched.update((item.reason_code, item) for item in findings)
            clean.clear()
        else:
            clean.update(pages.completed)
            if clean.issuperset(pages.executed):
                latched.clear()
                clean.clear()
        findings = list(latched.values())
        floor = connection.execute('SELECT last_observed_at_ms FROM result_identity WHERE project_id=?',
                                   (project_id,)).fetchone()
        now = max(context.now_ms, floor[0] or 0) if floor else context.now_ms
        overdue = pages.execute(
            "SELECT 1 FROM project_holds h INDEXED BY project_holds_due WHERE h.state<>'released' "
            'AND h.due_at_ms IS NOT NULL AND h.due_at_ms<? AND h.next_check_ms<? LIMIT 1',
            (now - self.interval_ms, now - self.interval_ms)).fetchone()
        if overdue:
            findings.append(_finding(project_id, 'hold_overdue',
                                     'A held resource is more than one sweep late for its next check; the supervisor must act.'))
        return findings

    def check_account(self, connection, context, project_id=None):
        from . import run_holds_schema as schema
        if context.expired():
            return []
        projects = [(project_id,)] if project_id else connection.execute(
            'SELECT project_id FROM projects WHERE project_id>? ORDER BY project_id LIMIT ?',
            (self._project_after, READ_PAGE_ROWS)).fetchall()
        if project_id is None and not projects and self._project_after:
            self._project_after = ''
            projects = connection.execute(
                'SELECT project_id FROM projects WHERE project_id>? ORDER BY project_id LIMIT ?',
                ('', READ_PAGE_ROWS)).fetchall()
        findings = []
        for row in projects:
            if context.expired():
                break
            project = row[0]
            cursors = self._cursors.setdefault(('account', project), {})
            pages = ValidatorPages(connection, cursors, context, project)
            checks = [pages.execute(sql).fetchone() is None for sql in schema._INCONSISTENT]
            evidence = schema._evidence_matches(pages)
            latched = self._account_latched.setdefault(project, {})
            clean = self._account_clean.setdefault(project, set())
            if not all(checks) or not evidence:
                item = _finding(project, 'hold_source_mismatch',
                                'A held resource and its account record disagree; the supervisor must reconcile it.')
                latched[item.reason_code] = item
                clean.clear()
            else:
                clean.update(pages.completed)
                if clean.issuperset(pages.executed):
                    latched.clear()
                    clean.clear()
            findings.extend(latched.values())
            if self._account_overdue(pages, context):
                findings.append(_finding(project, 'hold_overdue',
                                         'A held resource is more than one sweep late for its next check; the supervisor must act.'))
        if project_id is None:
            self._project_after = projects[-1][0] if projects else ''
        return findings

    def _account_overdue(self, pages, context):
        from .active_run_capacity import crashed_claim
        from .legacy_slots import deadline_sql, register_legacy_functions
        register_legacy_functions(pages)
        cutoff = context.now_ms - self.interval_ms
        crashed, parameters = crashed_claim('a', cutoff, time.monotonic_ns())
        crashed = crashed.replace(deadline_sql('a'), _legacy_deadline())
        # The deadline must be a full sweep old on both installed clocks. The
        # reboot comparison itself still uses the current monotonic reference.
        parameters = (parameters[0], parameters[1] - self.interval_ms * 1_000_000, *parameters[2:])
        prefix = ("SELECT 1 FROM run_holds r INDEXED BY run_holds_due WHERE r.state<>'released' "
                  "AND r.state='held' AND r.next_check_ms<? AND (")
        sql = prefix + (
            "(r.kind='binding' AND EXISTS(SELECT 1 FROM host_run_bindings h WHERE h.project_id=r.project_id "
            "AND h.handle_digest=r.identity AND h.binding_state='pending' AND h.expires_at_ms<?)) "
            "OR (r.kind='probe' AND EXISTS(SELECT 1 FROM account_billing_probe_requests p "
            "WHERE p.project_id=r.project_id AND p.request_key=r.identity AND p.state='reserved' "
            "AND p.deadline_at_ms<?)) OR (r.kind='run_slot' AND EXISTS(SELECT 1 FROM active_run_capacity a "
            "WHERE a.project_id=r.project_id AND a.handle_digest=r.identity AND a.released_at_ms IS NULL AND "
            + crashed + '))) LIMIT 1')
        return pages.execute(sql, (cutoff, cutoff, cutoff, *parameters)).fetchone() is not None

    def run(self, context):
        if self.state is None or context.expired():
            return []
        project_id = None
        try:
            with closing(open_reader(self.state.database)) as account:
                account.set_progress_handler(lambda: int(context.expired()), SQL_PROGRESS_OPS)
                projects = account.execute('SELECT project_id FROM projects WHERE project_id>? '
                                           'ORDER BY project_id LIMIT ?',
                                           (self._project_after, READ_PAGE_ROWS)).fetchall()
                self._observed.pop(None, None)
                if not projects:
                    self._project_after = ''
                    # Retained state ends when its project leaves the registry.
                    self._cursors = {key: value for key, value in self._cursors.items() if key[1] in self._seen}
                    for cache in (self._observed, self._latched, self._clean_pages,
                                  self._account_latched, self._account_clean):
                        for deleted in set(cache) - self._seen:
                            del cache[deleted]
                    self._seen.clear()
                    projects = account.execute('SELECT project_id FROM projects WHERE project_id>? '
                                               'ORDER BY project_id LIMIT ?', ('', READ_PAGE_ROWS)).fetchall()
                for row in projects:
                    if context.expired():
                        break
                    project_id = row[0]
                    self._project_after = project_id
                    self._seen.add(project_id)
                    findings = self.check_account(account, context, project_id)
                    database = self.state.root / 'projects' / project_id / 'history.sqlite3'
                    if database.exists() and not context.expired():
                        with closing(open_reader(database)) as project:
                            findings.extend(self.check_project(project, project_id, context, self.state.digest_key()))
                    self._observed[project_id] = (self._monotonic(), findings)
        except (OSError, sqlite3.Error, TimeoutError):
            if not context.expired():
                self._observed[project_id] = (self._monotonic(), [_finding(
                    project_id, 'holds_unreadable', 'The held resources could not be checked; the check will try again.')])
        return self._findings()

    def _findings(self):
        now = self._monotonic()
        findings = []
        for checked, items in self._observed.values():
            for item in items:
                if (now - checked) * 1000 > self.interval_ms:
                    item = replace(item, text=item.text + ' This reading is older than one sweep.')
                findings.append(item)
        return findings


def stop_faulted_service(host, project_id, *, report=None):
    """D13's one permitted action: report the fault, then close; never settle here."""
    if not host.faulted():
        return None
    report = report or (lambda finding: logging.getLogger(__name__).warning(finding.text))
    finding = _finding(project_id, 'service_writer_fault',
                       'The project service can no longer save results; it is stopping so a new service can recover.',
                       'service')
    report(finding)
    try:
        host.close()
    except Exception:
        finding = _finding(project_id, 'service_stop_failed',
                           'The project service could not stop after it stopped saving results; its resources remain held.',
                           'service')
        report(finding)
    return finding
