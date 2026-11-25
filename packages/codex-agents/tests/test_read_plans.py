"""U3: explain the actual budgeted reads made by representative owner lifecycle flows.

A read whose work grows with history is, under a SQL work budget, a lifetime cap in time instead of count
(ANALYSIS-0.4.0-lifetime-caps.md U3), and on an open, claim or append path it slows every run for good. Query
plans decide it: the ledger and the project store keep no statistics, so a plan is the same on an empty store and
on one with years of history, and a plan that reads a table by a selective key, or from a partial (live-only)
index, does work bounded by live rows.

A "history read" is a plan step that scans a table that keeps history (SCAN, without a partial index), or that
looks it up only by coarse columns (project, type, state, kind, source; or a range on a sequence or row id),
from an index that is not partial. Tables whose rows stay few by construction are in BOUNDED.

Part 1 (budgeted reads, as they run): every statement that runs inside a SQL read budget during the lifecycle
tests in BUDGETED_FLOWS has no history read.
"""
from __future__ import annotations

import re
import sqlite3
import unittest
from unittest import mock

import tests.support  # noqa: F401

from codex_agents.sql_read_budget import SQLReadConnection
from test_framework import requirements

# Tables whose rows stay few by construction (one per project or per kind, or a small fixed set).
BOUNDED = frozenset({'projects', 'owner_key_history', 'additive_migrations', 'collector_health', 'hook_ingestion',
                     'history_identity', 'result_identity', 'project_learning_activation', 'cocolo_backup_manifest',
                     'capture_candidates', 'sqlite_master', 'sqlite_schema', 'sqlite_sequence'})
# Columns that name a project, a type or a state: many history rows share each value.
COARSE = frozenset({'project_id', 'record_type', 'source', 'kind', 'state', 'binding_state', 'status', 'provider',
                    'producer', 'rowid'})
# Representative owner flows: preflight, provenance, denial, probe reservation,
# observation reservation, admission capture and attestation capture. Growth
# tests also explain registration, terminal and candidate-lineage reads.
BUDGETED_FLOWS = (
    'tests.test_account_original_open.OriginalAccountOpenPendingTests.'
    'test_original_pending_clock_is_durable_before_signatures_without_opening_a_run',
    'tests.test_host_open_provenance.HostOpenProvenanceTests.test_original_open_is_read_before_claim_registration_or_new_clock',
    'tests.test_run_provenance.RunProvenanceTests.test_original_registered_claim_admission_and_family_are_stable_metadata',
    'tests.test_claim_denial_recovery.ClaimDenialRecoveryTests.'
    'test_legacy_intent_without_launch_marker_is_unknown_not_an_unused_attempt',
    'tests.test_account_billing_admission.AccountBillingAdmissionTests.'
    'test_B0_5_probes_in_progress_still_fill_the_cap',
    'tests.test_account_observation.AccountObservationTests.'
    'test_changed_request_or_context_does_not_rebind_original_pending_work',
    'tests.test_read_growth.ReadGrowthTests.test_U3_6_the_admission_capture_reads_only_evidence_that_can_still_count',
    'tests.test_read_growth.ReadGrowthTests.test_U3_7_owner_attestations_are_read_by_type_as_history_grows',
)

_ALIAS_STOP = {'WHERE', 'ON', 'JOIN', 'LEFT', 'INNER', 'CROSS', 'USING', 'INDEXED', 'NOT', 'ORDER', 'GROUP', 'LIMIT',
               'UNION', 'SET', 'WHEN', 'AND', 'OR', 'VALUES', 'SELECT', 'AS', 'NATURAL', 'OUTER', 'EXCEPT', 'INTERSECT'}


def normalized(sql: str) -> str:
    return ' '.join(sql.split())


class Schema:
    """A store's indexes, partial or not, and their columns; and its tables."""

    def __init__(self, connection):
        self.connection = connection
        self.tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.partial, self.columns = {}, {}
        for name, sql in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='index'"):
            self.partial[name] = bool(sql) and re.search(r'\)\s*WHERE\s', sql, re.S) is not None
            self.columns[name] = [row[2] for row in connection.execute(f'PRAGMA index_info("{name}")')]



def _aliases(sql):
    aliases = {}
    for match in re.finditer(r'\b(?:FROM|JOIN)\s+([A-Za-z_]\w*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?', sql, re.I):
        aliases.setdefault(match.group(1), match.group(1))
        if match.group(2) and match.group(2).upper() not in _ALIAS_STOP:
            aliases[match.group(2)] = match.group(1)
    return aliases


def history_reads(schema, sql, plan):
    """The plan steps that read history, as described in the module docstring."""
    aliases, found = _aliases(sql), []
    # A plain indexed prefix count stops at the explicit caller cap; there is
    # no residual predicate that could walk past it into historical rows.
    bounded_prefix_count = re.fullmatch(
        r'SELECT COUNT\(\*\) FROM \(SELECT 1 FROM (\w+) WHERE project_id=\? LIMIT \?\)', normalized(sql))
    for line in plan:
        scan = re.match(r'SCAN (\w+)(?: USING (?:COVERING )?INDEX (\w+))?$', line)
        search = re.match(r'SEARCH (\w+) USING (?:COVERING )?(?:INDEX (\w+)|PRIMARY KEY) \((.*)\)$', line)
        if scan:
            table = aliases.get(scan.group(1), scan.group(1))
            if table in schema.tables and table not in BOUNDED and not schema.partial.get(scan.group(2)):
                found.append(line)
        elif search:
            table, index = aliases.get(search.group(1), search.group(1)), search.group(2)
            if (table not in schema.tables or table in BOUNDED or schema.partial.get(index)
                    or bounded_prefix_count and bounded_prefix_count[1] == table):
                continue
            terms = search.group(3).replace('<expr>', 'indexed_expression').split(' AND ')
            parsed = [re.fullmatch(r'(.*?)([=<>]+)\?', term) for term in terms]
            columns = [match[1] if match else term for match, term in zip(parsed, terms)]
            ranged = [column for column, match in zip(columns, parsed) if match is None or match[2] != '=']
            if any(column not in COARSE for column, term in zip(columns, terms) if column not in ranged):
                continue  # a selective key
            if any(column.endswith('_ms') for column in ranged):
                continue  # a time window
            following = schema.columns.get(index, [])[len(columns):len(columns) + 1]
            if not ranged and following and re.search(rf'\b(?:MAX|MIN)\(\s*(?:\w+\.)?{following[0]}\s*\)', sql, re.I):
                continue  # one end of the index (MIN/MAX of its next column)
            found.append(line)
    return found


class ReadPlanTests(unittest.TestCase):
    maxDiff = None
    @requirements("U3", why_not_scenario="Classifier controls use an in-memory synthetic schema.")
    def test_U3_guard_rejects_residual_scans_and_accepts_expression_seeks(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE records(project_id TEXT, record_type TEXT, content TEXT)')
        db.execute('CREATE INDEX broad ON records(project_id,record_type)')
        expression = "json_extract(content,'$.run_id')"
        sql = "SELECT 1 FROM records WHERE project_id=? AND record_type='event' AND " + expression + "=? LIMIT 1"
        schema = Schema(db)
        before = [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql, ('p','absent'))]
        self.assertTrue(history_reads(schema, sql, before))
        db.execute('CREATE INDEX selective ON records(project_id,' + expression + ',record_type)')
        schema = Schema(db)
        after = [r[3] for r in db.execute('EXPLAIN QUERY PLAN ' + sql, ('p','absent'))]
        self.assertEqual([], history_reads(schema, sql, after))

    @requirements("U3", why_not_scenario="SQL plans require the actual disposable owner connection and parameters.")
    def test_U3_6_every_budgeted_read_that_runs_uses_a_live_or_selective_index(self):
        seen, pending, schemas, plans = {}, {}, {}, {}
        real_execute = SQLReadConnection.execute
        real_scope = SQLReadConnection._scoped_read

        def execute(connection, sql, parameters=(), /):
            if connection._sql_scope is not None and type(sql) is str and sql.lstrip().upper().startswith(('SELECT', 'WITH')):
                pending.setdefault(connection, {})[normalized(sql)] = (sql, parameters)
            return real_execute(connection, sql, parameters)

        def scoped(connection, *args, **kwargs):
            value = real_scope(connection, *args, **kwargs)
            # Explain after the production scope has restored its settings, while
            # this owner connection (and its TEMP membership projection) is live.
            if connection not in schemas:
                schemas[connection] = Schema(connection)
            for key, (sql, parameters) in pending.pop(connection, {}).items():
                plan = [row[3] for row in real_execute(connection, 'EXPLAIN QUERY PLAN ' + sql, parameters)]
                plans[key] = {'sql': normalized(sql), 'plan': plan}
                seen[(id(connection), key)] = history_reads(schemas[connection], sql, plan)
            return value

        suite = unittest.defaultTestLoader.loadTestsFromNames(BUDGETED_FLOWS)
        result = unittest.TestResult()
        with (mock.patch.object(SQLReadConnection, 'execute', execute),
              mock.patch.object(SQLReadConnection, '_scoped_read', scoped)):
            suite.run(result)
        self.assertEqual([], [str(test) for test, _ in result.failures + result.errors], result.failures + result.errors)
        self.assertEqual(len(BUDGETED_FLOWS), result.testsRun)
        self.budgeted_plans = list(plans.values())
        self.assertGreater(len(seen), 40)
        self.assertEqual({}, {key[1]: reads for key, reads in seen.items() if reads})


if __name__ == '__main__':
    unittest.main()
