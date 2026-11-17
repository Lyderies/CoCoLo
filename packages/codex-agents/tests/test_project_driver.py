"""A bridged Codex run driven by the project host from open to released capacity.

The collector, the account services and the project host run as threads of
this process, so the fake provider clients the bridge tests trust are the ones
they resolve. Everything else is the product path: the in-process CLI, the
hosts' enrollment, the run driver and the original owners.
"""
import base64
import dataclasses
import io
import contextlib
import sqlite3
import json
import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT
from tests.learning_support import attest_provider
from tests.service_harness import ServiceHarness
from tests.test_bridge import (
    CLEAN, PARENT_GRANT, RATE_LIMIT, CliTestCase, cli, git, init_frame, null_filled, result, result_frame,
)

from codex_agents import codex_preparation, codex_worker, learning_store, project_driver
from codex_agents.account_api import AccountClient, AccountError
from codex_agents.rate_limit_contracts import RateLimitError
from codex_agents.liveness_clock import LivenessLimits

SECOND = 1_000_000_000


class DriverHarness(ServiceHarness, CliTestCase):
    """In-process collector, account and project hosts; the CLI and run driver are the product."""
    primary = 'codex'  # Which primary host class drives the CLI.

    def setUp(self):
        super().setUp()
        if self.primary == 'claude':
            patcher = mock.patch.dict(os.environ)
            patcher.start()
            self.addCleanup(patcher.stop)
            os.environ.pop('CODEX_THREAD_ID', None)
            os.environ.pop('CODEX_SESSION_ID', None)
            os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='claude-primary')
        self.admit('codex')
        self.start_services()

    def open_run(self, project=None):
        project = project or self.project
        name = project.name + '-'
        task = self.write(name + 'task.json', self.task())
        route = self.cli_for(project, 'learning-route', '--task', task)
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write(name + 'assignment.json', assigned),
                   '--parent-grant', self.write(name + 'grant.json', PARENT_GRANT))
        route_path = self.write(name + 'route.json', route)
        orchestration = self.cli_for(project, 'learning-open-orchestration')
        opened = self.cli_for(project, 'learning-open-host-run', *context, '--route', route_path,
                              '--orchestration-handle', orchestration['orchestration_handle'])
        return context, route_path, orchestration['orchestration_handle'], opened['run_handle']

    def cli_any(self, command, *arguments):
        """The CLI's reply whatever its exit status, for a command polled until it is ready."""
        from tests.test_bridge import cli as cli_module
        stdout = io.StringIO()
        argv = ["cli.py", command, "--state-root", str(self.state.root), "--project-root", str(self.project),
                *arguments]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(cli_module, "BUNDLE_ROOT", self.bundle), \
                contextlib.redirect_stdout(stdout):
            cli_module.main()
        return json.loads(stdout.getvalue())

    def separate_dispatch(self, context, route_path, run_handle, project=None):
        """A second primary process waits on dispatch while this one inspects the run."""
        env = {k: v for k, v in os.environ.items() if not any(m in k.upper() for m in ('API_KEY', 'ACCESS_TOKEN', 'SECRET'))}
        for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
            env.pop(name, None)
        if self.primary == 'claude':
            env.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='claude-primary')
        else:
            env['CODEX_THREAD_ID'] = 'driver-primary'
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', str(ORCHESTRATION_ROOT / 'cli.py'), 'dispatch',
            '--state-root', str(self.state.root), '--project-root', str(project or self.project), *context,
            '--route', route_path, '--run-handle', run_handle], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process


class ProjectDriverTests(DriverHarness):
    def test_routing_and_preflight_never_open_the_ledger_or_pause_the_collector(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'running-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        task = self.write('other-task.json', {**self.task(), 'id': 'task-other'})
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')):
            route = self.cli('learning-route', '--task', task)
            assigned = {**self.assignment_for(route), 'assignment_id': 'other', 'task_id': 'task-other'}
            other = ('--task', task, '--assignment', self.write('other-assignment.json', assigned),
                     '--parent-grant', self.write('grant.json', PARENT_GRANT))
            self.assertEqual(assigned, self.cli('validate-assignment', *other))
            self.assertTrue(self.cli('result-preflight', *other)['ready'])
        self.cli('learning-cancel-run', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        process.communicate(timeout=30)

    def test_a_run_whose_preparation_fails_says_which_stage_stopped_it(self):
        context, route_path, _, run_handle = self.open_run()
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('preparation_managed_configuration', refused['reason_code'], refused)
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_a_run_refused_before_or_inside_preparation_names_the_refusal(self):
        # local-routing's 0.8.17 test still saw only run_unavailable, 0.3 s after dispatch.
        cases = (
            ('submit', codex_preparation.PreparationError('preparation_full'), 'preparation_full'),
            ('submit', codex_preparation.PreparationError('preparation_not_authorized'), 'preparation_not_authorized'),
            ('build', RuntimeError('unexpected'), 'preparation_failed_at_read_paths'),
        )
        for where, error, reason in cases:
            with self.subTest(reason=reason, where=where):
                context, route_path, _, run_handle = self.open_run()
                patch = (mock.patch.object(codex_preparation.CodexPreparationLane, 'submit', side_effect=error)
                         if where == 'submit' else mock.patch.object(
                             codex_preparation.bridge, 'assignment_read_paths', side_effect=error))
                with patch:
                    refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle,
                                       expected=2)
                self.assertEqual(reason, refused['reason_code'], refused)
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_D1_D2_a_run_refused_before_its_claim_never_holds_an_update_back(self):
        # 0.3.2 requirements (local-routing: project_service_other_version after an update,
        # from runs admitted that morning whose dispatch failed before they claimed a slot).
        # D1: a child launches only after its claim, so replacing the collector or a project
        # service of another version never waits for an unclaimed run.
        # D2: the learning move counts it until its binding expires; then it can never be claimed.
        context, route_path, _, run_handle = self.open_run()
        with mock.patch.object(codex_preparation.bridge, 'codex_managed_configuration_present', return_value=True):
            self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        ledger = self.collector_ledger
        self.assertEqual(0, ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity").fetchone()[0])
        self.assertTrue(ledger.drained(children_only=True))                              # D1
        self.assertFalse(ledger.drained())                                               # D2, before expiry
        expires = ledger._connection.execute("SELECT MAX(expires_at_ms) FROM host_run_bindings").fetchone()[0]
        with mock.patch.object(ledger, '_clock_ms', return_value=expires + 1):
            self.assertTrue(ledger.drained())                                            # D2, after expiry

    def test_C8_three_children_of_one_project_run_at_once_a_fourth_is_refused_and_each_can_be_cancelled(self):
        # 0.3.3 requirement C8 (owner): a pool of six, at most three per project. One project's three
        # dispatches, started together, are all claimed and launched; a fourth of the same project is
        # refused by that project's own limit; and cancelling each of the three works (with four or
        # more a cancellation was not always carried out).
        runs = [self.open_run() for _ in range(4)]
        # Far longer than the test: each child ends only because it is cancelled.
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'one-of-three'}, {'sleep': 600},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        processes = [self.separate_dispatch(run[0], run[1], run[3]) for run in runs[:3]]
        launched = lambda: len([i for i in self.invocations('codex') if 'exec' in i['argv']])  # noqa: E731
        self.wait_for(lambda: launched() == 3, seconds=90)
        fourth = runs[3]
        refused = self.cli('dispatch', *fourth[0], '--route', fourth[1], '--run-handle', fourth[3], expected=2)
        self.assertEqual('preparation_full', refused['reason_code'], refused)
        self.assertEqual(3, launched())
        children = [i for i in self.invocations('codex') if 'exec' in i['argv']]
        for child in children:
            os.killpg(child['pgid'], 0)  # All three are alive together.
        for run, process in zip(runs, processes):
            started = time.monotonic()
            self.cli('learning-cancel-run', '--orchestration-handle', run[2], '--run-handle', run[3])
            output, errors = process.communicate(timeout=60)
            self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
            self.assertEqual('cancelled', json.loads(output)['bridge_report']['terminal_state'])
            self.assertLess(time.monotonic() - started, 30)
        for child in children:
            with self.assertRaises(ProcessLookupError):  # Every child's process group is gone.
                os.killpg(child['pgid'], 0)

    def busy_day(self, kind, count):
        """Add `count` copies of the account's latest `kind` reading from the last few hours, as a
        busy account records them; through a second connection, as the services keep running."""
        import hashlib
        from codex_agents.learning_contracts import canonical_json
        connection = sqlite3.connect(self.state.root / 'learning.sqlite3', timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            template = connection.execute(
                "SELECT project_id, source, content FROM records WHERE record_type=? "
                "ORDER BY observed_at_ms DESC LIMIT 1", (kind,)).fetchone()
            sequence = connection.execute("SELECT MAX(source_sequence) FROM records").fetchone()[0]
            now = int(time.time() * 1000)
            with connection:
                for index in range(count):
                    record = json.loads(bytes(template['content']))
                    observed = now - 60_000 - index * 10_000
                    record.update(record_id=f"{kind}:{hashlib.sha256(f'busy-{kind}-{index}'.encode()).hexdigest()}",
                                  observed_at_ms=observed)
                    content = canonical_json(record)
                    sequence += 1
                    connection.execute(
                        "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
                        "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                        (template['project_id'], record['record_id'], kind, template['source'], sequence, observed,
                         observed, observed + 90 * 86_400_000, content, hashlib.sha256(content).hexdigest()))
        finally:
            connection.close()

    def test_C1_a_busy_day_of_readings_still_opens_and_dispatches(self):
        # 0.3.3 requirement C1 (local-routing, 2026-11-16): 181 headroom and 70 billing readings
        # from one day, with 40 recent decisions, filled about 260 KiB of the 256 KiB the open and
        # claim may read, and the claim was refused. 4,096 records and 2 MiB fit, at the open and
        # at its claim; this is a day with about ten times local-routing's readings.
        self.busy_day('provider_headroom', 1800)
        self.busy_day('provider_billing', 700)
        context, route_path, _, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])

    def test_C1_more_than_a_capture_holds_is_refused_plainly_and_changes_nothing(self):
        # Over 4,096 records the open says so, and records no run.
        self.busy_day('provider_headroom', 4200)
        bindings = "SELECT COUNT(*) FROM host_run_bindings"
        before = self.collector_ledger._connection.execute(bindings).fetchone()[0]
        name = self.project.name + '-'
        task = self.write(name + 'task.json', self.task())
        route = self.cli_for(self.project, 'learning-route', '--task', task)
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write(name + 'assignment.json', assigned),
                   '--parent-grant', self.write(name + 'grant.json', PARENT_GRANT))
        orchestration = self.cli_for(self.project, 'learning-open-orchestration')
        refused = self.cli('learning-open-host-run', *context, '--route', self.write(name + 'route.json', route),
                           '--orchestration-handle', orchestration['orchestration_handle'], expected=2)
        self.assertEqual('admission_capture_limit', refused['reason_code'], refused)
        self.assertEqual(before, self.collector_ledger._connection.execute(bindings).fetchone()[0])

    def legacy_history(self, count, content_bytes):
        """Earlier dispatch results of this project as 0.2.x recorded them, through a second connection."""
        import hashlib
        connection = sqlite3.connect(self.state.root / 'learning.sqlite3', timeout=30)
        try:
            project, = connection.execute(
                "SELECT project_id FROM records WHERE record_type='provider_identity' LIMIT 1").fetchone()
            sequence = connection.execute("SELECT MAX(source_sequence) FROM records").fetchone()[0]
            now = int(time.time() * 1000)
            with connection:
                for index in range(count):
                    run_id = 'run:' + hashlib.sha256(f'legacy-{index}'.encode()).hexdigest()
                    content = json.dumps({'record_type': 'dispatch_result', 'run_id': run_id,
                                          'summary': 'x' * content_bytes}, separators=(',', ':')).encode()
                    sequence += 1
                    observed = now - 40 * 86_400_000 + index
                    connection.execute(
                        "INSERT INTO records(project_id,record_id,record_type,source,source_sequence,observed_at_ms,"
                        "ingested_at_ms,expires_at_ms,content,content_digest,late) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                        (project, 'dispatch_result:' + hashlib.sha256(content).hexdigest(), 'dispatch_result',
                         'lifecycle', sequence, observed, observed, observed + 90 * 86_400_000, content,
                         hashlib.sha256(content).hexdigest()))
        finally:
            connection.close()

    def test_C2_a_refused_claim_closes_whatever_the_size_of_an_earlier_record(self):
        # 0.3.3 requirement C2 (local-routing, 2026-11-16; COCOLO-run investigation): the fence
        # looked for the run among every earlier dispatch record of the project, within 2,000
        # progress callbacks, 50 ms and 16 KiB per value, and answered claim_busy when that ran out;
        # after ten refusals the run kept its lease for good. Neither a large earlier record nor many
        # of them stop a refused claim from closing.
        self.legacy_history(1, 20_000)
        self.assert_refused_claim_closes()

    def test_C2_a_refused_claim_closes_whatever_the_number_of_earlier_records(self):
        # 150,000 small earlier records (local-routing has 195,121), each well under 16 KiB: only the
        # callback budget is at stake.
        # With the earlier budget the run does not close; that is what this case proves fixed.
        from codex_agents import claim_denial
        self.legacy_history(150_000, 64)
        with mock.patch.object(claim_denial, 'DENIAL_LEGACY_READ_LIMITS', (2000, 50, 2 * 1_048_576)):
            with self.assertRaises(AssertionError):
                self.assert_refused_claim_closes(seconds=5)
        self.assert_refused_claim_closes()

    def assert_refused_claim_closes(self, seconds=20):
        from codex_agents import signature_job_intake
        from codex_agents.admission_inputs import AdmissionCaptureError
        context, route_path, _, run_handle = self.open_run()
        original = signature_job_intake.SignatureJobIntake.capture_admission

        def claim_over_limit(intake, project_id, provider, work_limits, anchor_ms=None):
            if anchor_ms is not None:
                raise AdmissionCaptureError("admission_capture_limit")
            return original(intake, project_id, provider, work_limits, anchor_ms)
        with mock.patch.object(signature_job_intake.SignatureJobIntake, 'capture_admission', autospec=True,
                               side_effect=claim_over_limit):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('admission_capture_limit', refused['reason_code'], refused)
        deadline = time.monotonic() + seconds
        while True:
            closed = self.cli_any('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                  '--reason', 'recovery_unavailable')
            if closed.get('ready') or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        self.assertEqual((True, 'already_closed'), (closed.get('ready'), closed.get('classification')), closed)

    def test_C2_a_claim_refused_without_a_receipt_settles_by_itself(self):
        # 0.3.3 requirement C2 (local-routing, 2026-11-16): the open was admitted, its one
        # dispatch was refused at the claim with admission_capture_limit and no receipt, and two
        # minutes later the abandonment command still answered history_conflict. A claim refused
        # without a receipt is fenced by the account; the run then closes by itself, holds no
        # capacity, can never be claimed, and the command says it is already closed.
        from codex_agents import signature_job_intake
        from codex_agents.admission_inputs import AdmissionCaptureError
        context, route_path, _, run_handle = self.open_run()
        original = signature_job_intake.SignatureJobIntake.capture_admission

        def claim_over_limit(intake, project_id, provider, work_limits, anchor_ms=None):
            if anchor_ms is not None:  # Only a claim passes its open's anchor.
                raise AdmissionCaptureError("admission_capture_limit")
            return original(intake, project_id, provider, work_limits, anchor_ms)
        with mock.patch.object(signature_job_intake.SignatureJobIntake, 'capture_admission', autospec=True,
                               side_effect=claim_over_limit):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('admission_capture_limit', refused['reason_code'], refused)
        deadline = time.monotonic() + 30
        while True:
            closed = self.cli_any('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                                  '--reason', 'recovery_unavailable')
            if closed.get('ready') or time.monotonic() > deadline:
                break
            time.sleep(0.5)
        self.assertEqual((True, 'already_closed', 'never_claimed'),
                         (closed.get('ready'), closed.get('classification'), closed.get('capacity_state')), closed)
        ledger = self.collector_ledger
        self.assertEqual(0, ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity").fetchone()[0])
        self.assertEqual(('abandoned',), tuple(ledger._connection.execute(
            "SELECT binding_state FROM host_run_bindings").fetchone()))
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_a_slow_enrolled_driver_sender_cannot_stall_the_host(self):
        from codex_agents.client_runtime import read_runtime_hint
        from codex_agents.service_enrollment import enrollment_preamble, proof_directory, read_capability

        _, control, _ = read_runtime_hint(self.paths, self.project)
        driver = control.parent / 'run.sock'
        frame = b'{"attempt":1,"inputs_digest":"' + b'0' * 64 + b'","operation":"run.prepare","protocol":1,"run_handle":"hrn_x"}'
        line = enrollment_preamble(read_capability(self.paths, self.project, 'host'), 'host', 'codex-host-v1', 1, frame,
                                   proof_directory(driver), context=b'{}')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.connect(str(driver))
            slow.sendall(line + b'\n' + frame[:10])  # Enrolled, then the request trickles in.
            started = time.monotonic()
            opened = self.cli('learning-open-orchestration')
            self.assertEqual('active', opened['status'])
            self.assertLess(time.monotonic() - started, 2)
            slow.sendall(frame[10:20])
            slow.settimeout(5)
            self.assertEqual(b'', slow.recv(1))  # Dropped at its absolute deadline, unanswered.

    def test_registering_another_project_during_a_run_does_not_disturb_it(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        # A new project registers, observes and completes the owner's signed billing
        # attestation while the child runs, without ever pausing the collector.
        newcomer = Path(self.temporary.name) / 'newcomer'
        newcomer.mkdir()
        git(newcomer, 'init', '-q')
        with mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')), \
                mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            registered = self.cli_for(newcomer, 'learning-register')
            self.assertRegex(registered['project_id'], '^prj_[0-9a-f]{32}$')
            observed = self.cli_for(newcomer, 'provider-observe', '--provider', 'codex')
            identity = observed['records'][0]
            self.assertEqual('provider_identity', identity['record_type'])
            grant = self.write('newcomer-grant.json', {
                'version': 1, 'provider': 'codex', 'account_fingerprint': identity['account_fingerprint'],
                'no_paid_overage': True, 'no_usage_credits': True, 'issued_at': 2})
            unsigned = self.cli_for(newcomer, 'learning-record-billing-attestation', '--provider', 'codex',
                                    '--grant', grant)
            from tests.learning_support import sign_owner_payload
            signature = sign_owner_payload(self.owner_key, base64.b64decode(unsigned['signing_payload_b64']),
                                           'newcomer-attestation')
            signed = self.cli_for(newcomer, 'learning-record-billing-attestation', '--provider', 'codex',
                                  '--grant', grant, '--signature', str(signature))
            self.assertTrue(signed['ready'])
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual(('running', None), (status['cancellation_state'], status['stop_reason']))
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('busy-observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('busy-result.json', dispatched['result']), '--observation', observation)
        # Terminal acceptance and capacity release reached the restarted account services.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=60)

    def test_learning_migration_never_takes_the_collector_from_under_a_run(self):
        from codex_agents.collector_client import CollectorClient
        from codex_agents.collector_contracts import CollectorError

        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        client = CollectorClient(self.state.socket, timeout=5)
        self.assertEqual({'moved': False, 'has_learning': False, 'drained': False, 'activation_pending': False,
                          'earlier_version_runs': {'holding': 0, 'finished_by_ms': None}},
                         client.learning_migration(self.project))
        with self.assertRaises(CollectorError) as busy:
            client.shutdown(only_if_drained=True)
        self.assertEqual('collector_busy', busy.exception.code)
        # The owner's command reports and refuses; it stops nothing and opens no store.
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            self.assertEqual({'ready': True, 'state': 'not_needed', 'can_start_now': False,
                              'earlier_version_runs': {'holding': 0, 'finished_by_ms': None}},
                             self.cli('learning-migrate-project', '--status'))
            refused = self.cli_for(self.project, 'learning-migrate-project', expected=2)
        self.assertEqual('migration_not_drained', refused['reason_code'])
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual(('running', None), (status['cancellation_state'], status['stop_reason']))
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('busy-observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('busy-result.json', dispatched['result']), '--observation', observation)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=60)
        self.assertTrue(client.learning_migration(self.project)['drained'])
        lifecycle = self.cli('learning-status')['ingestion']['managed_lifecycle']
        self.assertEqual((1, 1, 0), (lifecycle['host_runs_opened'], lifecycle['host_runs_terminal'],
                                     lifecycle['host_runs_pending']))

    def test_a_service_left_by_another_version_is_replaced_only_when_no_run_is_active(self):
        from codex_agents.client_runtime import runtime_hint_path
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST

        # The project was updated while its service idled: the running service is another version's.
        hint = runtime_hint_path(self.paths, self.project)
        value = json.loads(hint.read_text())
        value['scope']['runtime_digest'] = 'f' * 64
        replacement = hint.with_suffix('.tmp')
        replacement.write_text(json.dumps(value, sort_keys=True, separators=(',', ':')))
        replacement.chmod(0o600)
        os.replace(replacement, hint)
        from codex_agents.collector_client import CollectorClient
        from codex_agents.collector_contracts import CollectorError

        with mock.patch.object(cli.CollectorMaintenance, 'acquire',
                               side_effect=CollectorError('a child run is active', code='collector_busy')):
            refused = self.cli_for(self.project, 'learning-open-orchestration', expected=2)
        self.assertEqual('project_service_other_version', refused['reason_code'])
        self.assertIn('try again when none is running', refused['error'])
        self.assertTrue(hint.exists())  # Nothing was stopped.
        # With no run active anywhere the collector is stopped for the moment (so no run can
        # be claimed), the old service is asked to stop, and this version's services take over.
        def stop_started_collector():
            try:
                CollectorClient(self.state.socket, timeout=2).shutdown()
            except Exception:
                pass  # The harness may already have stopped everything.

        self.addCleanup(stop_started_collector)
        opened = self.cli('learning-open-orchestration')
        self.assertRegex(opened['orchestration_handle'], '^orc_[0-9a-f]{48}$')
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST, json.loads(hint.read_text())['scope']['runtime_digest'])
        self.assertEqual('active', self.cli('learning-orchestration-status', '--orchestration-handle',
                                            opened['orchestration_handle'])['status'])

    def test_a_replacement_that_lost_the_race_leaves_the_current_service_alone(self):
        from codex_agents.client_runtime import runtime_hint_path
        from codex_agents.collector_contracts import CollectorError, CollectorRestartError
        from codex_agents.project_host_client import project_host_serving, withdraw_project_host

        self.cli('learning-open-orchestration')  # This version's service serves the project.
        hint = runtime_hint_path(self.paths, self.project)
        published = hint.read_bytes()
        directory = Path(json.loads(published)['dispatch_endpoint']).parent
        arguments = argparse.Namespace(state_root=self.state.root, project_root=self.project)
        # Two commands saw another version's service; the first replaced it. The second
        # looks again once it holds maintenance, and withdraws nothing.
        maintenance = mock.Mock()
        with mock.patch.object(cli.CollectorMaintenance, 'acquire', return_value=maintenance), \
                mock.patch.object(cli, 'withdraw_project_host', side_effect=AssertionError('withdrawn')), \
                mock.patch.object(cli.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            self.assertTrue(cli._replace_other_version_host(arguments))
        maintenance.resume.assert_called_once_with()
        # The withdrawal itself reads the hint again, whatever its caller saw.
        self.assertTrue(withdraw_project_host(self.state.root, self.project, directory))
        self.assertEqual(published, hint.read_bytes())
        self.assertTrue(project_host_serving(self.state.root, self.project))
        # The service published under the lock a withdrawal takes: while one holds it the
        # other waits, so a hint is never withdrawn in the instant it is published.
        import fcntl
        from codex_agents.project_host_client import HINT_LOCK
        held = os.open(directory / HINT_LOCK, os.O_RDWR)
        try:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertFalse(withdraw_project_host(self.state.root, self.project, directory, timeout=0.3))
        finally:
            os.close(held)
        self.assertEqual(published, hint.read_bytes())
        # A collector that does not start again is reported, not passed over.
        maintenance = mock.Mock()
        maintenance.resume.side_effect = CollectorError('collector did not become healthy')
        with mock.patch.object(cli.CollectorMaintenance, 'acquire', return_value=maintenance), \
                self.assertRaises(CollectorRestartError) as stopped:
            cli._replace_other_version_host(arguments)
        self.assertIn('did not start again', str(stopped.exception))

    def test_a_service_asked_to_stop_finishes_the_run_it_holds(self):
        from codex_agents.client_runtime import runtime_hint_path

        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 4},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        # Its hint is withdrawn (as a replacement or a migration would) while the child
        # runs: the service is not found by new commands, but it does not drop the run.
        runtime_hint_path(self.paths, self.project).unlink()
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        self.assertEqual('result', json.loads(output)['bridge_report']['outcome'])
        self.assertNotIn(('driver', 'stopping', 'cancelled'), self.stopped_runs)

    def test_an_owner_ledger_command_is_refused_while_a_child_runs(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'busy-child'}, {'sleep': 3},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        # A command that needs the ledger to itself says why it cannot have it; the
        # shared services and the child keep running.
        refused = self.cli_for(self.project, 'learning-backup-project', '--file-id', 'while_running',
                               '--expires-in-seconds', '3600', expected=2)
        self.assertIn('a child run is active; nothing was changed', refused['error'])
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual(('running', None), (status['cancellation_state'], status['stop_reason']))
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('busy-observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write('busy-result.json', dispatched['result']), '--observation', observation)
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=60)


class LifecycleBoundaryTests(DriverHarness):
    """End-to-end checks of boundaries that are otherwise only tested below the project service."""

    def attempt(self, command, *arguments):
        """Run one command for the project; returns (exit code, report) without asserting either."""
        stdout = io.StringIO()
        argv = ['cli.py', command, '--state-root', str(self.state.root), '--project-root', str(self.project), *arguments]
        with mock.patch.object(sys, 'argv', argv), mock.patch.object(cli, 'BUNDLE_ROOT', self.bundle), \
                contextlib.redirect_stdout(stdout):
            code = cli.main()
        return code, json.loads(stdout.getvalue())

    def executions(self):
        return [i for i in self.invocations('codex') if 'exec' in i['argv']]

    def test_repeated_cancels_keep_one_decision_and_a_finished_run_is_never_launched_again(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'quiet-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(self.executions, seconds=30)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        # A handle nobody was given, in this orchestration or a made-up one, shows and stops nothing.
        for forged in (('--orchestration-handle', orchestration, '--run-handle', 'hrn_' + 'f' * 48),
                       ('--orchestration-handle', 'orc_' + 'f' * 48, '--run-handle', run_handle)):
            for command in ('learning-run-status', 'learning-cancel-run'):
                with self.subTest(command=command, forged=forged[1][:8] + forged[3][:8]):
                    code, refused = self.attempt(command, *forged)
                    self.assertEqual((2, False), (code, refused['ready']))
                    self.assertNotIn('health', refused)
        self.assertEqual('running', self.cli('learning-run-status', *target)['cancellation_state'])
        first = self.cli('learning-cancel-run', *target)
        again = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (first['stop_reason'], first['stop_requested']))
        self.assertEqual(first['stop_reason'], again['stop_reason'])
        # A request is not a confirmed stop: the state says which it is.
        self.assertIn(first['cancellation_state'], {'stop_requested', 'cleanup_confirmed'})
        output, errors = process.communicate(timeout=30)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        fenced = json.loads(output)
        self.assertEqual('cancelled', fenced['bridge_report']['terminal_state'])
        self.assertEqual('cancel_requested', self.cli('learning-cancel-run', *target)['stop_reason'])
        # Dispatching the same handle again never starts another child.
        code, repeated = self.attempt('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual(1, len(self.executions()))
        if code == 0:
            self.assertEqual(fenced['bridge_report']['terminal_state'], repeated['bridge_report']['terminal_state'])
            self.assertEqual(fenced['result'], repeated['result'])
        else:
            self.assertFalse(repeated['ready'])

    def test_an_invalid_result_is_fenced_with_its_reason_and_recorded_exactly_once(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        invalid = {**null_filled(result()), 'evidence': [], 'summary': 'CANARY-rejected-answer'}
        (self.tools / 'codex-last-message.json').write_text(json.dumps(invalid))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        report = dispatched['bridge_report']
        # Fenced as a failure with a typed reason; the rejected answer itself is not passed on.
        self.assertNotEqual('result', report['outcome'])
        self.assertNotIn('CANARY', json.dumps(dispatched))
        self.assertIn('effective_bounds', report)
        self.assertEqual(('invalid_result', 'failed', 'missing_evidence'), (
            report['outcome'], report['terminal_state'], report['result_validation_reason_code']))
        self.assertEqual('failed', dispatched['result']['execution_state'])
        observation = self.write('invalid-observation.json', {
            'run_handle': run_handle, 'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}],
            **({'terminal_state': report['terminal_state']} if report.get('terminal_state') else {})})
        record = ('learning-record-host-result', *context, '--route', route_path,
                  '--result', self.write('fenced-result.json', dispatched['result']), '--observation', observation)
        # The caller cannot turn the fenced failure into a pass.
        softened = {**dispatched['result'], 'semantic_verdict': 'pass', 'status': 'completed', 'execution_state': 'completed'}
        code, refused = self.attempt('learning-record-host-result', *context, '--route', route_path,
                                     '--result', self.write('softened.json', softened), '--observation', observation)
        self.assertEqual((2, 'history_conflict'), (code, refused['reason_code']))
        recorded = self.cli(*record)
        self.assertFalse(recorded['outcome']['accepted'])
        self.assertEqual('missing_evidence', recorded['dispatch_diagnostics']['result_validation_reason_code'])
        self.assertNotIn('CANARY', json.dumps(recorded))
        # Once recorded the host lets the run go; recording it again adds nothing.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        code, again = self.attempt(*record)
        self.assertEqual((2, False), (code, again['ready']))
        store = sqlite3.connect('file:' + str(next(self.state.root.glob('projects/*/history.sqlite3')))
                                + '?mode=ro', uri=True)
        self.addCleanup(store.close)
        self.assertEqual(1, store.execute("SELECT COUNT(*) FROM records WHERE record_type='outcome'").fetchone()[0])
        self.assertEqual([], [row for row in store.execute('SELECT content FROM records')
                              if b'CANARY' in bytes(row[0])])

    def test_the_preview_names_exactly_what_the_child_is_then_given(self):
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        context = ('--task', task, '--assignment', self.write('assignment.json', self.assignment_for(route)),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')):
            preview = self.cli('dispatch-preflight', *context)
        self.assertTrue(preview['preflight_only'])
        self.assertFalse(preview['admission_created'])
        route_path = self.write('route.json', route)
        orchestration = self.cli('learning-open-orchestration')
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration['orchestration_handle'])
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        self.assertEqual(sorted(item['path'] for item in preview['files']), sorted(self.executions()[0]['files']))
        self.assertEqual(preview['capabilities']['model'], route['model'])
        self.assertEqual(preview['capabilities']['sandbox'], 'read-only')


class HostRestartTests(DriverHarness):
    """The project host stops mid-run and starts again: nothing is claimed that is not known."""

    def test_after_a_host_restart_the_run_is_reported_lost_and_the_primary_can_continue(self):
        # The project host the harness started is replaced by one this test can stop alone.
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'orphan'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertIn(self.cli('learning-run-status', *target)['health'], {'starting', 'active', 'quiet'})
        self.restart_host()
        process.communicate(timeout=60)
        # The new host does not know the old run: it is lost (abandoned, outcome unknown), never an outcome.
        after = self.cli('learning-run-status', *target, expected=2)
        self.assertFalse(after['ready'])
        self.assertNotIn('health', after)
        self.assertEqual(('run_lost', 'abandoned', 'unknown'),
                         (after['reason_code'], after['terminal_state'], after['outcome']))
        # The stopped host settled it on its way out, once its child and watchdog were gone:
        # no release receipt is invented, but the binding is abandoned and the slot no longer counts.
        unsettled = [tuple(row) for row in self.ledger._connection.execute(
            'SELECT a.released_at_ms,a.release_digest,h.binding_state,h.abandoned_reason FROM active_run_capacity a '
            'JOIN host_run_bindings h ON h.project_id=a.project_id AND h.handle_digest=a.handle_digest')]
        self.assertEqual([(None, None, 'abandoned', 'service_lost')], unsettled)
        self.assertTrue(self.ledger.drained())
        # The primary carries on: a new run in the same orchestration routes, opens and runs.
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        task = self.write('next-task.json', {**self.task(), 'id': 'task-next'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next', 'task_id': 'task-next'}
        nxt = ('--task', task, '--assignment', self.write('next-assignment.json', assigned),
               '--parent-grant', self.write('grant.json', PARENT_GRANT))
        nxt_route = self.write('next-route.json', route)
        opened = self.cli('learning-open-host-run', *nxt, '--route', nxt_route, '--orchestration-handle', orchestration)
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled({**result(), 'assignment_id': 'next'})))
        dispatched = self.cli('dispatch', *nxt, '--route', nxt_route, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        # The earlier run's claim keeps no release receipt, and neither child was started twice.
        claims = [row[0] for row in self.ledger._connection.execute(
            'SELECT released_at_ms FROM active_run_capacity ORDER BY claimed_at_ms')]
        self.assertEqual(2, len(claims))
        self.assertIsNone(claims[0])
        self.assertEqual(2, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))

    def restart_host(self):
        """Stop the running project host and start a fresh one for the same project."""
        host = [t for t in self.threads if t.name.startswith('test-project-host-')][0]
        self._host_stop.set()
        host.join(20)
        self.assertFalse(host.is_alive())
        self.threads.remove(host)
        self.start_project_host(self.project)

    def setUp(self):
        self._host_stop = threading.Event()
        original = DriverHarness.start_project_host

        def first(harness, project, stop=None):
            return original(harness, project, stop or self._host_stop)

        with mock.patch.object(DriverHarness, 'start_project_host', first):
            super().setUp()


class ManyRunsTests(DriverHarness):
    """A long-lived service runs more children than it tracks at once (four), one after another."""

    worker_once = None  # Set below to HostVerificationTests.worker.
    verifier = None
    verified = None

    def recorded_run(self, name, orchestration):
        task = self.write(name + '-task.json', {**self.task(), 'id': 'task-' + name})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': name, 'task_id': 'task-' + name}
        context = ('--task', task, '--assignment', self.write(name + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(name + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled({**result(), 'assignment_id': name})))
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs[-30:],))):
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'], self.stopped_runs[-30:])
        observation = self.write(name + '-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        self.cli('learning-record-host-result', *context, '--route', route_path,
                 '--result', self.write(name + '-result.json', dispatched['result']), '--observation', observation)
        return opened['run_handle']

    def worker(self):
        """The worker, then four more recorded runs: more than the service tracks at once."""
        orchestration, worker_result, digest = self.worker_once()
        for index in range(4):
            self.recorded_run('later%d' % index, orchestration)
        return orchestration, worker_result, digest

    def test_a_verifier_and_qa_of_an_early_run_work_after_later_runs_pushed_it_past_four(self):
        from tests.test_bridge import passing_check_report
        worker, verifier, verifier_assignment, verifier_result = self.verified()
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        self.assertTrue(checks['checks_passed'])
        qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                      '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                      '--evaluator-assignment', self.write('eval-assignment.json', json.loads(
                          Path(self.temporary.name, 'project-assignment.json').read_text())),
                      '--evaluator-result', str(Path(self.temporary.name, 'worker-result.json')),
                      '--verifier-assignment', self.write('ver-assignment.json', verifier_assignment),
                      '--verifier-result', self.write('ver-result.json', verifier_result))
        self.assertTrue(qa['ready'])
        self.assertIn('accepted', json.dumps(qa))
        self.assertEqual(6, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))

    def test_status_of_a_run_the_service_has_forgotten_answers_not_found(self):
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        run_handle = self.recorded_run('only', orchestration)
        # Recorded, released and retired: the service no longer tracks it.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle,
                          expected=2)
        self.assertEqual((False, 'control_attempt_not_found'), (status['ready'], status['reason_code']))
        self.assertNotIn('health', status)


class HostVerificationTests(DriverHarness):
    """A verifier of a host-driven worker is opened, prepared and run through the same host."""

    def worker(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('worker-result.json', dispatched['result']),
                            '--observation', observation)
        return orchestration, dispatched['result'], recorded['host_outcome_digest']

    def verifier(self, orchestration, digest, **changes):
        task = {**self.task(), 'id': 'verify-1', 'objective': 'Independently verify the research result',
                'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': digest,
                'owned_paths': ['source.txt'], **changes}
        task_path = self.write('verifier-task.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = self.assignment_for(route, assignment_id='verify-assignment-1', task_id='verify-1',
                                       objective=task['objective'], context=[digest], owned_paths=['source.txt'],
                                       verification_required=route['verification_required'])
        context = ('--task', task_path, '--assignment', self.write('verifier-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        return route, assigned, context, self.write('verifier-route.json', route)

    def verified(self):
        orchestration, worker_result, digest = self.worker()
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        self.assertEqual(('codex', 'bridge'), (route['provider'], route['dispatch']))
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        (self.tools / 'codex-last-message.json').write_text(
            json.dumps(null_filled({**result(), 'assignment_id': assigned['assignment_id']})))
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        invocation = [i for i in self.invocations('codex') if 'exec' in i['argv']][-1]
        self.assertEqual(['.orchestration-input/worker-result.json', 'source.txt'], invocation['files'])
        placed = invocation['reserved_inputs']['worker-result.json']
        self.assertEqual(worker_result, json.loads(placed['content']))
        self.assertEqual('0o400', placed['mode'])  # Read-only, and the prompt names it.
        self.assertIn('.orchestration-input/worker-result.json', invocation['argv'][-1])
        observation = self.write('verifier-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('verifier-result.json', dispatched['result']),
                            '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        return digest, recorded['host_outcome_digest'], assigned, dispatched['result']

    def test_a_verifier_gets_the_recorded_worker_result_through_the_host(self):
        self.verified()

    def test_operational_qa_of_a_host_worker_and_verifier_goes_through_the_project(self):
        worker, verifier, verifier_assignment, verifier_result = self.verified()
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        self.assertTrue(checks['checks_passed'])
        files = {'--evaluator-assignment': self.write('eval-assignment.json', json.loads(
                     Path(self.temporary.name, 'project-assignment.json').read_text())),
                 '--evaluator-result': str(Path(self.temporary.name, 'worker-result.json')),
                 '--verifier-assignment': self.write('ver-assignment.json', verifier_assignment),
                 '--verifier-result': self.write('ver-result.json', verifier_result)}
        qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                      '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                      *[item for pair in files.items() for item in pair])
        self.assertTrue(qa['ready'])
        self.assertIn('accepted', json.dumps(qa))
        # A verifier assignment that is not the recorded one (here relabelled) is refused.
        relabelled = {**files, '--verifier-assignment': self.write('relabelled.json', {
            **verifier_assignment, 'role': 'another_verification_worker'})}
        refused = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                           '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                           *[item for pair in relabelled.items() for item in pair], expected=2)
        self.assertFalse(refused['ready'])
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('legacy ledger')):
            again = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                             '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                             *[item for pair in files.items() for item in pair])
        # A repeat is the same committed QA, now reported as known; no legacy ledger is opened.
        self.assertEqual((False, True), (qa['known'], again['known']))
        same = {'known', 'learning_cycle'}  # Each call runs its own learning cycle.
        self.assertEqual({k: v for k, v in qa.items() if k not in same},
                         {k: v for k, v in again.items() if k not in same})
        self.assertEqual('available', qa['learning_cycle']['summary_state'])

    def test_accepted_reconnaissance_of_the_same_task_is_trusted_routing_evidence(self):
        base = self.task()
        original_worker = self.worker

        def reconnaissance():
            # The worker's task does not yet know how broad the work is.
            with mock.patch.object(self, 'task', return_value={**base, 'context_breadth': 'unknown'}):
                return original_worker()

        with mock.patch.object(self, 'worker', reconnaissance):
            worker, verifier, verifier_assignment, verifier_result = self.verified()
        hard = {**base, 'operation': 'end_to_end', 'complexity': 'extreme', 'ambiguity': 'high',
                'context_breadth': 'cross_system', 'routing_evidence_refs': ['outcome:' + worker]}
        hard_path = self.write('hard-task.json', hard)
        plain = self.cli('learning-route', '--task', self.write('plain-task.json', {**hard, 'routing_evidence_refs': []}))
        # Recorded but not yet through QA: cited evidence is refused, not ignored.
        with mock.patch.object(cli, '_learning_ledger', side_effect=AssertionError('ledger path')):
            early = self.cli('learning-route', '--task', hard_path, expected=2)
        self.assertFalse(early['ready'])
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                      '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                      '--evaluator-assignment', self.write('eval-assignment.json', json.loads(
                          Path(self.temporary.name, 'project-assignment.json').read_text())),
                      '--evaluator-result', str(Path(self.temporary.name, 'worker-result.json')),
                      '--verifier-assignment', self.write('ver-assignment.json', verifier_assignment),
                      '--verifier-result', self.write('ver-result.json', verifier_result))
        self.assertTrue(qa['ready'])
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')):
            routed = self.cli('learning-route', '--task', hard_path)
            self.assertIn('hard_end_to_end_evidence', json.dumps(routed))
            self.assertNotIn('hard_end_to_end_evidence', json.dumps(plain))
            # The same evidence cited for another task, or for another family, is refused.
            for change in ({'id': 'another-task'}, {'family': 'coding'}):
                with self.subTest(change=change):
                    other = self.cli('learning-route', '--task', self.write('other-task.json', {**hard, **change}),
                                     expected=2)
                    self.assertFalse(other['ready'])
        # Evidence this service does not hold is left to the ledger, never assumed.
        unknown = self.write('unknown-task.json', {**hard, 'routing_evidence_refs': ['outcome:' + 'f' * 64]})
        with mock.patch.object(cli, '_learning_ledger', side_effect=learning_store.LedgerError("routing evidence is not QA accepted")):
            self.assertFalse(self.cli('learning-route', '--task', unknown, expected=2)['ready'])
        # Once the project's learning lives in its own store, the ledger's frozen copy
        # cannot vouch for a run: the owner may have rejected it since.
        from codex_agents.collector_client import CollectorClient
        self.assertTrue(CollectorClient(self.state.socket, timeout=5).learning_moved_if_empty(self.project))
        with mock.patch.object(cli, '_learning_ledger', side_effect=AssertionError('ledger path')):
            self.assertFalse(self.cli('learning-route', '--task', unknown, expected=2)['ready'])
            self.assertIn('hard_end_to_end_evidence', json.dumps(self.cli('learning-route', '--task', hard_path)))

    def test_a_complete_orchestration_never_opens_the_ledger_or_pauses_the_collector(self):
        from tests.test_bridge import passing_check_report

        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')), \
                mock.patch.object(cli, '_learning_ledger', side_effect=AssertionError('ledger path')):
            self.assertTrue(self.cli('learning-status')['active'])
            worker, verifier, verifier_assignment, verifier_result = self.verified()
            with mock.patch.object(cli, 'run_operational_check_suite',
                                   side_effect=lambda _root, **_scratch: passing_check_report()):
                checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                                  '--verifier-host-outcome-digest', verifier)
            qa = self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                          '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                          '--evaluator-assignment', self.write('eval-assignment.json', json.loads(
                              Path(self.temporary.name, 'project-assignment.json').read_text())),
                          '--evaluator-result', str(Path(self.temporary.name, 'worker-result.json')),
                          '--verifier-assignment', self.write('ver-assignment.json', verifier_assignment),
                          '--verifier-result', self.write('ver-result.json', verifier_result))
            self.assertEqual('available', qa['learning_cycle']['summary_state'])
            store = sqlite3.connect('file:' + str(next(self.state.root.glob('projects/*/history.sqlite3')))
                                    + '?mode=ro', uri=True)
            self.addCleanup(store.close)
            orchestration = json.loads(store.execute(
                'SELECT content FROM project_orchestration_checkpoints ORDER BY sequence DESC LIMIT 1').fetchone()[0])['snapshot']
            handle = orchestration['orchestration_handle']
            self.assertEqual('active', self.cli('learning-orchestration-status', '--orchestration-handle', handle)[
                'current']['snapshot']['status'])
            report = self.cli('learning-orchestration-report', '--orchestration-id', orchestration['orchestration_id'])
            self.assertEqual(1, report['qa']['passed'])
            closed = self.cli('learning-close-orchestration', '--orchestration-handle', handle, '--status', 'completed')
            self.assertEqual('completed', closed['current']['snapshot']['status'])

    def test_owner_learning_commands_work_on_a_project_whose_learning_lives_in_its_own_store(self):
        from codex_agents.collector_client import CollectorClient
        from tests.learning_support import sign_owner_payload
        from tests.test_bridge import passing_check_report

        worker, verifier, verifier_assignment, verifier_result = self.verified()
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            checks = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                              '--verifier-host-outcome-digest', verifier)
        self.cli('learning-record-operational-qa', '--host-outcome-digest', worker,
                 '--verifier-host-outcome-digest', verifier, '--check-receipt', checks['check_receipt'],
                 '--evaluator-assignment', self.write('eval-assignment.json', json.loads(
                     Path(self.temporary.name, 'project-assignment.json').read_text())),
                 '--evaluator-result', str(Path(self.temporary.name, 'worker-result.json')),
                 '--verifier-assignment', self.write('ver-assignment.json', verifier_assignment),
                 '--verifier-result', self.write('ver-result.json', verifier_result))
        # Nothing to copy from the ledger: the project is simply marked as living in its own store.
        client = CollectorClient(self.state.socket, timeout=5)
        self.assertTrue(client.learning_moved_if_empty(self.project))
        self.assertEqual('complete', self.cli('learning-status')['learning_migration'])
        with mock.patch.object(learning_store.LearningLedger, '__init__', side_effect=AssertionError('ledger opened')), \
                mock.patch.object(cli, 'CollectorMaintenance', side_effect=AssertionError('collector paused')):
            cycle = self.cli('learning-run-cycle')  # An explicit cycle reports in full.
            self.assertTrue(cycle['ready'])
            self.assertEqual(([], 0), (cycle['mined_case_ids'], cycle['expired_records']))
            self.assertEqual(0, cycle['analysis']['owner_feedback_excluded'])
            analysis = self.cli('learning-analyze', '--as-of-ms', '1', '--max-age-ms', '86400000')
            self.assertEqual(set(cycle['analysis']), set(analysis['analysis']))
            mined = self.cli_for(self.project, 'learning-mine-case', '--source-outcome-id', 'x', '--task', 'missing.json',
                                 '--criteria-digest', 'a' * 64, '--lineage-digest', 'b' * 64, expected=2)
            self.assertFalse(mined['ready'])
            # The verified worker outcome and the case mined from it.
            store = sqlite3.connect('file:' + str(next(self.state.root.glob('projects/*/history.sqlite3')))
                                    + '?mode=ro', uri=True)
            self.addCleanup(store.close)
            cases = [json.loads(row[0]) for row in store.execute(
                "SELECT content FROM records WHERE record_type='evaluation_case'")]
            self.assertEqual(1, len(cases))
            outcome = json.loads(store.execute("SELECT content FROM records WHERE record_id=?",
                                               (cases[0]['source_outcome_id'],)).fetchone()[0])
            review = ('learning-review-case', '--case-id', cases[0]['record_id'], '--review-digest', 'd' * 64,
                      '--similarity-group-digest', 'e' * 64, '--destination', 'promotion_holdout', '--freeze')
            reviewed = self.cli(*review)
            self.assertEqual((True, 'promotion_holdout', True, cases[0]['record_id']), (
                reviewed['stored'], reviewed['record']['corpus_state'], reviewed['record']['frozen'],
                reviewed['record']['supersedes_case_id']))
            self.assertEqual('available', reviewed['learning_cycle']['summary_state'])
            self.assertFalse(self.cli(*review)['stored'])  # The same review again is the same record.
            # A development case enters a controlled corpus once.
            again = self.cli_for(self.project, 'learning-review-case', '--case-id', cases[0]['record_id'],
                                 '--review-digest', 'f' * 64, '--similarity-group-digest', 'e' * 64,
                                 '--destination', 'canary', expected=2)
            self.assertEqual('case_review_refused', again['reason_code'])
            self.assertIn('controlled corpus', again['error'])
            missing = self.cli_for(self.project, 'learning-review-case', '--case-id', 'no-such-case',
                                   '--review-digest', 'd' * 64, '--similarity-group-digest', 'e' * 64,
                                   '--destination', 'canary', expected=2)
            self.assertEqual('case_unknown', missing['reason_code'])
            # The owner rejects the run: signed, checked by the account owner, kept with its signature.
            feedback = ('learning-record-feedback', '--run-id', outcome['run_id'], '--label', 'rejected',
                        '--reason-code', 'incorrect', '--observed-at-ms', '5')
            unsigned = self.cli(*feedback)
            self.assertTrue(unsigned['requires_signature'])
            payload = base64.b64decode(unsigned['signing_payload_b64'])
            forged = self.write('forged.sig', 'not a signature')
            refused = self.cli_for(self.project, *feedback, '--signature', forged, expected=2)
            self.assertEqual('owner record signature is invalid', refused['error'])
            signature = sign_owner_payload(self.owner_key, payload, 'feedback-signature')
            stored = self.cli(*feedback, '--signature', str(signature))
            self.assertEqual((True, 'rejected'), (stored['stored'], stored['record']['label']))
            self.assertFalse(self.cli(*feedback, '--signature', str(signature))['stored'])
            unknown = ('learning-record-feedback', '--run-id', 'run-nobody-ran', '--label', 'rejected',
                       '--reason-code', 'incorrect', '--observed-at-ms', '5')
            unknown_signature = sign_owner_payload(
                self.owner_key, base64.b64decode(self.cli(*unknown)['signing_payload_b64']), 'unknown-signature')
            self.assertEqual('feedback_run_unknown', self.cli_for(
                self.project, *unknown, '--signature', str(unknown_signature), expected=2)['reason_code'])
            after = self.cli('learning-run-cycle')
            self.assertEqual(1, after['analysis']['owner_feedback_excluded'])
            # Statistics of the orchestration, from the project's own records.
            orchestration = json.loads(store.execute(
                'SELECT content FROM project_orchestration_checkpoints ORDER BY sequence DESC LIMIT 1').fetchone()[0])['snapshot']
            statistics = self.cli('learning-orchestration-report', '--orchestration-id', orchestration['orchestration_id'])
            self.assertEqual(('active', 2, 2, 0), (statistics['status'], statistics['runs']['opened'],
                                                   statistics['runs']['terminal'], statistics['runs']['pending']))
            self.assertEqual({'passed': 1, 'failed': 0, 'pending': 0, 'attention_required': False}, statistics['qa'])
            self.assertEqual(2, sum(statistics['by_role'].values()))
            self.assertEqual({'bridge': 2}, statistics['by_agent_type'])
            self.assertNotIn('source.txt', json.dumps(statistics))
        self.assertEqual(1, store.execute('SELECT COUNT(*) FROM project_owner_attestations').fetchone()[0])
        self.assertEqual(1, store.execute("SELECT COUNT(*) FROM records WHERE record_type='feedback'").fetchone()[0])
        self.assertTrue(self.ledger.learning_moved(self.project_id))

    def recorded_worker(self, orchestration, task_id):
        """One more Codex worker in the same orchestration, dispatched and recorded."""
        task = {**self.task(), 'id': task_id}
        task_path = self.write(task_id + '.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = {**self.assignment_for(route), 'assignment_id': task_id + '-assignment', 'task_id': task_id}
        context = ('--task', task_path, '--assignment', self.write(task_id + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(task_id + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        worker_result = {**result(), 'assignment_id': assigned['assignment_id']}
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(worker_result)))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        observation = self.write(task_id + '-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(task_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return {'digest': recorded['host_outcome_digest'], 'assignment': assigned, 'result': dispatched['result']}

    def bound_verifier(self, orchestration, task_id, worker, *, context_digest=None, expected=0):
        task = {**self.task(), 'id': task_id, 'objective': 'Independently verify the research result',
                'family': 'verification', 'verification_for_family': 'research',
                'verification_for_provider': 'codex', 'verification_for_outcome': worker['digest'],
                'owned_paths': ['source.txt']}
        task_path = self.write(task_id + '.json', task)
        route = self.cli('learning-route', '--task', task_path)
        assigned = self.assignment_for(route, assignment_id=task_id + '-assignment', task_id=task_id,
                                       objective=task['objective'], context=[context_digest or worker['digest']],
                                       owned_paths=['source.txt'], verification_required=False)
        context = ('--task', task_path, '--assignment', self.write(task_id + '-assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write(task_id + '-route.json', route)
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration, expected=expected)
        if expected:
            return opened
        verifier_result = {**result(), 'assignment_id': assigned['assignment_id']}
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(verifier_result)))
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        observation = self.write(task_id + '-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(task_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return {'digest': recorded['host_outcome_digest'], 'assignment': assigned, 'result': dispatched['result']}

    def checks(self, worker, verifier, expected=0):
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            return self.cli('learning-run-operational-checks', '--host-outcome-digest', worker['digest'],
                            '--verifier-host-outcome-digest', verifier['digest'], expected=expected)

    def record_qa(self, worker, verifier, receipt, expected=0):
        files = {'--evaluator-assignment': self.write('qa-worker-assignment.json', worker['assignment']),
                 '--evaluator-result': self.write('qa-worker-result.json', worker['result']),
                 '--verifier-assignment': self.write('qa-verifier-assignment.json', verifier['assignment']),
                 '--verifier-result': self.write('qa-verifier-result.json', verifier['result'])}
        return self.cli('learning-record-operational-qa', '--host-outcome-digest', worker['digest'],
                        '--verifier-host-outcome-digest', verifier['digest'], '--check-receipt', receipt,
                        *[item for pair in files.items() for item in pair], expected=expected)

    def test_qa_binds_the_verifier_to_the_outcome_it_was_opened_for(self):
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        worker_a = self.recorded_worker(orchestration, 'task-a')
        worker_b = self.recorded_worker(orchestration, 'task-b')
        # Bound to A, but its assignment names B: refused before anything opens.
        refused = self.bound_verifier(orchestration, 'verify-x', worker_a, context_digest=worker_b['digest'],
                                      expected=2)
        self.assertFalse(refused['ready'])
        verifier_a = self.bound_verifier(orchestration, 'verify-a', worker_a)
        # The verifier opened for A cannot stand in for B's verification.
        self.assertFalse(self.checks(worker_b, verifier_a, expected=2)['ready'])
        checks = self.checks(worker_a, verifier_a)
        self.assertTrue(checks['checks_passed'])
        # Nor can A's receipt be spent on B's QA.
        self.assertFalse(self.record_qa(worker_b, verifier_a, checks['check_receipt'], expected=2)['ready'])
        self.assertTrue(self.record_qa(worker_a, verifier_a, checks['check_receipt'])['ready'])

    def test_a_verifier_bound_to_the_wrong_provider_family_or_orchestration_is_refused(self):
        orchestration, _, digest = self.worker()
        for name, changes in {'provider': {'verification_for_provider': 'claude'},
                              'family': {'verification_for_family': 'coding'}}.items():
            with self.subTest(wrong=name):
                route, assigned, context, route_path = self.verifier(orchestration, digest, **changes)
                refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                                   '--orchestration-handle', orchestration, expected=2)
                self.assertNotEqual('admitted', refused.get('admission'))
        other = self.cli('learning-open-orchestration')['orchestration_handle']
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                           '--orchestration-handle', other, expected=2)
        self.assertNotEqual('admitted', refused.get('admission'))
        self.assertFalse([i for i in self.invocations('codex') if 'exec' in i['argv']][1:])  # Only the worker ran.

    def test_a_verifier_of_an_unknown_or_unrecorded_outcome_is_refused(self):
        orchestration, _, digest = self.worker()
        route, assigned, context, route_path = self.verifier(orchestration, 'f' * 64)
        refused = self.cli('learning-open-host-run', *context, '--route', route_path,
                           '--orchestration-handle', orchestration, expected=2)
        self.assertNotEqual('admitted', refused.get('admission'))


class HostClaudeVerificationTests(HostVerificationTests):
    """The verifier of a Codex worker is a bridged Claude child, for a Claude primary."""
    primary = 'claude'

    def admit(self, provider):
        fingerprint = super().admit(provider)
        if provider == 'codex':
            super().admit('claude')
            from tests.test_bridge import FIXTURES_ROOT
            name = 'claude_agents_verification_worker'  # The routed Claude verifier's installed agent.
            (self.bundle / f'.claude/agents/{name}.md').write_text(
                (FIXTURES_ROOT / f'claude/{name}.md').read_text().replace('\n---\n', '\nmaxTurns: 8\n---\n', 1))
        return fingerprint

    def verified(self):
        orchestration, worker_result, digest = self.worker()  # Routed to Codex: Claude is not enabled yet.
        config = self.project / '.codex/orchestration/provider-routing.json'
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({'version': 1, 'providers': {'codex': {'enabled': True},
                                      'claude': {'enabled': True}}, 'codex_primary_may_bridge_to_claude': True}))
        git(self.project, 'add', '.')
        git(self.project, 'commit', '-qm', 'Both providers')
        route, assigned, context, route_path = self.verifier(orchestration, digest)
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        self.frames('claude', [init_frame(), RATE_LIMIT, result_frame(json.dumps(
            {**result(), 'assignment_id': assigned['assignment_id']}))])
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        invocation = [i for i in self.invocations('claude') if '-p' in i['argv']][-1]
        self.assertIn('.orchestration-input/worker-result.json', invocation['files'])
        self.assertEqual(worker_result, json.loads(invocation['reserved_inputs']['worker-result.json']['content']))
        observation = self.write('verifier-observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('verifier-result.json', dispatched['result']),
                            '--observation', observation)
        return digest, recorded['host_outcome_digest'], assigned, dispatched['result']


class HostClassDriverTests(DriverHarness):
    """What depends on the host class that opened the run; each primary runs these (C13b).

    Host-side behaviour that never reads the host class is in ProjectDriverTests, for one primary."""

    def test_the_host_prepares_claims_dispatches_and_releases_a_bridged_codex_run(self):
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        self.assertEqual(('codex', 'bridge'), (route['provider'], route['dispatch']))
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write('assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        route_path = self.write('route.json', route)
        orchestration = self.cli('learning-open-orchestration')
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration['orchestration_handle'])
        self.assertEqual(('admitted', True), (opened['admission'], opened['project_host_registered']))
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        argv = [i for i in self.invocations('codex') if 'exec' in i['argv']][0]['argv']
        self.assertIn('--ignore-rules', argv)
        self.assertEqual('read-only', argv[argv.index('-s') + 1])
        self.assertEqual(1, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))
        self.assertEqual('retained', dispatched['bridge_report']['capacity_state'])

        observation = self.write('observation.json', {'run_handle': opened['run_handle'], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        made_up = {**dispatched['result'], 'summary': 'A verdict the child never gave.'}
        refused = self.cli('learning-record-host-result', *context, '--route', route_path,
                           '--result', self.write('made-up.json', made_up), '--observation', observation, expected=2)
        self.assertEqual('history_conflict', refused['reason_code'])
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        self.assertEqual(route['model'], recorded['outcome']['model'])
        # Recording lets the host finish: terminal accepted, cleanup confirmed, capacity released.
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        self.assertEqual([], [entry for entry in self.stopped_runs if len(entry) == 2 and entry[0] in
                              {'recording_open', 'claiming', 'registering', 'terminal', 'releasing'}])

    def test_a_running_child_reports_live_status_and_stops_on_cancellation(self):
        context, route_path, orchestration, run_handle = self.open_run()
        # The child starts, then goes quiet well inside every bound.
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'quiet-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)

        status = self.cli('learning-run-status', *target)
        self.assertEqual(('attempt', 'running', False), (status['kind'], status['cancellation_state'], status['durable']))
        self.assertIn(status['health'], {'starting', 'active'})
        self.assertIsNone(status['stop_reason'])
        # Another host class cannot inspect or stop this run.
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('learning-cancel-run', *target, expected=2)
        self.assertEqual('control_not_authorized', foreign['reason_code'])

        started = time.monotonic()
        cancelled = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (cancelled['stop_reason'], cancelled['stop_requested']))
        self.assertIn(cancelled['cancellation_state'], {'stop_requested', 'cleanup_confirmed'})
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # Long before the child's own 30-second sleep ends.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('cancelled', dispatched['bridge_report']['terminal_state'])
        settled = self.cli('learning-run-status', *target)
        self.assertTrue(settled['stop_requested'])
        self.assertEqual('cancel_requested', settled['stop_reason'])

    def test_a_run_whose_client_changed_after_preparation_says_so(self):
        context, route_path, _, run_handle = self.open_run()
        original = codex_preparation.CodexPreparationLane.prepared

        def changed(lane, handle):
            value = original(lane, handle)
            return dataclasses.replace(value, client_identity=('/srv/other/codex', 'f' * 64))
        with mock.patch.object(codex_preparation.CodexPreparationLane, 'prepared', autospec=True, side_effect=changed):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('run_client_changed', refused['reason_code'], refused)
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])

    def test_only_the_opening_host_class_can_prepare_and_dispatch_its_run(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('host_client_mismatch', foreign['reason_code'])
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])
        # Nothing was claimed for the refused host; the owner can still run it once.
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        self.assertEqual(1, len([i for i in self.invocations('codex') if 'exec' in i['argv']]))

    def test_an_undispatched_run_is_abandoned_only_by_its_host_class_and_never_launches(self):
        context, route_path, orchestration, run_handle = self.open_run()
        with mock.patch.dict(os.environ):
            for name in ('CLAUDECODE', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'):
                os.environ.pop(name, None)
            if self.primary == 'claude':
                os.environ['CODEX_THREAD_ID'] = 'other-primary'
            else:
                os.environ.update(CLAUDECODE='1', CLAUDE_CODE_SESSION_ID='other-primary')
            foreign = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                               '--reason', 'owner_cancelled', expected=2)
        self.assertEqual('host_client_mismatch', foreign['reason_code'])
        abandoned = self.cli('learning-classify-host-run-abandoned', '--run-handle', run_handle,
                             '--reason', 'owner_cancelled')
        self.assertEqual(('abandoned', 'never_claimed'), (abandoned['classification'], abandoned['capacity_state']))
        self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual([], [i for i in self.invocations('codex') if 'exec' in i['argv']])


class ClaudePrimaryDriverTests(HostClassDriverTests):
    """Path B: a Claude primary bridges the same Codex child through the same host."""
    primary = 'claude'


class ClaudeChildHarness(DriverHarness):
    """Path C: a Codex primary bridges a Claude child (`claude -p`) through the same host."""

    def admit(self, provider):
        # Before the services start: the account is also attested for Claude and
        # the project routes its children to Claude.
        fingerprint = super().admit(provider)
        if provider == 'codex':
            super().admit('claude')
            config = self.project / '.codex/orchestration/provider-routing.json'
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text(json.dumps({'version': 1, 'providers': {'codex': {'enabled': False},
                                          'claude': {'enabled': True}}, 'codex_primary_may_bridge_to_claude': True}))
            git(self.project, 'add', '.')
            git(self.project, 'commit', '-qm', 'Bridge Claude children')
        return fingerprint

    def open_claude_run(self):
        context, route_path, orchestration, run_handle = self.open_run()
        route = json.loads(Path(route_path).read_text())
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        return context, route_path, orchestration, run_handle

    def dispatch_claude(self, context, route_path, run_handle):
        with mock.patch.object(self, 'assertEqual', side_effect=lambda *a: unittest.TestCase.assertEqual(
                self, *a[:2], (a[2] if len(a) > 2 else '') + ' driver stopped at %r' % (self.stopped_runs,))):
            return self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle)



class ClaudeChildDriverTests(ClaudeChildHarness):
    def test_a_prohibited_tool_stops_the_claude_child_at_once(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        # The child asks for a tool outside its read-only set and would then keep going.
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Bash', 'input': {'command': 'CANARY-COMMAND'}}]}},
            {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)
        report = dispatched['bridge_report']
        self.assertNotEqual('result', report['outcome'])
        self.assertIn('claude_tool_not_allowed', json.dumps(report))
        self.assertNotIn('CANARY', json.dumps(dispatched))

    def test_reasoning_and_tool_content_never_reach_status_or_the_project_store(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [
                {'type': 'thinking', 'thinking': 'CANARY-PRIVATE-REASONING'},
                {'type': 'text', 'text': 'CANARY-PRIVATE-TEXT'},
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': 'CANARY-PRIVATE-PATH'}}]}},
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'CANARY-PRIVATE-OUTPUT'}]}},
            {'sleep': 3}, result_frame()])
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('claude') if '-p' in i['argv']], seconds=30)
        self.wait_for(lambda: self.cli('learning-run-status', *target)['health'] in {'active', 'quiet'}, seconds=20)
        status = self.cli('learning-run-status', *target)
        # Status is counts and states: bounded, and none of what the child thought, said or read.
        self.assertNotIn('CANARY', json.dumps(status))
        self.assertLess(len(json.dumps(status)), 4096)
        output, errors = process.communicate(timeout=60)
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertNotIn('CANARY', json.dumps(dispatched['bridge_report']))
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.assertNotIn('CANARY', json.dumps(recorded))
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        # Nothing the services keep on disk holds it either: the stores, their journals, the hints.
        kept = [path for path in self.state.root.rglob('*') if path.is_file() and b'CANARY' in path.read_bytes()]
        self.assertEqual([], kept)

    def test_a_paid_usage_event_stops_the_claude_child_at_once(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed'}}
        # Mid-message, with partial streaming on: the stop is still immediate.
        self.frames('claude', [init_frame(),
                               {'type': 'stream_event', 'event': {'type': 'message_start', 'message': {
                                   'id': 'msg_1', 'model': 'claude-opus-5-5'}}},
                               {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'index': 0, 'delta': {
                                   'type': 'text_delta', 'text': 'Working'}}},
                               paid, {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)
        argv = [i for i in self.invocations('claude') if '-p' in i['argv']][-1]['argv']
        self.assertIn('--include-partial-messages', argv)
        self.assertEqual(('paid_usage_observed', 'failed'),
                         (dispatched['bridge_report']['outcome'], dispatched['bridge_report']['terminal_state']))
        self.assertGreater(dispatched['bridge_report']['rate_limit_records_kept'], 0)
        # The account kept the restriction, so the next Claude child on this account is refused.
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])


class ClaudeChildHostClassTests(ClaudeChildHarness):
    """A Claude child's whole run and its status and cancellation; each primary runs these (C13c).

    What depends only on the child's own stream is in ClaudeChildDriverTests, for a Codex primary."""

    def test_the_host_runs_a_claude_child_and_its_rate_limit_reaches_the_account(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Reading.'}]}},
            RATE_LIMIT, result_frame()])
        recorders, original = [], project_driver._RateLimitRecorder.__init__

        def remember(recorder, *args):
            recorders.append(recorder)
            return original(recorder, *args)

        with mock.patch.object(project_driver._RateLimitRecorder, '__init__', remember):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
        report = dispatched['bridge_report']
        self.assertEqual(('result', 'claude', 'enforced'), (report['outcome'], report['provider'], report['read_containment']))
        self.assertEqual({'exit_code', 'result_frame_seen', 'result_subtype', 'error_code'},
                         set(report['client_diagnostics']))
        self.assertEqual((1, False), (report['rate_limit_events'], report['rate_limit_record_failed']))
        self.assertGreaterEqual(report['rate_limit_records_kept'], 1)
        argv = [i for i in self.invocations('claude') if '-p' in i['argv']]
        self.assertEqual(1, len(argv))
        self.assertEqual('Read,Glob,Grep', argv[0]['argv'][argv[0]['argv'].index('--tools') + 1])
        observation = self.write('observation.json', {'run_handle': run_handle, 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write('result.json', dispatched['result']), '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')
        self.wait_for(lambda: ('driver', 'retiring', 'released') in self.stopped_runs, seconds=30)
        # The run is finished: the account takes no further rate-limit event for it.
        with self.assertRaises(RateLimitError):
            recorders[0].record_claude_rate_limit_event(
                self.project_id, run_handle=run_handle, rate_limit_info=RATE_LIMIT['rate_limit_info'])

    def test_a_running_claude_child_reports_live_status_and_stops_on_cancellation(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        # The child starts and does some work, then goes quiet well inside every bound.
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Reading.'}]}},
            {'sleep': 30}, result_frame()])
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('claude') if '-p' in i['argv']], seconds=30)
        self.wait_for(lambda: self.cli('learning-run-status', *target)['health'] in {'active', 'quiet'}, seconds=20)
        status = self.cli('learning-run-status', *target)
        self.assertEqual(('attempt', 'running'), (status['kind'], status['cancellation_state']))
        self.assertIsNone(status['stop_reason'])
        started = time.monotonic()
        cancelled = self.cli('learning-cancel-run', *target)
        self.assertEqual(('cancel_requested', True), (cancelled['stop_reason'], cancelled['stop_requested']))
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # The child never emits another event.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        self.assertEqual('cancelled', json.loads(output)['bridge_report']['terminal_state'])
        self.assertEqual('cancel_requested', self.cli('learning-run-status', *target)['stop_reason'])


class ClaudePrimaryClaudeChildTests(ClaudeChildHostClassTests):
    """Path D: a Claude primary's Claude child is a separate `claude -p` process run by the same host."""
    primary = 'claude'


class ClaudeOnlyVerificationTests(ClaudeChildHarness):
    """With Codex disabled, a Claude worker's verifier is Claude too, which QA does not accept."""

    def admit(self, provider):
        fingerprint = super().admit(provider)
        if provider == 'codex':
            from tests.test_bridge import FIXTURES_ROOT
            name = 'claude_agents_verification_worker'
            (self.bundle / f'.claude/agents/{name}.md').write_text(
                (FIXTURES_ROOT / f'claude/{name}.md').read_text().replace('\n---\n', '\nmaxTurns: 8\n---\n', 1))
        return fingerprint

    def run_claude(self, context, route_path, orchestration, assignment_id):
        opened = self.cli('learning-open-host-run', *context, '--route', route_path,
                          '--orchestration-handle', orchestration)
        self.frames('claude', [init_frame(), RATE_LIMIT, result_frame(json.dumps(
            {**result(), 'assignment_id': assignment_id}))])
        dispatched = self.cli('dispatch', *context, '--route', route_path, '--run-handle', opened['run_handle'])
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write(assignment_id + '-observation.json', {
            'run_handle': opened['run_handle'],
            'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *context, '--route', route_path,
                            '--result', self.write(assignment_id + '-result.json', dispatched['result']),
                            '--observation', observation)
        return recorded['host_outcome_digest']

    def test_a_same_provider_claude_verifier_is_refused_by_qa(self):
        orchestration = self.cli('learning-open-orchestration')['orchestration_handle']
        task = self.write('task.json', self.task())
        route = self.cli('learning-route', '--task', task)
        self.assertEqual(('claude', 'bridge'), (route['provider'], route['dispatch']))
        assigned = self.assignment_for(route)
        context = ('--task', task, '--assignment', self.write('assignment.json', assigned),
                   '--parent-grant', self.write('grant.json', PARENT_GRANT))
        worker = self.run_claude(context, self.write('route.json', route), orchestration, assigned['assignment_id'])
        verify = {**self.task(), 'id': 'verify-1', 'objective': 'Independently verify the research result',
                  'family': 'verification', 'verification_for_family': 'research',
                  'verification_for_provider': 'claude', 'verification_for_outcome': worker,
                  'owned_paths': ['source.txt']}
        verify_path = self.write('verify-task.json', verify)
        verifier_route = self.cli('learning-route', '--task', verify_path)
        self.assertEqual(('claude', 'bridge', 'claude_agents_verification_worker'),
                         (verifier_route['provider'], verifier_route['dispatch'], verifier_route['role']))
        verifier_assigned = self.assignment_for(
            verifier_route, assignment_id='verify-assignment-1', task_id='verify-1', objective=verify['objective'],
            context=[worker], owned_paths=['source.txt'], verification_required=False)
        verifier_context = ('--task', verify_path,
                            '--assignment', self.write('verify-assignment.json', verifier_assigned),
                            '--parent-grant', self.write('grant.json', PARENT_GRANT))
        verifier = self.run_claude(verifier_context, self.write('verify-route.json', verifier_route), orchestration,
                                   'verify-assignment-1')
        from tests.test_bridge import passing_check_report
        with mock.patch.object(cli, 'run_operational_check_suite',
                               side_effect=lambda _root, **_scratch: passing_check_report()):
            refused = self.cli('learning-run-operational-checks', '--host-outcome-digest', worker,
                               '--verifier-host-outcome-digest', verifier, expected=2)
        self.assertEqual('operational QA admission rejected', refused['error'])


class ClaudeRestrictionDeliveryTests(ClaudeChildHarness):
    def test_an_undelivered_paid_event_reaches_the_account_before_the_run_finishes(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed', 'padding': 'x' * 10_000}}
        self.frames('claude', [init_frame(), paid, {'sleep': 30}, result_frame()])
        original, calls = AccountClient.record_rate_limit_event, []

        def unreachable_once(client, *args, **options):
            calls.append(options['request_id'])
            if len(calls) == 1:
                raise AccountError('account_unavailable')
            return original(client, *args, **options)

        with mock.patch.object(AccountClient, 'record_rate_limit_event', unreachable_once):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
            self.assertEqual('rate_limit_event_invalid', dispatched['bridge_report']['outcome'])
            self.wait_for(lambda: ('driver', 'reporting', 'terminal') in self.stopped_runs, seconds=30)
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0], calls[1])  # The same request, so the account keeps it once.
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])

    def held_paid_event(self, limits, *, unreachable_first):
        """A paid event that is not accepted live still reaches the account before the terminal."""
        context, route_path, orchestration, run_handle = self.open_claude_run()
        paid = {**RATE_LIMIT, 'rate_limit_info': {**RATE_LIMIT['rate_limit_info'], 'isUsingOverage': True,
                                                  'overageStatus': 'allowed'}}
        self.frames('claude', [init_frame(), paid, {'sleep': 30}, result_frame()])
        original, calls = AccountClient.record_rate_limit_event, []

        def record(client, *args, **options):
            calls.append(options['request_id'])
            if unreachable_first and len(calls) == 1:
                raise AccountError('account_unavailable')
            return original(client, *args, **options)

        with mock.patch.multiple(project_driver, **limits), \
                mock.patch.object(AccountClient, 'record_rate_limit_event', record):
            dispatched = self.dispatch_claude(context, route_path, run_handle)
            self.assertEqual('rate_limit_event_invalid', dispatched['bridge_report']['outcome'])
            self.wait_for(lambda: ('driver', 'reporting', 'terminal') in self.stopped_runs, seconds=30)
        task = self.write('next-task.json', {**self.task(), 'id': 'task-2'})
        route = self.cli('learning-route', '--task', task)
        assigned = {**self.assignment_for(route), 'assignment_id': 'next-assignment', 'task_id': 'task-2'}
        refused = self.cli('learning-open-host-run', '--task', task, '--assignment', self.write('next.json', assigned),
                           '--parent-grant', context[5], '--route', self.write('next-route.json', route),
                           '--orchestration-handle', orchestration, expected=2)
        self.assertIn('paid', refused['reason_code'])
        return calls

    def test_an_event_past_the_run_cap_is_held_and_delivered(self):
        calls = self.held_paid_event({'MAX_RUN_RATE_LIMIT_EVENTS': 0}, unreachable_first=False)
        self.assertEqual(1, len(calls))  # Not sent live; delivered once before the terminal.

    def test_events_past_the_pending_limit_are_folded_and_delivered(self):
        calls = self.held_paid_event({'MAX_PENDING_RATE_LIMITS': 0}, unreachable_first=True)
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[1].endswith('-merged'))

    def test_a_client_changed_after_the_account_observed_it_is_never_launched(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), result_frame()])
        with mock.patch.object(codex_worker, '_client_digest', return_value='0' * 64):
            refused = self.cli('dispatch', *context, '--route', route_path, '--run-handle', run_handle, expected=2)
        self.assertEqual('dispatch_unavailable', refused['reason_code'])
        self.assertFalse([i for i in self.invocations('claude') if '-p' in i['argv']])


class ClaudeSilenceCutoffTests(ClaudeChildHarness):
    liveness = LivenessLimits(5 * SECOND, SECOND, 2 * SECOND, 3 * SECOND)

    def test_the_host_stops_a_claude_child_that_goes_silent_after_init(self):
        context, route_path, orchestration, run_handle = self.open_claude_run()
        self.frames('claude', [init_frame(), {'type': 'assistant', 'message': {
            'id': 'msg_1', 'model': 'claude-opus-5-5', 'content': [{'type': 'text', 'text': 'Starting.'}]}},
            {'sleep': 30}, result_frame()])
        started = time.monotonic()
        dispatched = self.dispatch_claude(context, route_path, run_handle)
        self.assertLess(time.monotonic() - started, 20)  # Cut off by silence, not by the child's own end.
        self.assertEqual(('work_silence', 'timed_out'),
                         (dispatched['bridge_report']['outcome'], dispatched['bridge_report']['terminal_state']))
        status = self.cli('learning-run-status', '--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.assertEqual('work_silence', status['stop_reason'])


class SilenceCutoffTests(DriverHarness):
    # Trusted startup policy, shortened so the silence cutoff happens within the test.
    liveness = LivenessLimits(5 * SECOND, SECOND, 2 * SECOND, 3 * SECOND)

    def test_the_host_stops_a_silent_child_without_any_request(self):
        context, route_path, orchestration, run_handle = self.open_run()
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'silent-child'},
                              {'type': 'turn.started'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        started = time.monotonic()
        process = self.separate_dispatch(context, route_path, run_handle)
        target = ('--orchestration-handle', orchestration, '--run-handle', run_handle)
        self.wait_for(lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']], seconds=30)
        seen = set()
        while process.poll() is None and time.monotonic() - started < 25:
            status = self.cli('learning-run-status', *target)
            seen.add(status['health'])
            time.sleep(.2)
        output, errors = process.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 20)  # Cut off by silence, not by the child's own end.
        self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('timed_out', dispatched['bridge_report']['terminal_state'])
        final = self.cli('learning-run-status', *target)
        self.assertEqual('work_silence', final['stop_reason'])
        self.assertTrue({'quiet', 'stale'} & seen, seen)


ManyRunsTests.worker_once = HostVerificationTests.worker
ManyRunsTests.verifier = HostVerificationTests.verifier
ManyRunsTests.verified = HostVerificationTests.verified


class SharedPoolTests(DriverHarness):
    """0.3.3 requirement C8 end to end: three projects share the machine's pool of six."""

    def admit(self, provider):
        fingerprint = super().admit(provider)
        self.others = []
        for name in ('second', 'third'):
            root = Path(self.temporary.name) / name
            root.mkdir()
            (root / 'source.txt').write_text(name + ' marker\n')
            git(root, 'init', '-q')
            git(root, 'config', 'user.email', 'tests@example.invalid')
            git(root, 'config', 'user.name', 'Bridge Tests')
            git(root, 'add', '.')
            git(root, 'commit', '-qm', name)
            project = self.ledger.register_project(root, now_ms=1)
            identity = self.ledger.observe_provider(project, provider, project_root=self.tools, environ=CLEAN)[0]
            attest_provider(self.ledger, project, self.owner_key, provider, identity['account_fingerprint'])
            self.others.append(root)
        return fingerprint

    def test_C8_six_children_across_projects_run_at_once_and_a_seventh_is_claim_busy(self):
        second, third = self.others
        self.start_project_host(second)
        self.start_project_host(third)
        runs = [(self.project, self.open_run(self.project)) for _ in range(3)]
        runs += [(second, self.open_run(second)) for _ in range(3)]
        seventh = self.open_run(third)
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'one-of-six'}, {'sleep': 600},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        processes = [self.separate_dispatch(run[0], run[1], run[3], project) for project, run in runs]
        launched = lambda: [i for i in self.invocations('codex') if 'exec' in i['argv']]  # noqa: E731
        self.wait_for(lambda: len(launched()) == 6, seconds=120)
        for child in launched():
            os.killpg(child['pgid'], 0)  # All six are alive together.
        refused = self.cli_for(third, 'dispatch', *seventh[0], '--route', seventh[1], '--run-handle', seventh[3],
                               expected=2)
        self.assertEqual('claim_busy', refused['reason_code'], refused)
        self.assertEqual(6, len(launched()))
        for (project, run), process in zip(runs, processes):
            self.cli_for(project, 'learning-cancel-run', '--orchestration-handle', run[2], '--run-handle', run[3])
            output, errors = process.communicate(timeout=60)
            self.assertEqual(0, process.returncode, (output + errors).decode(errors='replace'))


class TwoProjectTests(DriverHarness):
    def admit(self, provider):
        # A second registered, admitted project with its own host, sharing the account.
        self.second = Path(self.temporary.name) / 'second'
        self.second.mkdir()
        (self.second / 'source.txt').write_text('second marker\n')
        git(self.second, 'init', '-q')
        git(self.second, 'config', 'user.email', 'tests@example.invalid')
        git(self.second, 'config', 'user.name', 'Bridge Tests')
        git(self.second, 'add', '.')
        git(self.second, 'commit', '-qm', 'second')
        fingerprint = super().admit(provider)
        second = self.ledger.register_project(self.second, now_ms=1)
        identity = self.ledger.observe_provider(second, provider, project_root=self.tools, environ=CLEAN)[0]
        attest_provider(self.ledger, second, self.owner_key, provider, identity['account_fingerprint'])
        return fingerprint

    def test_two_projects_run_children_at_once_and_cancelling_one_leaves_the_other(self):
        self.start_project_host(self.second)
        first = self.open_run(self.project)
        second = self.open_run(self.second)
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'overlapping-child'}, {'sleep': 30},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        processes = [self.separate_dispatch(first[0], first[1], first[3], self.project),
                     self.separate_dispatch(second[0], second[1], second[3], self.second)]
        self.wait_for(lambda: len([i for i in self.invocations('codex') if 'exec' in i['argv']]) == 2, seconds=40)
        targets = [('--orchestration-handle', run[2], '--run-handle', run[3]) for run in (first, second)]
        for project, target in zip((self.project, self.second), targets):
            status = self.cli_for(project, 'learning-run-status', *target)
            self.assertEqual('running', status['cancellation_state'])
        # Neither project can see the other's run.
        foreign = self.cli_for(self.second, 'learning-run-status', *targets[0], expected=2)
        self.assertIn(foreign['reason_code'], {'control_not_authorized', 'control_attempt_not_found', 'control_wrong_scope'})

        cancelled = self.cli_for(self.project, 'learning-cancel-run', *targets[0])
        self.assertEqual('cancel_requested', cancelled['stop_reason'])
        output, errors = processes[0].communicate(timeout=30)
        self.assertEqual(0, processes[0].returncode, (output + errors).decode(errors='replace'))
        self.assertEqual('cancelled', json.loads(output)['bridge_report']['terminal_state'])
        # The other project's child is still running, untouched.
        self.assertIsNone(processes[1].poll())
        other = self.cli_for(self.second, 'learning-run-status', *targets[1])
        self.assertEqual(('running', None), (other['cancellation_state'], other['stop_reason']))
        self.cli_for(self.second, 'learning-cancel-run', *targets[1])
        output, errors = processes[1].communicate(timeout=30)
        self.assertEqual(0, processes[1].returncode, (output + errors).decode(errors='replace'))

    def test_one_projects_service_stopping_leaves_the_others_child_answering_and_finishing(self):
        stop_second = threading.Event()
        self.start_project_host(self.second, stop_second)
        second_host = self.threads[-1]
        first = self.open_run(self.project)
        second = self.open_run(self.second)
        self.frames('codex', [{'type': 'thread.started', 'thread_id': 'stalled-child'}, {'sleep': 8},
                              {'type': 'turn.completed', 'usage': {'input_tokens': 1}}])
        (self.tools / 'codex-last-message.json').write_text(json.dumps(null_filled(result())))
        processes = [self.separate_dispatch(first[0], first[1], first[3], self.project),
                     self.separate_dispatch(second[0], second[1], second[3], self.second)]
        self.wait_for(lambda: len([i for i in self.invocations('codex') if 'exec' in i['argv']]) == 2, seconds=40)
        targets = [('--orchestration-handle', run[2], '--run-handle', run[3]) for run in (first, second)]

        def timed_status(project, target, expected=0):
            started = time.monotonic()
            report = self.cli_for(project, 'learning-run-status', *target, expected=expected)
            # Control requests answer promptly while both children stall.
            self.assertLess(time.monotonic() - started, 2.0)
            return report

        for project, target in zip((self.project, self.second), targets):
            self.assertEqual('running', timed_status(project, target)['cancellation_state'])
        # The second project's service stops while its child runs.
        stop_second.set()
        second_host.join(30)
        self.assertFalse(second_host.is_alive())
        processes[1].communicate(timeout=30)
        self.assertFalse(timed_status(self.second, targets[1], expected=2)['ready'])
        # The first project's child is untouched: it still answers, and finishes with its result.
        self.assertEqual(('running', None), tuple(timed_status(self.project, targets[0])[name]
                                                  for name in ('cancellation_state', 'stop_reason')))
        output, errors = processes[0].communicate(timeout=60)
        self.assertEqual(0, processes[0].returncode, (output + errors).decode(errors='replace'))
        dispatched = json.loads(output)
        self.assertEqual('result', dispatched['bridge_report']['outcome'])
        observation = self.write('first-observation.json', {'run_handle': first[3], 'result_attempts': [
            {'status': 'accepted', 'reason_code': None, 'receipt_id': None}]})
        recorded = self.cli('learning-record-host-result', *first[0], '--route', first[1],
                            '--result', self.write('first-result.json', dispatched['result']),
                            '--observation', observation)
        self.assertRegex(recorded['host_outcome_digest'], '^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
