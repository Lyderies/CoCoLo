"""Isolated unittest worker and sticky layer guards (TF-1, TF-5, TF-9..13)."""
from __future__ import annotations

import faulthandler
import json
import os
from pathlib import Path
import select
import sys
import threading
import time
import traceback
import unittest


class Guard:
    def __init__(self, layer, module):
        self.layer, self.module = layer, module
        self.test = None
        self.violations = []

    def deny(self, operation):
        message = f'{self.layer} test {self.module} {self.test or "import"} must not {operation}'
        self.violations.append({'test': self.test, 'operation': operation})
        raise RuntimeError(message)

    def install(self):
        if self.layer not in ('unit', 'component'):
            return
        processes = {'subprocess.Popen', 'os.posix_spawn', 'os.spawn', 'os.exec', 'os.fork', 'os.forkpty', 'os.system'}
        sockets = {'socket.__new__', 'socket.connect', 'socket.bind', 'socket.getaddrinfo',
                   'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.sendto'}
        def audit(event, args):
            if event in processes:
                self.deny('start a process (' + event + ')')
            if event in sockets:
                self.deny('use a socket (' + event + ')')
        sys.addaudithook(audit)
        if self.layer != 'unit':
            return
        def no_sleep(seconds):
            self.deny('sleep')
        time.sleep = no_sleep
        real_wait, real_join, real_select = threading.Condition.wait, threading.Thread.join, select.select
        def wait(condition, timeout=None):
            if timeout is None or timeout > 0:
                self.deny('block on a condition/event wait')
            return real_wait(condition, timeout)
        def join(thread, timeout=None):
            if thread.is_alive() and (timeout is None or timeout > 0):
                self.deny('block on thread join')
            return real_join(thread, timeout)
        def poll_select(read, write, error, timeout=None):
            ready = real_select(read, write, error, 0)
            if timeout != 0 and not any(ready):
                self.deny('block on select')
            return ready
        threading.Condition.wait, threading.Thread.join, select.select = wait, join, poll_select
        for name in ('poll', 'epoll'):
            if not hasattr(select, name):
                continue
            factory = getattr(select, name)
            guard = self
            class Poll:
                def __init__(self, *args, _factory=factory, **kwargs):
                    self.inner = _factory(*args, **kwargs)
                def __getattr__(self, name):
                    return getattr(self.inner, name)
                def poll(self, timeout=None, *args, **kwargs):
                    ready = self.inner.poll(0, *args, **kwargs)
                    if timeout != 0 and not ready:
                        guard.deny('block on poll')
                    return ready
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.inner.close()
            setattr(select, name, Poll)
        if hasattr(select, 'kqueue'):
            factory = select.kqueue
            guard = self
            class Kqueue:
                def __init__(self):
                    self.inner = factory()
                def __getattr__(self, name):
                    return getattr(self.inner, name)
                def control(self, changelist, max_events, timeout=None):
                    ready = self.inner.control(changelist, max_events, 0)
                    if max_events and timeout != 0 and not ready:
                        guard.deny('block on kqueue')
                    return ready
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.inner.close()
            select.kqueue = Kqueue


class Result(unittest.TextTestResult):
    def __init__(self, *args, guard, **kwargs):
        super().__init__(*args, **kwargs)
        self.guard, self.outcomes = guard, {}

    def startTest(self, test):
        self.guard.test = test.id()
        super().startTest(test)

    def addSuccess(self, test):
        self.outcomes.setdefault(test.id(), 'passed')
        super().addSuccess(test)

    def addError(self, test, err):
        self.outcomes[test.id()] = 'error'
        super().addError(test, err)

    def addFailure(self, test, err):
        self.outcomes[test.id()] = 'failed'
        super().addFailure(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.outcomes[test.id()] = 'failed'
        super().addSubTest(test, subtest, err)

    def addSkip(self, test, reason):
        self.outcomes[test.id()] = 'skipped'
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.outcomes[test.id()] = 'expected_failure'
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.outcomes[test.id()] = 'unexpected_success'
        super().addUnexpectedSuccess(test)


def main(names=None):
    names = sys.argv[1:] if names is None else names
    destination = os.environ.get('COCOLO_TEST_RESULTS')
    faulthandler.dump_traceback_later(float(os.environ.get('COCOLO_WORKER_TIMEOUT', '600')), exit=True)
    from test_profiles import activate
    activate()
    # A nested legacy unit probe sets STAGE but may inherit its parent's layer.
    # The unit stage must always impose unit restrictions, never inherit broader permission.
    layer = 'unit' if os.environ.get('COCOLO_TEST_STAGE') == 'unit' else os.environ.get('COCOLO_TEST_LAYER', 'scenario')
    if layer not in ('unit', 'component', 'crash', 'scenario', 'soak'):
        raise ValueError('unknown worker layer')
    guard = Guard(layer, os.environ.get('COCOLO_TEST_MODULE', ','.join(names)))
    guard.install()
    sys.path.insert(0, '.')
    sys.path.insert(0, 'tests')
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    except Exception:
        traceback.print_exc()
        if destination:
            Path(destination).write_text(json.dumps({'tests': 0, 'outcomes': {name: 'error' for name in names},
                                                     'violations': guard.violations}) + '\n')
        faulthandler.cancel_dump_traceback_later()
        return 1
    result = unittest.TextTestRunner(resultclass=lambda *a, **k: Result(*a, guard=guard, **k)).run(suite)
    for violation in guard.violations:
        if violation['test']:
            result.outcomes[violation['test']] = 'failed'
        print(f'{layer} test {guard.module}: prohibited {violation["operation"]}', file=sys.stderr)
    if destination:
        Path(destination).write_text(json.dumps({'tests': result.testsRun, 'outcomes': result.outcomes,
                                                 'violations': guard.violations}) + '\n')
    faulthandler.cancel_dump_traceback_later()
    return 0 if result.wasSuccessful() and not guard.violations else 1
