"""The in-process service harness never keeps a test process alive (O10 HT-1).

CI run 44705995602 hung 600 s at interpreter exit: a test's setUp failed after
start_services() had started the collector thread, unittest skips tearDown when
setUp fails, so nothing ever set the harness's stop event and the non-daemon
collector thread kept the interpreter waiting in threading._shutdown.
"""
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXIT_SECONDS = 20  # The child module runs in about a second; a hang is any wait past this.

# A module whose only test's setUp fails once the collector and account services run.
SETUP_FAILS_AFTER_START = textwrap.dedent("""
    import sys, unittest
    sys.path.insert(0, '.')
    sys.path.insert(0, 'tests')
    from tests.service_harness import ServiceHarness
    from tests.test_bridge import CliTestCase

    class SetUpFailsAfterStart(ServiceHarness, CliTestCase):
        def setUp(self):
            super().setUp()
            self.start_services(project_host=False)
            raise RuntimeError('set-up failed after the services started')

        def test_never_runs(self):
            pass

    unittest.main(module='__main__', argv=['harness-exit'])
""")


class HarnessExitTests(unittest.TestCase):
    def run_module(self, code):
        try:
            return subprocess.run([sys.executable, '-B', '-c', code], cwd=PACKAGE_ROOT, capture_output=True,
                                  text=True, timeout=EXIT_SECONDS)
        except subprocess.TimeoutExpired as expired:
            output = (expired.stderr or b'')
            output = output.decode(errors='replace') if isinstance(output, bytes) else output
            self.fail('HT-1: the test process did not end within %d s of its last test; a harness '
                      'thread kept the interpreter alive.\n%s' % (EXIT_SECONDS, output[-2000:]))

    def test_HT1_a_setup_that_fails_after_the_services_start_still_lets_the_process_end(self):
        completed = self.run_module(SETUP_FAILS_AFTER_START)
        # The failing setUp is reported as an error, and the process exits by itself.
        self.assertIn('set-up failed after the services started', completed.stderr)
        self.assertIn('FAILED (errors=1)', completed.stderr)
        self.assertEqual(1, completed.returncode, completed.stderr[-2000:])


if __name__ == '__main__':
    unittest.main()
