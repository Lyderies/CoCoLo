"""Plain-terminal B7b output over real framed project history transport."""
from tests import test_holds_report_socket as wire
from test_framework import requirements


class RecoveryTerminalTests(wire.HoldsSocketTests):
    @requirements('RS-B-B7-4', 'RS-B-B7-6', why_not_scenario='Plain terminal exercises real project report/client/CLI; enrollment and unrelated account services are synthetic.')
    def test_learning_status_recovery_from_plain_terminal(self):
        # The inherited terminal test compares every report section, including recovery_pending,
        # and refuses foreign project/runtime access through the real history operation.
        self.assertIn('recovery_pending', wire.unit.report(self.store))
        self.test_B7_1_2_3_6_learning_status_from_plain_terminal()


class PlainTerminalOutputTests(__import__('unittest').TestCase):
    @requirements('RS-B-B7-4', 'RS-B-B7-6', why_not_scenario='Plain terminal exercises real project report/client/CLI; enrollment and unrelated account services are synthetic.')
    def test_plain_terminal_output_without_socket_privilege(self):
        import json
        import subprocess
        import sys
        from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
        program = '''
import sys
sys.path.insert(0, sys.argv[1])
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
import cli
from codex_agents import client_runtime, history_api
from codex_agents.project_history import ProjectHistory
from codex_agents.history_service import HistoryService, HistoryAuthorization
from codex_agents.history_api import history_request_digest
project, runtime = 'prj_' + 'a' * 32, 'a' * 64
with tempfile.TemporaryDirectory(dir=sys.argv[2]) as root:
    directory = Path(root) / 'history'
    directory.mkdir(mode=0o700)
    store = ProjectHistory(directory, project)
    service = HistoryService(project, runtime, lambda: store, close_history=False,
        authorizer=lambda request, token: HistoryAuthorization(project, runtime,
            history_request_digest(request), 'host', frozenset({'history.holds_report'})))
    scope = SimpleNamespace(project_id=project, runtime_digest=runtime)
    with mock.patch.object(cli, '_require_protected_runtime'), mock.patch.object(cli, '_require_own_roots'), \\
         mock.patch.object(cli, '_start_collector_quietly'), \\
         mock.patch.object(cli, 'status_client', return_value={'ready': True}), \\
         mock.patch.object(cli, 'capacity_report_client', return_value=None), \\
         mock.patch.object(client_runtime, 'read_runtime_hint', return_value=(scope, None, directory / 'unused.sock')), \\
         mock.patch.object(client_runtime, 'enroll_clients'), \\
         mock.patch.object(history_api, 'exchange_frame', side_effect=lambda endpoint, raw, **kw: service.handle(raw, None)):
        sys.argv = ['cli.py', 'learning-status', '--state-root', root, '--project-root', root]
        assert cli.main() == 0
    service.close()
    store.close()
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-S', '-c', program,
                                 str(ORCHESTRATION_ROOT), str(TEMP_ROOT)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual({'reasons': dict.fromkeys(('needs_settlement', 'needs_process_evidence', 'has_holds',
                           'stop_failed', 'account_unreachable'), 0), 'stale': True}, value['recovery_pending'])
        self.assertNotIn('prj_' + 'a' * 32, result.stdout)
