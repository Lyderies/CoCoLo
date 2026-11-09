"""Two real supervisor processes; fake enrollment/provider, no model requests."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import time
import unittest

import tests.support  # noqa: F401
from tests import test_account_claim_storage as fixtures
from tests.learning_support import admit_provider
from tests.test_bridge import CLEAN

from codex_agents.account_api import AccountRequest, account_request_digest
from codex_agents.account_claim_contracts import LaunchClaimInput
from codex_agents.account_service import AccountAuthorization, AccountService
from codex_agents.control_api import ControlScope, control_request, decode_control_response, encode_control_request
from codex_agents.execution_grant import ExecutionRegistrationInput
from codex_agents.host_open_contracts import OpenProvenanceInput
from codex_agents.learning_contracts import content_digest, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST
from codex_agents.result_contracts import ResultLeaseBinding
from codex_agents.project_history import ProjectHistory
from codex_agents.service_protocol import ServiceTarget
from codex_agents.terminal_delivery import TerminalSubmissionInput
from codex_agents.run_provenance import RunProvenanceInput
from codex_agents.sql_read_budget import SQLReadLimits


class ProjectExecutionProcessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AccountClaimStorageTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.store.close()  # Only the actual project writer opens it during the run.
        self.base, self.ledger = self.fixture.base, self.fixture.ledger
        self.bridge = self.base.fixture.fixture
        self.children, self.contexts = [], {}
        self.addCleanup(self.stop_children)
        self.service = AccountService(COLLECTOR_RUNTIME_DIGEST, self.base.views, authorizer=self.authorize,
                                      open_provenance_work_limits=SQLReadLimits(100_000, 1000, 2 * 1_048_576),
                                      run_provenance_work_limits=SQLReadLimits(100_000, 1000, 2 * 1_048_576))
        self.addCleanup(self.service.close)

    def authorize(self, request, context):
        entry = self.contexts.get(context)
        if entry is None or entry["scope"].project_id != request.project_id:
            return None
        return AccountAuthorization(request.project_id, COLLECTOR_RUNTIME_DIGEST, account_request_digest(request),
                                    entry["source"], frozenset({"account.claim_launch", "account.register_execution", "account.submit_terminal", "account.run_provenance", "account.open_provenance"}),
                                    "a" * 64, entry["scope"], entry["evidence"], entry.get('terminal_evidence'))

    def account(self, request, context):
        self.base.stamp = time.time_ns() // 1_000_000
        return self.base.call(request, service=self.service, context=context)

    def second_project(self):
        path = Path(self.bridge.temporary.name) / "second-project"
        path.mkdir(mode=0o700)
        project = self.ledger.register_project(path, now_ms=1)
        admit_provider(self.ledger, project, self.bridge.owner_key, tools=self.bridge.tools,
                       observed_at_ms=self.base.stamp - 120_000)
        self.ledger.observe_provider(project, "codex", project_root=self.bridge.tools, environ=CLEAN)
        route, assignment = self.base.fixture.route, self.base.fixture.assignment
        orchestration = self.ledger.open_orchestration(project, client_version="codex-host-v1", now_ms=self.base.stamp)
        run = self.ledger.open_host_run(project, assignment=assignment, task=self.bridge.task(), route=route,
                                        execution_configuration=route["execution_configuration"], client_version="bridge-v1",
                                        orchestration_handle=orchestration["orchestration_handle"], now_ms=self.base.stamp)
        identity = self.ledger.observe_provider(project, "codex", project_root=self.bridge.tools, environ=CLEAN,
                                                run_handle=run["run_handle"], phase="before")[0]
        request = AccountRequest(1, "account.claim_launch", "second-claim", project, COLLECTOR_RUNTIME_DIGEST,
                                 LaunchClaimInput(run["run_handle"], content_digest(identity)).payload())
        run_id = self.ledger._connection.execute("SELECT run_id FROM host_run_bindings WHERE project_id=? AND handle_digest=?",
                                                 (project, keyed_digest(self.fixture.key, run["run_handle"]))).fetchone()[0]
        scope = ResultLeaseBinding(project, "primary-b", COLLECTOR_RUNTIME_DIGEST, "supervisor-b", 1,
                                   orchestration["orchestration_handle"], run["run_handle"], run_id, assignment["attempt"],
                                   request.request_id, account_request_digest(request))
        history = Path(self.bridge.temporary.name) / "history-b"
        history.mkdir(mode=0o700)
        return scope, request, history

    def send(self, child, data):
        child["process"].stdin.write(json.dumps(data).encode() + b"\n")
        child["process"].stdin.flush()

    def read(self, child, expected):
        process = child["process"]
        if not select.select([process.stdout], [], [], 15)[0]:
            self.fail("supervisor fixture response timed out: " + expected)
        raw = process.stdout.readline(131_073)
        if not raw:
            process.wait(timeout=10)
            self.fail("supervisor fixture exited: " + process.stderr.read(5000).decode(errors="replace"))
        self.assertTrue(raw.endswith(b"\n"))
        self.assertLessEqual(len(raw), 131_072)
        value = json.loads(raw)
        self.assertEqual(expected, value["stage"])
        return value

    def start(self, scope, request, history, name, *, with_provenance=False, lose_consumption_reply=False, lose_open_reply=False):
        root = Path(self.bridge.temporary.name) / name
        root.mkdir(mode=0o700)
        for directory in ("control", "export", "tmp"):
            (root / directory).mkdir(mode=0o700)
        executable = root / "inert-exec"
        executable.write_text("#!" + sys.executable + "\n" + '''
import json, time
from pathlib import Path
root = Path(__file__).parent
(root / "launches").write_text("launch\\n")
print(json.dumps({"type":"thread.started","thread_id":"thread_fixture"}), flush=True)
print(json.dumps({"type":"turn.started"}), flush=True)
time.sleep(30)
''')
        executable.chmod(0o700)
        pairs = [socket.socketpair() for _ in range(2)]
        environment = dict(os.environ)
        for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"):
            environment.pop(key, None)
        process = subprocess.Popen([sys.executable, "-B", "-m", "tests.project_supervisor_fixture",
                                    *(str(pair[0].fileno()) for pair in pairs)],
                                   cwd=Path(__file__).resolve().parents[1], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   pass_fds=tuple(pair[0].fileno() for pair in pairs))
        for server, _ in pairs:
            server.close()
        child = {"process": process, "root": root, "scope": scope, 'history': history,
                 "sockets": [pair[1] for pair in pairs], "stopped": False}
        self.children.append(child)
        context = object()
        child['context'] = context
        self.contexts[context] = {"scope": scope, "source": "host", "evidence": None}
        self.send(child, {"scope": scope.as_data(), "root": str(root), "history": str(history),
                          "fixture_key": self.fixture.key.hex(), "task": self.bridge.task(),
                          "route": self.base.fixture.route, "assignment": self.base.fixture.assignment,
                          "with_provenance": with_provenance, "lose_consumption_reply": lose_consumption_reply,
                          "lose_open_reply": lose_open_reply})
        armed = self.read(child, "armed")
        self.contexts[context]["evidence"] = bytes.fromhex(armed["evidence"])
        if with_provenance:
            self.contexts[context]['source'] = 'project'
            original_open = self.account(replace(request, operation='account.open_provenance',
                request_id='original-open-' + name,
                payload=OpenProvenanceInput(scope.run_handle, self.base.fixture.assignment['assignment_id']).payload()), context)
            self.send(child, {'provenance': original_open})
            child['host_open_record'] = self.read(child, 'open_recorded')
            self.assertTrue(child['host_open_record']['record_id'].startswith('host_open_provenance:'))
            self.assertEqual(0, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims '
                'WHERE project_id=? AND handle_digest=?', (scope.project_id, keyed_digest(self.fixture.key, scope.run_handle))).fetchone()[0])
            self.contexts[context]['source'] = 'host'
        receipt = self.account(request, context)
        self.assertEqual("claimed", receipt["state"])
        child["receipt"] = receipt
        self.send(child, receipt)
        bound = self.read(child, "bound")
        self.contexts[context].update(source="project", evidence=bytes.fromhex(bound["evidence"]))
        registration = ExecutionRegistrationInput(scope.run_handle, receipt["bridge_run_id"], receipt["receipt_digest"],
                                                  request.request_id, request.payload["identity_digest"])
        registered = self.account(replace(request, operation="account.register_execution", request_id="register-" + name,
                                          payload=registration.payload()), context)
        self.assertFalse(registered["known"])
        child['registration'], child['registration_digest'] = registration, registered['grant']['registration_digest']
        if with_provenance:
            provenance = RunProvenanceInput(registration, registered['grant']['registration_digest'],
                                            self.base.fixture.assignment['assignment_id'])
            actual = self.account(replace(request, operation='account.run_provenance', request_id='provenance-' + name,
                                          payload=provenance.payload()), context)
            registered = {**registered, 'provenance': actual}
        self.send(child, registered)
        if lose_consumption_reply:
            child['consumption_fenced'] = self.read(child, 'consumption_fenced')
            self.assertEqual(0, process.wait(timeout=10))
            return child
        started = self.read(child, "started")
        self.assertEqual(process.pid, started["pid"])
        self.assertEqual(process.pid, started["registry_pid"])
        self.assertEqual(registered["grant"]["deadline_ns"], started["deadline_ns"])
        return child

    def accept_terminal(self, child):
        terminal = self.read(child, 'terminal')
        delivery = terminal['delivery']
        context = child['context']
        self.contexts[context]['terminal_evidence'] = bytes.fromhex(terminal['evidence'])
        submission = TerminalSubmissionInput(child['registration'], child['registration_digest'],
                                              delivery['record_id'], delivery['record_content_digest'], delivery['result_digest'])
        request = AccountRequest(1, 'account.submit_terminal', 'terminal', child['scope'].project_id,
                                 COLLECTOR_RUNTIME_DIGEST, submission.payload())
        self.contexts[context]['source'] = 'project'
        # Grant this closed operation on the fixture boundary, never through JSON.
        receipt = self.account(request, context)
        self.assertEqual('recorded', receipt['state'])
        self.assertFalse(receipt['known'])
        self.assertEqual({**receipt, 'known': True}, self.account(request, context))
        self.send(child, receipt)
        return self.read(child, 'done')

    def control(self, child, operation):
        scope = child["scope"]
        control_scope = ControlScope(**{name: asdict(scope)[name] for name in (
            "project_id", "primary_session_id", "runtime_digest", "supervisor_instance_id", "supervisor_generation")})
        request = control_request(control_scope, operation, ServiceTarget(scope.orchestration_handle, scope.run_handle, scope.attempt))
        connection = child["sockets"][0 if operation == "project.status" else 1]
        connection.settimeout(2)
        connection.sendall(encode_control_request(request) + b"\n")
        incoming = bytearray()
        while not incoming.endswith(b"\n") and len(incoming) <= 32768:
            part = connection.recv(32768)
            self.assertTrue(part, "control connection ended without reply")
            incoming.extend(part)
        return decode_control_response(bytes(incoming[:-1]), request)

    def stop_children(self):
        for child in self.children:
            process = child["process"]
            if child["stopped"]:
                # Still our unreaped child; do not recycle a PID after wait().
                os.kill(process.pid, signal.SIGCONT)
                child["stopped"] = False
            if process.returncode is None:
                try:
                    self.control(child, "project.cancel")
                except (OSError, AssertionError):
                    pass
            process.stdin.close()
        for child in self.children:
            process = child["process"]
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for connection in child["sockets"]:
                connection.close()
            process.stdout.close()
            process.stderr.close()

    def test_lost_committed_consumption_reply_retains_proof_and_never_issues_a_ticket(self):
        child = self.start(self.fixture.scope, self.fixture.request, self.fixture.directory,
                           'lost-consumption', with_provenance=True, lose_consumption_reply=True)
        self.assertEqual({'stage': 'consumption_fenced', 'tickets': 0, 'launched': False}, child['consumption_fenced'])
        store = ProjectHistory(child['history'], child['scope'].project_id)
        try:
            self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
            self.assertEqual(1, store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='execution_provenance'").fetchone()[0])
        finally:
            store.close()
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM capacity_executions').fetchone()[0])
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL').fetchone()[0])

    def test_lost_original_open_reply_retries_same_journal_before_any_account_claim(self):
        child = self.start(self.fixture.scope, self.fixture.request, self.fixture.directory,
            'lost-original-open', with_provenance=True, lose_open_reply=True, lose_consumption_reply=True)
        self.assertEqual({'stage': 'consumption_fenced', 'tickets': 0, 'launched': False}, child['consumption_fenced'])
        store = ProjectHistory(child['history'], child['scope'].project_id)
        try:
            self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM project_host_opens').fetchone()[0])
            self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM project_execution_consumptions').fetchone()[0])
            self.assertEqual(child['host_open_record']['content_digest'],
                store.record(child['host_open_record']['record_id']).content_digest)
        finally:
            store.close()
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM launch_claims').fetchone()[0])
        self.assertEqual(1, self.ledger._connection.execute('SELECT COUNT(*) FROM active_run_capacity').fetchone()[0])

    def test_suspended_project_a_cannot_block_project_b_control_cleanup_or_history(self):
        first = self.start(self.fixture.scope, self.fixture.request, self.fixture.directory, "supervisor-a", with_provenance=True)
        second = self.start(*self.second_project(), "supervisor-b", with_provenance=True)
        self.assertNotEqual(first["process"].pid, second["process"].pid)
        self.assertEqual(0, len(self.ledger._capacity_executions._pending))
        os.kill(first["process"].pid, signal.SIGSTOP)
        first["stopped"] = True
        end = time.monotonic() + 2
        while "T" not in subprocess.check_output(["/bin/ps", "-o", "stat=", "-p", str(first["process"].pid)]).decode():
            self.assertLess(time.monotonic(), end, "owned supervisor did not enter stopped state")
            time.sleep(.005)
        started = time.monotonic()
        self.assertFalse(self.control(second, "project.status").result["stop_requested"])
        reply = self.control(second, "project.cancel")
        self.assertEqual("stop_requested", reply.result["cancellation_state"])
        self.assertLess(time.monotonic() - started, 2)
        done_b = self.accept_terminal(second)
        self.assertTrue(first["stopped"])
        os.kill(first["process"].pid, signal.SIGCONT)
        first["stopped"] = False
        self.assertEqual("stop_requested", self.control(first, "project.cancel").result["cancellation_state"])
        done_a = self.accept_terminal(first)
        for child, done in ((first, done_a), (second, done_b)):
            self.assertTrue(done["cleanup_confirmed"])
            self.assertTrue(done["fenced"])
            self.assertEqual("cancelled", done["terminal_state"])
            self.assertEqual("cancel_requested", done["stop_reason"])
            self.assertEqual(1, done["ticket_count"])
            self.assertEqual(["launch"], done["launches"])
            self.assertTrue(done['acknowledged'])
            self.assertEqual(child["receipt"]["bridge_run_id"], done["persisted_claim"])
            self.assertEqual(0, child["process"].wait(timeout=10))
            store = ProjectHistory(child['history'], child['scope'].project_id)
            try:
                self.assertEqual((), store.pending_terminals())
                self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM terminal_outbox WHERE receipt_digest IS NOT NULL').fetchone()[0])
                self.assertEqual(1, store._connection.execute("SELECT COUNT(*) FROM records WHERE record_type='execution_provenance'").fetchone()[0])
                self.assertEqual(1, store._connection.execute('SELECT COUNT(*) FROM project_orchestrations').fetchone()[0])
            finally:
                store.close()
        self.assertEqual(2, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_executions").fetchone()[0])
        self.assertEqual(2, self.ledger._connection.execute("SELECT COUNT(*) FROM capacity_completions").fetchone()[0])
        # Completion/release transport is deliberately not invented by a local
        # fixture result. Unknown central completion must remain charged.
        self.assertEqual(2, self.ledger._connection.execute("SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
