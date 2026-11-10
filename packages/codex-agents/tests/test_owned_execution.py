"""Owned process cleanup and durable capacity release; no real AI client."""
from __future__ import annotations

from copy import copy
from copy import deepcopy
import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

import tests.test_bridge as fixtures
from codex_agents import bridge, process_tree
from codex_agents import learning_store
from codex_agents import owned_execution
from codex_agents.learning_store import LearningLedger, LedgerError
from codex_agents.owned_execution import ExecutionCustodyError, ExecutionTicket, OwnedExecutionKey, OwnedExecutionRegistry


class CleanupEvidenceTests(unittest.TestCase):
    def identified(self, child_exited=False):
        root, child = 20000, 20001
        records = {root: (1, root, "root", False), child: (root, root, "child", child_exited)}
        tree = process_tree.ProcessTree(root)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertTrue(tree.identify({root: 1, child: root}))
        return tree, records, SimpleNamespace(pid=root, returncode=0)

    def test_unreadable_descendant_is_not_forgotten_as_cleanup_proof(self):
        tree, records, root = self.identified()
        records.pop(20001)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            tree.update({20000: 1})
            self.assertNotIn(20001, tree.tracked)
            self.assertFalse(tree.cleanup_after_owned_wait(root))
        self.assertIn((20001, "child"), tree._cleanup_seen)

    def test_unresolved_adoption_candidate_cannot_prove_a_clean_tree(self):
        for candidate in (None, (1, 20001, "escaped", False)):
            with self.subTest(candidate=candidate):
                records = {20000: (1, 20000, "root", False), 20001: candidate}
                tree = process_tree.ProcessTree(20000)
                with mock.patch.object(process_tree, "process_record", side_effect=records.get):
                    self.assertTrue(tree.identify({20000: 1, 20001: 20000}))
                    self.assertNotIn(20001, tree.tracked)
                    self.assertFalse(tree.cleanup_after_owned_wait(SimpleNamespace(pid=20000, returncode=0)))

    def test_positive_exit_survives_later_disappearance(self):
        tree, records, root = self.identified(child_exited=True)
        records.pop(20001)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            tree.update({20000: 1})
            self.assertTrue(tree.cleanup_after_owned_wait(root))

    def test_running_descendant_snapshot_loss_and_overflow_refuse_confirmation(self):
        tree, records, root = self.identified()
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertFalse(tree.cleanup_after_owned_wait(root))
            tree._cleanup_incomplete = True
            records[20001] = (*records[20001][:3], True)
            self.assertFalse(tree.cleanup_after_owned_wait(root))
        with mock.patch.object(process_tree, "MAX_CLEANUP_IDENTITIES", 1):
            tree, records, root = self.identified(child_exited=True)
        with mock.patch.object(process_tree, "process_record", side_effect=records.get):
            self.assertFalse(tree.cleanup_after_owned_wait(root))


class OwnedExecutionRegistryTests(unittest.TestCase):
    def key(self):
        return OwnedExecutionKey("prj_" + "a" * 32, "b" * 64, "run:" + "c" * 64, "brg_" + "d" * 32)

    def owned_ticket(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        entry = registry._entry(ticket)
        self.addCleanup(self.reap_fixture, entry)
        return registry, ticket, entry

    def reap_fixture(self, entry):
        process = entry.process
        if process is not None:
            if process.returncode is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def spawn_sleep(self, ticket):
        return ticket.spawn([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={"PATH": os.defpath}, cwd="/private/tmp", start_new_session=True)

    def assert_custody_held(self, registry, ticket):
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)
        with self.assertRaises(ExecutionCustodyError):
            registry.enter(ticket)

    def test_factory_refuses_a_shared_process_group_before_creating_a_child(self):
        for options in ({}, {"start_new_session": False}, {"start_new_session": 1}):
            with self.subTest(options=options):
                registry, ticket, _ = self.owned_ticket()
                with mock.patch.object(owned_execution, "_POPEN") as factory, \
                        self.assertRaises(ExecutionCustodyError):
                    ticket.spawn([], **options)
                factory.assert_not_called()

    def test_handoff_fault_reaps_actual_child_preserves_error_and_retains_capacity(self):
        for error in (KeyboardInterrupt("handoff"), OSError("handoff")):
            with self.subTest(error=type(error).__name__):
                registry, ticket, entry = self.owned_ticket()
                original_entry = registry._entry
                calls = []

                def handoff(value):
                    calls.append(True)
                    if len(calls) == 2:
                        raise error
                    return original_entry(value)

                with mock.patch.object(registry, "_entry", side_effect=handoff), \
                        self.assertRaises(type(error)) as caught:
                    self.spawn_sleep(ticket)
                self.assertIs(error, caught.exception)
                self.assertIsNotNone(entry.process)
                self.assertIsNotNone(entry.process.returncode)
                self.assertTrue(entry.process.stdout.closed)
                self.assertEqual("unknown", entry.state)
                self.assert_custody_held(registry, ticket)

    def test_tree_construction_fault_still_reaps_the_new_child(self):
        registry, ticket, entry = self.owned_ticket()
        error = OSError("tree unavailable")
        with mock.patch.object(owned_execution, "ProcessTree", side_effect=error), \
                self.assertRaises(OSError) as caught:
            self.spawn_sleep(ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_errors_preserve_nonlocal_exit_and_uncertain_process_custody(self):
        first = KeyboardInterrupt("original handoff")
        second = KeyboardInterrupt("cleanup interruption")
        for original, cleanup, expected in ((first, OSError("cleanup"), first),
                                            (first, SystemExit(3), first),
                                            (OSError("handoff"), second, second)):
            with self.subTest(original=type(original).__name__, cleanup=type(cleanup).__name__):
                registry, ticket, entry = self.owned_ticket()
                original_entry = registry._entry
                calls = []

                def handoff(value):
                    calls.append(True)
                    if len(calls) == 2:
                        raise original
                    return original_entry(value)

                with mock.patch.object(registry, "_entry", side_effect=handoff), \
                        mock.patch.object(owned_execution, "_abort_spawn", side_effect=cleanup), \
                        self.assertRaises(type(expected)) as caught:
                    self.spawn_sleep(ticket)
                self.assertIs(expected, caught.exception)
                self.assertIsNotNone(entry.process)
                self.assertEqual("unknown", entry.state)
                self.assert_custody_held(registry, ticket)

    def test_bridge_tree_handoff_fault_cannot_bypass_owned_child_cleanup(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("bridge tree handoff")
        with mock.patch.object(registry, "tree", side_effect=error), \
                self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path("/private/tmp"),
                             deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_tracking_error_still_kills_and_waits_without_masking_interruption(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("frame interruption")

        def interrupt(_frame):
            raise error

        with mock.patch.object(process_tree.ProcessTree, "terminate", side_effect=OSError("tracking failed")), \
                self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; print('{}', flush=True); time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path("/private/tmp"),
                             deadline_seconds=5, on_frame=interrupt, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_cleanup_entry_fault_cannot_skip_stop_wait_or_mask_original_interrupt(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                registry, ticket, entry = self.owned_ticket()
                original = KeyboardInterrupt("original frame") if interrupted else bridge._Abort("cancelled")
                cleanup = OSError("cleanup entry unavailable")

                def abort(_frame):
                    raise original

                expected = KeyboardInterrupt if interrupted else OSError
                with mock.patch.object(registry, "begin_cleanup", side_effect=cleanup), \
                        self.assertRaises(expected) as caught:
                    bridge.run_child([sys.executable, "-I", "-B", "-c",
                                      "import time; print('{}', flush=True); time.sleep(30)"],
                                     environment={"PATH": os.defpath}, cwd=Path("/private/tmp"),
                                     deadline_seconds=5, on_frame=abort, _execution=ticket)
                self.assertIs(original if interrupted else cleanup, caught.exception)
                self.assertIsNotNone(entry.process.returncode)
                self.assertTrue(entry.process.stdout.closed)
                self.assert_custody_held(registry, ticket)

    def test_first_cleanup_interrupt_survives_later_ordinary_stop_error(self):
        registry, ticket, entry = self.owned_ticket()
        first = SystemExit(7)
        stop = bridge._stop_child

        def stop_then_fail(process, tree):
            stop(process, tree)
            raise OSError("later cleanup error")

        def abort(_frame):
            raise bridge._Abort("cancelled")

        with mock.patch.object(registry, "begin_cleanup", side_effect=first), \
                mock.patch.object(bridge, "_stop_child", side_effect=stop_then_fail), \
                self.assertRaises(SystemExit) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c",
                              "import time; print('{}', flush=True); time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path("/private/tmp"),
                             deadline_seconds=5, on_frame=abort, _execution=ticket)
        self.assertIs(first, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assert_custody_held(registry, ticket)

    def test_interruption_after_factory_return_before_local_assignment_still_reaps_child(self):
        registry, ticket, entry = self.owned_ticket()
        error = KeyboardInterrupt("after factory return")
        original_spawn = ExecutionTicket.spawn

        def handoff(value, *args, **kwargs):
            original_spawn(value, *args, **kwargs)
            raise error

        with mock.patch.object(ExecutionTicket, "spawn", handoff), self.assertRaises(KeyboardInterrupt) as caught:
            bridge.run_child([sys.executable, "-I", "-B", "-c", "import time; time.sleep(30)"],
                             environment={"PATH": os.defpath}, cwd=Path("/private/tmp"),
                             deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertIs(error, caught.exception)
        self.assertIsNotNone(entry.process.returncode)
        self.assertTrue(entry.process.stdout.closed)
        self.assertNotIn(entry.process.pid, bridge._ACTIVE_CHILDREN)
        self.assert_custody_held(registry, ticket)

    def test_constructed_copied_cross_registry_and_wrong_thread_tickets_are_refused(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        for forged in (ExecutionTicket(registry), copy(ticket), OwnedExecutionRegistry().issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)):
            with self.subTest(kind=type(forged).__name__), self.assertRaises(ExecutionCustodyError):
                registry.enter(forged)
        errors = []

        def wrong_thread():
            try:
                registry.enter(ticket)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([ExecutionCustodyError], [type(error) for error in errors])
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_actual_fixed_runner_waits_and_confirms_original_custody_once(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        with tempfile.TemporaryDirectory() as directory:
            outcome = bridge.run_child([sys.executable, "-I", "-B", "-c", "print('{}')"],
                                       environment={"PATH": os.defpath}, cwd=Path(directory),
                                       deadline_seconds=5, on_frame=lambda frame: None, _execution=ticket)
        self.assertEqual({"returncode": 0, "abort": None}, outcome)
        self.assertEqual(self.key(), registry.key(ticket, cleaned=True))
        with self.assertRaises(ExecutionCustodyError):
            registry.enter(ticket)
        registry.acknowledge_release(ticket)
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_custom_process_factory_cannot_generate_registered_completion(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        factory = mock.Mock(side_effect=AssertionError("custom factory executed"))
        outcome = bridge.run_child([], environment={}, cwd=Path("/private/tmp"), deadline_seconds=1,
                                   on_frame=lambda frame: None, popen=factory, _execution=ticket)
        self.assertEqual("launch_failed", outcome["abort"])
        factory.assert_not_called()
        with self.assertRaises(ExecutionCustodyError):
            registry.key(ticket, cleaned=True)

    def test_existing_owned_process_and_constructed_tree_are_not_substitutable(self):
        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        registry.enter(ticket)
        with subprocess.Popen([sys.executable, "-I", "-B", "-c", "pass"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as process:
            tree = process_tree.ProcessTree(process.pid)
            self.assertTrue(tree.identify(process_tree.process_table()))
            with self.assertRaises(ExecutionCustodyError):
                registry.bind(ticket, process, tree)
            process.wait(timeout=5)

        registry = OwnedExecutionRegistry()
        ticket = registry.issue(self.key(), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        process = ticket.spawn([sys.executable, "-I", "-B", "-c", "pass"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            constructed = process_tree.ProcessTree(process.pid)
            self.assertTrue(constructed.identify(process_tree.process_table()))
            with self.assertRaises(ExecutionCustodyError):
                ticket.bind(process, constructed)
        finally:
            process.wait(timeout=5)

    def test_expired_and_regressed_monotonic_time_never_enter_the_process_factory(self):
        for now, expected in ((250, "timed_out"), (50, "launch_failed")):
            registry = OwnedExecutionRegistry()
            with mock.patch.object(owned_execution.time, "monotonic_ns", return_value=100):
                ticket = registry.issue(self.key(), deadline_ns=200)
            factory = mock.Mock(side_effect=AssertionError("expired process factory"))
            with mock.patch.object(owned_execution.time, "monotonic_ns", return_value=now), \
                    mock.patch.object(owned_execution, "_POPEN", factory):
                result = bridge.run_child([], environment={}, cwd=Path("/private/tmp"), deadline_seconds=10,
                                          on_frame=lambda frame: None, _execution=ticket)
            self.assertEqual(expected, result["abort"])
            factory.assert_not_called()
            with self.assertRaises(ExecutionCustodyError):
                registry.key(ticket, cleaned=True)


class CapacityCompletionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexBridgeTests("test_happy_path_observes_before_and_after")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.tickets = []
        original = self.fixture.ledger._new_owned_execution

        def remember(*args):
            ticket = original(*args)
            self.tickets.append(ticket)
            return ticket

        patcher = mock.patch.object(self.fixture.ledger, "_new_owned_execution", side_effect=remember)
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def ledger(self):
        return self.fixture.ledger

    def run_one(self):
        route, assigned, handle = self.fixture.codex_run()
        self.fixture.frames("codex", [{"type": "thread.started", "thread_id": "t1"},
                                     {"type": "turn.completed", "usage": {"input_tokens": 1}}])
        (self.fixture.tools / "codex-last-message.json").write_text(json.dumps(fixtures.null_filled(fixtures.result())))
        return handle, self.fixture.dispatch(route, assigned, handle)

    def held(self):
        return self.ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL").fetchone()[0]

    def reopen(self):
        self.ledger.close()
        self.fixture.ledger = LearningLedger(self.fixture.state, client_capabilities=self.fixture.clients)
        self.addCleanup(self.fixture.ledger.close)

    def test_three_completed_owned_runs_release_default_slots_without_relaunch(self):
        for _ in range(3):
            last_handle, output = self.run_one()
            self.assertEqual("result", output["bridge_report"]["outcome"])
            self.assertEqual("released", output["bridge_report"]["capacity_state"])
            self.assertEqual(0, self.held())
        self.assertEqual(3, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(3, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_completions WHERE receipt_digest IS NOT NULL").fetchone()[0])
        with self.assertRaisesRegex(LedgerError, "owned execution is unavailable"):
            key = self.ledger._capacity_executions.key(self.tickets[-1])
            # A released claim never creates another execution ticket.
            self.ledger._new_owned_execution(key.project_id, last_handle, key.bridge_run_id)

    def reserve_claim(self):
        route, assigned, handle = self.fixture.codex_run()
        intent = self.ledger.record_dispatch_intent(self.fixture.project_id, run_handle=handle, assignment=assigned,
                                                     task=self.fixture.task(), route=route, deadline_seconds=10)
        self.ledger.bind_host_agent(self.fixture.project_id, run_handle=handle, agent_id=intent["bridge_run_id"])
        return handle, intent

    def prepare_claim(self):
        handle, intent = self.reserve_claim()
        ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        return handle, intent, ticket

    def test_registration_time_consumes_the_original_claimed_budget(self):
        clock = [100_000_000_000]
        execute = self.ledger._connection.execute

        def delayed(sql, *args, **kwargs):
            result = execute(sql, *args, **kwargs)
            if sql.startswith("INSERT INTO capacity_executions"):
                clock[0] += 1_000_000_000
            return result

        with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
            handle, intent = self.reserve_claim()
            claimed = self.ledger._connection.execute("SELECT claimed_at_ms FROM active_run_capacity").fetchone()[0]
            clock[0] += 8_000_000_000
            with mock.patch.object(self.ledger, "_clock_ms", return_value=claimed + 8000), \
                    mock.patch.object(self.ledger._connection, "execute", side_effect=delayed):
                ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(110_000_000_000, ticket.deadline_ns())

    def test_partial_wall_clock_rollback_cannot_renew_the_claim_budget(self):
        clock = [100_000_000_000]
        with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
            handle, intent = self.reserve_claim()
            claimed = self.ledger._connection.execute("SELECT claimed_at_ms FROM active_run_capacity").fetchone()[0]
            clock[0] += 9_000_000_000
            with mock.patch.object(self.ledger, "_clock_ms", return_value=claimed + 1000):
                ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
            self.assertEqual(110_000_000_000, ticket.deadline_ns())

    def test_original_monotonic_expiry_or_regression_refuses_registration_despite_valid_wall_time(self):
        for registration in (99_000_000_000, 110_000_000_000):
            with self.subTest(registration=registration):
                clock = [100_000_000_000]
                with mock.patch.object(owned_execution.time, "monotonic_ns", side_effect=lambda: clock[0]):
                    handle, intent = self.reserve_claim()
                    row = self.ledger._connection.execute(
                        "SELECT claimed_at_ms FROM active_run_capacity WHERE bridge_run_id=?", (intent["bridge_run_id"],),
                    ).fetchone()
                    clock[0] = registration
                    with mock.patch.object(self.ledger, "_clock_ms", return_value=row[0] + 1000), \
                            self.assertRaisesRegex(LedgerError, "deadline"):
                        self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
                self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])

    def test_clock_owner_cannot_be_reconstructed_after_restart_before_registration(self):
        handle, intent = self.reserve_claim()
        self.reopen()
        with self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_structurally_valid_clock_owner_tampering_refuses_registration(self):
        handle, intent = self.reserve_claim()
        with self.ledger._connection:
            self.ledger._connection.execute("UPDATE active_run_capacity SET clock_owner_digest=?", ("0" * 64,))
        with self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_expired_claim_refuses_registration_without_reissuing_execution(self):
        handle, intent = self.reserve_claim()
        deadline = self.ledger._connection.execute("SELECT deadline_at_ms FROM active_run_capacity").fetchone()[0]
        with mock.patch.object(self.ledger, "_clock_ms", return_value=deadline), self.assertRaisesRegex(LedgerError, "deadline"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, self.held())

    def test_registration_before_spawn_remains_single_use_across_restart(self):
        handle, intent, _ = self.prepare_claim()
        self.reopen()
        with self.assertRaisesRegex(LedgerError, "already registered"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(1, self.held())

    def test_registration_commit_rollback_does_not_burn_an_unreturned_ticket(self):
        handle, intent = self.reserve_claim()
        error = KeyboardInterrupt("registration before commit")
        with mock.patch.object(self.ledger.state, "harden_database_files", side_effect=error), \
                self.assertRaises(KeyboardInterrupt) as caught:
            self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertIs(error, caught.exception)
        self.assertEqual(0, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        ticket = self.ledger._new_owned_execution(self.fixture.project_id, handle, intent["bridge_run_id"])
        self.assertEqual(1, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(1, len(self.ledger._capacity_executions._pending))
        row = self.ledger._connection.execute("SELECT execution_deadline_ns FROM active_run_capacity").fetchone()
        self.assertEqual(row[0], ticket.deadline_ns())
        self.assertEqual(1, self.held())

    def test_terminal_record_and_caller_flags_without_actual_execution_cannot_release(self):
        handle, intent, ticket = self.prepare_claim()
        self.ledger.record_dispatch_result(self.fixture.project_id, run_handle=handle, bridge_run_id=intent["bridge_run_id"],
                                           result=fixtures.null_filled(fixtures.result()), fenced=False,
                                           reviewed=True, terminal_state=None)
        self.assertEqual("held", self.ledger._finish_owned_execution(ticket)["state"])
        self.assertEqual(1, self.held())

    def test_known_release_survives_restart_and_clock_rollback(self):
        handle, output = self.run_one()
        bridge_id = output["bridge_report"]["bridge_run_id"]
        first = self.ledger._release_active_capacity(self.tickets[0])
        self.assertTrue(first["known"])
        self.reopen()
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1):
            self.assertEqual(first, self.ledger._known_capacity_release(self.fixture.project_id, handle, bridge_id))

    def test_released_backup_requires_execution_and_completion_provenance(self):
        self.run_one()
        body = learning_store._snapshot_project_partition(self.ledger._connection, self.fixture.project_id,
                                                         ledger_schema_version=learning_store.LEDGER_SCHEMA_VERSION)
        self.assertEqual(1, learning_store._verify_project_backup_body(body)["capacity_completions"])
        for table in ("capacity_completions", "capacity_executions"):
            broken = deepcopy(body)
            broken["tables"][table] = []
            broken["counts"][table] = 0
            with self.subTest(table=table), self.assertRaisesRegex(LedgerError, "completion coverage"):
                learning_store._verify_project_backup_body(broken)

    def test_tampered_release_receipt_is_not_accepted_as_known_metadata(self):
        handle, output = self.run_one()
        self.ledger._connection.execute("UPDATE capacity_completions SET receipt_digest=?", ("f" * 64,))
        self.ledger._connection.execute("UPDATE active_run_capacity SET release_digest=?", ("f" * 64,))
        with self.assertRaisesRegex(LedgerError, "capacity completion is unavailable"):
            self.ledger._known_capacity_release(self.fixture.project_id, handle, output["bridge_report"]["bridge_run_id"])

    def test_missing_cleanup_proof_and_restart_keep_capacity_and_consumed_registration(self):
        # A forged success flag still cannot update the registry's actual state.
        with mock.patch.object(OwnedExecutionRegistry, "finish", return_value=True):
            handle, output = self.run_one()
        self.assertEqual("held", output["bridge_report"]["capacity_state"])
        self.assertEqual(1, self.held())
        bridge_id = output["bridge_report"]["bridge_run_id"]
        self.reopen()
        self.assertIsNone(self.ledger._known_capacity_release(self.fixture.project_id, handle, bridge_id))
        with self.assertRaisesRegex(LedgerError, "owned execution is unavailable"):
            self.ledger._new_owned_execution(self.fixture.project_id, handle, bridge_id)
        self.assertEqual(1, self.held())

    def test_interrupted_release_rolls_back_both_tables_and_reuses_original_cleanup(self):
        class FixtureStop(BaseException):
            pass

        execute = self.ledger._connection.execute

        def interrupted(sql, *args, **kwargs):
            value = execute(sql, *args, **kwargs)
            if sql.startswith("UPDATE capacity_completions SET released_at_ms="):
                raise FixtureStop()
            return value

        with mock.patch.object(self.ledger._connection, "execute", side_effect=interrupted), self.assertRaises(FixtureStop):
            self.run_one()
        self.assertEqual(1, self.held())
        self.assertIsNone(self.ledger._connection.execute("SELECT receipt_digest FROM capacity_completions").fetchone()[0])
        receipt = self.ledger._release_active_capacity(self.tickets[0])
        self.assertEqual("released", receipt["state"])
        self.assertFalse(receipt["known"])
        self.assertEqual(0, self.held())
        self.assertEqual(1, len([item for item in self.fixture.invocations("codex") if "exec" in item["argv"]]))

    def test_clock_rollback_cannot_create_a_new_release_but_ready_custody_is_retained(self):
        with mock.patch.object(self.ledger, "_finish_owned_execution", return_value={"state": "held"}):
            self.run_one()
        self.assertEqual(1, self.held())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1), self.assertRaises(LedgerError):
            self.ledger._release_active_capacity(self.tickets[0])
        self.assertEqual(1, self.held())
        self.assertEqual("released", self.ledger._release_active_capacity(self.tickets[0])["state"])

    def test_lost_registry_acknowledgement_retrieves_the_committed_receipt(self):
        class FixtureStop(BaseException):
            pass

        with mock.patch.object(self.ledger._capacity_executions, "acknowledge_release", side_effect=FixtureStop), \
                self.assertRaises(FixtureStop):
            self.run_one()
        self.assertEqual(0, self.held())
        with mock.patch.object(self.ledger, "_clock_ms", return_value=-1):
            known = self.ledger._release_active_capacity(self.tickets[0])
        self.assertTrue(known["known"])
        self.assertEqual("released", known["state"])

    def test_forged_and_foreign_tickets_cannot_free_a_held_slot(self):
        with mock.patch.object(OwnedExecutionRegistry, "finish", return_value=False):
            self.run_one()
        real = self.tickets[0]
        foreign = OwnedExecutionRegistry().issue(self.ledger._capacity_executions.key(real), deadline_ns=time.monotonic_ns() + 60_000_000_000)
        for forged in (ExecutionTicket(self.ledger._capacity_executions), copy(real), foreign):
            self.assertEqual("held", self.ledger._finish_owned_execution(forged)["state"])
        self.assertEqual(1, self.held())


if __name__ == "__main__":
    unittest.main()
