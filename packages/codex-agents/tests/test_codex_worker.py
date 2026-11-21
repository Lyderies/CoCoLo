"""Fixed worker/owner races with inert executable fixtures, never real Codex."""
from __future__ import annotations
from tests.support import SHORT_TMP

from copy import copy
from dataclasses import replace
import io
import json
import os
import signal
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import contextlib

import tests.support  # noqa: F401
import tests.test_bridge as fixtures
import tests.test_bridge_liveness as owners
from tests.test_liveness_clock import SECOND

from codex_agents import bridge, codex_worker, owned_execution
from codex_agents.control_api import ControlError
from codex_agents.learning_contracts import canonical_json
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES
from codex_agents.codex_worker import (
    MAX_INPUT_BYTES, MAX_PACKET_BYTES, CodexWorkerError, CodexWorkerHandle, CodexWorkerLimits, CodexWorkerPool,
)
from codex_agents.contracts import validate_result
from codex_agents.owned_execution import ExecutionCustodyError, OwnedExecutionKey, OwnedExecutionRegistry


class CodexWorkerTests(unittest.TestCase):
    setUp_owner = owners.BridgeLivenessTests.setUp
    authorize = owners.BridgeLivenessTests.authorize
    request = owners.BridgeLivenessTests.request
    at = owners.BridgeLivenessTests.at

    def setUp(self):
        self.setUp_owner()
        self.fixture = fixtures.BridgeTestCase(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.temporary = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(self.temporary.cleanup)
        self.workspaces = []
        self.registry = OwnedExecutionRegistry()
        self.pool = CodexWorkerPool(self.registry, CodexWorkerLimits(2, 16 * 1_048_576))
        self.addCleanup(self.stop_pool)

    def stop_pool(self):
        self.pool.close()
        for slot in self.pool._slots:
            slot.thread.join(6)
            self.assertFalse(slot.thread.is_alive(), "fixture worker did not stop")

    def prepare(self, number=1, *, value=None, rows=None, deadline=60, submit=True, mutate=None,
                before_bind=None, after_bind=None):
        root = Path(self.temporary.name) / f"job-{len(self.workspaces)}"
        root.mkdir(mode=0o700)
        self.workspaces.append(root)
        for name in ("control", "export", "tmp"):
            (root / name).mkdir(mode=0o700)
        executable = root / "inert-exec"
        executable.write_text("#!" + sys.executable + "\n" + '''
import json, sys, time
from pathlib import Path
root = Path(__file__).parent
with (root / "launches").open("a") as file:
    file.write("launch\\n")
for row in json.loads((root / "frames.json").read_text()):
    if "pause" in row:
        end = time.monotonic() + 15
        while not (root / row["pause"]).exists():
            if time.monotonic() >= end:
                sys.exit(13)
            time.sleep(0.005)
    elif "result" in row:
        Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(row["result"], sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(row["frame"]), flush=True)
''')
        executable.chmod(0o700)
        prefix = [{"frame": {"type": "thread.started", "thread_id": "thread_1"}},
                  {"frame": {"type": "turn.started"}}]
        if rows is None:
            rows = [{"frame": {"type": "item.completed", "item": {
                "id": "item_1", "type": "reasoning", "text": "PRIVATE-REASONING",
            }}}, {"result": fixtures.null_filled(fixtures.result() if value is None else value)},
                    {"frame": {"type": "turn.completed", "usage": {}}}]
        (root / "frames.json").write_text(json.dumps([*prefix, *rows]))
        route = self.fixture.bridged_route("codex")
        assignment = self.fixture.assignment_for(route)
        data = {"task": self.fixture.task(), "route": route, "assignment": assignment,
                "parent_grant": fixtures.PARENT_GRANT, "schema": fixtures.RESULT_SCHEMA.decode(),
                "export": str(root / "export"), "control": str(root / "control"),
                "executable": str(executable), "environ": {"PATH": os.defpath},
                "role_prompt": "Read the fixture only.", "worker_result_path": None}
        if mutate is not None:
            mutate(data)
        payload = json.dumps(data).encode()
        target = owners.attempt(number)
        hard_deadline = self.origin + deadline * SECOND
        self.monitor.register(target, hard_deadline_ns=hard_deadline)
        key = OwnedExecutionKey(target.project_id, f"{number:064x}", target.run_id, target.claim_id)
        handle = self.pool.prepare(payload)
        if before_bind is not None:
            before_bind(target, handle)
        ticket = self.registry.issue(key, deadline_ns=hard_deadline, run_handle=target.run_handle)
        self.pool.bind(handle, ticket)
        supervisor = self.pool.supervisor(handle)
        self.owner.bind(target, supervisor)
        if after_bind is not None:
            after_bind(target, handle)
        if submit:
            self.pool.submit(handle)
        return root, target, ticket, handle, supervisor, assignment

    def until(self, predicate, *, tick=False):
        end = time.monotonic() + 6
        while time.monotonic() < end:
            if tick:
                self.owner.run_once()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("bounded fixture did not finish")

    def returned(self, prepared):
        self.until(lambda: self.pool.snapshot(prepared[3])["returned"])

    def test_fixed_return_before_late_owner_poll_and_cancel_retains_exact_valid_result(self):
        prepared = self.prepare()
        root, target, _, handle, _, assignment = prepared
        self.returned(prepared)
        self.at(25)
        self.request(target)
        result = self.owner.result(target, self.pool, handle)
        self.assertIsNone(result.stop_reason)
        self.assertFalse(result.packet.fenced)
        self.assertEqual(fixtures.result(), json.loads(result.packet.body))
        validate_result(json.loads(result.packet.body), assignment=assignment)
        self.assertIs(result, self.owner.result(target, self.pool, handle))
        self.pool.submit(handle)
        self.assertEqual(["launch"], (root / "launches").read_text().splitlines())
        self.assertNotIn("PRIVATE-REASONING", repr(result))

    def test_cancel_during_slow_result_validation_fences_and_discards_the_late_success(self):
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = codex_worker._PREPARE_RESULT

        def blocked(*args):
            ready.set()
            if not resume.wait(5):
                raise RuntimeError("fixture validation gate timed out")
            return original(*args)

        value = {**fixtures.result(), "summary": "PRIVATE-CANCELLED-SUCCESS"}
        with mock.patch.object(codex_worker, "_PREPARE_RESULT", side_effect=blocked):
            prepared = self.prepare(value=value)
            _, target, _, handle, supervisor, assignment = prepared
            self.assertTrue(ready.wait(5))
            self.assertTrue(supervisor.snapshot()["cleanup_confirmed"])
            reply = self.request(target)
            self.assertEqual("stop_requested", reply.result["cancellation_state"])
            resume.set()
            self.returned(prepared)
        result = self.owner.result(target, self.pool, handle)
        body = json.loads(result.packet.body)
        self.assertEqual("cancel_requested", result.stop_reason)
        self.assertEqual("cancelled", body["execution_state"])
        self.assertTrue(result.packet.fenced)
        self.assertNotIn("PRIVATE", result.packet.body.decode())
        self.assertIsNone(self.pool.packet(handle).body)
        validate_result(body, assignment=assignment)

    def test_pool_close_before_completion_is_ordered_before_a_later_elapsed_bound(self):
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = codex_worker._PREPARE_RESULT

        def blocked(*args):
            ready.set()
            if not resume.wait(5):
                raise RuntimeError("fixture close gate timed out")
            return original(*args)

        with mock.patch.object(codex_worker, "_PREPARE_RESULT", side_effect=blocked):
            prepared = self.prepare()
            self.assertTrue(ready.wait(5))
            self.at(9)
            self.pool.close()
            self.at(12)
            resume.set()
            self.returned(prepared)
        result = self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertEqual("cancel_requested", result.stop_reason)
        self.assertEqual("cancelled", json.loads(result.packet.body)["execution_state"])

    def test_process_signal_uses_custody_guard_after_worker_reaped_root(self):
        ready, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        original = bridge._stop_child

        def waited(process, tree):
            original(process, tree)
            ready.set()
            if not resume.wait(5):
                raise RuntimeError("fixture reaped-root gate timed out")

        with mock.patch.object(bridge, "_stop_child", side_effect=waited):
            prepared = self.prepare()
            self.assertTrue(ready.wait(5))
            self.assertTrue(prepared[4].snapshot()["root_waited"])
            with bridge.signal_guard(), mock.patch.object(
                bridge, "_kill_child", side_effect=AssertionError("signal fallback on worker-owned root")
            ) as fallback, mock.patch.object(
                owned_execution.os, "killpg", side_effect=AssertionError("signal after owned root wait")
            ) as signal_group:
                with self.assertRaises(bridge.BridgeInterrupted):
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                fallback.assert_not_called()
                signal_group.assert_not_called()
            resume.set()
            self.returned(prepared)
        result = self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertEqual("cancel_requested", result.stop_reason)
        self.assertEqual("cancelled", json.loads(result.packet.body)["execution_state"])

    def test_explicit_body_discard_prevents_reconstruction_without_releasing_capacity(self):
        prepared = self.prepare()
        self.returned(prepared)
        self.owner.result(prepared[1], self.pool, prepared[3])
        charged = self.pool._charged
        self.owner.discard_result(prepared[1], self.pool, prepared[3])
        self.assertEqual(charged, self.pool._charged)
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.owner.result(prepared[1], self.pool, prepared[3])

    def test_completion_receipt_before_at_and_after_limits_has_deterministic_precedence(self):
        for number, completed, observed, deadline, reason in (
            (1, 9, 25, 60, None), (2, 10, 12, 60, "work_silence"),
            (3, 8, 12, 8, "absolute_deadline"),
        ):
            with self.subTest(reason=reason):
                # New attempt origin; old decisions remain captured and cannot revive.
                self.origin = max(self.clock.value, time.monotonic_ns())
                self.clock.value = self.origin
                ready, resume = threading.Event(), threading.Event()
                self.addCleanup(resume.set)
                original = codex_worker._PREPARE_RESULT

                def blocked(*args):
                    ready.set()
                    if not resume.wait(5):
                        raise RuntimeError("fixture completion gate timed out")
                    return original(*args)

                with mock.patch.object(codex_worker, "_PREPARE_RESULT", side_effect=blocked):
                    prepared = self.prepare(number, deadline=deadline)
                    self.assertTrue(ready.wait(5))
                    self.at(completed)
                    resume.set()
                    self.returned(prepared)
                self.at(observed)
                result = self.owner.result(prepared[1], self.pool, prepared[3])
                self.assertEqual(reason, result.stop_reason)
                self.assertEqual(reason is not None, result.packet.fenced)
                if reason is not None:
                    self.assertEqual("timed_out", json.loads(result.packet.body)["execution_state"])
                self.registry.acknowledge_release(prepared[2])  # Fixture-local custody, not a durable account receipt.
                self.owner.retire(prepared[1], self.pool, prepared[3])

    def test_slots_stay_charged_until_original_release_and_retirement(self):
        first, second = self.prepare(1), self.prepare(2)
        self.returned(first)
        self.returned(second)
        charged = self.pool._charged
        with self.assertRaises(CodexWorkerError):
            self.pool.retire(first[3])
        target = owners.attempt(3)
        key = OwnedExecutionKey(target.project_id, "c" * 64, target.run_id, target.claim_id)
        ticket = self.registry.issue(key, deadline_ns=self.deadline, run_handle=target.run_handle)
        with self.assertRaisesRegex(CodexWorkerError, "worker_busy"):
            self.pool.reserve(ticket, b"{}")
        self.assertEqual(charged, self.pool._charged)
        self.registry.acknowledge_release(first[2])
        self.owner.retire(first[1], self.pool, first[3])
        self.assertLess(self.pool._charged, charged)
        with self.assertRaises(CodexWorkerError):
            self.pool.packet(first[3])

    def test_local_resources_can_be_reserved_before_admission_and_only_unbound_inputs_withdraw(self):
        first, second = self.pool.prepare(b"{}"), self.pool.prepare(b"{}")
        self.assertEqual("prepared", self.pool.snapshot(first)["state"])
        with self.assertRaisesRegex(CodexWorkerError, "worker_busy"):
            self.pool.prepare(b"{}")
        self.pool.withdraw_prepared(first)
        self.pool.withdraw_prepared(second)
        self.assertEqual(0, self.pool._charged)
        prepared = self.prepare(submit=False)
        with self.assertRaises(CodexWorkerError):
            self.pool.withdraw_prepared(prepared[3])
        self.assertGreater(self.pool._charged, 0)

    def test_safe_validator_reason_survives_worker_and_owner_delivery_without_rejected_content(self):
        invalid = {**fixtures.result(), "evidence": [], "summary": "PRIVATE-REJECTED-RESULT"}
        prepared = self.prepare(value=invalid)
        self.returned(prepared)
        result = self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertEqual("missing_evidence", result.packet.validation_reason)
        self.assertEqual("missing_evidence", json.loads(result.packet.report)["result_validation_reason_code"])
        self.assertEqual("failed", json.loads(result.packet.body)["execution_state"])
        self.assertNotIn("PRIVATE", result.packet.body.decode())
        self.assertNotIn("PRIVATE", result.packet.report.decode())

    def test_an_invalid_answer_on_the_worker_path_is_kept_owner_only_with_its_digest(self):
        """Review r2 MED-1 (VF-INV-a, decision 26 "original kept"): the pool's runners did not offer the
        raw answer, so a worker-process dispatch kept nothing."""
        import hashlib
        import stat

        ledger, project = self.fixture.ledger, self.fixture.project_id

        class Keeper:  # The worker's project id names the run; the fixture ledger holds its one project.
            def keep_child_answer(self, _project_id, content):
                return ledger.keep_child_answer(project, content)

        invalid = {**fixtures.result(), "admission_state": "accepted", "summary": "PRIVATE-KEPT-CLAIM"}
        prepared = self.prepare(value=invalid, submit=False)
        self.pool.attach_answer_keeper(prepared[3], Keeper())
        self.pool.submit(prepared[3])
        self.returned(prepared)
        result = self.owner.result(prepared[1], self.pool, prepared[3])
        report = json.loads(result.packet.report)
        self.assertEqual("invalid_result", report["outcome"])
        self.assertNotIn("PRIVATE", result.packet.body.decode() + result.packet.report.decode())
        (path,) = sorted((self.fixture.state.artifacts / project).glob("child_answer_*.json"))
        raw = path.read_bytes()
        kept = report["raw_answer"]
        self.assertEqual({"kept", "sha256", "bytes", "truncated", "scrubbed"}, set(kept))
        self.assertEqual((hashlib.sha256(raw).hexdigest(), len(raw), True, False),
                         (kept["sha256"], kept["bytes"], kept["kept"], kept["scrubbed"]))
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertEqual("accepted", json.loads(raw)["admission_state"])
        with ledger._held():
            row = ledger._connection.execute("SELECT content_digest FROM stored_files WHERE project_id=? AND file_id=?",
                                             (project, path.stem)).fetchone()
        self.assertEqual(kept["sha256"], row["content_digest"])

    def test_a_worker_without_a_keeper_reports_no_kept_answer_and_still_returns(self):
        prepared = self.prepare()
        self.returned(prepared)
        report = json.loads(self.owner.result(prepared[1], self.pool, prepared[3]).packet.report)
        self.assertEqual("result", report["outcome"])
        self.assertNotIn("raw_answer", report)

    def test_unknown_native_model_text_is_not_retained_as_report_metadata(self):
        rows = [{"frame": {"type": "turn.started", "model": "PRIVATE-UNKNOWN-MODEL"}},
                {"result": fixtures.null_filled(fixtures.result())},
                {"frame": {"type": "turn.completed", "usage": {"input_tokens": "PRIVATE-USAGE"}}}]
        prepared = self.prepare(rows=rows)
        self.returned(prepared)
        result = self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertFalse(result.packet.reviewed)
        self.assertIsNone(json.loads(result.packet.report)["observed_model"])
        self.assertIsNone(json.loads(result.packet.report)["usage"])
        self.assertNotIn("PRIVATE", result.packet.report.decode())

    def test_supported_full_size_raw_result_is_handed_off_without_owner_body_parsing_or_copy(self):
        value = {**fixtures.result(), "summary": ""}
        size = len(canonical_json(fixtures.null_filled(value)))
        value["summary"] = "x" * (MAX_RESULT_BODY_BYTES - size)
        self.assertEqual(MAX_RESULT_BODY_BYTES, len(canonical_json(fixtures.null_filled(value))))
        prepared = self.prepare(value=value)
        self.returned(prepared)
        packet = self.pool.packet(prepared[3])
        self.assertFalse(packet.fenced)
        self.assertIsNone(packet.error_code)
        with mock.patch("codex_agents.bridge_liveness.json.loads", side_effect=AssertionError("owner body parse")):
            delivered = self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertIs(packet.body, delivered.packet.body)
        self.assertEqual(value, json.loads(delivered.packet.body))

    def test_forged_copied_wrong_scope_and_foreign_thread_handles_are_refused(self):
        prepared = self.prepare(submit=False)
        _, target, ticket, handle, _, _ = prepared
        for forged in (CodexWorkerHandle(), copy(handle)):
            with self.assertRaises(CodexWorkerError):
                self.pool.submit(forged)
        with self.assertRaises(ExecutionCustodyError):
            ticket.spawn([])
        with self.assertRaises(Exception):
            self.owner.result(replace(target, run_id="run:" + "b" * 64), self.pool, handle)
        outcomes = []

        def foreign():
            try:
                self.pool.snapshot(handle)
            except CodexWorkerError as error:
                outcomes.append(error.reason_code)

        thread = threading.Thread(target=foreign)
        thread.start()
        thread.join(2)
        self.assertEqual(["worker_invalid_handle"], outcomes)
        self.assertFalse(self.pool.snapshot(handle)["returned"])

    def test_submit_uses_prestarted_workers_and_decodes_only_off_the_owner_thread(self):
        prepared = self.prepare(submit=False)
        original = codex_worker.parse_bounded_json
        calls = []
        owner = threading.current_thread()

        def decode(*args, **kwargs):
            self.assertIsNot(owner, threading.current_thread())
            calls.append(True)
            return original(*args, **kwargs)

        with mock.patch.object(codex_worker, "parse_bounded_json", side_effect=decode), \
                mock.patch.object(threading.Thread, "start", side_effect=AssertionError("late thread start")), \
                mock.patch.object(threading.Thread, "join", side_effect=AssertionError("owner join")):
            self.pool.submit(prepared[3])
            self.returned(prepared)
        self.assertTrue(calls)
        self.assertIsNone(self.pool.packet(prepared[3]).error_code)

    def test_read_only_and_key_free_input_fail_before_any_process_without_raw_diagnostics(self):
        for number, mutate in ((1, lambda data: data["environ"].update(OPENAI_API_KEY="PRIVATE-KEY")),
                               (2, lambda data: data["route"].update(sandbox_mode="workspace-write"))):
            with self.subTest(number=number):
                prepared = self.prepare(number, mutate=mutate)
                self.returned(prepared)
                packet = self.pool.packet(prepared[3])
                self.assertEqual("worker_input_invalid", packet.error_code)
                self.assertIsNone(packet.body)
                self.assertFalse((prepared[0] / "launches").exists())
                # Corrected for O0c M1 (requirement): an execution that provably never launched is clean
                # once its worker returned, so its normal release frees its slots (it held them for good).
                self.assertTrue(prepared[4].snapshot()["cleanup_confirmed"])
                self.assertNotIn("PRIVATE", repr(packet))

    def test_publication_fault_keeps_unknown_custody_and_cannot_deliver_a_success(self):
        with mock.patch.object(self.registry, "_complete_worker", side_effect=OSError("PRIVATE-PUBLISH")):
            prepared = self.prepare()
            self.until(lambda: self.pool.snapshot(prepared[3])["state"] == "unknown")
        with self.assertRaises(CodexWorkerError):
            self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertIsNone(self.pool._jobs[prepared[3]].packet.body)
        self.assertGreater(self.pool._charged, 0)
        self.owner.run_once()
        self.assertEqual("monitor_unavailable", self.monitor.status(prepared[1]).stop_reason)

    def test_missing_actual_completion_cannot_be_substituted_by_a_returned_packet(self):
        with mock.patch.object(self.registry, "_complete_worker", return_value=None):
            prepared = self.prepare()
            self.until(lambda: self.pool.snapshot(prepared[3])["state"] == "unknown")
        with self.assertRaises(CodexWorkerError):
            self.owner.result(prepared[1], self.pool, prepared[3])
        self.assertFalse(prepared[4].snapshot()["worker_returned"])
        self.assertIsNone(self.pool._jobs[prepared[3]].packet.body)

    def test_worker_interrupt_has_fixed_metadata_and_no_input_bearing_traceback(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), mock.patch.object(
            codex_worker, "_PREPARE_RESULT", side_effect=KeyboardInterrupt("PRIVATE-INTERRUPT")
        ):
            prepared = self.prepare()
            self.returned(prepared)
        packet = self.pool.packet(prepared[3])
        self.assertEqual("worker_interrupted", packet.error_code)
        self.assertIsNone(packet.body)
        self.assertTrue(prepared[4].snapshot()["cleanup_confirmed"])
        self.assertNotIn("PRIVATE", captured.getvalue())

    def test_oversized_input_and_invalid_limits_do_not_reserve_or_launch(self):
        target = owners.attempt()
        key = OwnedExecutionKey(target.project_id, "b" * 64, target.run_id, target.claim_id)
        ticket = self.registry.issue(key, deadline_ns=self.deadline, run_handle=target.run_handle)
        with self.assertRaises(CodexWorkerError):
            self.pool.reserve(ticket, b"x" * (MAX_INPUT_BYTES + 1))
        self.assertEqual(0, self.pool._charged)
        for values in ((True, 4_000_000), (0, 4_000_000), (1, MAX_PACKET_BYTES - 1)):
            with self.assertRaises(CodexWorkerError):
                CodexWorkerLimits(*values)


if __name__ == "__main__":
    unittest.main()
