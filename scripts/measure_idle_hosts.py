#!/usr/bin/env python3
"""Measure real disposable collector/project host process CPU after readiness.

Run with the test interpreter and a short TMPDIR. No live account/service is used.
Supply --budget only after recording a baseline; CPU excludes startup and shutdown.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time

# The monitor reports this process's own user/system CPU, not its parent's or
# signature subprocesses'. The actual host entry still runs on its main thread.
MONITORED_HOST = r'''
import json, resource, runpy, sys, threading, time
entry, *arguments = sys.argv[1:]
def sample():
    for line in sys.stdin:
        if line.strip() != 'sample':
            return
        usage = resource.getrusage(resource.RUSAGE_SELF)
        print(json.dumps({'sample': True, 'wall': time.monotonic(),
                          'user': usage.ru_utime, 'system': usage.ru_stime}), flush=True)
threading.Thread(target=sample, daemon=True).start()
sys.argv = [entry, *arguments]
runpy.run_path(entry, run_name='__main__')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--budget', type=float, help='Maximum user+system CPU seconds per idle wall second')
    args = parser.parse_args()
    if args.seconds < 10 or (args.budget is not None and args.budget <= 0):
        parser.error('seconds must be >= 10 and budget must be positive')
    repository = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repository / 'packages' / 'codex-agents'))
    from tests.support import ORCHESTRATION_ROOT, SHORT_TMP
    from tests.learning_support import generate_test_owner_key
    from codex_agents.collector_client import CollectorClient
    from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
    from codex_agents.project_host_client import project_host_serving

    def sample(process):
        process.stdin.write(b'sample\n')
        process.stdin.flush()
        if not select.select([process.stdout], [], [], 5)[0]:
            raise RuntimeError('host CPU sampler did not answer')
        result = json.loads(process.stdout.readline())
        if result.get('sample') is not True:
            raise RuntimeError('unexpected host output during idle sampling')
        return result

    def ready(process, check):
        end = time.monotonic() + 30
        while process.poll() is None and time.monotonic() < end:
            try:
                if check():
                    return
            except Exception:
                pass
            time.sleep(.01)
        raise RuntimeError('disposable host did not become ready (check socket/sandbox access)')

    with tempfile.TemporaryDirectory(dir=SHORT_TMP, prefix='s13d') as temporary, contextlib.ExitStack() as stack:
        base = Path(temporary).resolve()
        state = LearningState(base / 's')
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / 'cli.py')
        _, public = generate_test_owner_key(base)
        state.install_owner_public_key(public)
        project = base / 'p'
        (project / '.git').mkdir(parents=True)
        ledger = LearningLedger(state)
        try:
            ledger.register_project(project)
        finally:
            ledger.close()

        def start(entry, *arguments):
            errors = stack.enter_context(open(base / (entry + '.stderr'), 'wb'))
            process = subprocess.Popen(
                [sys.executable, '-I', '-B', '-S', '-c', MONITORED_HOST,
                 str(ORCHESTRATION_ROOT / entry), '--state-root', str(state.root), *arguments],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, cwd='/',
                env={'PATH': os.defpath, 'TMPDIR': SHORT_TMP, 'xcrun_nocache': '1'}, start_new_session=True)
            def stop():
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(5)
                process.stdin.close()
                process.stdout.close()
            stack.callback(stop)
            return process

        collector = start('collector_host.py')
        ready(collector, lambda: CollectorClient(state.socket, timeout=1).health().get('ok'))
        host = start('project_host.py', '--project-root', str(project))
        ready(host, lambda: project_host_serving(state.root, project))
        # Both hosts are ready and warmed before either measurement starts.
        time.sleep(1)
        before = {name: sample(process) for name, process in [('collector', collector), ('project', host)]}
        time.sleep(args.seconds)
        failed = False
        for name, process in [('collector', collector), ('project', host)]:
            after = sample(process)
            elapsed = after['wall'] - before[name]['wall']
            user = after['user'] - before[name]['user']
            system = after['system'] - before[name]['system']
            rate = (user + system) / elapsed
            print(json.dumps({'host': name, 'idle_seconds': elapsed, 'user_seconds': user,
                              'system_seconds': system, 'cpu_seconds_per_idle_second': rate}, sort_keys=True))
            failed |= args.budget is not None and rate > args.budget
        return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
