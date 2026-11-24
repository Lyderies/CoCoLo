"""Explicit pre-cleanup schema fixtures; no production migration shortcuts."""
from dataclasses import replace

from codex_agents.claim_storage import lease_state_seal


SCHEMA_18_TABLES = ('project_holds', 'project_result_contexts', 'project_result_publications',
                    'project_account_settlements')


def legacy_orchestration_state_layout(store):
    """Remove only the schema21 layout additions (B6, decision 131) from a newer fixture: the six indexes (two added by the
    B6 review r1, one by the final fix) and the orchestration state column. B6's held orchestration rows are kept: the 20 -> 21 step adopts only the open
    orchestrations without one, so a fixture's rows come back unchanged (a real format 20 store, with none, is
    tests/fixtures/history_step20, test_b6_unattended_close)."""
    for name in ('project_orchestrations_state', 'result_reservations_live', 'project_holds_orchestration_check',
                 'result_reservations_orchestration', 'result_leases_orchestration',
                 'project_execution_consumptions_orchestration'):  # the sixth: B6 final fix (decision 210)
        store._connection.execute('DROP INDEX IF EXISTS ' + name)
    if 'state' in {row[1] for row in store._connection.execute('PRAGMA table_info(project_orchestrations)')}:
        store._connection.execute('ALTER TABLE project_orchestrations DROP COLUMN state')


def legacy_project_holds_layout(store):
    """Remove only the schema18 register and custody tables (slice SB, SH-1) from an older fixture.

    (requirement) B1: every lease and reservation take now writes its register row, so the lease and reservation
    rows go with the table (an older format never had them; the 17 -> 18 step adopts them again). Any other row is
    still refused."""
    legacy_orchestration_state_layout(store)  # schema21 (B6): an older fixture predates schema 21 too
    # B6: an open orchestration's held register row goes with the table (a format 17 store never had one).
    store._connection.execute("DELETE FROM project_holds WHERE kind='orchestration' AND state='held'")
    if store._connection.execute(
            "SELECT COUNT(*) FROM project_holds WHERE kind NOT IN ('lease','reservation')").fetchone()[0]:
        raise AssertionError('legacy fixture contains a hold, context, publication or settlement row')
    # D4 (decision 123 A): the prepare step keeps a reservation's material; an older format never had it.
    store._connection.execute('DELETE FROM project_result_publications WHERE claim_id IN (SELECT claim_id FROM result_reservations)')
    for name in SCHEMA_18_TABLES[1:]:
        if store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
            raise AssertionError('legacy fixture contains a hold, context, publication or settlement row')
    for name in SCHEMA_18_TABLES:
        store._connection.execute('DROP TABLE ' + name)
    store._connection.execute('DROP INDEX IF EXISTS project_registered_runs_run')  # schema19 (B3), on a table older formats keep
    store._connection.execute('DROP TABLE IF EXISTS project_result_recorders')  # schema20 (B2, decision 114.1 A)


def legacy_registered_run_layout(store):
    """Remove only the empty schema17 registered-run table (S1-R6) from an older fixture."""
    legacy_project_holds_layout(store)  # (requirement) SB: an older fixture predates schema 18 too
    if store._connection.execute('SELECT COUNT(*) FROM project_registered_runs').fetchone()[0]:
        raise AssertionError('legacy fixture contains a registered run')
    store._connection.execute('DROP TABLE project_registered_runs')


def legacy_learning_activation_layout(store):
    """Remove only the empty schema16 cutover table from an older fixture."""
    legacy_registered_run_layout(store)
    for name in ('project_owner_attestations', 'project_learning_activation'):
        if store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
            raise AssertionError('legacy fixture contains a learning cutover or owner record')
        store._connection.execute('DROP TABLE ' + name)


def legacy_learning_source_layout(store):
    """Remove only empty schema15 archive tables from an older fixture."""
    legacy_learning_activation_layout(store)
    names = ('project_learning_source_chunks', 'project_learning_source_images')
    for name in names:
        if store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
            raise AssertionError('legacy fixture contains original source image')
    for name in names:
        store._connection.execute('DROP TABLE ' + name)


def legacy_learning_chunk_layout(store):
    """Remove only empty schema14 membership metadata for an older fixture."""
    legacy_learning_source_layout(store)
    name = 'project_learning_chunk_memberships'
    if store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
        raise AssertionError('legacy fixture contains original chunk membership')
    store._connection.execute('DROP TABLE ' + name)


def legacy_learning_qa_layout(store):
    """Remove only empty schema13 QA staging tables for an older fixture."""
    legacy_learning_chunk_layout(store)
    for name in ('project_learning_qa_chunks', 'project_learning_qa_records',
                 'project_learning_qa_imports'):
        if store._connection.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]:
            raise AssertionError('legacy fixture contains staged operational QA')
        store._connection.execute('DROP TABLE ' + name)


def legacy_learning_layout(store):
    """Remove derived index and empty learning staging for an older fixture."""
    legacy_learning_qa_layout(store)
    store._connection.execute("DROP INDEX records_learning_outcomes")
    for name in ("project_learning_import_chunks", "project_learning_attestations",
                 "project_learning_records", "project_learning_imports"):
        if store._connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]:
            raise AssertionError("legacy fixture contains staged learning data")
        store._connection.execute("DROP TABLE " + name)


def legacy_cleanup_layout(store):
    """Restore the exact v8 lease layout/seal before an old-version snapshot.

    Fixtures have synthetic writer keys. Do not invent cleanup reservations in
    historical schemas, or silently omit new columns during snapshot comparison.
    """
    legacy_learning_layout(store)
    rows = store._connection.execute('SELECT * FROM result_leases').fetchall()
    for row in rows:
        value = replace(store._lease(row), cleanup_reservation_digest=None)
        store._connection.execute('UPDATE result_leases SET cleanup_reservation_digest=NULL,state_digest=? '
            'WHERE project_id=? AND run_handle=? AND attempt=?',
            (lease_state_seal(store._result_key, value), value.binding.project_id,
             value.binding.run_handle, value.binding.attempt))
    store._connection.execute('DROP TABLE project_cleanup_acknowledgements')
    store._connection.execute('DROP TABLE project_cleanup_receipts')
    store._connection.execute('DROP TABLE project_cleanup_reservations')
    store._connection.execute('ALTER TABLE result_leases DROP COLUMN cleanup_reservation_digest')


def assert_migrated_rows(testcase, store, table, before):
    after = [tuple(row) for row in store._connection.execute('SELECT * FROM ' + table)]
    if table == 'result_leases':
        testcase.assertEqual([(*row, None) for row in before], after)
    elif table == 'project_orchestrations' and before and after and len(after[0]) == len(before[0]) + 1:
        # B6 (step 20 -> 21): every orchestration keeps its row and gains only its state column.
        testcase.assertEqual(before, [row[:-1] for row in after])
        testcase.assertTrue(all(row[-1] in ('open', 'closed') for row in after))
    else:
        testcase.assertEqual(before, after)
