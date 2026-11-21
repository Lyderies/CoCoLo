"""R4 (probe-reuse analysis, T5): concurrent opens on a cold project all open.

Two CLI processes ask for an orchestration on a project whose service is not running. Both must
be answered by the one service that starts; neither may report the service unavailable.
"""
import subprocess
import threading
import unittest

from tests import test_project_service_startup as startup


class ColdStartConcurrentOpenTests(unittest.TestCase):
    setUp = startup.ProjectServiceStartupTests.setUp
    hosts = startup.ProjectServiceStartupTests.hosts
    stop_hosts = startup.ProjectServiceStartupTests.stop_hosts
    env = startup.ProjectServiceStartupTests.__dict__['env']
    cli_command = startup.ProjectServiceStartupTests.cli_command

    def test_T5_two_concurrent_opens_on_a_cold_project_both_open(self):
        for round_ in range(6):
            project = self.base / f'cold{round_}'
            (project / '.git').mkdir(parents=True)
            self.ledger.register_project(project)
            barrier, results = threading.Barrier(2), []

            def run(index):
                barrier.wait()
                done = subprocess.run(self.cli_command('learning-open-orchestration', '--request-id',
                    f'r4-{round_}-{index}', project=project), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=self.env(), timeout=40, check=False)
                results.append((done.returncode, (done.stdout + done.stderr).decode(errors='replace')))
            threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual([0, 0], sorted(code for code, _ in results), (round_, results))
            self.stop_hosts()


class LostStartWaitsForTheWinnerTests(unittest.TestCase):
    """Deterministic: a start whose host ended at once (it lost to another start's host, which holds no
    host lock yet) waits for that host instead of reporting the project unserved."""

    def run_start(self, reason, serves_after):
        import json
        import tempfile
        from pathlib import Path
        from unittest import mock
        from codex_agents import project_host_client as client
        calls = []

        def serving():
            calls.append(1)
            return len(calls) > serves_after
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as base:
            report_path = Path(base) / 'report.json'
            report_path.write_text(json.dumps({'ready': False, 'reason_code': reason}))
            state = mock.Mock(root=Path(base))
            with mock.patch.object(client.os, 'posix_spawn', return_value=4242), \
                    mock.patch.object(client.os, 'waitpid', return_value=(4242, 0)), \
                    mock.patch.object(client, '_host_alive', return_value=None), \
                    mock.patch.object(client, 'runtime_directory', return_value=Path(base)):
                return client._spawn_and_wait(state, Path('host.py'), Path(base), serving, 5, [], report_path, {})

    def test_T5_a_start_that_lost_before_the_winner_took_the_host_lock_waits_for_it(self):
        for reason in ('history_writer_busy', 'history_service_unavailable'):
            self.assertIs(True, self.run_start(reason, serves_after=40), reason)

    def test_T5_a_start_that_failed_for_another_reason_does_not_wait(self):
        self.assertIs(False, self.run_start('history_incompatible', serves_after=40))


if __name__ == '__main__':
    unittest.main()
