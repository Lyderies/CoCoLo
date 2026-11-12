"""Scoped request capabilities for local service clients.

A capability is a random key in the owner-only state root, issued by a service
host for one role and one host generation. Holding it is the authority v1
already gives anything able to open the ledger in that directory. It is not a
process identity and is no defence against another unsandboxed program of the
same user; that documented limit is unchanged. It binds exactly one request to
a role, a host class and the issuing generation, refuses replay inside its
freshness window and dies with that generation.
"""
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .hosts import HOST_CLIENTS
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json

ENROLLMENT_VERSION = 1
ROLES = frozenset({'host'})
KEY_BYTES = 32
MAX_PREAMBLE_BYTES = 512
MAX_WINDOW_MS = 120_000
MAX_NONCES = 65_536
REASON_CODES = frozenset({
    'enrollment_invalid', 'enrollment_unavailable', 'enrollment_stale', 'enrollment_replayed', 'enrollment_overloaded',
})
_FIELDS = ('enrollment_version', 'role', 'host_client', 'generation', 'issued_at_ms', 'nonce', 'request_digest')
_HEX = frozenset('0123456789abcdef')


class EnrollmentError(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code if type(reason_code) is str and reason_code in REASON_CODES else 'enrollment_invalid'
        super().__init__(self.reason_code)


def _hex(value, length):
    return type(value) is str and len(value) == length and set(value) <= _HEX


def _integer(value, *, minimum=0, maximum=2**63 - 1):
    return type(value) is int and minimum <= value <= maximum


def capability_path(state, project_root, role):
    if type(state) is not CollectorPaths or role not in ROLES:
        raise EnrollmentError('enrollment_invalid')
    try:
        root = _canonical_project_root(Path(project_root))
        return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.' + role + '.cap')
    except (StateError, OSError, ValueError):
        raise EnrollmentError('enrollment_unavailable') from None


def issue_capability(state, project_root, role):
    """Host side: replace any earlier generation's key with a fresh exclusive file."""
    path = capability_path(state, project_root, role)
    descriptor = None
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        _assert_no_symlink_components(path)
        _check_owner_mode(path.parent, directory=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(KEY_BYTES)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(descriptor, 0o600)
        if os.write(descriptor, key) != KEY_BYTES:
            raise OSError()
        os.fsync(descriptor)
        return key
    except (StateError, OSError, ValueError):
        raise EnrollmentError('enrollment_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def revoke_capability(state, project_root, role):
    try:
        capability_path(state, project_root, role).unlink()
    except (EnrollmentError, OSError):
        pass  # A missing key already refuses every new enrollment.


def read_capability(state, project_root, role):
    """Client side: one checked read of the current key; never creates state."""
    path = capability_path(state, project_root, role)
    descriptor = None
    try:
        _assert_no_symlink_components(path)
        _check_owner_mode(path.parent, directory=True)
        _check_owner_mode(path, directory=False)
        expected = path.lstat()
        if not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_NONBLOCK', 0):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
                or (hasattr(os, 'getuid') and opened.st_uid != os.getuid()) or opened.st_size != KEY_BYTES
                or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError()
        key = os.read(descriptor, KEY_BYTES + 1)
        if len(key) != KEY_BYTES:
            raise ValueError()
        return key
    except (StateError, OSError, ValueError):
        raise EnrollmentError('enrollment_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _mac(key, fields):
    return hmac.new(key, canonical_json({name: fields[name] for name in _FIELDS}), hashlib.sha256).hexdigest()


def request_digest(message):
    if type(message) is not bytes or not message or b'\n' in message:
        raise EnrollmentError('enrollment_invalid')
    return hashlib.sha256(message).hexdigest()


def enrollment_preamble(key, role, host_client, generation, message, *, now_ms=None, nonce=None):
    """Client side: one line that authorizes exactly this request frame once."""
    fields = {'enrollment_version': ENROLLMENT_VERSION, 'role': role, 'host_client': host_client,
              'generation': generation, 'issued_at_ms': time.time_ns() // 1_000_000 if now_ms is None else now_ms,
              'nonce': secrets.token_hex(16) if nonce is None else nonce, 'request_digest': request_digest(message)}
    if (type(key) is not bytes or len(key) != KEY_BYTES or role not in ROLES or host_client not in HOST_CLIENTS
            or not _integer(generation, minimum=1) or not _integer(fields['issued_at_ms']) or not _hex(fields['nonce'], 32)):
        raise EnrollmentError('enrollment_invalid')
    line = canonical_json({**fields, 'mac': _mac(key, fields)})
    if len(line) + 1 > MAX_PREAMBLE_BYTES or b'\n' in line:
        raise EnrollmentError('enrollment_invalid')
    return line


@dataclass(frozen=True)
class Enrolled:
    """Verifier output kept server-side; request labels can never construct it."""
    role: str
    host_client: str
    generation: int
    request_digest: str


class EnrollmentVerifier:
    """Host side: verify one preamble against this generation's key, once."""

    def __init__(self, key, role, generation, *, window_ms=30_000, max_nonces=4_096,
                 clock_ms=lambda: time.time_ns() // 1_000_000):
        if (type(key) is not bytes or len(key) != KEY_BYTES or role not in ROLES or not _integer(generation, minimum=1)
                or not _integer(window_ms, minimum=1, maximum=MAX_WINDOW_MS)
                or not _integer(max_nonces, minimum=1, maximum=MAX_NONCES) or not callable(clock_ms)):
            raise EnrollmentError('enrollment_invalid')
        self._key, self._role, self._generation = key, role, generation
        self._window_ms, self._max_nonces, self._clock_ms = window_ms, max_nonces, clock_ms
        self._seen, self._latest_ms = {}, 0
        self._lock = threading.Lock()

    def verify(self, line):
        if type(line) is not bytes or not 0 < len(line) < MAX_PREAMBLE_BYTES:
            raise EnrollmentError('enrollment_invalid')
        try:
            value = parse_bounded_json(line, max_bytes=MAX_PREAMBLE_BYTES)
        except Exception:
            raise EnrollmentError('enrollment_invalid') from None
        if (value.keys() != set(_FIELDS) | {'mac'} or value['enrollment_version'] != ENROLLMENT_VERSION
                or type(value['enrollment_version']) is not int
                or type(value['role']) is not str or type(value['host_client']) is not str
                or value['host_client'] not in HOST_CLIENTS or not _integer(value['generation'], minimum=1)
                or not _integer(value['issued_at_ms']) or not _hex(value['nonce'], 32)
                or not _hex(value['request_digest'], 64) or not _hex(value['mac'], 64)
                or canonical_json(value) != line):
            raise EnrollmentError('enrollment_invalid')
        # Authenticate before any role, generation or freshness decision is disclosed.
        if not hmac.compare_digest(_mac(self._key, value), value['mac']):
            raise EnrollmentError('enrollment_invalid')
        if value['role'] != self._role or value['generation'] != self._generation:
            raise EnrollmentError('enrollment_stale')
        now = self._clock_ms()
        if not _integer(now):
            raise EnrollmentError('enrollment_unavailable')
        with self._lock:
            # Judge freshness against the latest time seen, so a wall clock that
            # steps backwards cannot revive a preamble whose nonce was pruned.
            now = self._latest_ms = max(now, self._latest_ms)
            if abs(now - value['issued_at_ms']) > self._window_ms:
                raise EnrollmentError('enrollment_stale')
            for nonce in [n for n, issued in self._seen.items() if now - issued > 2 * self._window_ms]:
                del self._seen[nonce]
            if value['nonce'] in self._seen:
                raise EnrollmentError('enrollment_replayed')
            if len(self._seen) >= self._max_nonces:
                raise EnrollmentError('enrollment_overloaded')
            # Retained for twice the window, so a clock step cannot reopen it.
            self._seen[value['nonce']] = max(now, value['issued_at_ms'])
        return Enrolled(value['role'], value['host_client'], value['generation'], value['request_digest'])
