"""O10 SD2: a collector's shutdown ends within its bound whatever a step does, and its exit record
names the step it was stuck in (PLAN SD2-1 to SD2-3).

Integration stage (a real timer thread, sub-second bounds): the watchdog with its exit replaced, so
nothing here ends the test process.
"""
import tempfile
import threading
import time
import unittest

from tests.support import TEMP_ROOT

from codex_agents import collector_exit
from codex_agents.shutdown_watchdog import STOP_STEPS, ShutdownWatchdog


class ShutdownWatchdogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        from pathlib import Path
        self.root = Path(temporary.name)
        self.ended = threading.Event()
        self.codes = []

    def exit(self, code):
        self.codes.append(code)
        self.ended.set()

    def test_SD2_1_a_step_that_never_returns_still_ends_the_process_within_the_bound(self):
        watchdog = ShutdownWatchdog(self.root, started_at_ms=5, bound_seconds=0.3, exit=self.exit)
        started = time.monotonic()
        watchdog.step('ledger_close')  # and it never finishes
        self.assertTrue(self.ended.wait(5))
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual([ShutdownWatchdog.FORCED_EXIT_CODE], self.codes)

    def test_SD2_2_the_record_names_the_step_it_was_stuck_in(self):
        watchdog = ShutdownWatchdog(self.root, started_at_ms=5, bound_seconds=0.2, exit=self.exit)
        watchdog.step('account_close')
        watchdog.step('server_stop')
        self.assertTrue(self.ended.wait(5))
        record = collector_exit.read_exit(self.root)
        self.assertEqual('stop_forced', record['cause'])
        self.assertEqual('server_stop', record['stalled_in'])

    def test_SD2_1_a_shutdown_that_finishes_in_time_is_left_alone(self):
        watchdog = ShutdownWatchdog(self.root, started_at_ms=5, bound_seconds=0.3, exit=self.exit)
        for name in STOP_STEPS:
            watchdog.step(name)
        watchdog.done()
        self.assertFalse(self.ended.wait(0.6))
        self.assertEqual([], self.codes)

    def test_SD2_2_only_a_known_step_is_recorded(self):
        collector_exit.write_exit(self.root, started_at_ms=5, cause='stop_forced', error=None, stalled_in='elsewhere/x')
        self.assertNotIn('stalled_in', collector_exit.read_exit(self.root) or {})
        collector_exit.write_exit(self.root, started_at_ms=5, cause='stop_forced', error=None, stalled_in='ledger_close')
        self.assertEqual('ledger_close', collector_exit.read_exit(self.root)['stalled_in'])

    def test_SD2_the_collector_host_times_every_stop_step_with_the_watchdog(self):
        import ast
        from tests.support import ORCHESTRATION_ROOT
        tree = ast.parse((ORCHESTRATION_ROOT / 'collector_host.py').read_text())
        named = {node.args[0].value for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == 'step'
                 and node.args and isinstance(node.args[0], ast.Constant)}
        self.assertEqual(set(STOP_STEPS), named)


if __name__ == '__main__':
    unittest.main()
