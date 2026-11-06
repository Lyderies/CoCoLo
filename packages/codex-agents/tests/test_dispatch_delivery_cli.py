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
from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_store import CollectorError, LedgerError


class DispatchDeliveryCliTests(unittest.TestCase):
    """CLI boundary fixture; native admission/cleanup are not simulated proof."""

    def invoke(self, bridge_report, *, resume_failure=True, validation_error=None,
               owner_error=None, bridge_error=None):
        ledger = mock.Mock()
        maintenance = mock.Mock()
        if resume_failure:
            maintenance.resume.side_effect = CollectorError("CANARY-private-restart-detail")
        managed = cli._RestartingLedger(ledger, maintenance)
        assigned = assignment()
        inputs = {"task.json": {"id": assigned["task_id"]}, "route.json": {"provider": "codex"},
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
                validator = stack.enter_context(mock.patch.object(cli, "validate_assignment", side_effect=validation_error))
                owner = stack.enter_context(mock.patch.object(cli, "_require_host_owner", side_effect=owner_error))
                opened = stack.enter_context(mock.patch.object(cli, "_learning_ledger",
                    return_value=(managed, "prj_" + "a" * 32)))
                stack.enter_context(mock.patch.object(cli, "_json_input", side_effect=lambda name: inputs[name]))
                dispatched = stack.enter_context(mock.patch.object(cli, "dispatch_bridge",
                    return_value=bridge_report, side_effect=bridge_error))
                stack.enter_context(mock.patch.object(cli, "bridge_signal_guard", side_effect=contextlib.nullcontext))
                stack.enter_context(contextlib.redirect_stdout(output))
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
            "reviewed": True, "provider": "codex",
        }}

    def test_completed_dispatch_survives_resume_error_without_result_or_fence_changes(self):
        for kind in ("legacy", "negative", "fenced"):
            with self.subTest(kind=kind):
                original = self.report(kind=kind)
                expected = copy.deepcopy(original)
                code, response, ledger, maintenance, dispatched, validator, owner, opened = self.invoke(original)
                self.assertEqual(2, code)
                self.assertFalse(response["ready"])
                self.assertTrue(response["dispatch_completed"])
                self.assertEqual({"reason_code": "collector_restart_failed", "recovery_required": True},
                                 response["post_operation_error"])
                self.assertEqual(cli._COMPLETED_LEDGER_RESTART_ERROR, response["error"])
                self.assertEqual(expected["bridge_report"], response["bridge_report"])
                self.assertEqual(canonical_json(expected["result"]), canonical_json(response["result"]))
                self.assertEqual(expected, original)
                self.assertNotIn("terminal_state", response)
                for operation in (dispatched, validator, owner, opened):
                    operation.assert_called_once()
                ledger.close.assert_called_once_with()
                maintenance.resume.assert_called_once_with()

    def test_successful_close_keeps_the_normal_dispatch_response(self):
        original = self.report()
        code, response, ledger, maintenance, dispatched, *_ = self.invoke(original, resume_failure=False)
        self.assertEqual((0, {"ready": True, **original}), (code, response))
        dispatched.assert_called_once()
        ledger.close.assert_called_once_with()
        maintenance.resume.assert_called_once_with()

    def test_validation_failure_has_no_ledger_owner_or_dispatch_work(self):
        error = ContractError("CANARY-invalid-value", field="assignment")
        code, response, ledger, maintenance, dispatched, _, owner, opened = self.invoke(self.report(), validation_error=error)
        self.assertEqual((2, {"ready": False, "error": "input validation failed", "field": "assignment"}),
                         (code, response))
        for operation in (opened, owner, dispatched, ledger.close, maintenance.resume):
            operation.assert_not_called()

    def test_owner_failure_before_dispatch_is_not_replaced_by_recovery_metadata(self):
        code, response, ledger, maintenance, dispatched, _, owner, opened = self.invoke(
            self.report(), owner_error=LedgerError("host owner mismatch"),
        )
        self.assertEqual(2, code)
        self.assertFalse(response["ready"])
        self.assertEqual("host owner mismatch", response["error"])
        self.assertTrue({"result", "bridge_report", "dispatch_completed", "post_operation_error"}.isdisjoint(response))
        dispatched.assert_not_called()
        owner.assert_called_once()
        opened.assert_called_once()
        ledger.close.assert_called_once_with()
        maintenance.resume.assert_called_once_with()

    def test_bridge_refusal_preserves_the_original_error_without_a_result(self):
        code, response, ledger, maintenance, dispatched, *_ = self.invoke(
            self.report(), bridge_error=BridgeRefused("launch denied: billing_unverified"),
        )
        self.assertEqual((2, "dispatch refused", "launch denied: billing_unverified"),
                         (code, response["error"], response["reason"]))
        self.assertTrue({"result", "bridge_report", "dispatch_completed", "post_operation_error"}.isdisjoint(response))
        dispatched.assert_called_once()
        ledger.close.assert_called_once_with()
        maintenance.resume.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
