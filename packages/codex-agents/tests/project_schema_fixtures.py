"""Explicit pre-cleanup schema fixtures; no production migration shortcuts."""
from dataclasses import replace

from codex_agents.claim_storage import lease_state_seal


def legacy_cleanup_layout(store):
    """Restore the exact v8 lease layout/seal before an old-version snapshot.

    Fixtures have synthetic writer keys. Do not invent cleanup reservations in
    historical schemas, or silently omit new columns during snapshot comparison.
    """
    rows = store._connection.execute('SELECT * FROM result_leases').fetchall()
    for row in rows:
        value = replace(store._lease(row), cleanup_reservation_digest=None)
        store._connection.execute('UPDATE result_leases SET cleanup_reservation_digest=NULL,state_digest=? '
            'WHERE project_id=? AND run_handle=? AND attempt=?',
            (lease_state_seal(store._result_key, value), value.binding.project_id,
             value.binding.run_handle, value.binding.attempt))
    store._connection.execute('DROP TABLE project_cleanup_receipts')
    store._connection.execute('DROP TABLE project_cleanup_reservations')
    store._connection.execute('ALTER TABLE result_leases DROP COLUMN cleanup_reservation_digest')


def assert_migrated_rows(testcase, store, table, before):
    after = [tuple(row) for row in store._connection.execute('SELECT * FROM ' + table)]
    if table == 'result_leases':
        testcase.assertEqual([(*row, None) for row in before], after)
    else:
        testcase.assertEqual(before, after)
