"""Durable pre-claim capacity; values/fixtures are not native/account authority."""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_project_results as fixtures
from tests import test_account_claim_operation as account_fixtures
from tests.learning_support import assignment
from tests.test_project_history import binding, terminal
from tests.project_schema_fixtures import assert_migrated_rows, legacy_cleanup_layout

from codex_agents.history_contracts import HistoryError
from codex_agents.account_api import account_request_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import LEASE_SCOPE_FIELDS, ResultCapacity, ResultLeaseBinding


def lease_binding(number=1):
    target = binding(number)
    return ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                              claim_request_id=f"claim-request-{number}", claim_request_digest=f"{number:064x}")


class ResultLeaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectResultTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    @property
    def store(self):
        return self.fixture.store

    def reopen(self, **changes):
        self.store.close()
        self.fixture.store = self.fixture.open(**changes)

    def reserve(self, number=1):
        return self.store.reserve_result_lease(lease_binding(number), assignment())

    def bind(self, number=1, **changes):
        options = dict(hard_deadline_at_ms=2000)
        options.update(changes)
        return self.store.bind_result_lease(lease_binding(number), binding(number), assignment(), **options)

    def test_reserve_has_no_future_claim_and_retains_exact_assignment_request_and_times(self):
        first = self.reserve()
        self.assertEqual((1000, 61_000, "reserved"), (first.created_at_ms, first.reserved_until_ms, first.state))
        self.assertIsNone(first.claim_id)
        self.assertIsNone(first.claim_digest)
        self.assertIsNone(first.hard_deadline_at_ms)
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual([], self.fixture.files())
        self.fixture.now = 1500
        self.assertEqual(first, self.reserve())
        self.reopen(result_capacity=replace(self.fixture.capacity, reservation_ms=120_000))
        self.assertEqual(first, self.reserve())
        self.assertEqual(first, self.store.result_lease(lease_binding()))

    def test_arm_before_send_then_bind_actual_claim_preserves_charge_and_original_window(self):
        lease = self.reserve()
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.bind()
        armed = self.store.arm_result_lease(lease_binding())
        self.assertEqual("claim_pending", armed.state)
        self.assertEqual(lease.lease_digest, armed.lease_digest)
        self.fixture.now = 1500
        reserved = self.bind()
        self.assertEqual((1000, 2000, 61_000, "reserved"),
                         (reserved.created_at_ms, reserved.hard_deadline_at_ms, reserved.reserved_until_ms, reserved.state))
        linked = self.store.result_lease(lease_binding())
        self.assertEqual(("bound", binding().claim_id, binding().claim_digest),
                         (linked.state, linked.claim_id, linked.claim_digest))
        self.assertEqual(lease.lease_digest, linked.lease_digest)
        self.assertEqual(reserved, self.bind())
        self.reopen()
        self.assertEqual(reserved, self.bind())
        delivery = self.fixture.commit()
        self.assertEqual((delivery,), self.store.pending_terminals())
        self.assertEqual("bound", self.store.result_lease(lease_binding()).state)

    def test_pending_claim_expiry_and_restart_never_free_uncertain_capacity(self):
        self.reserve()
        first = self.store.arm_result_lease(lease_binding())
        self.reserve(2)
        self.store.arm_result_lease(lease_binding(2))
        self.fixture.now = 100_000
        self.reopen()
        self.assertEqual(0, self.store.expire_result_leases())
        self.assertEqual(first, self.store.arm_result_lease(lease_binding()))
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.bind(hard_deadline_at_ms=101_000)
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.reserve(3)
        self.assertEqual("claim_pending", self.store.result_lease(lease_binding()).state)

    def test_unused_expiry_frees_body_only_and_does_not_revive_or_reuse_a_lease(self):
        self.reserve()
        self.reserve(2)
        self.fixture.now = 61_000
        self.assertEqual(1, self.store.expire_result_leases(limit=1))
        self.assertEqual(1, self.store.expire_result_leases(limit=1))
        self.assertEqual("expired", self.reserve().state)
        with self.assertRaisesRegex(HistoryError, "history_result_expired"):
            self.store.arm_result_lease(lease_binding())
        self.assertEqual(1000, self.reserve().created_at_ms)
        self.reserve(3)
        self.reserve(4)
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.reserve(5)

    def test_body_and_entry_caps_include_both_lease_and_known_claim_paths_without_double_counting(self):
        self.fixture.capacity = replace(self.fixture.capacity, max_entries=2)
        self.reopen()
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        self.bind()
        self.fixture.reserve(2)
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.reserve(3)
        self.store.retire_result(binding())
        with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
            self.reserve(3)  # Tombstone remains an entry, bound lease is counted once.

    def test_mixed_live_body_capacity_cannot_be_bypassed_by_legacy_or_leased_reserve(self):
        self.fixture.reserve()
        self.reserve(2)
        for reserve in (lambda: self.reserve(3), lambda: self.fixture.reserve(3)):
            with self.assertRaisesRegex(HistoryError, "history_result_capacity"):
                reserve()
        self.store.arm_result_lease(lease_binding(2))
        self.bind(2)
        self.store.retire_result(binding())
        self.reserve(3)

    def test_changed_scope_request_assignment_or_claim_cannot_replace_original_reservation(self):
        self.reserve()
        for field, value in vars(lease_binding()).items():
            if field in {"project_id", "runtime_digest", "supervisor_generation", "attempt"}:
                changed = value + 1 if type(value) is int else "b" * 64 if field.endswith("digest") else "prj_" + "b" * 32
            elif field.endswith("digest"):
                changed = "b" * 64
            elif field == "orchestration_handle":
                changed = "orc_" + "b" * 48
            elif field == "run_handle":
                changed = "hrn_" + "b" * 48
            else:
                changed = "different"
            altered = replace(lease_binding(), **{field: changed})
            if field in {"run_handle", "attempt"}:  # Different identities may reserve their own capacity.
                continue
            with self.subTest(field=field), self.assertRaises(HistoryError):
                self.store.reserve_result_lease(altered, assignment())
        assigned = assignment()
        assigned["objective"] += " changed"
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.store.reserve_result_lease(lease_binding(), assigned)
        self.store.arm_result_lease(lease_binding())
        for changed in (replace(binding(), primary_session_id="foreign"), replace(binding(), supervisor_generation=2)):
            with self.assertRaisesRegex(HistoryError, "history_conflict"):
                self.store.bind_result_lease(lease_binding(), changed, assignment(), hard_deadline_at_ms=2000)
        self.bind()
        with self.assertRaises(HistoryError):
            self.store.bind_result_lease(lease_binding(), replace(binding(), claim_id="another-claim"), assignment(),
                                         hard_deadline_at_ms=2000)

    def test_original_deadline_must_fit_and_legacy_metadata_cannot_bypass_an_unbound_lease(self):
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        for deadline in (True, None, 1000, 61_000, 100_000):
            with self.subTest(deadline=deadline), self.assertRaises(HistoryError):
                self.bind(hard_deadline_at_ms=deadline)
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.fixture.reserve()
        with self.assertRaisesRegex(HistoryError, "history_result_not_ready"):
            self.store.commit_terminal(terminal(), binding(), ingested_at_ms=1000)
        self.assertEqual((), self.store.pending_terminals())
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])

    def test_failed_atomic_bind_rolls_back_both_parent_and_link_and_exact_retry_finishes(self):
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        with mock.patch.object(self.store, "_set_lease", side_effect=OSError("PRIVATE-BIND-FAILURE")), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.bind()
        self.assertEqual("claim_pending", self.store.result_lease(lease_binding()).state)
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])
        self.bind()
        self.assertEqual("bound", self.store.result_lease(lease_binding()).state)

    def test_lost_bind_reply_retrieves_original_durable_link_after_restart(self):
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        original = self.store._transaction

        @contextmanager
        def lose_reply():
            with original():
                yield
            raise OSError("PRIVATE-LOST-BIND-REPLY")

        with mock.patch.object(self.store, "_transaction", new=lose_reply), \
                self.assertRaisesRegex(HistoryError, "history_unavailable"):
            self.bind()
        first = self.store.result_lease(lease_binding())
        self.assertEqual("bound", first.state)
        self.fixture.now = 1500
        self.reopen()
        self.bind()
        self.assertEqual(first, self.store.result_lease(lease_binding()))
        with self.assertRaisesRegex(HistoryError, "history_conflict"):
            self.bind(hard_deadline_at_ms=2001)

    def test_state_or_scope_corruption_cannot_free_lease_capacity(self):
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        self.store._connection.execute("UPDATE result_leases SET state='expired'")
        for operation in (lambda: self.store.result_lease(lease_binding()), lambda: self.reserve(2),
                          lambda: self.fixture.reserve(2)):
            with self.assertRaisesRegex(HistoryError, "history_incompatible"):
                operation()
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])

    def test_assignment_and_prompt_content_never_enters_lease_sqlite_or_diagnostics(self):
        assigned = assignment()
        assigned["objective"] = "PRIVATE-ASSIGNMENT-CANARY"
        self.store.reserve_result_lease(lease_binding(), assigned)
        self.store.arm_result_lease(lease_binding())
        self.store.bind_result_lease(lease_binding(), binding(), assigned, hard_deadline_at_ms=2000)
        dumped = "\n".join(self.store._connection.iterdump())
        self.assertNotIn("PRIVATE-", dumped)
        self.assertNotIn(b"PRIVATE-ASSIGNMENT-CANARY".hex(), dumped)
        self.assertEqual([], self.fixture.files())
        wrong = replace(lease_binding(), claim_request_digest="c" * 64)
        with self.assertRaisesRegex(HistoryError, "history_conflict") as caught:
            self.store.arm_result_lease(wrong)
        self.assertIsNone(caught.exception.__context__)

    def test_corrupt_unused_lease_does_not_starve_other_due_leases_in_one_row_batches(self):
        self.reserve()
        self.reserve(2)
        self.store._connection.execute("UPDATE result_leases SET state_digest=? WHERE run_handle=?",
                                       ("f" * 64, lease_binding().run_handle))
        self.fixture.now = 61_000
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.store.expire_result_leases(limit=1)
        self.assertEqual(1, self.store.expire_result_leases(limit=1))
        self.assertEqual("expired", self.store.result_lease(lease_binding(2)).state)
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.store.expire_result_leases(limit=1)  # Cursor wraps; failed row stays charged.

    def test_preclaim_lease_then_actual_account_receipt_binds_without_placeholder_claim(self):
        # Existing real account handler/SQLite/signature checks with fake provider
        # observations and synthetic frontend identity. No native/model launch.
        account = account_fixtures.AccountClaimOperationTests(methodName="runTest")
        account.setUp()
        self.addCleanup(account.doCleanups)
        account.assert_no_claim()
        request = account.request("preleased-request")
        run_id = account.ledger._connection.execute("SELECT run_id FROM host_run_bindings WHERE project_id=?",
                                                    (account.project,)).fetchone()[0]
        target = replace(binding(), project_id=account.project, runtime_digest=COLLECTOR_RUNTIME_DIGEST,
                         run_handle=account.handle, run_id=run_id)
        scope = ResultLeaseBinding(**{name: getattr(target, name) for name in LEASE_SCOPE_FIELDS},
                                   claim_request_id=request.request_id, claim_request_digest=account_request_digest(request))
        directory = self.fixture.directory.parent / "account-history"
        directory.mkdir(mode=0o700)
        store = ProjectHistory(directory, account.project, result_key=fixtures.KEY,
                               allow_legacy_orchestration_reference=True,
                               result_capacity=ResultCapacity(8, 2 * fixtures.MAX_RESULT_BODY_BYTES, 1_800_000, 120_000),
                               result_clock_ms=lambda: account.stamp)
        self.addCleanup(store.close)
        assigned = account.fixture.assignment
        lease = store.reserve_result_lease(scope, assigned)
        self.assertIsNone(lease.claim_id)
        store.arm_result_lease(scope)
        account.assert_no_claim()
        receipt = account.call(request)
        self.assertEqual("claimed", receipt["state"])
        actual = replace(target, claim_id=receipt["bridge_run_id"], claim_digest=receipt["receipt_digest"])
        reserved = store.bind_result_lease(scope, actual, assigned, hard_deadline_at_ms=receipt["deadline_at_ms"])
        self.assertEqual((lease.created_at_ms, lease.reserved_until_ms),
                         (reserved.created_at_ms, reserved.reserved_until_ms))
        self.assertEqual(1, account.count("launch_claims"))
        self.assertEqual(1, account.count("launch_receipts"))
        self.assertEqual(0, account.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertTrue(account.call(request)["known"])
        self.assertEqual(reserved, store.bind_result_lease(scope, actual, assigned,
                                                          hard_deadline_at_ms=receipt["deadline_at_ms"]))

    def test_actual_process_death_mid_bind_preserves_pending_lease_and_all_capacity(self):
        self.reserve()
        self.store.arm_result_lease(lease_binding())
        self.store.close()
        code = '''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import tests.support
from tests.learning_support import assignment
from tests.test_project_history import PROJECT_A,binding
from tests.test_result_leases import lease_binding
from tests.test_project_results import KEY
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import ResultCapacity,MAX_RESULT_BODY_BYTES
class Cut(ProjectHistory):
 def _set_lease(self,value):
  if value.state=='bound':os._exit(79)
  return super()._set_lease(value)
store=Cut(Path(sys.argv[2]),PROJECT_A,result_key=KEY,allow_legacy_orchestration_reference=True,
 result_capacity=ResultCapacity(16,2*MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1001)
store.bind_result_lease(lease_binding(),binding(),assignment(),hard_deadline_at_ms=2000)
'''
        process = subprocess.run([sys.executable, "-B", "-c", code, str(Path(__file__).parents[1]),
                                  str(self.fixture.directory)], capture_output=True, timeout=10)
        self.assertEqual(79, process.returncode, process.stderr.decode())
        self.fixture.store = self.fixture.open()
        with self.assertRaisesRegex(HistoryError, "history_result_unavailable"):
            self.store.result_lease(lease_binding())  # Child's trusted clock advance survives rollback.
        self.fixture.now = 1002
        self.assertEqual("claim_pending", self.store.result_lease(lease_binding()).state)
        self.assertEqual(0, self.store._connection.execute("SELECT COUNT(*) FROM result_reservations").fetchone()[0])
        self.bind()

    def schema2(self):
        self.fixture.reserve()
        self.fixture.commit()
        legacy_cleanup_layout(self.store)
        snapshot = {name: [tuple(row) for row in self.store._connection.execute("SELECT * FROM " + name)]
                    for name in ("history_identity", "records", "terminal_outbox", "result_identity", "result_reservations")}
        self.store._connection.execute('DROP TABLE project_operational_qa')
        self.store._connection.execute('DROP TABLE project_qa_check_requests')
        self.store._connection.execute('DROP TABLE project_qa_check_receipts')
        self.store._connection.execute('DROP TABLE project_host_opens')
        self.store._connection.execute("DROP TABLE project_execution_consumptions")
        self.store._connection.execute("DROP TABLE result_leases")
        for table in ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations'):
            self.store._connection.execute('DROP TABLE ' + table)
        self.store._connection.execute("PRAGMA user_version=2")
        self.store.close()
        return snapshot

    def test_schema2_normal_open_refuses_without_writes_and_explicit_upgrade_preserves_existing_bytes(self):
        snapshot = self.schema2()
        path = self.fixture.directory / "history.sqlite3"
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        for options in ({}, {"result_key": fixtures.KEY, "result_capacity": self.fixture.capacity}):
            with self.assertRaisesRegex(HistoryError, "history_incompatible"):
                ProjectHistory(self.fixture.directory, self.fixture.store.project_id, **options)
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.fixture.store = self.fixture.open(allow_schema_upgrade=True)
        self.assertEqual(14, self.store._connection.execute("PRAGMA user_version").fetchone()[0])
        for name, rows in snapshot.items():
            self.assertEqual(rows, [tuple(row) for row in self.store._connection.execute("SELECT * FROM " + name)])
        self.assertEqual(self.fixture.payload()[0], self.store.read_result(binding()))
        self.reserve(2)

    def test_altered_schema2_refuses_before_any_lease_migration_or_trigger_execution(self):
        self.schema2()
        path = self.fixture.directory / "history.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TRIGGER unexpected AFTER INSERT ON records BEGIN DELETE FROM terminal_outbox; END")
            connection.commit()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, "history_incompatible"):
            self.fixture.open(allow_schema_upgrade=True)
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(2, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='result_leases'").fetchone())
            self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM terminal_outbox").fetchone()[0])

    def test_schema4_upgrade_preserves_all_rows_body_windows_and_does_not_invent_orchestrations(self):
        self.fixture.reserve()
        self.fixture.commit()
        legacy_cleanup_layout(self.store)
        old_tables = ('history_identity', 'records', 'terminal_outbox', 'result_identity',
                      'result_reservations', 'result_leases', 'project_execution_consumptions')
        snapshot = {name: [tuple(row) for row in self.store._connection.execute('SELECT * FROM ' + name)]
                    for name in old_tables}
        self.store._connection.execute('DROP TABLE project_operational_qa')
        self.store._connection.execute('DROP TABLE project_qa_check_requests')
        self.store._connection.execute('DROP TABLE project_qa_check_receipts')
        self.store._connection.execute('DROP TABLE project_host_opens')
        for table in ('project_orchestration_requests', 'project_orchestration_checkpoints', 'project_orchestrations'):
            self.store._connection.execute('DROP TABLE ' + table)
        self.store._connection.execute('PRAGMA user_version=4')
        self.store.close()
        path = self.fixture.directory / 'history.sqlite3'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(HistoryError, 'history_incompatible'):
            self.fixture.open()
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.fixture.store = self.fixture.open(allow_schema_upgrade=True)
        self.assertEqual(14, self.store._connection.execute('PRAGMA user_version').fetchone()[0])
        for name, rows in snapshot.items():
            assert_migrated_rows(self, self.store, name, rows)
        self.assertEqual(self.fixture.payload()[0], self.store.read_result(binding()))
        for table in ('project_orchestrations', 'project_orchestration_checkpoints', 'project_orchestration_requests'):
            self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0])

    def test_process_death_during_schema2_upgrade_rolls_back_all_lease_ddl(self):
        snapshot = self.schema2()
        code = '''
import os,sqlite3,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import tests.support
from tests.test_project_history import PROJECT_A
from codex_agents import project_history
native=sqlite3.connect
class Cut(sqlite3.Connection):
 def execute(self,sql,*args,**kwargs):
  result=super().execute(sql,*args,**kwargs)
  if sql.lstrip().startswith('CREATE TABLE result_leases'):os._exit(87)
  return result
def connect(*args,**kwargs):return native(*args,**{**kwargs,'factory':Cut})
project_history.sqlite3.connect=connect
project_history.ProjectHistory(Path(sys.argv[2]),PROJECT_A,allow_schema_upgrade=True)
'''
        process = subprocess.run([sys.executable, "-B", "-c", code, str(Path(__file__).parents[1]),
                                  str(self.fixture.directory)], capture_output=True, timeout=10)
        self.assertEqual(87, process.returncode, process.stderr.decode())
        with closing(sqlite3.connect(self.fixture.directory / "history.sqlite3")) as connection:
            self.assertEqual(2, connection.execute("PRAGMA user_version").fetchone()[0])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='result_leases'").fetchone())
        self.fixture.store = self.fixture.open(allow_schema_upgrade=True)
        for name, rows in snapshot.items():
            self.assertEqual(rows, [tuple(row) for row in self.store._connection.execute("SELECT * FROM " + name)])
        self.assertEqual(self.fixture.payload()[0], self.store.read_result(binding()))


if __name__ == "__main__":
    unittest.main()
