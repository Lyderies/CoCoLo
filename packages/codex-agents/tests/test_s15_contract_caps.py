"""Real past-limit contract and parser stimuli for decision 382."""
from dataclasses import asdict
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from test_framework import requirements
import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_project_history import PROJECT_A, binding
from tests.test_project_monitor import scope
from tests.test_history_api import request as history_request
from tests.test_learning_contracts import usage_record
from codex_agents import (
    account_api, collector_exit, control_api, core_api, dispatch_api,
    history_api, learning_contracts, orchestration_api, result_read_api,
    routing, service_enrollment, signature_input_wire,
)
from codex_agents.service_protocol import ServiceTarget


@requirements('S15-O0C-BINDINGS', why_not_scenario='Direct product codecs, validators and real bounded directory pruning; no process or socket.')
class ContractCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def no_sockets(event, args):
            if event.startswith('socket.'):
                raise AssertionError('S15 contract boundaries must not open sockets')
        sys.addaudithook(no_sockets)

    def test_request_codecs_accept_exact_frame_and_refuse_one_byte_past(self):
        control_scope = control_api.ControlScope(**asdict(scope()))
        target = ServiceTarget(binding().orchestration_handle, binding().run_handle, binding().attempt)
        orchestration_scope = orchestration_api.OrchestrationScope(PROJECT_A, 'primary', 'a' * 64, 'supervisor', 1)
        cases = (
            (account_api, 'MAX_ACCOUNT_FRAME_BYTES', 'account', 'AccountError',
             account_api.AccountRequest(1, 'account.health', 'request', PROJECT_A, 'a' * 64, {})),
            (control_api, 'MAX_CONTROL_FRAME_BYTES', 'control', 'ControlError',
             control_api.control_request(control_scope, 'project.status', target)),
            (dispatch_api, 'MAX_DISPATCH_FRAME_BYTES', 'dispatch', 'DispatchError',
             dispatch_api.dispatch_request(control_scope, 'dispatch.submit', binding().run_handle, 1, 'a' * 64)),
            (history_api, 'MAX_FRAME_BYTES', 'history', 'HistoryError', history_request()),
            (orchestration_api, 'MAX_ORCHESTRATION_FRAME_BYTES', 'orchestration', 'HistoryError',
             orchestration_api.OrchestrationRequest(1, 'orchestration.open', 'request', orchestration_scope, {})),
            (result_read_api, 'MAX_RESULT_READ_FRAME_BYTES', 'result_read', 'HistoryError',
             result_read_api.result_read_request(binding(), 'a' * 64)),
        )
        for module, constant, prefix, error_name, request in cases:
            encode = getattr(module, 'encode_' + prefix + '_request')
            decode = getattr(module, 'decode_' + prefix + '_request')
            error = getattr(module, error_name)
            raw = encode(request)
            with self.subTest(codec=prefix), mock.patch.object(module, constant, len(raw) + 1):
                self.assertEqual(raw, encode(request))
                self.assertEqual(request, decode(raw))
                with self.assertRaises(error):
                    decode(raw + b' ')
            with self.subTest(encoder=prefix), mock.patch.object(module, constant, len(raw)):
                with self.assertRaises(error):
                    encode(request)
            self.assertEqual(request, decode(encode(request)))

    def test_clean_depth_crosses_limit_and_later_shallow_values_still_work(self):
        with mock.patch.object(core_api, 'CLEAN_DEPTH', 2):
            self.assertEqual({'a': {'b': 'kept'}}, core_api._clean({'a': {'b': 'kept'}}))
            self.assertEqual({'a': {'b': {'c': None}}}, core_api._clean({'a': {'b': {'c': 'cut'}}}))
            self.assertEqual({'a': 'kept'}, core_api._clean({'a': 'kept'}))

    def test_exit_locations_cross_reduced_limit_without_losing_valid_records(self):
        record = dict(schema_version=1, stopped_at_ms=1, started_at_ms=0,
                      cause='error', error='RuntimeError', where=['worker.py:1 run'] * 2)
        with mock.patch.object(collector_exit, 'MAX_LOCATIONS', 2):
            self.assertEqual(record, collector_exit.checked(record))
            self.assertIsNone(collector_exit.checked({**record, 'where': record['where'] + ['worker.py:2 run']}))
            self.assertEqual(record, collector_exit.checked(record))

    def test_provider_model_and_helper_arrays_cross_reduced_limits(self):
        record = usage_record()
        learning_contracts.validate_provider_usage(record)
        with mock.patch.object(learning_contracts, 'MAX_USAGE_MODELS', 1):
            with self.assertRaisesRegex(learning_contracts.ContractError, 'models must be a bounded array'):
                learning_contracts.validate_provider_usage(record)
            single = {**record, 'models': record['models'][:1]}
            self.assertEqual(single, learning_contracts.validate_provider_usage(single))
            helpers = sorted(['claude-haiku-5-5', 'claude-opus-5-5'])
            with self.assertRaisesRegex(learning_contracts.ContractError, 'helper_models'):
                learning_contracts.validate_provider_usage({**single, 'helper_models': helpers})
            self.assertEqual(single, learning_contracts.validate_provider_usage(single))

    def test_escalation_ladder_crosses_reduced_limit_before_any_selection(self):
        policy = routing.load_policy(ORCHESTRATION_ROOT / 'config' / 'routing-policy.json')
        family, ladder = next((k, v) for k, v in policy['escalation']['ladders'].items() if len(v) > 1)
        routing._validate_escalation(policy)
        with mock.patch.object(routing, 'MAX_LADDER_RUNGS', len(ladder) - 1):
            with self.assertRaisesRegex(routing.PolicyError, 'bounded non-empty array'):
                routing._validate_escalation(policy)
        routing._validate_escalation(policy)

    def test_proof_pruning_bounds_each_pass_and_drains_past_the_limit(self):
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            folder = Path(directory)
            for index in range(3):
                path = folder / str(index)
                path.write_bytes(b'proof')
                os.utime(path, (1, 1))
            with mock.patch.object(service_enrollment, 'MAX_PROOFS', 2):
                service_enrollment.prune_proofs(folder, older_than_seconds=1, now=10)
                self.assertEqual(1, len(list(folder.iterdir())))
                service_enrollment.prune_proofs(folder, older_than_seconds=1, now=10)
                self.assertEqual([], list(folder.iterdir()))

    def test_signature_writer_refuses_actual_bytes_past_each_independent_budget(self):
        wire = signature_input_wire
        limits = wire.SignatureWireLimits(2, 8)
        writer = wire._Writer(limits)
        writer.add(b'x' * limits.max_wire_bytes)
        self.assertEqual(limits.max_wire_bytes, writer.wire_bytes)
        with self.assertRaises(wire.SignatureWireError):
            writer.add(b'x')
        writer = wire._Writer(limits)
        writer.text('x' * limits.max_field_bytes)
        self.assertEqual(limits.max_field_bytes, writer.used)
        with self.assertRaises(wire.SignatureWireError):
            writer.text('x')
        budget = wire._Budget(limits)
        budget.charge(limits.max_field_bytes)
        with self.assertRaises(wire.SignatureWireError):
            budget.charge(1)
        self.assertEqual(limits.max_field_bytes, budget.used)

    def test_json_value_and_service_encoder_cross_exact_reduced_byte_limits(self):
        from tests.test_service_protocol import request
        from codex_agents import service_protocol
        raw = b'{"value":"kept"}'
        self.assertEqual({'value': 'kept'}, learning_contracts.parse_bounded_json_value(raw, max_bytes=len(raw)))
        with self.assertRaises(learning_contracts.ContractError):
            learning_contracts.parse_bounded_json_value(raw, max_bytes=len(raw) - 1)
        command = service_protocol.decode_service_request(__import__('json').dumps(request()).encode())
        raw = service_protocol.canonical_request_bytes(command)
        with mock.patch.object(service_protocol, 'MAX_CONTROL_REQUEST_BYTES', len(raw)):
            self.assertEqual(raw, service_protocol.canonical_request_bytes(command))
        with mock.patch.object(service_protocol, 'MAX_CONTROL_REQUEST_BYTES', len(raw) - 1), \
                self.assertRaisesRegex(service_protocol.ServiceProtocolError, 'request_too_large'):
            service_protocol.canonical_request_bytes(command)
        self.assertEqual(raw, service_protocol.canonical_request_bytes(command))

    def test_scalar_text_and_client_version_cross_reduced_limits(self):
        from codex_agents import providers, rate_limit_contracts
        with mock.patch.object(core_api, 'STRING_MAX_CHARS', 2):
            self.assertEqual('xx', core_api.CoreService._need_str({'value': 'xx'}, 'value'))
            with self.assertRaises(core_api.ApiError):
                core_api.CoreService._need_str({'value': 'xxx'}, 'value')
        with mock.patch.object(rate_limit_contracts, 'MAX_TEXT', 2):
            self.assertEqual('xx', rate_limit_contracts._scalar('xx'))
            self.assertIsNone(rate_limit_contracts._scalar('xxx'))
            self.assertEqual('xx', rate_limit_contracts._scalar('xx'))
        for raw in ('codex-cli 0.1.0', b'codex-cli 0.1.0'):
            with self.subTest(raw_type=type(raw).__name__), \
                    mock.patch.object(providers, 'MAX_VERSION_BYTES', len(raw)):
                self.assertEqual('0.1.0', providers.parse_client_version('codex', raw))
                self.assertIsNone(providers.parse_client_version('codex', raw + raw[:1]))
                self.assertEqual('0.1.0', providers.parse_client_version('codex', raw))
        info = {'status': 'allowed'}
        size = len(learning_contracts.canonical_json(info))
        with mock.patch.object(rate_limit_contracts, 'MAX_RATE_LIMIT_INFO_BYTES', size):
            self.assertEqual(info, rate_limit_contracts.RateLimitEventInput('hrn_' + 'a' * 48, info).rate_limit_info)
        with mock.patch.object(rate_limit_contracts, 'MAX_RATE_LIMIT_INFO_BYTES', size - 1), \
                self.assertRaises(rate_limit_contracts.RateLimitError):
            rate_limit_contracts.RateLimitEventInput('hrn_' + 'a' * 48, info)

    def test_real_settings_and_exit_files_cross_exact_byte_limits(self):
        from codex_agents import claude_host
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            folder = Path(directory)
            path = folder / 'settings.json'
            raw = b'{"value":"kept"}'
            path.write_bytes(raw)
            with mock.patch.object(claude_host, 'MAX_SETTINGS_BYTES', len(raw)):
                self.assertEqual({'value': 'kept'}, claude_host._settings(path))
                path.write_bytes(raw + b' ')
                with self.assertRaises(claude_host.ClaudeHostDenied):
                    claude_host._settings(path)
                path.write_bytes(raw)
                self.assertEqual({'value': 'kept'}, claude_host._settings(path))
            record = dict(schema_version=1, stopped_at_ms=1, started_at_ms=0,
                          cause='error', error='RuntimeError', where=['worker.py:1 run'])
            raw = learning_contracts.canonical_json(record)
            path = collector_exit.exit_path(folder)
            path.write_bytes(raw)
            path.chmod(0o600)
            with mock.patch.object(collector_exit, 'MAX_RECORD_BYTES', len(raw)):
                self.assertEqual(record, collector_exit.read_exit(folder))
                path.write_bytes(raw + b' ')
                self.assertIsNone(collector_exit.read_exit(folder))
                path.write_bytes(raw)
                self.assertEqual(record, collector_exit.read_exit(folder))

    def test_captured_receipt_bytes_cross_each_independent_limit(self):
        from codex_agents import release_contracts, terminal_delivery
        from codex_agents.account_claim_contracts import ClaimError
        for module, constant, capture in (
                (release_contracts, 'MAX_CLEANUP_EVIDENCE_BYTES', release_contracts.capture_cleanup_evidence),
                (terminal_delivery, 'MAX_TERMINAL_EVIDENCE_BYTES', terminal_delivery.capture_terminal_evidence)):
            with self.subTest(capture=capture.__name__), mock.patch.object(module, constant, 2):
                self.assertEqual(b'xx', capture(b'xx'))
                with self.assertRaises(ClaimError):
                    capture(b'xxx')
                self.assertEqual(b'xx', capture(b'xx'))

    def test_enrollment_subject_context_and_preamble_cross_reduced_limits(self):
        from codex_agents.service_enrollment import EnrollmentError
        with mock.patch.object(service_enrollment, 'MAX_SUBJECT_BYTES', 3):
            self.assertTrue(service_enrollment._subject('/xx'))
            self.assertFalse(service_enrollment._subject('/xxx'))
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            proofs = Path(directory)
            args = (b'x' * service_enrollment.KEY_BYTES, 'host', 'codex-host-v1', 1, b'{}', proofs)
            with mock.patch.object(service_enrollment, 'MAX_CONTEXT_BYTES', 2):
                raw = service_enrollment.enrollment_preamble(*args, context=b'xx', now_ms=1, nonce='a' * 32)
                self.assertEqual(1, len(list(proofs.iterdir())))
                with self.assertRaises(EnrollmentError):
                    service_enrollment.enrollment_preamble(*args, context=b'xxx', now_ms=1, nonce='b' * 32)
                self.assertEqual(1, len(list(proofs.iterdir())))
            with mock.patch.object(service_enrollment, 'MAX_PREAMBLE_BYTES', len(raw) + 1):
                accepted = service_enrollment.enrollment_preamble(*args, context=b'xx', now_ms=1, nonce='c' * 32)
                self.assertEqual(len(raw), len(accepted))
            with mock.patch.object(service_enrollment, 'MAX_PREAMBLE_BYTES', len(raw)), self.assertRaises(EnrollmentError):
                service_enrollment.enrollment_preamble(*args, context=b'xx', now_ms=1, nonce='d' * 32)

    def test_manifest_and_directory_populations_cross_reduced_count_limits(self):
        from codex_agents import host_commands
        files = {host_commands.CLI_RELATIVE.as_posix(): 'a' * 64, '.codex/one': 'a' * 64, '.codex/two': 'a' * 64}
        with mock.patch.object(host_commands, 'MAX_MANIFEST_FILES', 2):
            def raw(count):
                return learning_contracts.canonical_json(dict(format=1, version='fixture', files=dict(list(files.items())[:count])))
            self.assertEqual(2, len(host_commands._manifest_files(raw(2))))
            with self.assertRaises(host_commands.HostCommandError):
                host_commands._manifest_files(raw(3))
            self.assertEqual(2, len(host_commands._manifest_files(raw(2))))
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            for number in range(3):
                (root / str(number)).write_bytes(b'')
            self.assertEqual(['0', '1'], host_commands.unexpected_entries(root, set(), limit=2))
            (root / '0').unlink()
            (root / '1').unlink()
            self.assertEqual(['2'], host_commands.unexpected_entries(root, set(), limit=2))
            (root / '2').unlink()
            self.assertEqual([], host_commands.unexpected_entries(root, set(), limit=2))

    def test_gate_command_patch_nesting_and_pattern_cross_independent_limits(self):
        from tests.test_gate_policy import policy, bash, request, patch, add
        from codex_agents import gate_policy as gate
        pol = policy()
        for command in ('ls src', ['ls', 'src']):
            size = len(command.encode()) if type(command) is str else sum(len(w.encode()) for w in command)
            with self.subTest(command=command), mock.patch.object(gate, 'MAX_COMMAND_BYTES', size):
                self.assertEqual('allow', gate.decide(pol, bash(command)).outcome)
            with mock.patch.object(gate, 'MAX_COMMAND_BYTES', size - 1):
                self.assertEqual('command_malformed', gate.decide(pol, bash(command)).rule)
        value = patch(add('src/new.py'))
        size = len(value['tool_input']['command'].encode())
        with mock.patch.object(gate, 'MAX_PATCH_BYTES', size):
            self.assertEqual('allow', gate.decide(pol, value).outcome)
        with mock.patch.object(gate, 'MAX_PATCH_BYTES', size - 1):
            self.assertEqual('patch_malformed', gate.decide(pol, value).rule)
        with mock.patch.object(gate, 'MAX_NESTING', 1):
            self.assertEqual('allow', gate.decide(pol, bash("bash -c 'ls'")).outcome)
            for command in ('bash -c "bash -c ls"', 'uv run uv run ls'):
                self.assertEqual('command_nesting_too_deep', gate.decide(pol, bash(command)).rule)
        with mock.patch.object(gate, 'MAX_PATTERN_ALTERNATIVES', 2):
            self.assertEqual('allow', gate.decide(pol, request('Glob', pattern='src/{a,b}')).outcome)
            self.assertEqual('pattern_unreadable', gate.decide(pol, request('Glob', pattern='src/{a,b,c}')).rule)

    def test_admission_identity_billing_and_restriction_ages_cross_each_horizon(self):
        import json
        from copy import deepcopy
        from tests.test_admission_metadata import FIXTURE, calculate
        from codex_agents import admission_metadata as metadata
        cases = {c['name']: c for c in json.loads(FIXTURE.read_text())['cases']}
        for entrypoint, suffix in (('evaluate_admission', 'admitted'), ('evaluate_launch', 'launch')):
            for age in (metadata.BILLING_MAX_AGE_MS, metadata.BILLING_MAX_AGE_MS + 1):
                case = deepcopy(cases['codex-' + suffix])
                args = case['arguments']
                args['billing'][0][0]['observed_at_ms'] = args['now_ms'] - age
                actual = calculate(metadata, case)
                actual = actual['reason_code'] if entrypoint == 'evaluate_admission' else actual
                expected = ('admitted' if entrypoint == 'evaluate_admission' else None) if age == metadata.BILLING_MAX_AGE_MS else 'billing_unverified'
                with self.subTest(entrypoint=entrypoint, age=age):
                    self.assertEqual(expected, actual)
        identity = cases['codex-admitted']['arguments']['identities'][0]
        observed = identity[0]['observed_at_ms']
        self.assertEqual(identity, metadata.select_provider_identity('codex', [identity], observed + metadata.IDENTITY_MAX_AGE_MS))
        self.assertIsNone(metadata.select_provider_identity('codex', [identity], observed + metadata.IDENTITY_MAX_AGE_MS + 1))
        record = dict(provider='codex', account_fingerprint='a' * 64, observed_at_ms=observed,
                      spend_control_reached=True, paid_usage_usable=True, is_using_overage=True)
        for operation in (metadata.spend_control_digest, metadata.paid_usage_digest):
            with self.subTest(operation=operation.__name__):
                self.assertEqual('b' * 64, operation('codex', 'a' * 64, [(record, 'b' * 64)], observed + metadata.EVIDENCE_HORIZON_MS))
                self.assertIsNone(operation('codex', 'a' * 64, [(record, 'b' * 64)], observed + metadata.EVIDENCE_HORIZON_MS + 1))
                self.assertEqual('b' * 64, operation('codex', 'a' * 64, [(record, 'b' * 64)], observed - 1))

    def test_probe_record_and_result_values_cross_independent_byte_limits(self):
        from tests.learning_support import event_record, assignment
        from codex_agents import billing_probe_worker, billing_probe_contracts, result_contracts, admission_input_decode, bridge
        from tests.test_owner_key_contracts import public_value
        def boundary(module, constant, operation, error, size=None):
            expected = operation()
            size = len(expected if type(expected) is bytes else learning_contracts.canonical_json(expected)) if size is None else size
            with mock.patch.object(module, constant, size):
                self.assertEqual(expected, operation())
            with mock.patch.object(module, constant, size - 1), self.assertRaises(error):
                operation()
            self.assertEqual(expected, operation())
        operation = lambda: billing_probe_worker.probe_payload(PROJECT_A, 'request', '/tmp/project', {}, deadline_ns=1000, created_at_ms=1)
        boundary(billing_probe_worker, 'MAX_PROBE_INPUT_BYTES', operation, billing_probe_worker.ProbeWorkerError)
        operation = lambda: billing_probe_worker._normalized([], None, {'account_fingerprint': 'a'*64}, 1)
        boundary(billing_probe_worker, 'MAX_PROBE_PACKET_BYTES', operation, billing_probe_worker.ProbeWorkerError, len(operation()[0]))
        reply = dict(probe='denied', reason_code='provider_identity_missing', records=[], known=False, observed_at_ms=1)
        boundary(billing_probe_contracts, 'MAX_BILLING_REPLY_BYTES',
                 lambda: billing_probe_contracts.checked_billing_probe_reply(reply), billing_probe_contracts.BillingProbeError)
        record = event_record()
        boundary(learning_contracts, 'MAX_RECORD_BYTES', lambda: learning_contracts.validate_learning_record(record), learning_contracts.ContractError)
        boundary(result_contracts, 'MAX_ASSIGNMENT_CANONICAL_BYTES',
                 lambda: result_contracts.assignment_bytes(assignment(), binding()), history_api.HistoryError)
        text = '{"summary":"kept"}'
        with mock.patch.object(bridge, 'MAX_RESULT_BYTES', len(text.encode())):
            self.assertEqual({'summary': 'kept'}, bridge._result_object(text))
            self.assertIsNone(bridge._result_object(text + ' '))
            self.assertEqual({'summary': 'kept'}, bridge._result_object(text))
        signer = ('codex-agents-owner ' + public_value('ssh-ed25519', bytes(32)) + '\n').encode()
        with mock.patch.object(admission_input_decode, 'OWNER_SIGNERS_MAX_BYTES', len(signer)):
            self.assertIsNone(admission_input_decode._signer(signer))
            with self.assertRaises(admission_input_decode.AdmissionDecodeError):
                admission_input_decode._signer(signer + b'x')

    def test_original_wire_peeks_and_proxy_head_cross_actual_byte_limits(self):
        import io
        from codex_agents import account_ingress, history_service, egress_proxy
        from codex_agents.account_api import AccountError
        raw = b'{"operation":"history.health"}'
        with mock.patch.object(account_ingress, 'MAX_ACCOUNT_FRAME_BYTES', len(raw) + 1):
            self.assertEqual(raw, account_ingress.AccountIngress._wire(raw))
            with self.assertRaises(AccountError):
                account_ingress.AccountIngress._wire(raw + b' ')
        with mock.patch.object(history_service, 'MAX_FRAME_BYTES', len(raw) + 1):
            self.assertEqual('history.health', history_service.HistoryService._wire_operation(raw))
            self.assertIsNone(history_service.HistoryService._wire_operation(raw + b' '))
        class Connection:
            def __init__(self, raw):
                self.input = io.BytesIO(raw)
            def recv(self, size):
                return self.input.read(size)
        head = b'GET / HTTP/1.1\r\nHost: localhost'
        with mock.patch.object(egress_proxy, 'MAX_HEAD_BYTES', len(head)):
            self.assertEqual((head, b'body'), egress_proxy._read_head(Connection(head + b'\r\n\r\nbody')))
            self.assertIsNone(egress_proxy._read_head(Connection(head + b'x\r\n\r\nbody'))[0])
            self.assertEqual((head, b''), egress_proxy._read_head(Connection(head + b'\r\n\r\n')))

    def test_managed_project_and_legacy_signer_files_cross_byte_limits(self):
        import hashlib
        from codex_agents import learning_engine, learning_store, hook_config
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            (root / '.codex').mkdir()
            path = root / '.codex' / 'input'
            path.write_bytes(b'xx')
            readers = (lambda limit: learning_engine._read_managed_bytes(root, '.codex/input', max_bytes=limit),
                       lambda limit: routing._read_project_regular(root, '.codex/input', max_bytes=limit))
            for read in readers:
                self.assertEqual(b'xx', read(2))
                with self.assertRaises((learning_contracts.ContractError, routing.PolicyError)):
                    read(1)
                self.assertEqual(b'xx', read(2))
            signer = root / 'owner-allowed-signers'
            signer.write_bytes(b'xx')
            with mock.patch.object(learning_store, 'LEGACY_SIGNERS_READ_LIMIT', 2):
                self.assertEqual(hashlib.sha256(b'xx').hexdigest(), learning_store.legacy_signers_digest(root))
                signer.write_bytes(b'xxx')
                with self.assertRaises(learning_store.StateError):
                    learning_store.legacy_signers_digest(root)
            raw = hook_config.render_local_manifest(Path('/usr/local/bin/python3'), b'{}')
            with mock.patch.object(hook_config, 'MAX_LOCAL_BYTES', len(raw)):
                self.assertEqual(Path('/usr/local/bin/python3'), hook_config.parse_local_manifest(raw)[0])
                with self.assertRaises(hook_config.HookConfigError):
                    hook_config.parse_local_manifest(raw + b' ')

    def test_launch_journal_file_marker_and_rotation_cross_byte_limits(self):
        import json
        from codex_agents import launch_journal, process_tree, egress_proxy
        from tests.test_launch_journal import claimed, BRIDGE
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            journal = launch_journal.LaunchJournal(root / 'journal')
            self.addCleanup(journal.release)
            claimed(journal)
            path = journal.directory / (BRIDGE + '.json')
            raw = path.read_bytes()
            entry = launch_journal._read_entry(path)
            with mock.patch.object(launch_journal, 'MAX_ENTRY_BYTES', len(raw)):
                self.assertIsNotNone(launch_journal._read_entry(path))
                journal._write(BRIDGE, entry)
            with mock.patch.object(launch_journal, 'MAX_ENTRY_BYTES', len(raw) - 1):
                self.assertIsNone(launch_journal._read_entry(path))
                with self.assertRaises(launch_journal.JournalError):
                    journal._write(BRIDGE, entry)
            self.assertEqual(raw, path.read_bytes())
            marker = root / 'marker'
            process_tree.write_cleanup_marker(str(marker), BRIDGE, 123, 'birth')
            value = {'bridge_run_id': BRIDGE, 'child': {'pid': 123, 'birth': 'birth'}}
            size = marker.stat().st_size
            with mock.patch.object(launch_journal, 'MAX_MARKER_BYTES', size):
                self.assertTrue(launch_journal._confirmed(value, str(marker)))
                marker.write_bytes(marker.read_bytes() + b' ')
                self.assertFalse(launch_journal._confirmed(value, str(marker)))
            record = {'run': 'fixture', 'decision': 'kept', 'padding': 'x' * 1024}
            size = len((json.dumps(record, sort_keys=True, ensure_ascii=True) + '\n').encode('ascii'))
            writer = egress_proxy.RecordLog(str(root), max_bytes=size * 2)
            writer.write(record)
            writer.write(record)
            self.assertEqual(size * 2, Path(writer.path).stat().st_size)
            writer.write(record)
            self.assertEqual(size, Path(writer.path).stat().st_size)
            self.assertEqual(size * 2, Path(writer.path + '.1').stat().st_size)

    def test_driver_intake_and_dispatch_token_populations_cross_their_limits(self):
        import io
        from types import SimpleNamespace
        from codex_agents import project_driver
        from codex_agents.project_driver import RunDriver, _Run
        class Peer:
            def __init__(self, raw):
                self.input, self.closed = io.BytesIO(raw), False
            def recv(self, size):
                return self.input.read(size)
            def setblocking(self, value):
                pass
            def close(self):
                self.closed = True
        driver = object.__new__(RunDriver)
        driver._intake, driver._clock = [], lambda: 0
        driver._request = mock.Mock()
        enrolled = SimpleNamespace(role='host')
        with mock.patch.object(project_driver, 'MAX_INTAKE', 2):
            first, second, third = Peer(b'{}\n'), Peer(b'{} \n'), Peer(b'{}\n')
            driver.handle(first, enrolled)
            driver.handle(second, enrolled)
            driver.handle(third, enrolled)
            self.assertTrue(third.closed)
            self.assertEqual(2, len(driver._intake))
        with mock.patch.object(project_driver, 'MAX_REQUEST_BYTES', 3):
            driver._read_intake()
        driver._request.assert_called_once_with(first, enrolled, b'{}\n')
        self.assertTrue(second.closed)
        self.assertEqual([], driver._intake)
        driver.handle(Peer(b'{}\n'), enrolled)
        self.assertEqual(1, len(driver._intake))
        driver._scope = orchestration_api.OrchestrationScope(PROJECT_A, 'primary', 'a'*64, 'supervisor', 1)
        target = binding()
        run = _Run((target.run_handle, target.attempt), driver._scope, 'codex-host-v1', None, target.run_id, 'a'*64)
        run.state, run.context = 'ready', object()  # Token retention only; grants no dispatch authority.
        driver._runs = {run.key: run}
        submit = dispatch_api.dispatch_request(core_scope := driver._control_scope(), 'dispatch.submit',
                                              target.run_handle, target.attempt, 'a'*64)
        status = dispatch_api.dispatch_request(core_scope, 'dispatch.status', target.run_handle, target.attempt, 'a'*64)
        current = [submit]
        driver._enrolled = lambda token: SimpleNamespace(role='host', host_client='codex-host-v1',
            request_digest=dispatch_api.dispatch_request_digest(current[0]))
        pinned = object()
        self.assertIsNotNone(driver._authorize_dispatch(submit, pinned))
        current[0] = status
        tokens = [object() for _ in range(257)]
        for token in tokens:
            self.assertIsNotNone(driver._authorize_dispatch(status, token))
        self.assertEqual(256, len(run.dispatch_tokens))
        self.assertNotIn(tokens[0], run.dispatch_tokens)
        self.assertIn(tokens[-1], run.dispatch_tokens)
        self.assertEqual((pinned, dispatch_api.dispatch_request_digest(submit)), run.submit_token)
        driver._runs.clear()
        self.assertIsNone(driver._authorize_dispatch(status, tokens[-1]))

    def test_collector_binding_population_and_request_bytes_cross_real_limits(self):
        import io
        from types import SimpleNamespace
        from codex_agents import learning_store
        server = object.__new__(learning_store.CollectorServer)
        server._bindings = {}
        server._clients = {'fixture-v1': {'producers': ['app_server'], 'app_server_coverage': 'full'}}
        server.ledger = SimpleNamespace(bind_session=lambda *args: (PROJECT_A, 'a'*64, 'root-run'))
        request = dict(protocol=learning_store.COLLECTOR_PROTOCOL_VERSION, op='bind', project_root='/tmp/project',
                       session_id='fixture', producer='app_server', client_version='fixture-v1',
                       runtime_digest=learning_store.COLLECTOR_RUNTIME_DIGEST)
        with mock.patch.object(learning_store.time, 'time_ns', return_value=1_000_000):
            for _ in range(4096):
                self.assertTrue(server._dispatch(request)['ok'])
            self.assertEqual('binding_capacity', server._dispatch(request)['code'])
        self.assertEqual(4096, len(server._bindings))
        with mock.patch.object(learning_store.time, 'time_ns', return_value=301_001_000_000):
            self.assertTrue(server._dispatch(request)['ok'])
        self.assertEqual(1, len(server._bindings))
        class Peer:
            def __init__(self, raw):
                self.input = io.BytesIO(raw)
            def settimeout(self, timeout):
                pass
            def recv(self, size):
                return self.input.read(size)
        with mock.patch.object(learning_store, 'MAX_REQUEST_BYTES', 3):
            self.assertEqual('request_oversized', server._handle(Peer(b'{} \n'))['code'])
            self.assertEqual('protocol_mismatch', server._handle(Peer(b'{}\n'))['code'])

    def test_enrollment_consumer_verifier_and_frontend_cross_byte_limits_without_sockets(self):
        import json
        from types import SimpleNamespace
        from codex_agents import service_host
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            proofs = Path(directory) / 'proofs'
            service_enrollment.prepare_proof_directory(proofs)
            key = b'k' * 32
            def line(nonce, context):
                return service_enrollment.enrollment_preamble(key, 'host', 'codex-host-v1', 1, b'{}', proofs,
                    context=context, now_ms=1000, nonce=nonce * 32)
            exact, over = line('a', b'xx'), line('b', b'xxx')
            with mock.patch.object(service_enrollment, 'MAX_CONTEXT_BYTES', 2):
                self.assertEqual(b'xx', service_enrollment._consume_proof(proofs, json.loads(exact)))
                with self.assertRaises(service_enrollment.EnrollmentError):
                    service_enrollment._consume_proof(proofs, json.loads(over))
            valid = line('c', b'xx')
            verifier = service_enrollment.EnrollmentVerifier(key, 'host', 1, proofs, clock_ms=lambda: 1000)
            with mock.patch.object(service_enrollment, 'MAX_PREAMBLE_BYTES', len(valid)):
                with self.assertRaises(service_enrollment.EnrollmentError):
                    verifier.verify(valid)
                self.assertTrue((proofs / ('c' * 32)).exists())
            with mock.patch.object(service_enrollment, 'MAX_PREAMBLE_BYTES', len(valid) + 1):
                self.assertEqual(b'xx', verifier.verify(valid).context)
            frontend = service_host.EnrolledFrontend(verifier,
                service_host.ServiceHostLimits(2, 1000000, 2, 1000000, 30000, 2), lambda *args: self.fail('oversized input attached'), proofs, clock_ns=lambda: 0)
            self.addCleanup(frontend.close)
            class Peer:
                def recv(self, size, *flags):
                    return b'x' * size
            pending = service_host._Pending(Peer(), 'project', 1000000000)
            with mock.patch.object(service_host, 'MAX_PREAMBLE_BYTES', 3):
                self.assertTrue(frontend._enroll(pending))
            self.assertEqual('service_refused_enrollment', pending.refusal)
            with mock.patch.object(service_host, 'MAX_SOCKET_PATH_BYTES', 3), self.assertRaises(history_api.HistoryError):
                frontend.listen(Path('/tmp/too-long.sock'), 'project')

    def test_pipe_response_proc_reader_and_signature_payload_cross_byte_limits(self):
        from types import SimpleNamespace
        from codex_agents import providers, process_tree, signature_payloads
        from tests import test_signature_payloads
        raw = b'{"id":1,"result":{}}\n'
        def response(limit):
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, raw)
            finally:
                os.close(write_fd)
            with os.fdopen(read_fd, 'rb') as stream, mock.patch.object(providers, 'MAX_OBSERVATION_BYTES', limit):
                session = object.__new__(providers.AppServerSession)
                session._process = SimpleNamespace(stdout=stream)
                session._timeout, session._buffer, session._received = 1, b'', 0
                return session._response(1)
        self.assertEqual({'id': 1, 'result': {}}, response(len(raw)))
        with self.assertRaisesRegex(learning_contracts.ContractError, 'output is too large'):
            response(len(raw) - 1)
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            path = Path(directory) / 'environ'
            path.write_bytes(b'A=')
            with mock.patch.object(process_tree, 'MAX_ENVIRONMENT_BYTES', 2), \
                    mock.patch('builtins.open', side_effect=lambda *args, **kwargs: path.open('rb')):
                self.assertEqual([b'A='], process_tree._linux_environment(123))
                path.write_bytes(b'A=x')
                self.assertIsNone(process_tree._linux_environment(123))
        case = test_signature_payloads.SignaturePayloadTests('runTest')
        case.setUp()
        with mock.patch.object(signature_payloads, 'MAX_SIGNATURE_PAYLOAD_BYTES', len(case.payload)):
            self.assertIsNone(signature_payloads._check_payload(case.payload, case.project))
        with mock.patch.object(signature_payloads, 'MAX_SIGNATURE_PAYLOAD_BYTES', len(case.payload) - 1), \
                self.assertRaises(signature_payloads.SignaturePayloadError):
            signature_payloads._check_payload(case.payload, case.project)

    def test_runtime_library_identity_and_snapshot_cross_actual_byte_budgets(self):
        import hashlib
        from codex_agents import learning_engine
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            cellar = root / 'Cellar'
            library = cellar / 'fixture' / '1' / 'lib' / 'libfixture.dylib'
            library.parent.mkdir(parents=True)
            library.write_bytes(b'xx')
            configured = [{'path': str(library.resolve()), 'sha256': hashlib.sha256(b'xx').hexdigest()}]
            with mock.patch.object(learning_engine, 'HOMEBREW_CELLAR_ROOT', cellar.resolve()), \
                    mock.patch.object(learning_engine, 'MAX_RUNTIME_LIBRARY_BYTES', 2):
                identities = learning_engine._runtime_library_identities(configured, driver='python-wrapper', interpreter={})
            with mock.patch.object(learning_engine, 'HOMEBREW_CELLAR_ROOT', cellar.resolve()), \
                    mock.patch.object(learning_engine, 'MAX_RUNTIME_LIBRARY_BYTES', 1), self.assertRaises(learning_contracts.ContractError):
                learning_engine._runtime_library_identities(configured, driver='python-wrapper', interpreter={})
            exact, over = root / 'exact', root / 'over'
            exact.mkdir()
            over.mkdir()
            with mock.patch.object(learning_engine, 'MAX_RUNTIME_LIBRARY_BYTES', 2):
                learning_engine._snapshot_runtime_libraries(exact, identities)
            self.assertEqual(b'xx', (exact / 'runtime-libraries' / library.name).read_bytes())
            with mock.patch.object(learning_engine, 'MAX_RUNTIME_LIBRARY_BYTES', 1), self.assertRaises(learning_contracts.ContractError):
                learning_engine._snapshot_runtime_libraries(over, identities)

    def test_probe_event_counter_and_earlier_fence_refusals_reach_their_limits(self):
        from concurrent.futures import Future
        from types import SimpleNamespace
        from tests import test_bridge as fixtures
        from codex_agents import bridge, project_driver
        from codex_agents.account_api import AccountError
        offered = []
        def child(*args, on_frame, **kwargs):
            for frame in (fixtures.PROBE_INIT, *([fixtures.FAVOURABLE] * 3)):
                offered.append(frame)
                try:
                    on_frame(frame)
                except bridge._Abort as stopped:
                    return {'abort': stopped.reason, 'returncode': 0}
            self.fail('event limit did not stop input')
        with mock.patch.object(bridge, 'MAX_PROBE_EVENTS', 2), mock.patch.object(bridge, 'run_child', side_effect=child):
            infos, reason = bridge._run_claude_billing_probe([], environment={}, cwd=Path('/tmp'), deadline_seconds=1)
        self.assertEqual('too_many_rate_limit_events', reason)
        self.assertEqual(2, len(infos))
        self.assertEqual(3, len(offered))  # init and two events; a third event is never requested
        lane = mock.Mock()
        driver = object.__new__(project_driver.RunDriver)
        now = [0.0]
        driver._clock, driver._runtime = lambda: now[0], SimpleNamespace(persistence=lane)
        driver._earlier_listed, driver._intent_fences, driver._intent_asked = True, {}, set()
        driver._earlier_admitted, driver._fenced = {}, {}
        entry = dict(binding=SimpleNamespace(run_handle='hrn_'+'a'*48), evidence=b'fixture', host_client='codex-host-v1',
                     future=None, closure=None, settling=None, refusals=0, outages=0, at=0.0)
        driver._earlier_fences = {'request': entry}
        with mock.patch.object(project_driver, 'MAX_FENCE_ATTEMPTS', 2):
            for attempt in (1, 2):
                future = Future()
                future.set_exception(AccountError('account_invalid_request'))
                entry['future'] = future
                now[0] = entry['at']
                driver._fence_earlier()
                self.assertEqual(attempt, entry['refusals'])
                self.assertEqual(attempt < 2, 'request' in driver._earlier_fences)
            driver._fence_earlier()
        lane.settle_earlier_lease.assert_not_called()  # refusal never fabricates release

    def test_client_result_body_and_worker_export_cross_byte_limits_before_io(self):
        from codex_agents import bridge, client_runtime
        from codex_agents.history_contracts import HistoryError
        value = {'fixture': 'actual result bytes'}
        size = len(learning_contracts.canonical_json(value))
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            for name in ('exact', 'over'):
                (root / name).mkdir()
            with mock.patch.object(bridge, 'MAX_RESULT_BODY_BYTES', size):
                relative = bridge.place_worker_result(root / 'exact', value)
                self.assertEqual(learning_contracts.canonical_json(value), (root / 'exact' / relative).read_bytes())
            with mock.patch.object(bridge, 'MAX_RESULT_BODY_BYTES', size - 1), self.assertRaises(bridge.BridgeRefused):
                bridge.place_worker_result(root / 'over', value)
            self.assertEqual([], list((root / 'over').iterdir()))
        with mock.patch.object(client_runtime, 'MAX_RESULT_BODY_BYTES', size - 1), \
                mock.patch.object(client_runtime, 'read_runtime_hint', side_effect=AssertionError('oversized result opened endpoint')):
            with self.assertRaisesRegex(client_runtime.RuntimeResultError, 'history_invalid_input'):
                client_runtime.record_codex_result_client('/tmp/state', '/tmp/project', {},
                    {'provider': 'codex', 'dispatch': 'bridge'}, {}, {}, value,
                    {'result_attempts': [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]}, host_client='codex-host-v1')
