"""0.3.3 C10 and C11: a collector that stops says why, and an empty account reply says so.

Found in a live soak on 0.8.27: the collector restarted by itself,
calls in flight got an empty reply, the client called it an invalid reply, and nothing
recorded why the collector stopped.
"""
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT  # noqa: F401  (bootstraps the import path)
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

import tests.test_account_host as host_tests

from codex_agents import collector_exit
from codex_agents.account_api import AccountClient, AccountError, AccountRequest, decode_account_response
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, CollectorClient

# A location in COCOLO or Python, or `<other>` for any other file (C10a).
WHERE = r"(?:[A-Za-z0-9_.-]{1,64}\.py|<other>):[0-9]{1,6} [A-Za-z_<>][A-Za-z0-9_<>]{0,63}"


class ExitRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_H1c_3_a_self_restart_is_recorded_with_its_fixed_cause_only(self):
        collector_exit.write_exit(self.root, started_at_ms=5, cause='account_restart', error=None,
                                  detail='ingress_cleanup_failed')
        record = collector_exit.read_exit(self.root)
        self.assertEqual(('account_restart', 'ingress_cleanup_failed', []),
                         (record['cause'], record['error'], record['where']))
        collector_exit.write_exit(self.root, started_at_ms=5, cause='account_restart', error=None,
                                  detail='not a fixed cause /x')
        self.assertIsNone(collector_exit.read_exit(self.root)['error'])

    def test_C10_a_record_round_trips_owner_only(self):
        collector_exit.write_exit(self.root, started_at_ms=5, cause='error', error=RuntimeError('a secret value'))
        record = collector_exit.read_exit(self.root)
        self.assertEqual(('error', 'RuntimeError', 5), (record['cause'], record['error'], record['started_at_ms']))
        self.assertNotIn('secret', json.dumps(record))
        self.assertEqual(0o600, os.stat(collector_exit.exit_path(self.root)).st_mode & 0o777)

    def test_C10_a_missing_or_malformed_record_reads_as_none(self):
        self.assertIsNone(collector_exit.read_exit(self.root))
        for content in (b'', b'{', b'[]', json.dumps({'cause': 'error', 'error': 'x' * 300}).encode(),
                        json.dumps({'schema_version': 1, 'cause': 'nonsense'}).encode()):
            collector_exit.exit_path(self.root).write_bytes(content)
            self.assertIsNone(collector_exit.read_exit(self.root), content[:40])

    def test_C10_every_cause_without_an_error_has_no_locations(self):
        for cause in ('signal', 'requested', 'state_removed', 'thread_ended'):
            collector_exit.write_exit(self.root, started_at_ms=None, cause=cause, error=None)
            record = collector_exit.read_exit(self.root)
            self.assertEqual((cause, None, []), (record['cause'], record['error'], record['where']))


    # C10a, from the COCOLO review.

    def test_C10a_a_fifo_or_symlink_reads_as_none_without_blocking(self):
        path = collector_exit.exit_path(self.root)
        os.mkfifo(path, 0o600)
        started = time.monotonic()
        self.assertIsNone(collector_exit.read_exit(self.root))
        self.assertLess(time.monotonic() - started, 2)
        path.unlink()
        real = self.root / 'real.json'
        collector_exit.write_exit(self.root, started_at_ms=1, cause='signal', error=None)
        collector_exit.exit_path(self.root).rename(real)
        collector_exit.exit_path(self.root).symlink_to(real)
        self.assertIsNone(collector_exit.read_exit(self.root))

    def test_C10a_a_file_others_can_read_is_not_trusted(self):
        collector_exit.write_exit(self.root, started_at_ms=1, cause='signal', error=None)
        os.chmod(collector_exit.exit_path(self.root), 0o644)
        self.assertIsNone(collector_exit.read_exit(self.root))

    def test_C10a_names_from_outside_cocolo_and_python_are_not_recorded(self):
        namespace = {}
        exec(compile("class SyntheticSecret42(Exception):\n    pass\n"
                     "def raise_it():\n    raise SyntheticSecret42('value')\n",
                     '/synthetic/private/SyntheticTenant42.py', 'exec'), namespace)
        try:
            namespace['raise_it']()
        except Exception as error:
            collector_exit.write_exit(self.root, started_at_ms=1, cause='error', error=error)
        record = collector_exit.read_exit(self.root)
        text = json.dumps(record)
        self.assertEqual('Exception', record['error'])
        self.assertNotIn('Synthetic', text)
        self.assertIn('<other>', ' '.join(record['where']))

    def test_C10a_locations_are_read_without_opening_source_files(self):
        import linecache
        try:
            raise ValueError('x')
        except ValueError as error:
            with unittest.mock.patch.object(linecache, 'getline', side_effect=AssertionError('source read')), \
                    unittest.mock.patch.object(linecache, 'updatecache', side_effect=AssertionError('source read')):
                collector_exit.write_exit(self.root, started_at_ms=1, cause='error', error=error)
        self.assertEqual('ValueError', collector_exit.read_exit(self.root)['error'])

    def test_C10a_a_malformed_record_of_the_right_fields_is_none(self):
        base = {'schema_version': 1, 'stopped_at_ms': 1, 'started_at_ms': None, 'error': None, 'where': []}
        for cause in ([], {}, 3, None):
            self.assertIsNone(collector_exit.checked({**base, 'cause': cause}))
        for version in (True, 1.0, '1'):
            self.assertIsNone(collector_exit.checked({**base, 'cause': 'signal', 'schema_version': version}))

    # C10b, from the second COCOLO review.

    def test_C10b_only_mode_0600_is_trusted(self):
        for mode in (0o400, 0o700, 0o640):
            collector_exit.write_exit(self.root, started_at_ms=1, cause='signal', error=None)
            os.chmod(collector_exit.exit_path(self.root), mode)
            self.assertIsNone(collector_exit.read_exit(self.root), oct(mode))

    def test_C10b_a_class_claiming_a_trusted_module_is_not_trusted(self):
        for claimed in ('json', 'codex_agents_extra', 'codex_agents.collector_exit'):
            forged = type('SyntheticSecret42', (Exception,), {'__module__': claimed})
            try:
                raise forged('value')
            except Exception as error:
                collector_exit.write_exit(self.root, started_at_ms=1, cause='error', error=error)
            self.assertEqual('Exception', collector_exit.read_exit(self.root)['error'], claimed)

    def test_C10b_a_third_party_package_under_the_python_folder_is_other(self):
        # A Python whose site-packages is a real folder inside its library (as /usr/local/lib/python3.x
        # on Linux), built here so the check does not depend on this machine's layout.
        library = self.root / 'lib' / 'python3'
        for folder in ('site-packages', 'dist-packages'):
            (library / folder).mkdir(parents=True)
            (library / folder / 'SyntheticTenant42.py').write_text('')
        (library / 'json.py').write_text('')
        with unittest.mock.patch.object(collector_exit, '_STDLIB', library.resolve()):
            self.assertFalse(collector_exit._trusted_file(str(library / 'site-packages' / 'SyntheticTenant42.py')))
            self.assertFalse(collector_exit._trusted_file(str(library / 'dist-packages' / 'SyntheticTenant42.py')))
            self.assertTrue(collector_exit._trusted_file(str(library / 'json.py')))

    def test_C10b_a_failed_replace_leaves_no_temporary_file(self):
        collector_exit.exit_path(self.root).mkdir()  # The record's name is a directory: replace fails.
        collector_exit.write_exit(self.root, started_at_ms=1, cause='signal', error=None)
        self.assertEqual(['collector-exit.json'], sorted(p.name for p in self.root.iterdir()))

    # C10c, from the third COCOLO review.

    def test_C10c_a_restrictive_umask_still_writes_a_readable_record(self):
        previous = os.umask(0o277)
        try:
            collector_exit.write_exit(self.root, started_at_ms=1, cause='signal', error=None)
        finally:
            os.umask(previous)
        self.assertEqual('signal', collector_exit.read_exit(self.root)['cause'])

    def test_C10c_a_file_under_cocolo_that_is_not_a_runtime_file_is_other(self):
        stray = Path(collector_exit._OWN_ROOT) / 'codex_agents' / 'SyntheticTenant42.py'
        self.assertFalse(collector_exit._trusted_file(str(stray)))
        self.assertTrue(collector_exit._trusted_file(collector_exit.__file__))

    def test_C10b_real_standard_library_and_cocolo_types_are_named(self):
        import sqlite3
        from codex_agents.account_api import AccountError as Own
        for error in (sqlite3.OperationalError('x'), Own('account_unavailable'), ValueError('x')):
            try:
                raise error
            except Exception as caught:
                collector_exit.write_exit(self.root, started_at_ms=1, cause='error', error=caught)
            self.assertEqual(type(error).__name__, collector_exit.read_exit(self.root)['error'])


class EmptyReplyTests(unittest.TestCase):
    def request(self):
        return AccountRequest(1, 'account.health', 'empty-1', 'prj_' + '0' * 32, COLLECTOR_RUNTIME_DIGEST, {})

    def test_C11_an_empty_reply_says_the_account_closed_without_replying(self):
        with self.assertRaises(AccountError) as refused:
            decode_account_response(b'', self.request())
        self.assertEqual('account_closed_without_reply', refused.exception.reason_code)

    def test_C11_a_present_but_invalid_reply_is_still_invalid(self):
        for raw in (b'{', b'[]', b'{"ready": true}', bytearray(), memoryview(b'')):
            with self.assertRaises(AccountError) as refused:
                decode_account_response(raw, self.request())
            self.assertEqual('account_invalid_response', refused.exception.reason_code, raw)

    def test_C11_an_account_that_closes_the_connection_is_reported_as_such(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / 'a.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        self.addCleanup(listener.close)

        def close_at_once():
            connection, _ = listener.accept()
            connection.recv(65536)
            connection.close()
        threading.Thread(target=close_at_once, daemon=True).start()
        client = AccountClient(path, 'prj_' + '0' * 32, COLLECTOR_RUNTIME_DIGEST, timeout=5)
        with self.assertRaises(AccountError) as refused:
            client.observe_provider('codex', request_id='empty-2')
        self.assertEqual('account_closed_without_reply', refused.exception.reason_code)


FALSE_WRAPPER = '''
import sys
sys.path.insert(0, sys.argv[1])
from codex_agents import account_host
class Falsy(SystemExit):
    def __bool__(self):
        raise RuntimeError("truth value of a secret")
calls = []
real = account_host.AccountServiceHost.serve_once
def failing(self):
    calls.append(1)
    if len(calls) == 50:
        raise Falsy(0)
    return real(self)
account_host.AccountServiceHost.serve_once = failing
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

THREAD_WRAPPER = '''
import sys
sys.path.insert(0, sys.argv[1])
from codex_agents import learning_store
def failing(self, connection):
    raise RuntimeError("a secret value from the collector thread")
learning_store.CollectorServer._handle = failing
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

RESTART_WRAPPER = '''
import sys
sys.path.insert(0, sys.argv[1])
from codex_agents import account_restart
account_restart.CHECK_EVERY_SECONDS = 0.05
account_restart.decide = lambda *args, **kwargs: {'action': 'restart', 'lane': 'ingress',
                                                  'cause': 'ingress_cleanup_failed', 'held': {}}
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

IDLE_WRAPPER = '''
import sys
sys.path.insert(0, sys.argv[1])
from codex_agents import account_restart
account_restart.CHECK_EVERY_SECONDS = 3600  # the check never runs during this test
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''

WRAPPER = '''
import sys
sys.path.insert(0, sys.argv[1])
from codex_agents import account_host
calls = []
real = account_host.AccountServiceHost.serve_once
def failing(self):
    calls.append(1)
    if len(calls) == 50:
        raise RuntimeError("a secret value from the loop")
    return real(self)
account_host.AccountServiceHost.serve_once = failing
sys.argv = [sys.argv[1] + "/collector_host.py", "--state-root", sys.argv[2]]
import runpy
runpy.run_path(sys.argv[0], run_name="__main__")
'''


class CollectorExitProcessTests(host_tests.AccountHostTests):
    """The real collector process: each way it stops is recorded, and the next one reports it."""

    def wait_gone(self):
        end = time.monotonic() + 20
        while self.processes('collector_host.py'):
            self.assertLess(time.monotonic(), end, 'collector did not stop')
            time.sleep(.05)

    def ready_status(self):
        """Start a collector through `learning-status` and wait until it is ready. Under load its
        account service can still be starting when the first status answers (C13g)."""
        end = time.monotonic() + 20
        while True:
            code, status = self.cli('learning-status', expected=None)
            # O10 ST1: a starting account now says so; both are retried.
            if (code, status.get('ready')) != (1, False) or status.get('reason_code') not in {
                    'status_account_unavailable', 'status_account_starting'}:
                self.assertEqual((0, True), (code, status.get('ready')), json.dumps(status))
                return status
            self.assertLess(time.monotonic(), end, json.dumps(status))  # Retries stop after 20 s.
            time.sleep(.2)

    def previous_exit(self):
        status = self.ready_status()  # Starts a new collector.
        return status.get('collector_previous_exit'), json.dumps(status)

    def test_S1_U9a_status_reports_how_long_each_start_up_phase_took(self):
        status = self.ready_status()
        phases = status.get('collector_startup_ms')
        self.assertEqual({'ledger_open', 'collector', 'account'}, set(phases), json.dumps(status))
        for name, value in phases.items():
            self.assertIs(int, type(value), name)
            self.assertGreaterEqual(value, 0, name)
        self.assertLess(sum(phases.values()), 60_000)  # milliseconds, not some other unit

    def test_C10_a_signal_is_recorded(self):
        self.ready_status()
        for pid in self.processes('collector_host.py'):
            os.kill(pid, signal.SIGTERM)
        self.wait_gone()
        record, _ = self.previous_exit()
        self.assertEqual('signal', record['cause'])

    def test_C10_a_shutdown_request_is_recorded(self):
        self.ready_status()
        CollectorClient(self.paths.socket, timeout=2).shutdown()
        self.wait_gone()
        record, _ = self.previous_exit()
        self.assertEqual('requested', record['cause'])

    def test_C10_an_error_in_the_account_loop_is_recorded_without_its_message(self):
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', WRAPPER, str(ORCHESTRATION_ROOT),
                                    str(self.state.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        process.wait(timeout=30)
        self.wait_gone()
        record, text = self.previous_exit()
        self.assertEqual(('error', 'RuntimeError'), (record['cause'], record['error']))
        self.assertTrue(record['where'])
        self.assertTrue(all(__import__('re').fullmatch(WHERE, item) for item in record['where']), record['where'])
        self.assertNotIn('secret', text)

    def run_wrapper(self, source):
        process = subprocess.Popen([sys.executable, '-I', '-B', '-S', '-c', source, str(ORCHESTRATION_ROOT),
                                    str(self.state.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def test_H1c_3_a_stuck_account_with_nothing_running_stops_itself_and_records_why(self):
        process = self.run_wrapper(RESTART_WRAPPER)
        process.wait(timeout=60)
        self.wait_gone()
        # C5 (review 040-s1-h1c): the exit record keeps a cause older readers know; the restart's lane
        # and cause are in account-restart.json.
        record, _ = self.previous_exit()
        self.assertEqual('requested', record['cause'])
        from codex_agents import account_restart
        last = account_restart.last_restart(self.state.root)
        self.assertEqual(('ingress', 'ingress_cleanup_failed'), (last['lane'], last['cause']))

    def test_E1_the_last_self_restart_is_shown_from_start_up(self):
        from codex_agents import account_restart
        self.state.root.mkdir(parents=True, exist_ok=True)
        self.assertTrue(account_restart.record_restart(self.state.root, lane='ingress', cause='ingress_cleanup_failed',
                                                       now_ms=1_700_000_000_000, mono_ns=1))
        process = self.run_wrapper(IDLE_WRAPPER)
        end = time.monotonic() + 30
        while True:
            code, status = self.cli('learning-status', expected=None)
            text = ' '.join((status.get('account_health') or {}).get('attention') or [])
            if 'restarted itself at' in text:
                break
            self.assertLess(time.monotonic(), end, json.dumps(status))
            time.sleep(.2)
        self.assertIn('ingress_cleanup_failed', text)
        process.terminate()

    def test_C10a_an_error_in_the_collector_thread_is_recorded(self):
        process = self.run_wrapper(THREAD_WRAPPER)
        end = time.monotonic() + 20
        while not self.paths.socket.exists():
            self.assertLess(time.monotonic(), end)
            time.sleep(.05)
        with contextlib.suppress(Exception):
            CollectorClient(self.paths.socket, timeout=2).health()  # The thread fails on this request.
        process.wait(timeout=30)
        record, text = self.previous_exit()
        self.assertEqual(('error', 'RuntimeError'), (record['cause'], record['error']))
        self.assertNotIn('secret', text)

    def test_C10c_an_exception_that_is_false_is_still_recorded_and_the_ledger_released(self):
        process = self.run_wrapper(FALSE_WRAPPER)
        process.wait(timeout=30)
        self.assertEqual(0, process.returncode)  # 0.8.27 swallows an owner-loop SystemExit the same way.
        record, _ = self.previous_exit()
        self.assertEqual(('error', 'Exception'), (record['cause'], record['error']))

    def test_C10a_a_removed_socket_is_recorded(self):
        self.ready_status()
        self.paths.socket.unlink()
        self.wait_gone()
        record, _ = self.previous_exit()
        self.assertEqual('state_removed', record['cause'])

    # The inherited AccountHostTests tests run once in their own module.
    for name in [n for n in dir(host_tests.AccountHostTests) if n.startswith('test_')]:
        locals()[name] = None
    del name




class CollectorMainTests(unittest.TestCase):
    """C10d: collector_host.main itself, with stand-in parts, so ledger.close() is observed directly."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('collector_host_under_test', ORCHESTRATION_ROOT / 'collector_host.py')
        self.host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.host)
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.closed, self.handlers, self.records = [], {}, []
        test = self

        class State:
            def __init__(self, *_args, **_options):
                self.root, self.socket = test.root, test.root / 'collector.sock'

        class Ledger:
            def __init__(self, _state):
                pass

            def close(self):
                test.closed.append(True)

        class Server:
            fail_construction = None
            stops_itself = False

            def __init__(self, _ledger):
                if Server.fail_construction is not None:
                    raise Server.fail_construction
                self._stop, self.thread_error, self._started_at_ms = threading.Event(), None, 1
                self.previous_exit = None

            def start(self):
                pass

            def wait(self, _timeout):
                if Server.stops_itself:
                    return True  # The collector thread ended by itself.
                self._stop.set()  # A shutdown request.
                return True

            def stop(self):
                self._stop.set()  # As the real stop() does.

        self.Server = Server
        self.enterContext(unittest.mock.patch.object(self.host, 'LearningState', State))
        self.enterContext(unittest.mock.patch.object(self.host, 'require_authorized_collector_runtime', lambda _s: None))
        self.enterContext(unittest.mock.patch.object(self.host, 'LearningLedger', Ledger))
        self.enterContext(unittest.mock.patch.object(self.host, 'CollectorServer', Server))
        self.enterContext(unittest.mock.patch.object(self.host, 'AccountServiceHost',
                                                     unittest.mock.Mock(side_effect=self.host.HistoryError('history_service_unavailable'))))
        self.enterContext(unittest.mock.patch.object(self.host, 'write_exit',
                                                     lambda _root, **record: self.records.append(record)))
        self.enterContext(unittest.mock.patch('os.chdir'))
        self.enterContext(unittest.mock.patch('signal.signal', side_effect=lambda n, h: self.handlers.__setitem__(n, h)))
        self.enterContext(unittest.mock.patch('builtins.print'))

    def test_C10d_a_requested_stop_and_a_thread_that_ended_by_itself_are_told_apart(self):
        self.assertEqual(0, self.host.main(['--state-root', str(self.root)]))
        self.Server.stops_itself = True
        self.assertEqual(0, self.host.main(['--state-root', str(self.root)]))
        self.assertEqual(['requested', 'thread_ended'], [r['cause'] for r in self.records])
        self.assertEqual([True, True], self.closed)

    def test_C10d_a_failed_recording_still_closes_the_ledger_and_keeps_the_exit(self):
        self.host.write_exit = unittest.mock.Mock(side_effect=KeyboardInterrupt)
        self.assertEqual(0, self.host.main(['--state-root', str(self.root)]))
        self.assertEqual([True], self.closed)

    def test_C10d_a_signal_while_reading_the_record_is_recorded_and_closes_the_ledger(self):
        def read(_root):
            self.handlers[signal.SIGTERM]()  # The signal arrives during the read.
        self.host.read_exit = read
        self.assertEqual(0, self.host.main(['--state-root', str(self.root)]))
        self.assertEqual(['signal'], [r['cause'] for r in self.records])
        self.assertEqual([True], self.closed)

    def test_C10d_a_failure_while_constructing_the_server_is_recorded_and_still_raised(self):
        self.Server.fail_construction = RuntimeError('a secret')
        with self.assertRaises(RuntimeError):
            self.host.main(['--state-root', str(self.root)])
        self.assertEqual([('error', RuntimeError)], [(r['cause'], type(r['error'])) for r in self.records])
        self.assertEqual([True], self.closed)

    def test_C10d_a_signal_while_constructing_the_server_is_recorded(self):
        def signalled(_ledger):
            self.handlers[signal.SIGTERM]()
        self.Server.fail_construction = None
        with unittest.mock.patch.object(self.host, 'CollectorServer', side_effect=signalled):
            with self.assertRaises(SystemExit):
                self.host.main(['--state-root', str(self.root)])
        self.assertEqual(['signal'], [r['cause'] for r in self.records])
        self.assertEqual([True], self.closed)


if __name__ == '__main__':
    unittest.main()
