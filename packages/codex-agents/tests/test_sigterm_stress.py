"""O10 SD3 stress check (ANALYSIS-0.4.0-SD3-shutdown.md): start the real collector through the CLI,
send it SIGTERM at a random moment, and count the collectors still running 6 s later. Before SD3,
about 2-4 % of SIGTERMs were lost this way. Not part of the normal suite: it runs only when
COCOLO_SIGTERM_STRESS_SECONDS is set, for that many seconds, and fails if any collector outlived its
SIGTERM.

Run: COCOLO_SIGTERM_STRESS_SECONDS=300 python3 -B scripts/parallel_tests.py --module codex:tests.test_sigterm_stress
"""
import json
import os
import random
import signal
import time
import unittest

from tests import test_collector_exit as exit_tests

SECONDS = float(os.environ.get('COCOLO_SIGTERM_STRESS_SECONDS') or 0)


@unittest.skipUnless(SECONDS > 0, 'stress check: set COCOLO_SIGTERM_STRESS_SECONDS to run it')
class SigtermStress(exit_tests.CollectorExitProcessTests):
    # Only the stress loop runs here, not the inherited tests.
    for name in [n for n in dir(exit_tests.CollectorExitProcessTests) if n.startswith('test_')]:
        locals()[name] = None
    del name

    def test_SD3_no_sigterm_is_lost(self):
        end = time.monotonic() + SECONDS
        tries, stuck, exits = 0, [], []
        while time.monotonic() < end:
            self.ready_status()
            pids = self.processes('collector_host.py')
            if len(pids) != 1:
                continue
            time.sleep(random.uniform(0.0, 1.5))
            tries += 1
            sent = time.monotonic()
            os.kill(pids[0], signal.SIGTERM)
            while self.processes('collector_host.py') and time.monotonic() - sent < 6:
                time.sleep(.02)
            if self.processes('collector_host.py'):
                stuck.append({'pid': pids[0], **self.late_stop()})
                for pid in self.processes('collector_host.py'):
                    os.kill(pid, signal.SIGKILL)
                time.sleep(.5)
            else:
                exits.append(round(time.monotonic() - sent, 2))
        exits.sort()
        summary = {'tries': tries, 'stuck': len(stuck), 'exit_s_median': exits[len(exits) // 2] if exits else None,
                   'exit_s_max': exits[-1] if exits else None}
        print('\nSD3 STRESS ' + json.dumps(summary), flush=True)
        self.assertEqual([], stuck, json.dumps(summary))
