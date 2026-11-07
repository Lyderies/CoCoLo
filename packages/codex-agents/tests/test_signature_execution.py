from __future__ import annotations

import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tests.test_signature_inputs as capture_fixture
from codex_agents import signature_execution as execution
from codex_agents.signature_input_wire import SignatureWireLimits
from codex_agents.signature_payloads import SignaturePayloadError

MACOS_WORKER = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
BOOTSTRAP_HEADER = """import sys,time,os
sys.path.insert(0,sys.argv[1])
from codex_agents.signature_process import _require_fork_denial,_ready
_require_fork_denial()
"""


@unittest.skipUnless(MACOS_WORKER, "fixed macOS test guard; no Linux worker policy is selected")
class SignatureExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        self.fixture.setUp()
        self.inputs = self.fixture.capture()
        self.threads = []
        self.outcomes = []

    def tearDown(self):
        for thread in self.threads:
            thread.join(timeout=7)
            self.assertFalse(thread.is_alive(), "fixed execution fixture did not end")
        for outcome in self.outcomes:
            if isinstance(outcome, execution.SignatureExecutionUnconfirmed):
                context = outcome.context
                self.assertTrue(context.reaped, "fixture retained an unreaped owned root")
        self.assertTrue(self.fixture.doCleanups(), "owned fixture cleanup failed")

    def run_worker(self, *, duration_ns=30_000_000_000):
        incoming, outgoing = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        ready = threading.Event()

        def worker():
            ready.set()
            inputs, deadline = incoming.get()
            try:
                result = execution._execute_signature_job(
                    inputs, SignatureWireLimits(8, 65536), deadline,
                    threading.Event(), 10_000_000, 5_000_000_000,
                )
            except BaseException as error:
                result = error
            inputs = None
            outgoing.put_nowait(result)

        thread = threading.Thread(target=worker, daemon=True)
        self.threads.append(thread)
        thread.start()
        self.assertTrue(ready.wait(timeout=5))
        incoming.put_nowait((self.inputs, time.monotonic_ns() + duration_ns))
        result = outgoing.get(timeout=8)
        self.outcomes.append(result)
        return result

    def test_actual_silent_helper_expires_and_reaps_without_process_table_or_group_signal(self):
        ready = threading.Event()
        original = execution._Context.sample

        def observe(context, *args, **kwargs):
            if context.guarded:
                ready.set()
            return original(context, *args, **kwargs)

        stalled = BOOTSTRAP_HEADER + '_ready(b"P")\nsys.stdin.buffer.read(65563)\ntime.sleep(60)\n'
        with mock.patch.object(execution, "_BOOTSTRAP", stalled), \
                mock.patch.object(execution._Context, "sample", observe), \
                mock.patch("codex_agents.process_tree.process_table", side_effect=AssertionError("process-table scan")), \
                mock.patch("codex_agents.process_tree.os.killpg", side_effect=AssertionError("group signal")):
            result = self.run_worker(duration_ns=1_000_000_000)
        self.assertTrue(ready.is_set())
        self.assertIs(type(result), execution.SignatureExecutionResult)
        self.assertEqual(("signature_execution_expired", (), 1, False),
                         (result.reason, result.indices, result.roots_reaped, result.no_process_started))

    def test_actual_oversized_output_cannot_escape_the_response_cap(self):
        overflow = BOOTSTRAP_HEADER + '_ready(b"P")\nsys.stdin.buffer.read(65563)\nos.write(1,b"x"*2161)\n'
        with mock.patch.object(execution, "_BOOTSTRAP", overflow):
            result = self.run_worker()
        self.assertIs(type(result), execution.SignatureExecutionResult)
        self.assertEqual(("signature_execution_output_limit", (), 1),
                         (result.reason, result.indices, result.roots_reaped))

    def test_foreign_actual_response_is_refused_after_owned_root_reaping_and_scratch_cleanup(self):
        observed = {}
        original = execution._Context.sample

        def observe(context, *args, **kwargs):
            if context.process is not None:
                observed["process"] = context.process
                observed["scratch"] = context.scratch
            return original(context, *args, **kwargs)

        foreign = BOOTSTRAP_HEADER + """from codex_agents.signature_input_wire import SignatureWireLimits
from codex_agents.signature_payloads import prepare_signature_payloads
_ready(b"P")
wire=sys.stdin.buffer.read(65563)
response=bytearray(prepare_signature_payloads(wire,SignatureWireLimits(8,65536)))
response[8]^=1
os.write(1,response)
"""
        with mock.patch.object(execution, "_BOOTSTRAP", foreign), \
                mock.patch.object(execution._Context, "sample", observe):
            result = self.run_worker()
        self.assertIs(type(result), SignaturePayloadError)
        self.assertEqual("signature_payload_invalid_response", result.reason_code)
        self.assertEqual(0, observed["process"].returncode)
        self.assertFalse(observed["scratch"].exists())
