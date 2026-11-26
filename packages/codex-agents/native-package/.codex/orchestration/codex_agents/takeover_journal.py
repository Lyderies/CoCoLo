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
MAX_TAKEOVER_RUNS = 64
MAX_INTEGER = 2**63 - 1
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
        if (type(document) is not dict or set(document) - {
                'schema_version', 'phase', 'takeover_id', 'project_id', 'root_digest', 'recorded_at_ms', 'updated_at_ms',
                'predecessor_collector', 'predecessor_service', 'successor', 'prior_authorization', 'runs',
                'runs_unlisted', 'stopped_children', 'outcomes'} or document['schema_version'] != 1
                or document['phase'] not in PHASES or not _hex(document['takeover_id'], 32)
                or not re.fullmatch(r'prj_[0-9a-f]{32}', document['project_id'])
                or not _hex(document['root_digest'], 64)
                or type(document['recorded_at_ms']) is not int or document['recorded_at_ms'] < 0
                or type(document['updated_at_ms']) is not int
                or not 0 <= document['recorded_at_ms'] <= document['updated_at_ms'] <= MAX_INTEGER):
            raise ValueError()
        for name in ('predecessor_collector', 'predecessor_service', 'successor'):
            identity = document[name]
            if identity is None and name != 'successor':
                continue
            allowed = {'digest', 'version'} | ({'entry_b64', 'directory_digest', 'service_generation',
                       'service_stop', 'pid', 'birth'} if name == 'predecessor_service' else set())
            if (type(identity) is not dict or set(identity) - allowed or not _hex(identity['digest'], 64)
                    or not re.fullmatch(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}', identity['version'])):
                raise ValueError()
        prior = base64.b64decode(document['prior_authorization'], validate=True)
        if 'collector_children' in document and type(document['collector_children']) is not bool:
            raise ValueError()
        if 'stopped_children' in document and (type(document['stopped_children']) is not int
                or not 0 <= document['stopped_children'] <= min(MAX_INTEGER, len(document['runs']) + document.get('runs_unlisted', 0))):
            raise ValueError()
        if 'outcomes' in document and (type(document['outcomes']) is not dict
                or set(document['outcomes']) != {'lost', 'lost_held', 'finished_kept', 'refused_unverifiable'}
                or any(type(n) is not int or not 0 <= n <= MAX_INTEGER for n in document['outcomes'].values())):
            raise ValueError()
        if ('reconciled_entries' in document or len(prior) > MAX_AUTHORIZATION_BYTES
                or type(document['runs']) is not list or len(document['runs']) > MAX_TAKEOVER_RUNS
                or type(document.get('runs_unlisted')) is not int or not 0 <= document['runs_unlisted'] <= MAX_INTEGER):
            raise ValueError()
        service = document['predecessor_service']
        if service is not None:
            if any(key in service for key in ('entry', 'directory', 'project_endpoint')):
                raise ValueError()
            entry = base64.b64decode(service['entry_b64'], validate=True)
            if len(entry) > 4095 or not entry or b'\0' in entry:
                raise ValueError()
            if 'directory_digest' in service and not _hex(service['directory_digest'], 64):
                raise ValueError()
            for key in ('pid', 'service_generation'):
                if key in service and (type(service[key]) is not int or not 1 <= service[key] <= MAX_INTEGER):
                    raise ValueError()
            if 'birth' in service:
                birth = service['birth']
                if not ((type(birth) is str and re.fullmatch(r'[0-9]{1,19}(?:\.[0-9]{6})?',birth) is not None)
                        or (type(birth) is int and 0 <= birth <= MAX_INTEGER)):
                    raise ValueError()
            if 'service_stop' in service and service['service_stop'] not in ('none','hint','signal','stop_op'):
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
        import hashlib
        intent = dict(intent)
        intent.pop('collector_children', None)  # a drained-stop refusal is retried through authenticated shutdown
        if intent.get('predecessor_service') is not None:
            service = dict(intent['predecessor_service'])
            if 'entry' in service:
                service['entry_b64'] = base64.b64encode(os.fsencode(service.pop('entry'))).decode('ascii')
            if 'directory' in service:
                service['directory_digest'] = hashlib.sha256(os.fsencode(service.pop('directory'))).hexdigest()
            service.pop('project_endpoint', None)
            if service.pop('stop_op', False):
                service['service_stop'] = 'stop_op'
            if 'scope' in service:
                service['service_generation'] = service.pop('scope')['supervisor_generation']
            intent['predecessor_service'] = service
        document = dict(intent, schema_version=1, takeover_id=secrets.token_hex(16), phase='intent', runs=[],
                        runs_unlisted=0,
                        recorded_at_ms=now, updated_at_ms=now,
                        prior_authorization=base64.b64encode(prior).decode('ascii'))
        return self.reconcile(document, runs)

    def remove(self):
        self.read()  # refuses an unsafe replacement
        self.path.unlink()
        fsync_directory(self.root)

    def phase(self, document, phase):
        document = dict(document, phase=phase,
                        updated_at_ms=max(document['updated_at_ms'], time.time_ns() // 1_000_000))
        self.write(document)
        return document

    @staticmethod
    def serialized(document):
        return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n'

    @classmethod
    def widest(cls, document):
        """Reserve all growth through J14 before adding even the first run."""
        import copy
        value = copy.deepcopy(document)
        value.update(phase=max(PHASES, key=len), updated_at_ms=MAX_INTEGER,
                     recorded_at_ms=MAX_INTEGER, runs_unlisted=MAX_INTEGER, stopped_children=MAX_INTEGER,
                     outcomes={key: MAX_INTEGER for key in ('lost', 'lost_held', 'finished_kept', 'refused_unverifiable')})
        for identity in ('predecessor_collector', 'predecessor_service', 'successor'):
            if value.get(identity) is not None:
                value[identity]['version'] = '99999.99999.99999'
        service = value.get('predecessor_service')
        if service is not None:
            stop_op = service.get('service_stop') == 'stop_op'
            service.update(pid=MAX_INTEGER, birth='9' * 32, service_stop='stop_op')
            if stop_op:
                service['service_generation'] = MAX_INTEGER
        # Each run has the same fixed-width identifiers; reserve the widest attempt.
        value['runs'] = [dict(run, attempt=MAX_INTEGER) for run in value['runs']]
        return cls.serialized(value)

    def reconcile(self, document, runs):
        combined = {run['bridge_run_id']: run for run in document['runs']}
        observed = {}
        for run in runs:
            known = combined.get(run['bridge_run_id']) or observed.get(run['bridge_run_id'])
            if known is not None and known != run:
                raise JournalError('a launch identity changed during the hand-over')
            observed[run['bridge_run_id']] = run
        for identity, run in observed.items():
            if identity in combined:
                continue
            proposed = dict(document, runs=[*combined.values(), run])
            if len(combined) >= MAX_TAKEOVER_RUNS or len(self.widest(proposed)) > MAX_JOURNAL_BYTES:
                continue
            combined[identity] = run
        document = dict(document, runs=list(combined.values()),
                        runs_unlisted=max(document['runs_unlisted'], len(set(observed) - set(combined))))
        self.write(document)
        return document
