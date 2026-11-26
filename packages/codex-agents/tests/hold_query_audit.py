"""Source-derived query-plan cases for the S14g tests (no load or timing measurements)."""
import ast
import importlib
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
import tests.support  # noqa: E402,F401
from tests import test_learning_store, test_result_leases  # noqa: E402
from codex_agents import (active_run_capacity, health_holds, learning_source_image, learning_store,
                          legacy_slots, project_backup, project_history, project_holds_schema, project_takeover)  # noqa: E402

HOLD = re.compile(r'\b(?:FROM|JOIN|UPDATE)\s+(run_holds|project_holds)\b', re.I)



def resolve(node, env):
    """Only strings and known pure SQL-building operations; never evaluate arbitrary runtime code."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.Attribute):
        return getattr(resolve(node.value, env), node.attr)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = resolve(node.left, env), resolve(node.right, env)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    if isinstance(node, ast.JoinedStr):
        return ''.join(str(resolve(n.value, env)) if isinstance(n, ast.FormattedValue) else n.value for n in node.values)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ('_in', '_attempt_of', 'deadline_sql', 'unverified_hold_condition'):
            return env[node.func.id](*(resolve(a, env) for a in node.args))
        if isinstance(node.func, ast.Attribute) and node.func.attr in ('join', 'format'):
            value = resolve(node.func.value, env)
            if isinstance(value, str):
                return getattr(value, node.func.attr)(*(resolve(a, env) for a in node.args))
    raise ValueError('dynamic expression')


def inventory():
    account = test_learning_store.LearningStoreTests(methodName='runTest')
    project = test_result_leases.ResultLeaseTests(methodName='runTest')
    account.setUp()
    project.setUp()
    try:
        # A small real-schema fixture with actual product-written rows, including the lease/custody writers.
        project.reserve(1)
        project.reserve(2)
        dbs = {'run_holds': account.ledger._connection, 'project_holds': project.store._connection}
        account.ledger._connection.create_function('run_hold_raw_digest', 1, lambda s: s)
        rows, unresolved = [], []
        root = Path(project_takeover.__file__).parent
        for path in sorted(root.glob('*.py')):
            if not HOLD.search(path.read_text()):
                continue
            module = importlib.import_module('codex_agents.' + path.stem)
            tree = ast.parse(path.read_text())
            env = dict(vars(module))
            env.update(crashed=active_run_capacity.crashed_claim('a', 2000000000000, 1)[0],
                       deadline_sql=legacy_slots.deadline_sql, condition='1', order='identity',
                       rowids=None, where='', selection='',
                       project_holds_schema=project_holds_schema)
            candidates = {}
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr)):
                    continue
                try:
                    value = resolve(node, env)
                except (NameError, KeyError, AttributeError, TypeError, ValueError, SyntaxError):
                    continue
                if not isinstance(value, str) or not HOLD.search(value):
                    continue
                if value.startswith(('CREATE ', '\nCREATE ')) or not value.lstrip().startswith(
                        ('SELECT ', 'UPDATE ', 'DELETE ', 'INSERT ', 'EXISTS(', '(EXISTS(', 'FROM ')):
                    continue
                if '{hold}' not in value and '{condition}' not in value:  # trigger template; expanded by each source-table writer
                    candidates.setdefault(value, set()).add(node.lineno)
            # Evaluate assembled global SQL tuples, not just the individual string fragments in their AST.
            for name, value in vars(module).items():
                values = value if isinstance(value, tuple) else (value,)
                for sql in values:
                    if isinstance(sql, str) and HOLD.search(sql) and sql.startswith(('SELECT ', 'EXISTS(', '(EXISTS(')):
                        line = next((n.lineno for n in tree.body if isinstance(n, ast.Assign) and
                                     any(isinstance(t, ast.Name) and t.id == name for t in n.targets)), None)
                        if line is not None:
                            candidates.setdefault(sql, set()).add(line)
            candidates = {sql: lines for sql, lines in candidates.items() if not any(
                sql != other and sql in other for other in candidates)}
            for sql, lines in sorted(candidates.items(), key=lambda pair: min(pair[1])):
                line = min(lines)
                table = HOLD.search(sql).group(1)
                statement = sql
                if sql.startswith('FROM '):
                    statement = 'SELECT 1 ' + sql
                elif sql.startswith(('EXISTS(', '(EXISTS(')):
                    outer = 'active_run_capacity a' if table == 'run_holds' else 'project_result_contexts c'
                    if 'o.project_id' in sql:
                        outer = 'project_orchestrations o'
                    statement = 'SELECT 1 FROM ' + outer + ' WHERE ' + sql
                if '{}' in statement:
                    from codex_agents.lost_run_closure import RELEASE_KINDS
                    statement = statement.format(','.join('?' * (len(RELEASE_KINDS) + 1)))
                if '{condition}' in statement:
                    statement = statement.format(condition='1', order='identity')
                params = {n: 1 for n in re.findall(r':([a-z_]\w*)', statement)} if re.search(r':[a-z_]\w*', statement) else (None,) * statement.count('?')
                try:
                    steps = [r[3] for r in dbs[table].execute('EXPLAIN QUERY PLAN ' + statement, params)]
                except sqlite3.Error as error:
                    unresolved.append({'file': path.name, 'line': line, 'sql': sql, 'error': str(error)})
                    continue
                rows.append({'file': path.name, 'line': line, 'lines': sorted(lines), 'sql': statement, 'plan': steps})
        # Capture the composed account overdue statement directly from the real runtime method.
        class Recorder:
            def __init__(self, db):
                self.db, self.calls = db, []
            def __getattr__(self, name):
                return getattr(self.db, name)
            def execute(self, sql, params=()):
                self.calls.append((sql, params))
                return self.db.execute(sql, params)
        from types import SimpleNamespace
        recorder = Recorder(dbs['run_holds'])
        health_holds.HoldsCheck(None, sweep_interval_seconds=1)._account_overdue(
            recorder, SimpleNamespace(now_ms=2000000000000), account.project_a_id)
        for sql, params in recorder.calls:
            if HOLD.search(sql):
                rows.append({'file': 'health_holds.py', 'line': 297, 'lines': [297], 'sql': sql,
                             'plan': [r[3] for r in dbs['run_holds'].execute('EXPLAIN QUERY PLAN ' + sql, params)]})
        # Dynamic table-name exporters have no literal hold-table SQL. Capture their real expansions too.
        class ExportRecorder(Recorder):
            def execute(self, sql, params=()):
                if HOLD.search(sql) and not sql.startswith('EXPLAIN '):
                    caller = sys._getframe(1)
                    rows.append({'file': Path(caller.f_code.co_filename).name, 'line': caller.f_lineno,
                                 'lines': [caller.f_lineno], 'sql': sql,
                                 'plan': [r[3] for r in self.db.execute('EXPLAIN QUERY PLAN ' + sql, params)]})
                return self.db.execute(sql, params)
        exporter = ExportRecorder(dbs['run_holds'])
        project_backup.estimate_bytes(exporter, account.project_a_id, learning_store.LEDGER_SCHEMA_VERSION)
        learning_store._snapshot_project_partition(exporter, account.project_a_id,
                                                  ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION)
        for last in (None, ('binding', 'identity')):
            learning_source_image._page(exporter, learning_store, account.project_a_id, 'file', 'run_holds', last)
        list(project_backup.table_rows(exporter, 'run_holds', list(learning_store._BACKUP_TABLE_COLUMNS['run_holds']),
                                       project_id=account.project_a_id, order="kind,identity"))
        window = ExportRecorder(dbs['project_holds'])
        owner = SimpleNamespace(_connection=window, project_id=project.store.project_id,
                                _OWNER=project_history.ProjectHistory._OWNER,
                                _HELD_WINDOW=project_history.ProjectHistory._HELD_WINDOW)
        project_history.ProjectHistory._held_window(owner, 'm', ('a', 'identity'), 10)
        # Health's state-only pages intentionally enumerate every live kind in a bounded index range.
        for table in dbs:
            sql = (f'SELECT rowid,state,next_check_ms FROM {table} INDEXED BY {table}_due '
                   "WHERE state<>'released' AND (state,next_check_ms,rowid)>(?,?,?) "
                   'ORDER BY state,next_check_ms,rowid LIMIT ?')
            rows.append({'file': 'health_holds.py', 'line': 103, 'lines': [103], 'sql': sql,
                         'plan': [r[3] for r in dbs[table].execute('EXPLAIN QUERY PLAN ' + sql, ('', -1, 0, 64))]})
        # Proper fragments are represented by the successfully assembled statement at their starting line.
        unresolved = [r for r in unresolved if not any(x['file'] == r['file'] and r['sql'] in x['sql'] for x in rows)]
        return {'sqlite': sqlite3.sqlite_version, 'fixture': 'LearningStoreTests + ResultLeaseTests, two product-written leases',
                'queries': rows, 'unresolved': unresolved}
    finally:
        project.doCleanups()
        account.tearDown()

