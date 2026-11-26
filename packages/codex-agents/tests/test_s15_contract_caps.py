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
