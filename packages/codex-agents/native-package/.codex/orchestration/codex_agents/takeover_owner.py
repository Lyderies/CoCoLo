"""OS-owner operations for the journal coordinator; no database connection here.

The CLI supplies a read-only preflight. All store changes use the existing
authorized offline project open and the existing ledger owner-registration open.
"""
from __future__ import annotations

import base64
import errno
import socket
from contextlib import closing
import fcntl
import json
import hashlib
import os
from pathlib import Path
import time

from .collector_client import CollectorClient, COLLECTOR_RUNTIME_DIGEST
from .collector_contracts import CollectorError, COLLECTOR_PROTOCOL_VERSION, _version_tuple
from .takeover_journal import TakeoverJournal, atomic_owner_file, fsync_directory
from .takeover_process import pin_predecessor, require_free_locks
from .update_takeover import TakeoverError, UpdateTakeover, stop_until_excluded

# UPG-6: these and later collectors judge the stop using their loaded digest.
FIRST_IDENTITY_STOP_VERSION = (0, 8, 56)


class OwnerOperations:
    def __init__(self, state, cli_path, project_root, preflight, *, descriptor=None):
        self.state, self.cli_path, self.project_root = state, Path(cli_path), Path(project_root)
        self.read_preflight, self.descriptor = preflight, descriptor
        self.pinned = None
        self.killed = False

    def preflight(self):
        return self.read_preflight()

    def close(self):
        if self.pinned is not None:
            self.pinned.close()
            self.pinned = None
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def _launch_snapshot(self):
        from .launch_journal import _entry_name, _read_entry
        intent = TakeoverJournal(self.state.root).read() or self.preflight()
        directory = self.state.root / 'projects' / intent['project_id'] / 'launches'
        try:
            names = sorted(path for path in directory.iterdir() if _entry_name(path.name))
        except FileNotFoundError:
            return []
        entries = []
        for path in names:
            value = _read_entry(path)
            if value is None:
                raise TakeoverError('predecessor_unverified')
            entries.append((path.name, value))
        return entries

    def snapshot_runs(self, *, include_settled=False):
        return self._runs(self._launch_snapshot(), include_settled=include_settled)

    @staticmethod
    def _runs(entries, *, include_settled=False):
        runs = {}
        for _, value in entries:
            if value.get('bridge_run_id') is None or (not include_settled and value['state'] == 'settled'):
                continue
            run = {key: value[key] for key in ('run_handle', 'attempt', 'bridge_run_id')}
            known = runs.get(run['bridge_run_id'])
            if known is not None and known != run:
                raise TakeoverError('predecessor_unverified')
            runs[run['bridge_run_id']] = run
        return list(runs.values())

    def reconcile_runs(self, document, *, late):
        return TakeoverJournal(self.state.root).reconcile(document, self._runs(self._launch_snapshot()))

    def _old(self, journal):
        old = journal.get('predecessor_service')
        if old is None or 'entry_b64' not in old:
            return old
        from .client_runtime import runtime_directory
        directory = runtime_directory(self.state, self.project_root)
        old = dict(old, entry=os.fsdecode(base64.b64decode(old['entry_b64'], validate=True)),
                   stop_op=old.get('service_stop') == 'stop_op')
        if 'directory_digest' in old:
            if hashlib.sha256(os.fsencode(directory)).hexdigest() != old['directory_digest']:
                raise TakeoverError('predecessor_unverified')
            old.update(directory=str(directory), project_endpoint=str(directory / 'p.sock'))
        return old

    def quiesce(self, journal):
        identity = journal['successor']
        raw = json.dumps(dict(schema_version=1, runtime_version=identity['version'],
                              runtime_digest=identity['digest'], quiesced_for_upgrade=True),
                         sort_keys=True, separators=(',', ':')).encode('ascii') + b'\n'
        atomic_owner_file(self.state.collector_authorization, raw)

    def stop_collector(self, journal):
        from .learning_store import _wait_for_collector_shutdown, _wait_for_writer_release
        client = CollectorClient(self.state.socket, timeout=2)
        deadline = time.monotonic() + 30.0
        try:
            health = client._request({'protocol': COLLECTOR_PROTOCOL_VERSION, 'op': 'health'})
        except CollectorError:
            if _wait_for_writer_release(self.state, deadline):
                return
            raise TakeoverError('collector_stop_failed') from None
        predecessor = journal['predecessor_collector']
        if (predecessor is None or health.get('runtime_digest') != predecessor['digest']
                or health.get('runtime_version') != predecessor['version']):
            raise TakeoverError('predecessor_unverified')
        if predecessor['digest'] == COLLECTOR_RUNTIME_DIGEST:
            options = {}
        elif _version_tuple(predecessor['version']) < FIRST_IDENTITY_STOP_VERSION:
            options = {'earlier_collector': True}
        else:
            options = {'echo_digest': predecessor['digest']}
        drained = not journal.get('collector_children', bool(journal.get('runs')))
        try:
            try:
                client.shutdown(only_if_drained=drained, **options)
            except CollectorError as error:
                if not drained or error.code != 'collector_busy':
                    raise
                # The only refusal B4t replaces: a child admitted since T0.
                client.shutdown(only_if_drained=False, **options)
        except CollectorError:
            raise TakeoverError('collector_stop_failed') from None
        if not _wait_for_collector_shutdown(client, self.state, deadline):
            raise TakeoverError('collector_stop_failed')

    def withdraw_hint(self, journal):
        from .client_runtime import runtime_hint_path
        from .project_host_client import HINT_LOCK, _hinted_host
        old = self._old(journal)
        if old is None or old['digest'] == COLLECTOR_RUNTIME_DIGEST or not old.get('directory'):
            return
        descriptor = os.open(Path(old['directory']) / HINT_LOCK, os.O_RDWR | os.O_NOFOLLOW)
        try:
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TakeoverError('service_stop_failed')
                    time.sleep(0.02)
            hinted = _hinted_host(self.state, self.project_root, any_runtime=True, hint_locked=True)
            if hinted is not None and (hinted[0].runtime_digest != old['digest']
                    or hinted[1][0].parent != Path(old['directory'])):
                raise TakeoverError('predecessor_unverified')
            runtime_hint_path(self.state, self.project_root).unlink(missing_ok=True)
        finally:
            os.close(descriptor)

    def _service_locks(self, journal):
        old = self._old(journal)
        directory = self.state.root / 'projects' / journal['project_id']
        locks = [directory / 'history.lock']
        if old.get('directory'):
            locks.append(Path(old['directory']) / 'host.lock')
        locks.extend((directory / 'launches').glob('*.lock'))
        return locks

    @staticmethod
    def _service_answers(old):
        # Probe the recorded scope's endpoint, not its withdrawn hint. Only a
        # missing/refused endpoint proves that it no longer answers.
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                connection.connect(old['project_endpoint'])
            return True
        except OSError as error:
            return error.errno not in (errno.ENOENT,errno.ECONNREFUSED)

    def stop_service(self, journal):
        old = self._old(journal)
        if old is None or old['digest'] == COLLECTOR_RUNTIME_DIGEST or not old.get('directory'):
            return
        if 'pid' not in old and old.get('service_stop') != 'hint':
            require_free_locks(self._service_locks(journal))
            return
        idle_proven = False
        if (old.get('service_stop') == 'signal' and 'root_digest' in journal):
            # J4 has proven collector exit. Re-read final admission state: an
            # idle service exits on hint withdrawal without any signal, while
            # a claim admitted before T2 still takes the proven signal path.
            from .takeover_preflight import _registry
            _, _, children = _registry(self.state, journal['root_digest'])
            if not children.get(journal['project_id'], 0):
                old = dict(old, service_stop='hint')
                idle_proven = True
        if old.get('stop_op'):
            from .client_runtime import enroll_clients
            from .history_api import HistoryClient
            from .hosts import OWNER_CLIENT
            from types import SimpleNamespace
            from .history_contracts import HistoryError
            scope = SimpleNamespace(supervisor_generation=old.get('service_generation',
                (old.get('scope') or {}).get('supervisor_generation')))
            project_id = journal.get('project_id', (old.get('scope') or {}).get('project_id'))
            endpoint = Path(old['project_endpoint'])
            enroll_clients(self.state, self.project_root, scope, (endpoint,), host_client=OWNER_CLIENT)
            acknowledged = False
            try:
                HistoryClient(endpoint, project_id, old['digest'], timeout=2)._call(
                    'service.stop_for_update', 'update-stop', {})
                acknowledged = True
            except HistoryError:
                # A normal close can race its acknowledgement; only the lock
                # proof below can then establish successful exclusion.
                pass
        elif old.get('service_stop') != 'hint':
            from .process_tree import process_gone
            if not process_gone(old['pid']):
                self.pinned = pin_predecessor(old['pid'], entry=Path(old['entry']), state_root=self.state.root,
                    project_root=self.project_root)
                if self.pinned.identity['birth'] != old['birth']:
                    raise TakeoverError('predecessor_unproven')
                self.killed = self.pinned.stop()
                self.pinned = None
            # The 30 s TERM + 5 s escalation budget has already ended, and the
            # exact process exited. A surviving lock is inherited custody; it
            # is not another reason to wait a second service-stop budget.
            require_free_locks(self._service_locks(journal))
            return
        deadline = time.monotonic() + 30.0
        while True:
            try:
                require_free_locks(self._service_locks(journal))
                if old.get('service_stop') == 'hint':
                    if self._service_answers(old):
                        raise TakeoverError('service_still_finishing')
                if old.get('stop_op') and not acknowledged:
                    if old.get('service_stop') == 'hint':
                        if idle_proven:
                            from .process_tree import process_gone
                            if process_gone(old['pid']):
                                raise TakeoverError('predecessor_lock_inherited') from None
                            self.pinned = pin_predecessor(old['pid'],entry=Path(old['entry']),
                                state_root=self.state.root,project_root=self.project_root)
                            if self.pinned.identity['birth'] != old['birth']:
                                raise TakeoverError('predecessor_unproven')
                            self.killed=self.pinned.stop()
                            self.pinned=None
                            require_free_locks(self._service_locks(journal))
                            return
                        raise TakeoverError('service_still_finishing' if self._service_answers(old)
                                            else 'predecessor_lock_inherited') from None
                    from .process_tree import process_gone, process_record
                    record = process_record(old['pid'])
                    if not (process_gone(old['pid']) or (record is not None and record[2] != old['birth'])):
                        raise TakeoverError('service_stop_failed')
                return
            except TakeoverError:
                if time.monotonic() >= deadline:
                    if old.get('service_stop') == 'hint':
                        if idle_proven:
                            from .process_tree import process_gone
                            if process_gone(old['pid']):
                                raise TakeoverError('predecessor_lock_inherited') from None
                            self.pinned = pin_predecessor(old['pid'],entry=Path(old['entry']),
                                state_root=self.state.root,project_root=self.project_root)
                            if self.pinned.identity['birth'] != old['birth']:
                                raise TakeoverError('predecessor_unproven')
                            self.killed=self.pinned.stop()
                            self.pinned=None
                            require_free_locks(self._service_locks(journal))
                            return
                        raise TakeoverError('service_still_finishing' if self._service_answers(old)
                                            else 'predecessor_lock_inherited') from None
                    from .process_tree import process_gone, process_record
                    record = process_record(old['pid'])
                    if not (process_gone(old['pid']) or (record is not None and record[2] != old['birth'])):
                        raise TakeoverError('service_stop_failed') from None
                    raise TakeoverError('predecessor_lock_inherited') from None
                time.sleep(0.02)

    def _stop_project_straggler(self, journal):
        from .takeover_preflight import _lock_held, _peer
        from .takeover_process import Kernel, _trusted
        lock = self.state.root / 'projects' / journal['project_id'] / 'history.lock'
        if not _lock_held(lock):
            return
        old = self._old(journal)
        if not old.get('project_endpoint'):
            raise TakeoverError('predecessor_lock_inherited')
        try:
            pid = _peer(Path(old['project_endpoint']))
        except OSError:
            raise TakeoverError('predecessor_lock_inherited') from None
        if old.get('stop_op'):
            identity = Kernel().identity(pid)
            if not _trusted(identity, Path(old['entry']), self.state.root, self.project_root):
                raise TakeoverError('predecessor_unproven')
        else:
            pinned = pin_predecessor(pid, entry=Path(old['entry']), state_root=self.state.root,
                                     project_root=self.project_root)
            try:
                identity = pinned.identity
            finally:
                pinned.close()
        # A start paused before J2 has a new birth identity. Save the freshly
        # proven peer before the stop; no signal uses the previous pid's proof.
        journal['predecessor_service'] = dict(journal['predecessor_service'], pid=pid, birth=identity['birth'],
            service_stop='signal' if not old.get('stop_op') else 'stop_op')
        if old.get('stop_op'):
            from .project_host_client import _hinted_host
            hinted = _hinted_host(self.state, self.project_root, any_runtime=True)
            if hinted is None or hinted[0].runtime_digest != old['digest'] or not hinted[2]:
                raise TakeoverError('predecessor_lock_inherited')
            journal['predecessor_service']['service_generation'] = hinted[0].supervisor_generation
        TakeoverJournal(self.state.root).write(journal)
        self.stop_service(journal)

    def adopt_project(self, journal):
        from .project_service import open_offline_history
        from .history_contracts import HistoryError
        old = self._old(journal)
        if old is None or old['digest'] == COLLECTOR_RUNTIME_DIGEST:
            return
        def try_owner():
            try:
                require_free_locks(self._service_locks(journal))
                with closing(open_offline_history(self.state, journal['project_id'], takeover=journal)) as history:
                    from .project_takeover import takeover_report, stopped_children
                    report = takeover_report(history)
                    journal['outcomes'] = {name: report[name] for name in
                                          ('lost', 'lost_held', 'finished_kept', 'refused_unverifiable')}
                    journal['stopped_children'] = stopped_children(history, journal['runs']) + journal.get('runs_unlisted', 0)
                    TakeoverJournal(self.state.root).write(journal)
            except TakeoverError as error:
                if error.code == 'predecessor_lock_inherited':
                    return True
                raise
            except HistoryError as error:
                if error.reason_code == 'history_writer_busy':
                    return True
                raise TakeoverError('store_adoption_failed') from None
            return False
        stop_until_excluded(lambda: self._stop_project_straggler(journal), try_owner, phase='store_adopting')

    def adopt_ledger(self, journal):
        from .learning_store import LearningLedger, LedgerError, ledger_schema_behind, copy_ledger_before_upgrade
        from .takeover_preflight import _lock_held
        def try_owner():
            if _lock_held(self.state.lock):
                return True
            behind = ledger_schema_behind(self.state)
            if behind is not None:
                copy_ledger_before_upgrade(self.state, behind)
            try:
                with closing(LearningLedger(self.state, allow_schema_upgrade=True,
                        backup_before_upgrade=behind is not None, takeover=journal)):
                    pass
            except LedgerError:
                if _lock_held(self.state.lock):
                    return True  # an old start passed its marker check before J2
                raise TakeoverError('ledger_migration_failed') from None
            return False
        stop_until_excluded(lambda: self.stop_collector(journal), try_owner)

    def authorize(self, journal):
        from .learning_store import authorize_collector_runtime
        authorize_collector_runtime(self.state, self.cli_path, project_root=self.project_root)

    def start_collector(self, journal):
        from .learning_store import CollectorMaintenance
        descriptor, self.descriptor = self.descriptor, None
        CollectorMaintenance(self.state, self.cli_path, self.project_root, descriptor, restart=True).resume()

    def undo(self, journal):
        prior = base64.b64decode(journal['prior_authorization'], validate=True)
        if prior:
            atomic_owner_file(self.state.collector_authorization, prior)
        else:
            self.state.collector_authorization.unlink(missing_ok=True)
            fsync_directory(self.state.root)

    def finish(self, journal):
        # The wizard calls this only after registration and a ready new service.
        from .project_host_client import project_host_serving
        if not project_host_serving(self.state.root, self.project_root):
            raise TakeoverError('service_stop_failed', phase='installed')
        from .client_runtime import takeover_report_client
        from .hosts import OWNER_CLIENT
        report = takeover_report_client(self.state.root, self.project_root, host_client=OWNER_CLIENT)
        if report is None or any(row['state'] != 'complete' for row in report['records']):
            raise TakeoverError('takeover_unfinished', phase='installed')


def run_owner_takeover(operations, *, action='run'):
    """T0 first; one runtime-lock descriptor stays owned through account restart."""
    operations.preflight()
    descriptor = os.open(operations.state.runtime_lock, os.O_RDWR | os.O_NOFOLLOW)
    operations.descriptor = descriptor
    try:
        deadline = time.monotonic() + 20.0
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TakeoverError('update_busy') from None
                time.sleep(0.02)
        command = UpdateTakeover(operations.state.root, operations)
        return getattr(command, action)()
    finally:
        operations.close()
