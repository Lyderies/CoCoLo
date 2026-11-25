"""RS-B-4 real SIGKILL checkpoints and a real hung flock owner."""
from contextlib import closing
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import unittest

import tests.support
from tests.test_b6_unattended_close import KEY
from codex_agents.project_history import ProjectHistory
from codex_agents.orchestration_api import OrchestrationLimits
from codex_agents.result_contracts import ResultCapacity, MAX_RESULT_BODY_BYTES
from codex_agents import project_holds_schema as schema

requirements = __import__('test_framework').requirements


class MemoryCrashTests(unittest.TestCase):
    def start(self, stage):
        process = subprocess.Popen([sys.executable, '-B', '-m', 'tests.memory_holds_fixture', stage],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop, process)
        self.assertTrue(select.select([process.stdout], [], [], 25)[0], 'checkpoint did not arrive')
        line = process.stdout.readline()
        self.assertTrue(line, process.stderr.read().decode() if process.poll() is not None else 'child exited')
        return process, json.loads(line)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)

    def open(self, data):
        return ProjectHistory(Path(data['directory']), data['project'], result_key=KEY,
            result_clock_ms=lambda: data.get('now_ms',1000), orchestration_limits=OrchestrationLimits(8, 12),
            result_capacity=ResultCapacity(8, 4 * MAX_RESULT_BODY_BYTES, 60_000, 120_000))

    @requirements('RS-B-4', why_not_scenario='Real executable crash and flock evidence test store custody without service sockets.')
    def test_M1_transition_kills(self):
        from codex_agents import memory_holds as mh
        for stage in ('registration_transaction', 'registration_commit', 'before_submit', 'discard', 'retire'):
            with self.subTest(stage=stage):
                process, data = self.start(stage)
                self.stop(process)
                self.assertEqual(-9, process.returncode)
                with closing(self.open(data)) as store:
                    rows = store._connection.execute("SELECT kind,state FROM project_holds WHERE kind IN "
                        "('driver_entry','preparation')").fetchall()
                    self.assertEqual(0 if stage == 'registration_transaction' else
                                     1 if stage == 'registration_commit' else 2, len(rows))
                    mh.recover(store, Path(data['directory']).parent / 'launches', 'current')
                    rows = store._connection.execute("SELECT state,release_kind FROM project_holds WHERE kind IN "
                        "('driver_entry','preparation')").fetchall()
                    self.assertTrue(all(tuple(row) == ('released', 'service_stopped') for row in rows))
                    self.assertTrue(schema.register_consistent(store._connection))

    @requirements('RS-B-4', why_not_scenario='Real executable crash and flock evidence test store custody without service sockets.')
    def test_M1_entry_deleted_before_release_record(self):
        from codex_agents import memory_holds as mh
        process,data = self.start('entry_delete')
        self.stop(process)
        with closing(self.open(data)) as store:
            mh.recover(store,Path(data['directory']).parent/'launches','current')
            rows = {kind:release for kind,release in store._connection.execute(
                "SELECT kind,release_kind FROM project_holds WHERE kind IN ('driver_entry','preparation')")}
            self.assertEqual({'driver_entry':'service_stopped','preparation':'retired'},rows)
            self.assertTrue(schema.register_consistent(store._connection))

    @requirements('RS-B-4', why_not_scenario='Real executable crash and flock evidence test store custody without service sockets.')
    def test_M4_hung_and_missing_lock(self):
        from codex_agents import memory_holds as mh
        process, data = self.start('hung')
        launches = Path(data['directory']).parent / 'launches'
        with closing(self.open(data)) as store:
            for missing in (False, True):
                if missing:
                    (launches / (data['instance'] + '.lock')).unlink()
                mh.recover(store, launches, 'current')
                row = store._connection.execute("SELECT state,evidence FROM project_holds WHERE kind='driver_entry'").fetchone()
                self.assertEqual('held', row[0])
                self.assertEqual('instance_unproven', json.loads(row[1])['reason'])
            # A replacement inode also cannot stand in for the original hung owner's lock.
            replacement = launches / (data['instance'] + '.lock')
            descriptor = os.open(replacement,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            os.close(descriptor)
            mh.recover(store,launches,'current')
            self.assertEqual('held',store._connection.execute("SELECT state FROM project_holds WHERE kind='driver_entry'").fetchone()[0])
            self.stop(process)
            replacement.unlink()
            mh.recover(store, launches, 'current')
            self.assertEqual('held', store._connection.execute("SELECT state FROM project_holds WHERE kind='driver_entry'").fetchone()[0])

    def fence_successor(self,data,lease):
        import time
        from unittest import mock
        from tests.test_memory_holds_lane import MemoryLaneTests
        from tests.test_project_runtime import limits
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.project_driver import RunDriver
        from codex_agents.project_runtime import ProjectRuntime
        from codex_agents.orchestration_api import OrchestrationScope, SCOPE_FIELDS
        scope = OrchestrationScope(**{key:getattr(lease.binding,key) for key in SCOPE_FIELDS})
        closure = MemoryLaneTests.closure(self,lease)
        runtime = ProjectRuntime(scope,lambda:self.open(data),limits())
        driver = RunDriver(CollectorPaths(Path(data['state_root'])),Path(data['project_root']),
            Path(data['bundle_root']),scope,runtime)
        try:
            with mock.patch.object(driver,'_fence_earlier_call',return_value={
                    'receipt':closure['denial'],'closure':closure}) as fence:
                end = time.monotonic()+10
                while lease.binding.run_handle not in driver._fenced:
                    runtime.run_once()
                    driver.step()
                    self.assertLess(time.monotonic(),end,'successor fence did not complete')
                    time.sleep(.002)
                self.assertEqual(1,fence.call_count)
        finally:
            driver.close()
            runtime.request_close()
            end = time.monotonic()+10
            while not runtime.threads_stopped():
                self.assertLess(time.monotonic(),end,'successor did not stop')
                time.sleep(.002)
            driver.settle_on_exit()

    @requirements('RS-B-4', why_not_scenario='Real executable crash and flock evidence test store custody without service sockets.')
    def test_L7_handoff_kills(self):
        from codex_agents import memory_holds as mh
        for stage in ('handoff', 'detach'):
            with self.subTest(stage=stage):
                process, data = self.start(stage)
                self.stop(process)
                with closing(self.open(data)) as store:
                    mh.recover(store, Path(data['directory']).parent / 'launches', 'current')
                    row = store._connection.execute("SELECT state,release_kind FROM project_holds WHERE kind='driver_entry'").fetchone()
                    self.assertEqual(('released', 'service_stopped'), tuple(row))
                    lease = store._connection.execute("SELECT state,lease_case,evidence FROM project_holds WHERE kind='lease'").fetchone()
                    self.assertEqual(('held', 'armed'), tuple(lease[:2]))
                    self.assertEqual('sweep', json.loads(lease[2])['instance'])
                    self.assertTrue(schema.register_consistent(store._connection))
                    original = store._lease(store._connection.execute('SELECT * FROM result_leases').fetchone())
                self.fence_successor(data,original)
                with closing(self.open(data)) as store:
                    row = store._connection.execute("SELECT state,release_kind FROM project_holds WHERE kind='lease'").fetchone()
                    self.assertEqual(('released','fenced'),tuple(row))
                    self.assertTrue(schema.register_consistent(store._connection))


if __name__ == '__main__':
    unittest.main()
