"""Owner-only durable update intent. No store access and no import-time I/O."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

MAX_JOURNAL_BYTES = 16 * 1024
MAX_AUTHORIZATION_BYTES = 1024
PHASES = ('intent', 'quiesced', 'collector_stopping', 'collector_stopped', 'runs_reconciled',
          'hint_withdrawn', 'service_stopped', 'late_runs_reconciled', 'store_adopting',
          'store_adopted', 'ledger_migrating', 'ledger_migrated', 'authorized', 'account_done',
          'installed', 'complete')


class JournalError(ValueError):
    pass


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JournalError('update intent has duplicate fields')
        result[key] = value
    return result


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _owned(info, *, directory=False):
    if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise JournalError('update intent is not private to this owner')


def validate(document):
    try:
        if (type(document) is not dict or document['schema_version'] != 1
                or document['phase'] not in PHASES or not _hex(document['takeover_id'], 32)
                or not re.fullmatch(r'prj_[0-9a-f]{32}', document['project_id'])
                or not _hex(document['root_digest'], 64)
                or type(document['recorded_at_ms']) is not int or document['recorded_at_ms'] < 0
                or type(document['updated_at_ms']) is not int
                or document['updated_at_ms'] < document['recorded_at_ms']):
            raise ValueError()
        for name in ('predecessor_collector', 'predecessor_service', 'successor'):
            identity = document[name]
            if identity is None and name != 'successor':
                continue
            if (type(identity) is not dict or not _hex(identity['digest'], 64)
                    or not re.fullmatch(r'\d+\.\d+\.\d+', identity['version'])):
                raise ValueError()
        prior = base64.b64decode(document['prior_authorization'], validate=True)
        if 'collector_children' in document and type(document['collector_children']) is not bool:
            raise ValueError()
        if 'stopped_children' in document and (type(document['stopped_children']) is not int
                or not 0 <= document['stopped_children'] <= len(document['runs'])):
            raise ValueError()
        if 'outcomes' in document and (type(document['outcomes']) is not dict
                or set(document['outcomes']) != {'lost', 'lost_held', 'finished_kept', 'refused_unverifiable'}
                or any(type(n) is not int or n < 0 for n in document['outcomes'].values())):
            raise ValueError()
        if 'reconciled_entries' in document:
            names = document['reconciled_entries']
            from .launch_journal import _entry_name
            if (type(names) is not list or any(type(name) is not str or not _entry_name(name) for name in names)
                    or len(names) != len(set(names))):
                raise ValueError()
        if len(prior) > MAX_AUTHORIZATION_BYTES or type(document['runs']) is not list:
            raise ValueError()
        seen = set()
        for run in document['runs']:
            if (type(run) is not dict or set(run) != {'run_handle', 'attempt', 'bridge_run_id'}
                    or not re.fullmatch(r'hrn_[0-9a-f]{48}', run['run_handle'])
                    or type(run['attempt']) is not int or not 1 <= run['attempt'] < 2**63
                    or not re.fullmatch(r'brg_[0-9a-f]{32}', run['bridge_run_id'])
                    or run['bridge_run_id'] in seen):
                raise ValueError()
            seen.add(run['bridge_run_id'])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise JournalError('update intent is invalid; keep it for recovery') from None
    return document


def atomic_owner_file(path, raw):
    """Replace a private regular file, making the data and directory entry durable."""
    _owned(path.parent.lstat(), directory=True)
    try:
        _owned(path.lstat())
    except FileNotFoundError:
        pass
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(16))
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_DIRECTORY', 0))
    try:
        _owned(os.fstat(descriptor), directory=True)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TakeoverJournal:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'update-takeover.json'

    def read(self):
        descriptor = None
        try:
            _owned(self.root.lstat(), directory=True)
            try:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            _owned(os.fstat(descriptor))
            raw = os.read(descriptor, MAX_JOURNAL_BYTES + 1)
            if len(raw) > MAX_JOURNAL_BYTES:
                raise JournalError('update intent exceeds its byte limit')
            return validate(json.loads(raw, object_pairs_hook=_unique))
        except (OSError, UnicodeError, ValueError) as error:
            if isinstance(error, JournalError):
                raise
            raise JournalError('update intent is unsafe or unreadable; keep it for recovery') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def write(self, document):
        validate(document)
        raw = json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError('update intent exceeds its byte limit')
        atomic_owner_file(self.path, raw)

    def create(self, intent, runs):
        prior = intent['prior_authorization']
        if type(prior) is not bytes or len(prior) > MAX_AUTHORIZATION_BYTES:
            raise JournalError('prior runtime authorization is invalid')
        now = time.time_ns() // 1_000_000
        document = dict(intent, schema_version=1, takeover_id=secrets.token_hex(16), phase='intent', runs=runs,
                        recorded_at_ms=now, updated_at_ms=now,
                        prior_authorization=base64.b64encode(prior).decode('ascii'))
        self.write(document)
        return document

    def remove(self):
        self.read()  # refuses an unsafe replacement
        self.path.unlink()
        fsync_directory(self.root)

    def phase(self, document, phase):
        document = dict(document, phase=phase,
                        updated_at_ms=max(document['updated_at_ms'], time.time_ns() // 1_000_000))
        self.write(document)
        return document

    def reconcile(self, document, runs):
        combined = {run['bridge_run_id']: run for run in document['runs']}
        for run in runs:
            known = combined.get(run['bridge_run_id'])
            if known is not None and known != run:
                raise JournalError('a launch identity changed during the hand-over')
            combined[run['bridge_run_id']] = run
        document = dict(document, runs=list(combined.values()))
        self.write(document)
        return document
