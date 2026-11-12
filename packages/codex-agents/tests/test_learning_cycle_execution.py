"""Proposed synthetic custody tests and disposable Darwin policy tests.

No production activation or native acceptance. All canaries/metadata are synthetic.
Real-process cases retain the production fixed command and no-fork policy.
"""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from codex_agents import learning_cycle_execution as execution
from codex_agents import learning_cycle_process as helper
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_cycle_core import plan_learning_cycle

BINDING = "d" * 64
MACOS = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


def request(operation="select_mining", outcomes=(), cases=(), **extra):
    return canonical_json({
        "schema_version": 1, "operation": operation, "binding_digest": BINDING,
        "outcomes": list(outcomes), "cases": list(cases),
        "owner_invalidated_run_ids": [], **extra,
    })


def reply(raw, **changes):
    return canonical_json({
        "schema_version": 1, "operation": "select_mining", "binding_digest": BINDING,
        "input_digest": hashlib.sha256(raw).hexdigest(),
        "result": {"mining_references": []}, **changes,
    })


class Stream:
    def __init__(self, descriptor):
        self.descriptor, self.closed = descriptor, False

    def fileno(self):
        return self.descriptor

    def close(self):
        self.closed = True


class Synthetic:
    """Scripted nonblocking pipes, short writes and an unreaped original child."""
    def __init__(self, raw, *, body=None, marker=None, stderr=b"", code=0, stall=False):
        self.raw = raw
        self.stdin, self.stdout, self.stderr = (Stream(i) for i in (10, 11, 12))
        self.pid, self.returncode = 43210, None
        ready = helper.LEARNING_PROCESS_READY + b"S" + struct.pack(">Q", self.pid)
        marker = ready if marker is None else marker
        self.header = bytearray(marker)
        self.body = bytearray(reply(raw) if body is None else body)
        self.error = bytearray(stderr)
        self.code, self.stall, self.killed = code, stall, False
        self.out_eof = self.err_eof = False
        self.written = bytearray()
        self.registered = {}
        self.tick = 0
        self.context = None

    def clock(self):
        self.tick += 1_000_000
        return self.tick

    def register(self, stream, event):
        self.registered[stream] = event

    def unregister(self, stream):
        del self.registered[stream]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def select(self, delay):
        assert 0 <= delay <= 0.1
        ready = []
        for stream, event in tuple(self.registered.items()):
            available = (
                stream is self.stdin
                or (stream is self.stdout and (
                    self.header or (self.stdin.closed and not self.stall)))
                or (stream is self.stderr and not self.stall)
            )
            if available:
                ready.append((SimpleNamespace(fileobj=stream), event))
        return ready

    def read(self, descriptor, maximum):
        assert 1 <= maximum <= 65_536
        if descriptor == 12:
            if self.error:
                result = bytes(self.error[:maximum])
                del self.error[:maximum]
                return result
            self.err_eof = True
            return b""
        assert descriptor == 11
        if self.header:
            # Deliberately fragmented marker.
            size = min(4, maximum, len(self.header))
            result = bytes(self.header[:size])
            del self.header[:size]
            return result
        assert self.stdin.closed
        if self.body:
            result = bytes(self.body[:maximum])
            del self.body[:maximum]
            return result
        self.out_eof = True
        return b""

    def write(self, descriptor, data):
        assert descriptor == 10 and not self.header
        assert self.context.guarded, "input preceded original PID/phase custody"
        assert 0 < len(data) <= 65_536
        count = min(3, len(data))
        self.written.extend(data[:count])
        return count

    def record(self, pid):
        assert pid == self.pid
        assert self.context.process is self, "Popen handle was not retained first"
        return os.getpid(), self.pid, "original-birth", self.exited()

    def exited(self):
        return self.killed or (self.out_eof and self.err_eof and not self.stall)

    def kill(self):
        self.killed = True

    def wait(self, timeout):
        assert 0 <= timeout <= 5
        if not self.exited():
            raise subprocess.TimeoutExpired("fixed", timeout)
        self.returncode = -9 if self.killed else self.code
        return self.returncode

    def installed(self, test):
        stack = ExitStack()
        test.addCleanup(stack.close)
        original = execution._Context.sample

        def sample(context):
            self.context = context
            return original(context)

        for target, name, value in (
            (execution.sys, "platform", "darwin"),
            (execution.subprocess, "Popen", mock.Mock(return_value=self)),
            (execution.tempfile, "mkdtemp", mock.Mock(return_value="/synthetic-learning-owned")),
            (execution.os, "rmdir", mock.Mock()),
            (execution.os, "set_blocking", mock.Mock()),
            (execution.os, "read", self.read),
            (execution.os, "write", self.write),
            (execution.selectors, "DefaultSelector", mock.Mock(return_value=self)),
            (execution.time, "monotonic_ns", self.clock),
            (execution, "process_record", self.record),
            (execution._Context, "sample", sample),
        ):
            stack.enter_context(mock.patch.object(target, name, value))
        return self


class LearningExecutionSyntheticTests(unittest.TestCase):
    def run_job(self, raw, **changes):
        options = dict(operation="select_mining", binding_digest=BINDING,
                       deadline_ns=10_000_000_000, cancelled=threading.Event())
        options.update(changes)
        return execution._execute_learning_job(raw, **options)

    def test_invalid_types_and_limits_never_launch(self):
        raw = request()
        invalid = [
            {"raw": bytearray(raw)}, {"raw": b""}, {"raw": True},
            {"operation": True}, {"operation": "CANARY"}, {"binding_digest": "D" * 64},
            {"binding_digest": "d" * 63}, {"binding_digest": b"d" * 64},
            {"deadline_ns": True}, {"deadline_ns": 0}, {"deadline_ns": 1 << 63},
            {"cancelled": object()}, {"poll_ns": True}, {"poll_ns": 0},
            {"poll_ns": 999_999}, {"poll_ns": 100_000_001},
            {"cleanup_ns": True}, {"cleanup_ns": 0}, {"cleanup_ns": 999_999_999},
            {"cleanup_ns": 5_000_000_001},
        ]
        with mock.patch.object(execution.sys, "platform", "darwin"), \
                mock.patch.object(execution.subprocess, "Popen") as launch:
            for changes in invalid:
                with self.subTest(fields=tuple(changes)), self.assertRaisesRegex(
                        ValueError, "^learning_execution_invalid_input$"):
                    self.run_job(**{"raw": raw, **changes})
            with mock.patch.object(execution, "MAX_INPUT_BYTES", 1), \
                    self.assertRaisesRegex(ValueError, "^learning_execution_invalid_input$"):
                self.run_job(raw)
            with mock.patch.object(execution.sys, "platform", "linux"), \
                    self.assertRaisesRegex(ValueError, "^learning_execution_invalid_input$"):
                self.run_job(raw)
            launch.assert_not_called()

    def test_prelaunch_cancel_expiry_and_clock_failure(self):
        for scenario in ("cancelled", "expired", "clock_failure"):
            with self.subTest(scenario=scenario), \
                    mock.patch.object(execution.sys, "platform", "darwin"), \
                    mock.patch.object(execution.subprocess, "Popen") as launch, \
                    mock.patch.object(execution.tempfile, "mkdtemp") as scratch, \
                    mock.patch.object(execution.time, "monotonic_ns", return_value=100):
                event = threading.Event()
                deadline = 1000
                if scenario == "cancelled":
                    event.set()
                if scenario == "expired":
                    deadline = 100
                with ExitStack() as stack:
                    if scenario == "clock_failure":
                        stack.enter_context(mock.patch.object(
                            execution.time, "monotonic_ns", side_effect=OSError("CANARY")))
                    result = self.run_job(request(), deadline_ns=deadline, cancelled=event)
                self.assertEqual("learning_execution_" + scenario, result.reason)
                self.assertIsNone(result.result)
                self.assertTrue(result.no_process_started)
                self.assertEqual(0, result.roots_reaped)
                launch.assert_not_called()
                scratch.assert_not_called()

    def test_exact_launch_short_writes_fragmented_ready_and_reaped_success(self):
        raw = request()
        fake = Synthetic(raw).installed(self)
        result = self.run_job(raw)
        self.assertEqual(ORCHESTRATION_ROOT, execution._HELPER_ROOT)
        self.assertEqual(("learning_execution_completed", {"mining_references": []}, 1, False),
                         (result.reason, result.result, result.roots_reaped, result.no_process_started))
        self.assertEqual(raw, bytes(fake.written))
        self.assertEqual(0, fake.returncode)
        self.assertNotIn("mining_references", repr(result))
        command = execution.subprocess.Popen.call_args.args[0]
        self.assertEqual([
            "/usr/bin/sandbox-exec", "-p", execution._PROFILE,
            str(Path(sys.executable).resolve()), "-I", "-B", "-S", "-c",
            execution._BOOTSTRAP, str(execution._HELPER_ROOT), "select_mining", "120",
        ], command)
        self.assertEqual(dict(
            cwd=Path("/synthetic-learning-owned"), env=dict(helper._ENVIRONMENT),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, start_new_session=True, bufsize=0,
        ), execution.subprocess.Popen.call_args.kwargs)
        execution.os.rmdir.assert_called_once_with(Path("/synthetic-learning-owned"))

    def test_pre_ready_refusals_retain_original_custody_and_send_no_input(self):
        raw = request()
        correct = helper.LEARNING_PROCESS_READY + b"S" + struct.pack(">Q", 43210)
        for marker in (b"bad", correct[:8] + b"P" + correct[9:],
                       correct[:-8] + struct.pack(">Q", 43211)):
            with self.subTest(marker_length=len(marker)):
                fake = Synthetic(raw, marker=marker).installed(self)
                # Short malformed markers must eventually EOF, not hang waiting
                # for input that the parent correctly refuses to send.
                original_select = fake.select
                def select(delay):
                    if not fake.header and not fake.context.guarded:
                        fake.stdin.closed = True
                        fake.body.clear()
                    return original_select(delay)
                with mock.patch.object(fake, "select", select), self.assertRaises(
                        execution.LearningExecutionUnconfirmed) as caught:
                    self.run_job(raw)
                self.assertIs(caught.exception.context.process, fake)
                self.assertEqual(b"", bytes(fake.written))
                self.assertTrue(caught.exception.context.reaped)
                self.assertEqual("learning_execution_unconfirmed", str(caught.exception))
                execution.os.rmdir.assert_not_called()

    def test_reply_binding_stderr_nonzero_and_overflow_are_failures_after_cleanup(self):
        raw = request()
        cases = [
            (dict(body=b"CANARY"), "invalid_output"),
            (dict(body=reply(raw, operation="plan")), "invalid_output"),
            (dict(body=reply(raw, binding_digest="f" * 64)), "invalid_output"),
            (dict(body=reply(raw, input_digest="f" * 64)), "invalid_output"),
            (dict(stderr=b"SYNTHETIC-SECRET"), "worker_failed"),
            (dict(code=2), "worker_failed"),
            (dict(body=b"x" * 513), "output_limit"),
        ]
        for options, suffix in cases:
            with self.subTest(suffix=suffix), ExitStack() as stack:
                fake = Synthetic(raw, **options).installed(self)
                # Error bytes arrive only after ready, so cleanup is confirmed.
                if options.get("stderr"):
                    select = fake.select
                    def delayed_error(delay):
                        return [(key, event) for key, event in select(delay)
                                if key.fileobj is not fake.stderr or fake.context.guarded]
                    stack.enter_context(mock.patch.object(fake, "select", delayed_error))
                if suffix == "output_limit":
                    stack.enter_context(mock.patch.object(execution, "MAX_OUTPUT_BYTES", 512))
                result = self.run_job(raw)
                self.assertEqual("learning_execution_" + suffix, result.reason)
                self.assertIsNone(result.result)
                self.assertEqual(1, result.roots_reaped)
                self.assertIs(type(fake.returncode), int)
                self.assertNotIn("CANARY", repr(result))

    def test_stall_cancel_and_deadline_kill_only_original_root(self):
        raw = request()
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                fake = Synthetic(raw, stall=True).installed(self)
                event = threading.Event()
                clock = fake.clock
                def tick():
                    now = clock()
                    if cancel and fake.context is not None and fake.context.guarded:
                        event.set()
                    return now
                with mock.patch.object(execution.time, "monotonic_ns", tick):
                    result = self.run_job(raw, cancelled=event, deadline_ns=100_000_000)
                self.assertEqual("learning_execution_" + ("cancelled" if cancel else "expired"),
                                 result.reason)
                self.assertIsNone(result.result)
                self.assertEqual(1, result.roots_reaped)
                self.assertTrue(fake.killed)
                self.assertEqual(-9, fake.returncode)

    def test_spawn_birth_identity_and_cleanup_faults_retain_unknown(self):
        raw = request()
        for fault in ("spawn", "birth", "changed_birth", "changed_parent", "wait", "directory"):
            with self.subTest(fault=fault), ExitStack() as stack:
                fake = Synthetic(raw).installed(self)
                if fault == "spawn":
                    stack.enter_context(mock.patch.object(
                        execution.subprocess, "Popen", side_effect=OSError("CANARY")))
                elif fault == "birth":
                    stack.enter_context(mock.patch.object(execution, "process_record", return_value=None))
                elif fault in ("changed_birth", "changed_parent"):
                    record = fake.record
                    calls = [0]
                    def changed(pid):
                        calls[0] += 1
                        parent, group, birth, exited = record(pid)
                        if calls[0] > 2:
                            if fault == "changed_birth":
                                birth = "replacement"
                            else:
                                parent += 1
                        return parent, group, birth, exited
                    stack.enter_context(mock.patch.object(execution, "process_record", changed))
                elif fault == "wait":
                    stack.enter_context(mock.patch.object(
                        fake, "wait", side_effect=subprocess.TimeoutExpired("fixed", 0)))
                else:
                    stack.enter_context(mock.patch.object(
                        execution.os, "rmdir", side_effect=OSError("CANARY")))
                with self.assertRaises(execution.LearningExecutionUnconfirmed) as caught:
                    self.run_job(raw)
                context = caught.exception.context
                self.assertIsNotNone(context.scratch)
                self.assertEqual(raw, context.raw)
                if fault == "spawn":
                    self.assertTrue(context.spawn_ambiguous)
                else:
                    self.assertIs(context.process, fake)
                if fault in ("birth", "changed_birth", "changed_parent"):
                    self.assertFalse(fake.killed)
                self.assertNotIn("CANARY", str(caught.exception))

    def test_decode_clock_and_late_cleanup_cancel_cannot_publish(self):
        raw = request()
        for fault in ("decode", "clock", "reverse", "late_cancel", "late_expiry"):
            with self.subTest(fault=fault), ExitStack() as stack:
                fake = Synthetic(raw).installed(self)
                event = threading.Event()
                decoder = execution.decode_calculation_reply
                def decode(*args, **kwargs):
                    value = decoder(*args, **kwargs)
                    if fault == "decode":
                        raise RuntimeError("SYNTHETIC-SECRET")
                    if fault in ("clock", "reverse"):
                        stack.enter_context(mock.patch.object(
                            execution.time, "monotonic_ns",
                            side_effect=OSError("CANARY") if fault == "clock" else None,
                            return_value=0))
                    return value
                stack.enter_context(mock.patch.object(execution, "decode_calculation_reply", decode))
                def remove(path):
                    if fault == "late_cancel":
                        event.set()
                    if fault == "late_expiry":
                        fake.tick = 10_000_000_000
                stack.enter_context(mock.patch.object(execution.os, "rmdir", remove))
                result = self.run_job(raw, cancelled=event)
                suffix = {"decode": "internal_failure", "clock": "clock_failure",
                          "reverse": "clock_failure", "late_cancel": "cancelled",
                          "late_expiry": "expired"}[fault]
                self.assertEqual("learning_execution_" + suffix, result.reason)
                self.assertIsNone(result.result)
                self.assertEqual(1, result.roots_reaped)


@unittest.skipUnless(MACOS, "disposable Darwin policy required; no native acceptance")
class LearningExecutionDarwinTests(unittest.TestCase):
    def run_job(self, raw, operation="select_mining", *, cancelled=None, duration=10_000_000_000):
        return execution._execute_learning_job(
            raw, operation=operation, binding_digest=BINDING,
            deadline_ns=time.monotonic_ns() + duration,
            cancelled=threading.Event() if cancelled is None else cancelled,
            poll_ns=10_000_000, cleanup_ns=2_000_000_000,
        )

    def test_real_nonempty_selection_no_inherited_environment_or_descriptors(self):
        from tests.learning_support import verification_outcome
        outcome = verification_outcome()
        raw = request(outcomes=[outcome])
        # The inherited file contains only a synthetic canary, never credentials.
        with tempfile.TemporaryFile() as inherited, \
                mock.patch.dict(os.environ, {"SYNTHETIC_EXECUTION_SECRET": "SYNTHETIC-CANARY",
                                             "PYTHONPATH": "/synthetic-refused"}):
            inherited.write(b"SYNTHETIC-CANARY")
            inherited.flush()
            os.set_inheritable(inherited.fileno(), True)
            result = self.run_job(raw)
        self.assertEqual("learning_execution_completed", result.reason)
        self.assertEqual({"mining_references": [[outcome["record_id"], content_digest(outcome)]]},
                         result.result)
        self.assertEqual((1, False), (result.roots_reaped, result.no_process_started))

    def test_real_full_plan_with_nonempty_candidate_matches_keyless_core(self):
        from tests.learning_support import (
            champion_configuration, full_usage, heldout_cases, verification_outcome,
        )
        outcomes, cases = [], heldout_cases()
        for index in range(20):
            for model, tokens in (("gpt-5.6-terra", 200), ("gpt-5.6-luna", 100)):
                outcome = verification_outcome("execution-%s-%s" % (index, model))
                configuration = {**champion_configuration(), "model": model}
                outcome.update(
                    **configuration, task_slice="coding-medium-low",
                    fixture_digest=f"{index + 2000:064x}",
                    configuration_digest=content_digest(configuration),
                    usage=full_usage(input_tokens=tokens, output_tokens=tokens, model_calls=2),
                )
                outcomes.append(outcome)
                case = dict(heldout_cases(1)[0])
                case.update(
                    record_id="source-case-" + str(len(outcomes)), source_outcome_id=outcome["record_id"],
                    supersedes_case_id=None, corpus_state="development", reviewed=False,
                    frozen=False, review_digest=None, similarity_group_digest=None,
                    lineage_digest=f"{len(outcomes) + 5000:064x}",
                )
                cases.append(case)
        expected = plan_learning_cycle(outcomes, all_cases=cases, as_of_ms=400, max_age_ms=1000)
        self.assertGreater(len(expected["candidate_specs"]), 0)
        result = self.run_job(request("plan", outcomes, cases, as_of_ms=400, max_age_ms=1000), "plan")
        self.assertEqual("learning_execution_completed", result.reason)
        self.assertEqual(expected, result.result)
        self.assertEqual(1, result.roots_reaped)

    def test_real_bootstrap_refuses_sql_owner_provider_imports(self):
        probe = '''import importlib
for forbidden in ("sqlite3","_sqlite3","codex_agents.learning_store",
                  "codex_agents.owner_key_contracts","codex_agents.providers","openai"):
    try:
        importlib.import_module(forbidden)
    except RuntimeError:
        pass
    else:
        raise AssertionError("fixed dependency guard failed")
assert not any(n.startswith(("sqlite3","codex_agents.owner","codex_agents.learning_store"))
               for n in sys.modules)
'''
        bootstrap = execution._BOOTSTRAP.replace(
            "raise SystemExit(main(sys.argv[2:]))",
            probe + "raise SystemExit(main(sys.argv[2:]))",
        )
        with mock.patch.object(execution, "_BOOTSTRAP", bootstrap):
            result = self.run_job(request())
        self.assertEqual("learning_execution_completed", result.reason)

    def test_real_fixed_helper_stall_expires_or_cancels_and_reaps(self):
        # Keep the production guard, no-fork check, ready marker and descriptor
        # validation. Only calculation is replaced with a bounded synthetic stall.
        probe = '''from codex_agents import learning_cycle_process as p
import time
def stall(*args,**kwargs):
    time.sleep(5)
    return b""
p.calculate_learning_request=stall
'''
        bootstrap = execution._BOOTSTRAP.replace(
            "from codex_agents.learning_cycle_process import main",
            probe + "from codex_agents.learning_cycle_process import main",
        )
        original = execution._Context.sample
        observed = []
        event = threading.Event()
        def sample(context):
            if context.process is not None and context.process not in observed:
                observed.append(context.process)
            if context.guarded and cancel:
                event.set()
            return original(context)
        for cancel in (False, True):
            with self.subTest(cancel=cancel), mock.patch.object(execution, "_BOOTSTRAP", bootstrap), \
                    mock.patch.object(execution._Context, "sample", sample), \
                    mock.patch("codex_agents.process_tree.process_table", side_effect=AssertionError), \
                    mock.patch("codex_agents.process_tree.os.killpg", side_effect=AssertionError):
                event.clear()
                result = self.run_job(request(), cancelled=event, duration=2_000_000_000)
            self.assertEqual("learning_execution_" + ("cancelled" if cancel else "expired"),
                             result.reason)
            self.assertEqual((None, 1), (result.result, result.roots_reaped))
        self.assertEqual(2, len(observed))
        self.assertTrue(all(type(process.returncode) is int for process in observed))
