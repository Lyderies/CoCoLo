"""Scoped request enrollment for local service clients.

Enrollment has two parts. The proof is a single-use file the client creates in
the host's owner-only proof directory: only a process that can WRITE the state
root can make one, which is exactly who could write the ledger in 0.2.x. A
sandboxed session that can merely read the state root cannot enroll. The
capability is a random key issued by the host for one role and one host start;
its MAC binds the proof to one request, a role, a host class and the issuing
generation inside a freshness window.

Neither part is a process identity, and neither defends against another
unsandboxed program of the same user; that documented limit is unchanged.
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
# A host credential is per project; the account credential is per state root and
# names the project root it acts for, checked by the account owner. The account key
# serves two roles: a primary's own calls ('account') and a project host's run
# custody calls ('project').
ROLES = frozenset({'host', 'account', 'project'})
MAX_SUBJECT_BYTES = 4_096
# Protected originals a trusted client hands the owner for exactly one request.
MAX_CONTEXT_BYTES = 262_144
PROOF_DIRECTORY = 'proofs'
MAX_PROOFS = 4_096
KEY_BYTES = 32
MAX_PREAMBLE_BYTES = 8_192
MAX_WINDOW_MS = 120_000
MAX_NONCES = 65_536
REASON_CODES = frozenset({
    'enrollment_invalid', 'enrollment_unavailable', 'enrollment_stale', 'enrollment_replayed', 'enrollment_overloaded',
})
_FIELDS = ('enrollment_version', 'role', 'host_client', 'generation', 'issued_at_ms', 'nonce', 'request_digest',
           'subject', 'context_digest')
_HEX = frozenset('0123456789abcdef')


class EnrollmentError(RuntimeError):
    def __init__(self, reason_code):
        self.reason_code = reason_code if type(reason_code) is str and reason_code in REASON_CODES else 'enrollment_invalid'
        super().__init__(self.reason_code)


def _hex(value, length):
    return type(value) is str and len(value) == length and set(value) <= _HEX


def _integer(value, *, minimum=0, maximum=2**63 - 1):
    return type(value) is int and minimum <= value <= maximum


def _subject(value):
    return value is None or (type(value) is str and 0 < len(value.encode()) <= MAX_SUBJECT_BYTES
                             and '\0' not in value and Path(value).is_absolute())


def capability_path(state, project_root, role):
    """A project's host key, or with no project the state root's account key."""
    if type(state) is not CollectorPaths or role not in ROLES or (project_root is None) != (role != 'host'):
        raise EnrollmentError('enrollment_invalid')
    try:
        if project_root is None:
            return state.root / 'client-runtimes' / 'account.cap'
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
        if descriptor is not None:
            try:
                path.unlink()  # Never leave a partly written key behind.
            except OSError:
                pass
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


def account_endpoint_path(state):
    """The account services' fixed endpoint under the state root."""
    return state.root / 'rt' / 'account' / 'a.sock'


def proof_directory(endpoint):
    """Where a client proves write access for this endpoint: beside its socket."""
    return Path(endpoint).parent / PROOF_DIRECTORY


def prepare_proof_directory(directory):
    """Host side: an empty owner-only directory; earlier proofs are discarded."""
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        _assert_no_symlink_components(directory)
        _check_owner_mode(directory, directory=True)
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= 4 * MAX_PROOFS:
                    raise OSError()
                os.unlink(entry.path)
    except (StateError, OSError):
        raise EnrollmentError('enrollment_unavailable') from None


def prune_proofs(directory, *, older_than_seconds, now=None):
    """Host side: drop proofs no accepted preamble claimed; bounded per call."""
    limit = (time.time() if now is None else now) - older_than_seconds
    try:
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                if count >= MAX_PROOFS:
                    break
                try:
                    if entry.stat(follow_symlinks=False).st_mtime < limit:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _create_proof(directory, nonce, context=b''):
    descriptor = None
    try:
        _assert_no_symlink_components(directory)
        _check_owner_mode(directory, directory=True)
        descriptor = os.open(directory / nonce, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(descriptor, 0o600)
        view = memoryview(context)
        while view:
            view = view[os.write(descriptor, view):]
    except (StateError, OSError):
        raise EnrollmentError('enrollment_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _consume_proof(directory, nonce, context_digest):
    """One owner-made regular file per nonce, read once and removed as it is accepted.

    Its bytes are the request's protected context, which must match the digest
    the preamble signed; a proof without a signed context must be empty.
    """
    path = directory / nonce
    descriptor = None
    try:
        expected = path.lstat()
        if not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_NONBLOCK', 0):
            raise OSError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())
                or (expected.st_dev, expected.st_ino) != (info.st_dev, info.st_ino)
                or info.st_size > MAX_CONTEXT_BYTES or (context_digest is None and info.st_size != 0)):
            raise OSError()
        chunks, total = [], 0
        while total <= MAX_CONTEXT_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_CONTEXT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        context = b''.join(chunks)
        path.unlink()
    except OSError:
        raise EnrollmentError('enrollment_invalid') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if context_digest is None:
        return None
    if len(context) > MAX_CONTEXT_BYTES or not hmac.compare_digest(hashlib.sha256(context).hexdigest(), context_digest):
        raise EnrollmentError('enrollment_invalid')
    return context


def _mac(key, fields):
    return hmac.new(key, canonical_json({name: fields[name] for name in _FIELDS}), hashlib.sha256).hexdigest()


def request_digest(message):
    if type(message) is not bytes or not message or b'\n' in message:
        raise EnrollmentError('enrollment_invalid')
    return hashlib.sha256(message).hexdigest()


def enrollment_preamble(key, role, host_client, generation, message, proofs, *, subject=None, context=None,
                        now_ms=None, nonce=None):
    """Client side: prove write access, then name exactly this request frame once.

    ``context`` is optional protected input for the owner, carried in the proof
    file and bound to this request by its signed digest.
    """
    if context is not None and (type(context) is not bytes or not 0 < len(context) <= MAX_CONTEXT_BYTES):
        raise EnrollmentError('enrollment_invalid')
    fields = {'enrollment_version': ENROLLMENT_VERSION, 'role': role, 'host_client': host_client,
              'generation': generation, 'issued_at_ms': time.time_ns() // 1_000_000 if now_ms is None else now_ms,
              'nonce': secrets.token_hex(16) if nonce is None else nonce, 'request_digest': request_digest(message),
              'subject': subject, 'context_digest': None if context is None else hashlib.sha256(context).hexdigest()}
    if (type(key) is not bytes or len(key) != KEY_BYTES or role not in ROLES or host_client not in HOST_CLIENTS
            or not _integer(generation, minimum=1) or not _integer(fields['issued_at_ms']) or not _hex(fields['nonce'], 32)
            or not _subject(subject) or (subject is None) != (role == 'host')):
        raise EnrollmentError('enrollment_invalid')
    line = canonical_json({**fields, 'mac': _mac(key, fields)})
    if len(line) + 1 > MAX_PREAMBLE_BYTES or b'\n' in line or not isinstance(proofs, Path):
        raise EnrollmentError('enrollment_invalid')
    _create_proof(proofs, fields['nonce'], b'' if context is None else context)
    return line


@dataclass(frozen=True)
class Enrolled:
    """Verifier output kept server-side; request labels can never construct it."""
    role: str
    host_client: str
    generation: int
    request_digest: str
    subject: str | None = None
    context: bytes | None = None


class EnrollmentVerifier:
    """Host side: verify one preamble against this generation's key, once."""

    def __init__(self, key, role, generation, proofs, *, window_ms=30_000, max_nonces=4_096,
                 clock_ms=lambda: time.time_ns() // 1_000_000):
        roles = frozenset({role}) if type(role) is str else role
        if (type(key) is not bytes or len(key) != KEY_BYTES or type(roles) is not frozenset or not roles
                or not roles <= ROLES or not _integer(generation, minimum=1)
                or not isinstance(proofs, Path) or not proofs.is_absolute()
                or not _integer(window_ms, minimum=1, maximum=MAX_WINDOW_MS)
                or not _integer(max_nonces, minimum=1, maximum=MAX_NONCES) or not callable(clock_ms)):
            raise EnrollmentError('enrollment_invalid')
        self._key, self._roles, self._generation, self._proofs = key, roles, generation, proofs
        self._window_ms, self._max_nonces, self._clock_ms = window_ms, max_nonces, clock_ms
        self._seen, self._latest_ms, self._pruned_ms = {}, 0, 0
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
                or not _subject(value['subject'])
                or (value['context_digest'] is not None and not _hex(value['context_digest'], 64))
                or not _integer(value['issued_at_ms']) or not _hex(value['nonce'], 32)
                or not _hex(value['request_digest'], 64) or not _hex(value['mac'], 64)
                or canonical_json(value) != line):
            raise EnrollmentError('enrollment_invalid')
        # Authenticate before any role, generation or freshness decision is disclosed.
        if not hmac.compare_digest(_mac(self._key, value), value['mac']):
            raise EnrollmentError('enrollment_invalid')
        if value['role'] not in self._roles or value['generation'] != self._generation:
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
            # One sweep per second at most, so a burst of valid preambles does
            # not rescan the whole table for each of them.
            if now - self._pruned_ms >= 1_000 or len(self._seen) >= self._max_nonces:
                self._pruned_ms = now
                for nonce in [n for n, issued in self._seen.items() if now - issued > 2 * self._window_ms]:
                    del self._seen[nonce]
            if value['nonce'] in self._seen:
                raise EnrollmentError('enrollment_replayed')
            if len(self._seen) >= self._max_nonces:
                raise EnrollmentError('enrollment_overloaded')
            # Retained for twice the window, so a clock step cannot reopen it.
            self._seen[value['nonce']] = max(now, value['issued_at_ms'])
        # The MAC shows the key was read; only this file shows the sender can
        # write the state root. A nonce without its proof stays spent.
        context = _consume_proof(self._proofs, value['nonce'], value['context_digest'])
        return Enrolled(value['role'], value['host_client'], value['generation'], value['request_digest'],
                        value['subject'], context)
