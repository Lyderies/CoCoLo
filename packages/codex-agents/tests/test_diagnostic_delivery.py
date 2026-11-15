"""Composed diagnostics regression; fixture identity/transport confer no native authority."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401 -- native package import path
from codex_agents.cli_reports import command_report
from codex_agents.history_api import HISTORY_OPERATIONS, HistoryClient, history_request_digest
from codex_agents.history_contracts import HistoryError
from codex_agents.history_service import HistoryAuthorization, HistoryService
from codex_agents.learning_contracts import canonical_json, validate_learning_record
from codex_agents.project_history import ProjectHistory
from tests.test_bridge import (
    ACCEPTED, RATE_LIMIT, BridgeTestCase, init_frame, result, result_frame,
)
from tests.test_cli_reports import cycle
from tests.test_project_history import binding


class DiagnosticDeliveryTests(BridgeTestCase):
    def test_bridge_reason_survives_history_retry_restart_and_report_formats(self):
        route, assigned, handle = self.claude_run()
        invalid = {**result(), "evidence": [], "summary": "CANARY_REJECTED_DELIVERY"}
        self.frames("claude", [init_frame(), RATE_LIMIT, result_frame(json.dumps(invalid))])
        output = self.dispatch(route, assigned, handle)
        self.assertEqual([], self.workspace_entries())
        self.assertEqual("missing_evidence", output["bridge_report"]["result_validation_reason_code"])
        self.assertEqual("failed", output["bridge_report"]["terminal_state"])
        with self.ledger.repository(self.project_id) as repository:
            originals = repository.records("dispatch_result")
        self.assertEqual(1, len(originals))
        record = {key: value for key, value in originals[0].items() if not key.startswith("_")}
        validate_learning_record(record)
        committed = self.ledger.commit_host_result(
            self.project_id, assignment=assigned, task=self.task(), route=route,
            result=output["result"], execution_configuration=route["execution_configuration"],
            observation={"run_handle": handle, "terminal_state": "failed", "result_attempts": ACCEPTED},
        )
        self.assertFalse(committed["outcome"]["accepted"])
        self.assertEqual("missing_evidence", committed["dispatch_diagnostics"]["result_validation_reason_code"])

        # These adapters are deliberately fake. This checks byte/record/report
        # composition, not native enrollment, admission claims or C19 migration.
        runtime = "a" * 64
        directory = Path(self.temporary.name) / "diagnostic-history"
        directory.mkdir(mode=0o700)
        fixture_context = object()

        def authorize(request, context):
            if context is not fixture_context:
                return None
            return HistoryAuthorization(request.project_id, request.runtime_digest,
                                        history_request_digest(request), "host", HISTORY_OPERATIONS)

        def open_service():
            service = HistoryService(self.project_id, runtime, lambda: ProjectHistory(directory, self.project_id),
                                     authorizer=authorize, clock_ms=lambda: 1000)
            self.addCleanup(service.close)
            return service

        service = open_service()
        client = HistoryClient(directory / "fixture-only.sock", self.project_id, runtime, timeout=1)
        bound = replace(binding(project=self.project_id), runtime_digest=runtime,
                        run_handle=handle, run_id=record["run_id"])
        exchanges = []

        def exchange(path, raw, **options):
            reply = service.handle(raw, fixture_context)
            exchanges.append((raw, reply))
            return reply

        with mock.patch("codex_agents.history_api.exchange_frame", side_effect=exchange):
            delivery = client.commit_terminal(record, bound)
            self.assertEqual(record, delivery["dispatch_result"])
            self.assertEqual(record["result_digest"], delivery["result_digest"])
            self.assertEqual(hashlib.sha256(canonical_json(record)).hexdigest(), delivery["record_content_digest"])
            self.assertEqual(delivery, client.commit_terminal(record, bound))
            self.assertEqual([delivery], client.pending_terminals(limit=1)["deliveries"])
            self.assertEqual(record, client.read_record(record["record_id"])["record"])
            with self.assertRaisesRegex(HistoryError, "history_conflict"):
                client.commit_terminal({**record, "result_validation_reason_code": "unknown_fields"}, bound)
            self.assertEqual(record, client.read_record(record["record_id"])["record"])
            service.close()
            service = open_service()
            self.assertEqual(delivery, client.commit_terminal(record, bound))
            self.assertEqual([delivery], client.pending_terminals(limit=1)["deliveries"])
            self.assertEqual(record, client.read_record(record["record_id"])["record"])

        # The cycle is incidental synthetic learning data; the committed
        # diagnostic and fence above come from an actual fake-client dispatch.
        report = {**committed, "learning_cycle": cycle(1000)}
        compact = command_report("learning-record-host-result", report)
        self.assertEqual("compact-learning-cycle-v1", compact["learning_cycle"]["format"])
        self.assertEqual(committed["dispatch_diagnostics"], compact["dispatch_diagnostics"])
        self.assertIs(report, command_report("learning-record-host-result", report, full=True))
        self.assertLess(len(canonical_json(compact["learning_cycle"])), 2048)
        self.assertNotIn("CANARY", json.dumps([output, record, committed, delivery, compact, exchanges], default=str))
        self.assertEqual([], self.workspace_entries())
