"""B7a wire/terminal integration. PRIMARY ONLY: opens Unix sockets.

Real history store/service and framed transport. Native enrollment and unrelated
collector/account status are synthetic; these tests do not prove native enrollment.
"""
import json
import socket
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
from tests import test_deadlines_retention as dr
from tests import test_holds_report as unit
from codex_agents.history_api import MAX_HISTORY_REPLY_BYTES


class HoldsSocketTests(dr.LeaseBackedFixture):
    def setUp(self):
        super().setUp()
        self.bound()
        self.publish()
        self.service = unit.service_for(self.store)
        self.addCleanup(self.service.close)
        short = tempfile.TemporaryDirectory(dir=SHORT_TMP)
        self.addCleanup(short.cleanup)
        self.endpoint = Path(short.name) / 'holds.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(str(self.endpoint))
        self.listener.listen(4)
        self.listener.settimeout(.05)
        self.requests = []

    def terminal(self, code):
        program = 'import sys\nsys.path.insert(0, sys.argv[1])\n' + code
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', program,
            str(ORCHESTRATION_ROOT), str(self.endpoint), unit.PROJECT_A, unit.RUNTIME],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 15
            while process.poll() is None:
                self.assertLess(time.monotonic(), deadline, 'terminal exceeded bounded exchanges')
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(2)
                    raw = b''
                    while not raw.endswith(b'\n'):
                        chunk = connection.recv(16385 - len(raw))
                        self.assertTrue(chunk)
                        raw += chunk
                        self.assertLessEqual(len(raw), 16384)
                    self.requests.append(json.loads(raw))
                    reply = self.service.handle(raw[:-1], unit.TOKEN)
                    self.assertLessEqual(len(reply), MAX_HISTORY_REPLY_BYTES)
                    connection.sendall(reply + b'\n')
            stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(0, process.returncode, stdout + stderr)
            return json.loads(stdout)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=2)

    def test_B7_1_2_3_6_real_wire_new_op_and_project_runtime_denial(self):
        value = self.terminal('''
from pathlib import Path
from codex_agents.history_api import HistoryClient
from codex_agents.history_contracts import HistoryError
import json
client = HistoryClient(Path(sys.argv[2]), sys.argv[3], sys.argv[4], timeout=2)
value = client.holds_report()
for project, runtime in [('prj_' + 'b' * 32, sys.argv[4]), (sys.argv[3], 'b' * 64)]:
    try:
        HistoryClient(Path(sys.argv[2]), project, runtime, timeout=2).holds_report()
    except HistoryError as error:
        assert error.reason_code == 'history_not_authorized'
    else:
        raise AssertionError('foreign scope accepted')
print(json.dumps(value))
''')
        self.assertEqual(unit.report(self.store), value)
        self.assertEqual(['history.holds_report'] * 3, [r['operation'] for r in self.requests])

    def test_B7_1_2_3_6_learning_status_from_plain_terminal(self):
        value = self.terminal('''
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import cli
from codex_agents import client_runtime
endpoint, project, runtime = Path(sys.argv[2]), sys.argv[3], sys.argv[4]
# Synthetic enrolled primary; the real holds client, protocol and service remain.
scope = SimpleNamespace(project_id=project, runtime_digest=runtime)
with mock.patch.object(cli, '_require_protected_runtime'), \\
     mock.patch.object(cli, '_require_own_roots'), \\
     mock.patch.object(cli, '_start_collector_quietly'), \\
     mock.patch.object(cli, 'status_client', return_value={'ready': True}), \\
     mock.patch.object(cli, 'capacity_report_client', return_value=None), \\
     mock.patch.object(client_runtime, 'read_runtime_hint', return_value=(scope, None, endpoint)), \\
     mock.patch.object(client_runtime, 'enroll_clients'):
    sys.argv = ['cli.py', 'learning-status', '--state-root', str(endpoint.parent), '--project-root', str(endpoint.parent)]
    raise SystemExit(cli.main())
''')
        for name, expected in unit.report(self.store).items():
            self.assertEqual(expected, value[name])
        for private in (unit.PROJECT_A, unit.PROJECT_B, 'PRIVATE', '.context-', '.result-', str(self.endpoint)):
            self.assertNotIn(private, json.dumps(value))
        # B4t D2.8: learning-status also reads the one new project read for its hand-over lines.
        self.assertEqual(['history.holds_report', 'history.takeover_report'], [r['operation'] for r in self.requests])


if __name__ == '__main__':
    unittest.main()
