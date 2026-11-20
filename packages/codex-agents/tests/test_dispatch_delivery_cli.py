from __future__ import annotations

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import TEMP_ROOT
from tests.learning_support import assignment, result

import cli
from codex_agents.bridge import BridgeRefused, _fenced_result
from codex_agents.contracts import ContractError, validate_result
from codex_agents.dispatch_api import DispatchError
from codex_agents.learning_store import CollectorError


class DispatchDeliveryCliTests(unittest.TestCase):
    """Remaining Claude collector lifecycle and Codex service-client boundary.

    Native admission/cleanup are not simulated proof.
    """

    def invoke(self, bridge_report, *, resume_failure=True, validation_error=None,
               owner_error=None, bridge_error=None, close_error=None,
               guard_exit_error=None, probe=None, provider='claude', client_error=None):
        ledger = mock.Mock()
        ledger.close.side_effect = close_error
        maintenance = mock.Mock()
        if resume_failure:
            maintenance.resume.side_effect = CollectorError("CANARY-private-restart-detail")
        managed = cli._RestartingLedger(ledger, maintenance)
        guard = mock.MagicMock()
        guard.__exit__.return_value = False
        guard.__exit__.side_effect = guard_exit_error
        assigned = assignment()
        inputs = {"task.json": {"id": assigned["task_id"]}, "route.json": {"provider": provider},
                  "assignment.json": assigned, "grant.json": assigned["permissions"]}
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            argv = ["cli.py", "dispatch", "--project-root", directory, "--run-handle", "hrn_" + "a" * 48,
                    "--task", "task.json", "--route", "route.json", "--assignment", "assignment.json",
                    "--parent-grant", "grant.json", "--expected-export-commit", "a" * 40]
            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                for name in ("_require_protected_runtime", "_require_own_roots"):
                    stack.enter_context(mock.patch.object(cli, name))
                stack.enter_context(mock.patch.object(cli, '_host_client', return_value='codex-host-v1'))
                validator = stack.enter_context(mock.patch.object(cli, "validate_assignment", side_effect=validation_error))
                owner = stack.enter_context(mock.patch.object(cli, "_require_host_owner", side_effect=owner_error))
                opened = stack.enter_context(mock.patch.object(cli, "_learning_ledger",
                    return_value=(managed, "prj_" + "a" * 32)))
                stack.enter_context(mock.patch.object(cli, "_json_input", side_effect=lambda name: inputs[name]))
                # O10 D9: the CLI no longer has a direct bridge dispatch; nothing may stand in for one.
                dispatched = mock.Mock(return_value=bridge_report, side_effect=bridge_error)
                client = stack.enter_context(mock.patch.object(cli, "dispatch_codex_client",
                    return_value=bridge_report, side_effect=client_error))
                stack.enter_context(contextlib.redirect_stdout(output))
                if probe is not None:
                    probe.update(ledger=ledger, maintenance=maintenance, dispatched=dispatched,
                                 output=output, guard=guard, client=client)
                code = cli.main()
            decoded = json.loads(output.getvalue())  # Exactly one complete JSON object.
            if dispatched.call_count:
                self.assertEqual(Path(directory), dispatched.call_args.kwargs["project_root"])
                self.assertEqual("a" * 40, dispatched.call_args.kwargs["expected_export_commit"])
        self.assertNotIn("CANARY-private-restart-detail", output.getvalue())
        return code, decoded, ledger, maintenance, dispatched, validator, owner, opened

    def report(self, *, kind="legacy"):
        fenced = kind == "fenced"
        payload = _fenced_result(assignment(), "failed", "unparseable_result") if fenced else result()
        if kind == "negative":
            payload.update(execution_state="completed", semantic_verdict="not-ready", admission_state="unverified",
                           retry={"attempt": 1, "retry_of_assignment_id": None})
            payload["evidence"][0]["outcome"] = "fail"
        validate_result(payload, assignment=assignment())
        return {"result": payload, "bridge_report": {
            "outcome": "unparseable_result" if fenced else "result",
            "terminal_state": "failed" if fenced else None,
            "reviewed": True, "provider": "claude",
        }}

    def test_validation_failure_has_no_ledger_owner_or_dispatch_work(self):
        error = ContractError("CANARY-invalid-value", field="assignment")
        code, response, ledger, maintenance, dispatched, _, owner, opened = self.invoke(self.report(), validation_error=error)
        self.assertEqual((2, {"ready": False, "error": "input validation failed", "field": "assignment"}),
                         (code, response))
        for operation in (opened, owner, dispatched, ledger.close, maintenance.resume):
            operation.assert_not_called()

    def test_codex_service_result_bypasses_ledger_owner_and_collector_even_for_fenced_results(self):
        for kind in ('legacy', 'negative', 'fenced'):
            with self.subTest(kind=kind):
                original = self.report(kind=kind)
                original['bridge_report']['provider'] = 'codex'
                expected = copy.deepcopy(original)
                probe = {}
                code, response, ledger, maintenance, dispatched, validator, owner, opened = self.invoke(
                    original, provider='codex', probe=probe)
                self.assertEqual((0, {'ready': True, **expected}), (code, response))
                self.assertEqual(expected, original)
                validator.assert_called_once()
                probe['client'].assert_called_once()
                self.assertEqual('a'*40, probe['client'].call_args.kwargs['expected_export_commit'])
                for operation in (opened, owner, dispatched, ledger.close, maintenance.resume, probe['guard'].__enter__):
                    operation.assert_not_called()

    def test_codex_service_refusal_never_falls_back_to_legacy_dispatch(self):
        probe = {}
        code, response, ledger, maintenance, dispatched, _, owner, opened = self.invoke(
            self.report(), provider='codex', probe=probe, client_error=DispatchError('dispatch_not_authorized'))
        from codex_agents.refusal_steps import next_step  # S1-H1b: every refusal names its next step
        self.assertEqual((2, {'ready': False, 'error': 'dispatch unavailable',
                             'reason_code': 'dispatch_not_authorized',
                             'next_step': next_step('dispatch_not_authorized')}), (code, response))
        probe['client'].assert_called_once()
        for operation in (opened, owner, dispatched, ledger.close, maintenance.resume):
            operation.assert_not_called()


ROUTE_SHAPES = [{**({} if provider is None else {'provider': provider}), **({} if way is None else {'dispatch': way})}
                for provider in ('codex', 'claude', 'gemini', None) for way in ('bridge', 'native', 'other', None)]


class DirectDispatchTests(unittest.TestCase):
    """O10 D9: the CLI's direct bridge dispatch, for a route the project host does not run."""

    def test_D9_every_route_the_project_host_does_not_run_is_refused_by_the_bridge_before_anything(self):
        from codex_agents import bridge
        refused = 0
        for route in ROUTE_SHAPES:
            if cli._host_driven(route):
                continue
            with self.subTest(route=route):
                ledger = mock.Mock()
                with self.assertRaises(BridgeRefused):
                    bridge.dispatch(ledger, 'prj_' + 'a' * 32, project_root=Path('/nonexistent'),
                                    bundle_root=Path('/nonexistent'), task={}, route=route, assignment=assignment(),
                                    parent_grant={}, run_handle='hrn_' + 'a' * 48, result_schema=b'{}', environ={})
                self.assertEqual([], ledger.mock_calls)
                refused += 1
        self.assertEqual(11, refused)  # Only codex routes and bridged Claude routes are the host's.

    def test_D9_a_route_the_project_host_does_not_run_is_refused_without_the_legacy_ledger(self):
        assigned = assignment()
        for route in ROUTE_SHAPES:
            if cli._host_driven(route):
                continue
            with self.subTest(route=route):
                inputs = {"task.json": {"id": assigned["task_id"]}, "route.json": route,
                          "assignment.json": assigned, "grant.json": assigned["permissions"]}
                output = io.StringIO()
                with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory, contextlib.ExitStack() as stack:
                    argv = ["cli.py", "dispatch", "--project-root", directory, "--run-handle", "hrn_" + "a" * 48,
                            "--task", "task.json", "--route", "route.json", "--assignment", "assignment.json",
                            "--parent-grant", "grant.json", "--expected-export-commit", "a" * 40]
                    stack.enter_context(mock.patch.object(sys, "argv", argv))
                    for name in ("_require_protected_runtime", "_require_own_roots"):
                        stack.enter_context(mock.patch.object(cli, name))
                    stack.enter_context(mock.patch.object(cli, "validate_assignment"))
                    stack.enter_context(mock.patch.object(cli, "_json_input", side_effect=lambda name: inputs[name]))
                    opened = stack.enter_context(mock.patch.object(cli, "_learning_ledger"))
                    client = stack.enter_context(mock.patch.object(cli, "dispatch_codex_client"))
                    stack.enter_context(contextlib.redirect_stdout(output))
                    code = cli.main()
                self.assertEqual(2, code)
                self.assertEqual({"ready": False, "error": "dispatch refused",
                                  "reason": "route is not run by the project host"}, json.loads(output.getvalue()))
                opened.assert_not_called()
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
