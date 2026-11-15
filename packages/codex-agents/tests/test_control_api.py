from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_liveness_clock import FakeClock, SECOND
from tests.test_project_monitor import SequencedClock, binding, limits, scope

from codex_agents.control_api import (
    MAX_CONTROL_FRAME_BYTES, ControlClient, ControlError, ControlScope,
    control_request, decode_control_request, decode_control_response, encode_control_request,
)
from codex_agents.control_service import ControlAuthorization, ControlService
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_monitor import ProjectMonitor
from codex_agents.service_protocol import ServiceTarget, service_request_digest
from codex_agents.service_transport import ServiceTransportError

CONTEXT = object()  # A test-only native adapter marker, not authority from JSON.


class ControlApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.clock = FakeClock()
        self.monitor = ProjectMonitor(scope(), limits(), max_tracked_attempts=4, clock_ns=self.clock)
        self.monitor.register(binding(), hard_deadline_ns=100 * SECOND)
        self.scope = ControlScope(**asdict(scope()))
        self.publication_clock = FakeClock()
        self.service = self.new_service()
        self.client = ControlClient(self.base / "unused.sock", self.scope, timeout=1, clock_ns=self.publication_clock)

    def authorize(self, request, context):
        if context is not CONTEXT:
            return None
        return ControlAuthorization(self.scope, request.target, service_request_digest(request),
                                    frozenset({"project.status", "project.cancel"}))

    def new_service(self, **changes):
        options = dict(authorizer=self.authorize, clock_ns=self.publication_clock)
        options.update(changes)
        service = ControlService(self.monitor, **options)
        self.addCleanup(service.close)
        return service

    def target(self, number=1, *, orchestration=None):
        return ServiceTarget(orchestration or binding().orchestration_handle, binding(number).run_handle, 1)

    def request(self, operation="project.status", target=None, scope_value=None):
        return control_request(scope_value or self.scope, operation, target or self.target())

    def call(self, request=None, *, service=None, context=CONTEXT):
        request = request or self.request()
        raw = (service or self.service).handle(encode_control_request(request), context)
        return decode_control_response(raw, request)

    def wire(self, path, raw, **options):
        self.assertEqual(MAX_CONTROL_FRAME_BYTES, options["request_limit"])
        self.assertEqual(MAX_CONTROL_FRAME_BYTES, options["response_limit"])
        return self.service.handle(raw, CONTEXT)

    def test_status_covers_activity_quiet_stale_and_recovery_without_refreshing_limits(self):
        self.assertEqual("starting", self.call().result["health"])
        self.monitor.startup(binding())
        self.assertEqual("active", self.call().result["health"])
        self.clock.at(2)
        self.assertEqual("quiet", self.call().result["health"])
        self.clock.at(5)
        self.assertEqual("stale", self.call().result["health"])
        self.monitor.activity(binding())
        view = self.call()
        self.assertEqual("active", view.result["health"])
        self.assertEqual(95_000, view.result["liveness"]["hard_deadline_remaining_ms"])
        self.assertFalse(view.result["durable"])
        self.clock.at(15)
        self.assertEqual("work_silence", self.call().result["stop_reason"])

    def test_cancel_is_only_requested_until_actual_owner_cleanup_observation(self):
        first = self.call(self.request("project.cancel"))
        self.assertEqual("stop_requested", first.result["cancellation_state"])
        self.assertEqual("cancel_requested", first.result["stop_reason"])
        self.assertTrue(first.result["liveness_frozen"])
        self.assertFalse(first.result["cleanup_confirmed"])
        self.assertFalse(first.result["terminal_observed"])
        self.clock.at(50)
        self.assertEqual(first.result, self.call(self.request("project.cancel")).result)
        self.monitor.observe_terminal(binding())
        pending = self.call().result
        self.assertTrue(pending["terminal_observed"])
        self.assertEqual("stop_requested", pending["cancellation_state"])
        self.monitor.observe_cleanup(binding())
        final = self.call().result
        self.assertEqual("cleanup_confirmed", final["cancellation_state"])
        self.assertEqual("cleaned", final["health"])
        self.assertIsNone(final["liveness"])
        self.assertFalse(final["durable"])

    def test_prior_deadline_or_terminal_wins_a_later_cancel(self):
        self.clock.at(18)
        timed = self.call(self.request("project.cancel")).result
        self.assertEqual("startup_silence", timed["stop_reason"])
        self.assertFalse(timed["terminal_observed"])
        self.monitor.register(binding(2), hard_deadline_ns=100 * SECOND)
        self.monitor.observe_terminal(binding(2))
        terminal = self.call(self.request("project.cancel", self.target(2))).result
        self.assertEqual("terminal_observed", terminal["cancellation_state"])
        self.assertIsNone(terminal["stop_reason"])
        self.assertFalse(terminal["cleanup_confirmed"])

    def test_summary_is_bounded_counts_for_only_the_authorized_orchestration(self):
        self.monitor.register(binding(2), hard_deadline_ns=100 * SECOND)
        other = replace(binding(3), orchestration_handle="orc_" + "b" * 48)
        self.monitor.register(other, hard_deadline_ns=100 * SECOND)
        self.monitor.startup(binding())
        self.call(self.request("project.cancel", self.target(2)))
        summary = self.call(self.request(target=ServiceTarget(binding().orchestration_handle, None, None))).result
        self.assertEqual(2, summary["tracked_attempts"])
        self.assertEqual(1, summary["health_counts"]["active"])
        self.assertEqual(1, summary["health_counts"]["stopping"])
        self.assertEqual(1, summary["stop_requested"])
        self.assertFalse(summary["durable"])
        self.assertNotIn("attempts", summary)

    def test_default_denial_and_all_scope_mismatches_precede_monitor_work(self):
        before = self.clock.calls
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.call(service=self.new_service(authorizer=None))
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.call(context={"role": "primary", "authorized": True})
        for field, value in asdict(self.scope).items():
            changed = (value + 1 if type(value) is int else "prj_" + "b" * 32 if field == "project_id"
                       else "b" * 64 if field == "runtime_digest" else "other")
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "control_not_authorized"):
                self.call(self.request(scope_value=replace(self.scope, **{field: changed})))
        self.assertEqual(before, self.clock.calls)

    def test_unknown_or_wrong_orchestration_target_cannot_touch_a_registered_attempt(self):
        before = self.clock.calls
        with self.assertRaisesRegex(ControlError, "control_attempt_not_found"):
            self.call(self.request("project.cancel", self.target(2)))
        with self.assertRaisesRegex(ControlError, "control_wrong_scope"):
            self.call(self.request("project.cancel", self.target(orchestration="orc_" + "b" * 48)))
        self.assertEqual(before, self.clock.calls)
        self.assertFalse(self.monitor.status(binding()).stop_requested)

    def test_authorizer_closure_cannot_cancel_after_the_facade_is_closed(self):
        def closing(request, context):
            authorization = self.authorize(request, context)
            self.service.close()
            return authorization
        self.service._authorizer = closing
        with self.assertRaisesRegex(ControlError, 'control_unavailable'):
            self.call(self.request('project.cancel'))
        self.assertFalse(self.monitor.status(binding()).stop_requested)

    def test_closure_during_publication_withholds_reply_without_undoing_accepted_cancel(self):
        def closing_clock():
            self.service.close()
            return self.publication_clock()
        self.service._clock = closing_clock
        with self.assertRaisesRegex(ControlError, 'control_unavailable'):
            self.call(self.request('project.cancel'))
        self.assertTrue(self.monitor.status(binding()).stop_requested)

    def test_revocation_after_encoding_withholds_ready_status(self):
        calls = []
        def revoking(request, context):
            calls.append(request.operation)
            return self.authorize(request, context) if len(calls) == 1 else None
        self.service._authorizer = revoking
        with self.assertRaisesRegex(ControlError, 'control_not_authorized'):
            self.call()
        self.assertEqual(['project.status', 'project.status'], calls)

    def test_allowed_operations_and_full_authorization_binding_are_checked(self):
        before = self.clock.calls
        def status_only(request, context):
            return replace(self.authorize(request, context), allowed_operations=frozenset({"project.status"}))
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.call(self.request("project.cancel"), service=self.new_service(authorizer=status_only))
        for field, value in (("request_digest", "b" * 64), ("target", self.target(2)),
                              ("scope", replace(self.scope, supervisor_generation=2))):
            def wrong(request, context):
                return replace(self.authorize(request, context), **{field: value})
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "control_not_authorized"):
                self.call(service=self.new_service(authorizer=wrong))
        self.assertEqual(before, self.clock.calls)

    def test_mutating_authorizer_does_not_retarget_or_expose_a_changed_scope(self):
        def mutate(request, context):
            authorization = self.authorize(request, context)
            object.__setattr__(request.target, "run_handle", binding(2).run_handle)
            return authorization
        before = self.clock.calls
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.call(service=self.new_service(authorizer=mutate))
        self.assertEqual(before, self.clock.calls)

    def test_request_identity_binds_every_parameter_without_alias_storage(self):
        request = self.request("project.cancel")
        valid = json.loads(encode_control_request(request))
        for field in ("project_id", "primary_session_id", "supervisor_instance_id", "supervisor_generation", "runtime_digest"):
            changed = valid.copy()
            changed[field] = (2 if field == "supervisor_generation" else "prj_" + "b" * 32 if field == "project_id"
                              else "b" * 64 if field == "runtime_digest" else "other")
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "control_request_conflict"):
                decode_control_request(canonical_json(changed))
        value = {**valid, "target": {**valid["target"], "run_handle": binding(2).run_handle}}
        before = self.clock.calls
        raw = self.service.handle(canonical_json(value), CONTEXT)
        self.assertEqual("control_request_conflict", json.loads(raw)["reason_code"])
        self.assertEqual(before, self.clock.calls)
        self.assertEqual(request.request_id, self.request("project.cancel").request_id)
        self.assertNotEqual(request.request_id, self.request().request_id)
        self.assertNotEqual(request.request_id, self.request("project.cancel", self.target(2)).request_id)

    def test_only_status_cancel_and_closed_metadata_are_accepted(self):
        valid = json.loads(encode_control_request(self.request()))
        for field, value in (("operation", "project.launch"), ("operation", "execute_sql"), ("protocol_version", True),
                              ("role", "primary"), ("cleanup_confirmed", True), ("path", "/arbitrary"),
                              ("deadline", 100000), ("limits", {}), ("pid", 1)):
            with self.subTest(field=field), self.assertRaises(ControlError):
                decode_control_request(canonical_json({**valid, field: value}))
        for raw in (b"PRIVATE-INVALID-CANARY", b"\xff", b"[" * 1000,
                    b"x" * MAX_CONTROL_FRAME_BYTES, b'{"operation":1,"operation":2}'):
            reply = json.loads(self.service.handle(raw, CONTEXT))
            self.assertFalse(reply["ready"])
            self.assertIsNone(reply["scope"])
            self.assertNotIn("PRIVATE", str(reply))
            with self.assertRaises(ControlError) as caught:
                decode_control_request(raw)
            self.assertIsNone(caught.exception.__context__)

    def test_response_codec_refuses_scope_types_flags_and_inconsistent_states(self):
        request = self.request()
        valid = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        cases = [{**valid, "scope": {**valid["scope"], "supervisor_generation": True}},
                 {**valid, "target": {**valid["target"], "attempt": True}}]
        for field, value in (("ready", 1), ("observed_at_ns", True), ("protocol_version", True),
                              ("request_id", "other"), ("request_digest", "b" * 64), ("extra", 1)):
            cases.append({**valid, field: value})
        for field, value in (("durable", True), ("cleanup_confirmed", True), ("stop_requested", True),
                              ("cancellation_state", "cleanup_confirmed"), ("health", "active"),
                              ("liveness_frozen", True), ("terminal_observed", 1), ("extra", 1)):
            cases.append({**valid, "result": {**valid["result"], field: value}})
        for field, value in (("elapsed_ms", True), ("activity_events", -1), ("stop_reason", "RAW-CANARY"),
                              ("terminal", True), ("health", "active"), ("extra", 1)):
            cases.append({**valid, "result": {**valid["result"], "liveness": {**valid["result"]["liveness"], field: value}}})
        for value in cases:
            with self.subTest(value=value), self.assertRaisesRegex(ControlError, "control_invalid_response"):
                decode_control_response(canonical_json(value), request)

    def test_summary_rechecks_earlier_rows_after_shared_clock_failure(self):
        source = SequencedClock()
        monitor = ProjectMonitor(scope(), limits(), max_tracked_attempts=4, clock_ns=source)
        monitor.register(binding(), hard_deadline_ns=100 * SECOND)
        monitor.register(binding(2), hard_deadline_ns=100 * SECOND)
        source.samples = [0, ValueError("PRIVATE-CLOCK-CANARY")]
        service = ControlService(monitor, authorizer=self.authorize, clock_ns=self.publication_clock)
        self.addCleanup(service.close)
        request = self.request(target=ServiceTarget(binding().orchestration_handle, None, None))
        result = self.call(request, service=service).result
        self.assertEqual(2, result["health_counts"]["unavailable"])
        self.assertEqual(2, result["stop_requested"])

    def test_live_attempt_cannot_claim_terminal_even_in_a_correlated_cancel_reply(self):
        for health, seconds in (("starting", None), ("active", 0), ("quiet", 2), ("stale", 5)):
            if seconds is not None:
                if seconds == 0:
                    self.monitor.startup(binding())
                else:
                    self.clock.at(seconds)
            status = self.request()
            live = json.loads(self.service.handle(encode_control_request(status), CONTEXT))
            self.assertEqual(health, live["result"]["health"])
            for operation in ("project.status", "project.cancel"):
                request = self.request(operation)
                changed = deepcopy(live)
                changed.update(request_id=request.request_id, request_digest=service_request_digest(request))
                changed["result"].update(terminal_observed=True, cancellation_state="terminal_observed")
                with self.subTest(health=health, operation=operation), self.assertRaisesRegex(
                    ControlError, "control_invalid_response"
                ):
                    decode_control_response(canonical_json(changed), request)

    def test_summary_stop_and_terminal_facts_cannot_be_attributed_to_live_health(self):
        request = self.request(target=ServiceTarget(binding().orchestration_handle, None, None))
        for health, seconds in (("starting", None), ("active", 0), ("quiet", 2), ("stale", 5)):
            if seconds is not None:
                if seconds == 0:
                    self.monitor.startup(binding())
                else:
                    self.clock.at(seconds)
            live = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
            self.assertEqual(1, live["result"]["health_counts"][health])
            for fact in ("stop_requested", "terminal_observed"):
                changed = deepcopy(live)
                changed["result"][fact] = 1
                with self.subTest(health=health, fact=fact), self.assertRaisesRegex(
                    ControlError, "control_invalid_response"
                ):
                    decode_control_response(canonical_json(changed), request)
        self.monitor.observe_terminal(binding())
        terminal = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        terminal["result"]["stop_requested"] = 1
        with self.assertRaisesRegex(ControlError, "control_invalid_response"):
            decode_control_response(canonical_json(terminal), request)

    def test_unavailable_summary_requires_its_stop_to_be_counted(self):
        self.clock.value = ValueError("PRIVATE-CLOCK")
        request = self.request(target=ServiceTarget(binding().orchestration_handle, None, None))
        unavailable = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        self.assertEqual(1, unavailable["result"]["health_counts"]["unavailable"])
        unavailable["result"]["stop_requested"] = 0
        with self.assertRaisesRegex(ControlError, "control_invalid_response"):
            decode_control_response(canonical_json(unavailable), request)

    def test_summary_preserves_first_stop_and_cleanup_before_terminal(self):
        for number in (2, 3, 4):
            self.monitor.register(binding(number), hard_deadline_ns=100 * SECOND)
        self.monitor.startup(binding())
        self.monitor.cancel(binding(2))
        self.monitor.observe_terminal(binding(2))
        self.monitor.observe_terminal(binding(3))
        self.monitor.observe_cleanup(binding(4))
        request = self.request(target=ServiceTarget(binding().orchestration_handle, None, None))
        initial = self.call(request).result
        self.assertEqual((1, 2, 1), tuple(initial[name] for name in
                                         ("stop_requested", "terminal_observed", "cleanup_confirmed")))
        self.assertEqual({"active": 1, "stopping": 1, "terminal": 1, "cleaned": 1},
                         {name: count for name, count in initial["health_counts"].items() if count})
        self.monitor.observe_terminal(binding(4))
        self.assertEqual(3, self.call(request).result["terminal_observed"])
        self.clock.value = ValueError("PRIVATE-CLOCK")
        self.monitor.observe_terminal(binding())
        final = self.call(request).result
        self.assertEqual((2, 4, 1), tuple(final[name] for name in
                                       ("stop_requested", "terminal_observed", "cleanup_confirmed")))
        self.assertEqual(1, final["health_counts"]["unavailable"])

    def test_summary_types_and_unavailable_or_activity_metadata_are_consistent(self):
        request = self.request(target=ServiceTarget(binding().orchestration_handle, None, None))
        valid = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        for field, value in (("tracked_attempts", True), ("stop_requested", 2), ("cleanup_confirmed", 1),
                              ("durable", True), ("health_counts", {"starting": 1})):
            changed = {**valid, "result": {**valid["result"], field: value}}
            with self.subTest(field=field), self.assertRaisesRegex(ControlError, "control_invalid_response"):
                decode_control_response(canonical_json(changed), request)
        self.monitor.startup(binding())
        request = self.request()
        active = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        active["result"]["liveness"]["last_activity_category"] = "qualifying_work"
        with self.assertRaisesRegex(ControlError, "control_invalid_response"):
            decode_control_response(canonical_json(active), request)
        self.clock.value = ValueError("PRIVATE-CLOCK")
        unavailable = json.loads(self.service.handle(encode_control_request(request), CONTEXT))
        self.assertEqual("unavailable", decode_control_response(canonical_json(unavailable), request).result["health"])
        unavailable["result"].update(stop_requested=False, stop_reason=None, cancellation_state="running")
        with self.assertRaisesRegex(ControlError, "control_invalid_response"):
            decode_control_response(canonical_json(unavailable), request)

    def test_largest_closed_metadata_envelope_fits_the_16_kib_frame(self):
        maximal = ControlScope("prj_" + "f" * 32, "s" * 160, "v" * 160, (1 << 63) - 1, "f" * 64)
        target = ServiceTarget("orc_" + "f" * 48, "hrn_" + "f" * 48, (1 << 31) - 1)
        request = control_request(maximal, "project.status", target)
        ordinary = self.request()
        reply = json.loads(self.service.handle(encode_control_request(ordinary), CONTEXT))
        reply.update(request_id=request.request_id, request_digest=service_request_digest(request), scope=asdict(maximal),
                     target=asdict(target), observed_at_ns=(1 << 63) - 1)
        maximum_ms = (((1 << 63) - 1) + 999_999) // 1_000_000
        reply["result"]["liveness"].update(elapsed_ms=maximum_ms, startup_remaining_ms=maximum_ms,
                                             hard_deadline_remaining_ms=maximum_ms)
        encoded = canonical_json(reply)
        self.assertLessEqual(len(encode_control_request(request)) + 1, MAX_CONTROL_FRAME_BYTES)
        self.assertLessEqual(len(encoded) + 1, MAX_CONTROL_FRAME_BYTES)
        self.assertEqual(maximum_ms, decode_control_response(encoded, request).result["liveness"]["elapsed_ms"])

    def test_publication_clock_regression_latches_and_never_refreshes_a_stop(self):
        source = FakeClock(1000)
        service = self.new_service(clock_ns=source)
        first = self.call(self.request("project.cancel"), service=service)
        source.value = 999
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.call(service=service)
        source.value = 2000
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.call(service=service)
        self.assertEqual(first.result["liveness"]["elapsed_ms"], self.monitor.status(binding()).liveness.elapsed_ms)

    def test_publication_failure_after_cancel_does_not_claim_no_stop_or_fake_freshness(self):
        callback = mock.Mock(side_effect=ValueError("PRIVATE-PUBLICATION-CANARY"))
        service = self.new_service(clock_ns=callback)
        with self.assertRaisesRegex(ControlError, "control_unavailable") as caught:
            self.call(self.request("project.cancel"), service=service)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual("cancel_requested", self.monitor.status(binding()).stop_reason)
        callback.side_effect = None
        callback.return_value = 1000
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.call(service=service)
        callback.assert_called_once_with()

    def test_reentrant_or_interrupted_publication_clock_stays_failed(self):
        request = self.request()
        service = None
        inner = []
        def reenter():
            inner.append(json.loads(service.handle(encode_control_request(request), CONTEXT)))
            return 1000
        service = self.new_service(clock_ns=reenter)
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.call(request, service=service)
        self.assertEqual("control_unavailable", inner[0]["reason_code"])
        callback = mock.Mock(side_effect=KeyboardInterrupt())
        service = self.new_service(clock_ns=callback)
        with self.assertRaises(KeyboardInterrupt):
            self.call(request, service=service)
        callback.side_effect = None
        callback.return_value = 1000
        with self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.call(request, service=service)
        callback.assert_called_once_with()

    def test_client_lost_cancel_reply_is_not_resent_and_explicit_retry_keeps_decision(self):
        requests = []
        def lost(path, raw, **options):
            requests.append(raw)
            self.wire(path, raw, **options)
            raise ServiceTransportError("transport_unavailable")
        with mock.patch("codex_agents.control_api.exchange_frame", side_effect=lost) as wire, \
                self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.client.cancel(self.target())
        self.assertEqual(1, wire.call_count)
        first = self.monitor.status(binding())
        self.clock.at(99)
        with mock.patch("codex_agents.control_api.exchange_frame", side_effect=self.wire) as wire:
            retry = self.client.cancel(self.target())
        self.assertEqual(1, wire.call_count)
        self.assertEqual(requests[0], wire.call_args.args[1])
        self.assertEqual(first.stop_reason, retry.result["stop_reason"])
        self.assertEqual(first.liveness.elapsed_ms, retry.result["liveness"]["elapsed_ms"])
        self.assertFalse(retry.result["cleanup_confirmed"])

    def test_client_rejects_old_future_or_late_publication_under_one_budget(self):
        raw = self.service.handle(encode_control_request(self.request()), CONTEXT)
        self.publication_clock.at(1)
        with mock.patch("codex_agents.control_api.exchange_frame", return_value=raw), \
                self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.client.status(self.target())
        self.publication_clock.value = 0
        def late(path, request, **options):
            reply = self.wire(path, request, **options)
            self.publication_clock.at(2)
            return reply
        with mock.patch("codex_agents.control_api.exchange_frame", side_effect=late), \
                self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.client.status(self.target())
        data = json.loads(raw)
        data["observed_at_ns"] = 3 * SECOND
        with mock.patch("codex_agents.control_api.exchange_frame", return_value=canonical_json(data)), \
                self.assertRaisesRegex(ControlError, "control_unavailable"):
            self.client.status(self.target())

    def test_close_preserves_monitor_safety_and_wrapper_retry_keeps_same_stop(self):
        first = self.call(self.request("project.cancel"))
        self.service.close()
        reply = json.loads(self.service.handle(encode_control_request(self.request()), CONTEXT))
        self.assertEqual("control_wrong_owner", reply["reason_code"])
        self.assertIsNone(reply["scope"])
        self.clock.at(99)
        self.assertTrue(self.monitor.tick()[0].stop_requested)
        second = self.new_service()
        self.assertEqual(first.result, self.call(self.request("project.cancel"), service=second).result)

    def test_new_supervisor_generation_rejects_old_scope_before_monitor_work(self):
        next_scope = replace(scope(), supervisor_generation=2)
        monitor = ProjectMonitor(next_scope, limits(), max_tracked_attempts=4, clock_ns=self.clock)
        monitor.register(replace(binding(), supervisor_generation=2), hard_deadline_ns=100 * SECOND)
        service = ControlService(monitor, authorizer=self.authorize, clock_ns=self.publication_clock)
        self.addCleanup(service.close)
        before = self.clock.calls
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            self.call(self.request("project.cancel"), service=service)
        self.assertEqual(before, self.clock.calls)

    def test_foreign_thread_cannot_adopt_monitor_or_route_a_request(self):
        replies = []
        def foreign():
            try:
                ControlService(self.monitor)
            except ControlError as exc:
                replies.append(exc.reason_code)
            replies.append(json.loads(self.service.handle(encode_control_request(self.request()), CONTEXT)))
            try:
                self.service.close()
            except ControlError as exc:
                replies.append(exc.reason_code)
        before = self.clock.calls
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual("control_wrong_owner", replies[0])
        self.assertEqual("control_wrong_owner", replies[1]["reason_code"])
        self.assertIsNone(replies[1]["scope"])
        self.assertEqual("control_wrong_owner", replies[2])
        self.assertEqual(before, self.clock.calls)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fork")
    def test_inherited_service_cannot_change_or_close_parent_monitor(self):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:
            try:
                os.close(read_fd)
                value = json.loads(self.service.handle(encode_control_request(self.request("project.cancel")), CONTEXT))
                os.write(write_fd, canonical_json({"reason": value["reason_code"]}))
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            if not select.select([read_fd], [], [], 5)[0]:
                os.kill(child, signal.SIGKILL)
                self.fail("owned fork fixture did not reply")
            reply = json.loads(os.read(read_fd, 4096))
        finally:
            os.close(read_fd)
            os.waitpid(child, 0)
        self.assertEqual({"reason": "control_wrong_owner"}, reply)
        self.assertFalse(self.monitor.status(binding()).stop_requested)

    def test_client_unavailability_never_imports_persistence_or_monitor_owner(self):
        program = r'''
import importlib.abc,json,sys
from pathlib import Path
class Guard(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name in {'sqlite3','_sqlite3','codex_agents.learning_store','codex_agents.project_history','codex_agents.control_service','codex_agents.project_monitor'}:
   raise AssertionError('owner import')
sys.meta_path.insert(0,Guard())
from codex_agents.control_api import ControlClient,ControlScope,ControlError
from codex_agents.service_protocol import ServiceTarget
try:
 ControlClient(Path(sys.argv[1]),ControlScope(**json.loads(sys.argv[2])),timeout=0.1).cancel(
  ServiceTarget('orc_'+'a'*48,'hrn_'+f'{1:048x}',1))
except ControlError as exc:
 print(json.dumps({'reason':exc.reason_code,'sqlite_loaded':'sqlite3' in sys.modules,'monitor_loaded':'codex_agents.project_monitor' in sys.modules}))
'''
        completed = subprocess.run([sys.executable, "-I", "-B", "-S", "-c",
                                    f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program,
                                    str(self.base / "missing.sock"), json.dumps(asdict(self.scope))],
                                   capture_output=True, text=True, timeout=10)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual({"reason": "control_unavailable", "sqlite_loaded": False, "monitor_loaded": False},
                         json.loads(completed.stdout))


if __name__ == "__main__":
    unittest.main()
