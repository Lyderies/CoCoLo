"""The launch_receipts layout is frozen to the schema it was shipped with.

Requirement: an existing ledger (schema 15, made by 0.8.45) is checked against the receipts DDL at every open
(`_assert_launch_schema_locked`). The DDL therefore may only change together with a ledger schema bump and a
versioned upgrade. Admission reason codes grow with the billing work (probe_replay_stale, BR-13); the receipt
table never records them, so its CHECK list must not follow them.
"""
import hashlib
import sqlite3
import unittest

from codex_agents import learning_store
from codex_agents.collector_contracts import LEDGER_SCHEMA_VERSION

# sha256 of the whitespace-normalized sqlite_master sql, as tests/fixtures/learning/schema11-layout.json records it.
FROZEN_FOR_SCHEMA_15 = 'e2d505aac99614b436bb135af66d7e62c9bedf10309cb4688e6cce552c27fa9f'


def stored_layout():
    db = sqlite3.connect(':memory:')
    db.execute(learning_store._LAUNCH_RECEIPTS_SCHEMA)
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='launch_receipts'").fetchone()[0]
    return hashlib.sha256(' '.join(sql.replace('"', '').split()).encode()).hexdigest()


class LaunchReceiptsLayoutTests(unittest.TestCase):
    def test_receipts_ddl_changes_only_with_a_schema_bump(self):
        self.assertEqual(16, LEDGER_SCHEMA_VERSION,
                         'the ledger schema changed: add the new layout hash and its upgrade, then update this test')
        self.assertEqual(FROZEN_FOR_SCHEMA_15, stored_layout())

    def test_receipts_do_not_admit_a_later_admission_reason(self):
        db = sqlite3.connect(':memory:')
        db.execute(learning_store._LAUNCH_RECEIPTS_SCHEMA)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='launch_receipts'").fetchone()[0]
        self.assertNotIn('probe_replay_stale', sql)


if __name__ == '__main__':
    unittest.main()
