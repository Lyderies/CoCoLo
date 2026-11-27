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
from .hosts import HOST_CLIENTS, OWNER_CLIENT
from .learning_contracts import canonical_json, keyed_digest, parse_bounded_json

ENROLLMENT_VERSION = 1
# A host credential is per project; the account credential is per state root and
# names the project root it acts for, checked by the account owner. The account key
# serves two roles: a primary's own calls ('account') and a project host's run
# custody calls ('project').
ROLES = frozenset({'host', 'account', 'project'})
# The owner's terminal label: a project's service and the account owner, never a run's project role.
OWNER_ROLES = frozenset({'host', 'account'})
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


def wake_capability_path(state, project_root):
    """RS-E E2a-5: a project's wake key, which the collector issues per active project; it signs only that
    project's wake publications (the verifier picks it by the preamble's subject)."""
    if type(state) is not CollectorPaths or project_root is None:
        raise EnrollmentError('enrollment_invalid')
    try:
        root = _canonical_project_root(Path(project_root))
        return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.wake.cap')
    except (StateError, OSError, ValueError):
        raise EnrollmentError('enrollment_unavailable') from None


def issue_wake_capability(state, project_root, project_id=None):
    """Issue the project's wake key; with its project id, this process's verifier also learns it (E2a-5)."""
    key = _issue_key(wake_capability_path(state, project_root))
    if project_id is not None:
        try:
            subject = str(_canonical_project_root(Path(project_root)))
        except (StateError, OSError, ValueError):
            raise EnrollmentError('enrollment_unavailable') from None
        wake_keys(state).remember(subject, project_id, key)
    return key


def keep_wake_capability(state, project_root, project_id):
    """UPDFIX-5 (L1.2): the project's wake key as it is when it is a valid one (the file, owner-only, of the right size), so
    registering a project again on a running collector changes nothing; the collector's verifier learns the same key. Only a
    missing or invalid key is issued. A collector that starts issues a fresh key per active project (`issue_wake_capability`)."""
    try:
        key = _read_key(wake_capability_path(state, project_root))
    except EnrollmentError:
        return issue_wake_capability(state, project_root, project_id)
    try:
        subject = str(_canonical_project_root(Path(project_root)))
    except (StateError, OSError, ValueError):
        raise EnrollmentError('enrollment_unavailable') from None
    wake_keys(state).remember(subject, project_id, key)
    return key


class WakeKeys:
    """RS-E E2a-5 (review round 1): the wake keys this process issued, in memory, each bound to the immutable id of
    the project it was issued for. A lookup never touches the filesystem: a subject with no key gets a dummy key, so
    a known and an unknown subject cost the verifier the same one lookup and one MAC."""

    def __init__(self):
        self._keys, self._lock = {}, threading.Lock()
        self._dummy = secrets.token_bytes(KEY_BYTES)

    def remember(self, subject, project_id, key):
        if type(subject) is not str or type(project_id) is not str or type(key) is not bytes or len(key) != KEY_BYTES:
            raise EnrollmentError('enrollment_invalid')
        with self._lock:
            self._keys[subject] = (project_id, key)

    def lookup(self, subject):
        """(project id or None, key) for this subject; a miss is (None, the dummy key)."""
        with self._lock:
            found = self._keys.get(subject) if type(subject) is str else None
        return found if found is not None else (None, self._dummy)


_WAKE_KEYS = {}
_WAKE_KEYS_LOCK = threading.Lock()


def wake_keys(state):
    """The process's WakeKeys for this state root (shared by the collector's registration and the account host)."""
    if type(state) is not CollectorPaths:
        raise EnrollmentError('enrollment_invalid')
    with _WAKE_KEYS_LOCK:
        return _WAKE_KEYS.setdefault(str(state.root), WakeKeys())


# RS-E E2a-3/11 (review round 1): the owner's credential for the collector's setup operations (`record_launch`,
# `launch_status`). The collector issues a fresh key at each start; a request is the owner's only with an
# `owner-cli-v1` preamble under it whose proof was written in the owner-only proof folder (the same trust level as
# the account services' owner enrollment), naming exactly this request and its project root as the subject.
def collector_owner_paths(state):
    if type(state) is not CollectorPaths:
        raise EnrollmentError('enrollment_invalid')
    folder = state.root / 'collector-owner'
    return folder / 'owner.cap', folder / 'proofs'


def issue_collector_owner(state):
    """Collector side: a fresh key and an empty proof folder; the verifier for this collector's life."""
    key_path, proofs = collector_owner_paths(state)
    key = _issue_key(key_path)
    prepare_proof_directory(proofs)
    return EnrollmentVerifier(key, 'account', 1, proofs)


def collector_owner_preamble(state, message, subject):
    """Owner side: the preamble for one collector setup request (`message`, its canonical bytes)."""
    key_path, proofs = collector_owner_paths(state)
    return enrollment_preamble(_read_key(key_path), 'account', OWNER_CLIENT, 1, message, proofs, subject=subject)


def read_wake_capability(state, project_root):
    return _read_key(wake_capability_path(state, project_root))


def issue_capability(state, project_root, role):
    """Host side: replace any earlier generation's key with a fresh exclusive file."""
    return _issue_key(capability_path(state, project_root, role))


def _issue_key(path):
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
    return _read_key(capability_path(state, project_root, role))


def _read_key(path):
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


def _proof_binding(fields):
    """The writer's own statement of the request a proof is for, MAC fields only."""
    return canonical_json({name: fields[name] for name in _FIELDS})


def _consume_proof(directory, value):
    """Read one owner-made proof once, remove it, and return the context it carries.

    The proof holds the writer's binding (every signed field of its preamble) on
    its first line, then the context. A reader of the key can sign a preamble but
    cannot write a proof, so the preamble must match the binding exactly: a proof
    can never be spent on a request, subject or role its writer did not name.
    """
    path = directory / value['nonce']
    descriptor = None
    limit = MAX_CONTEXT_BYTES + MAX_PREAMBLE_BYTES
    try:
        expected = path.lstat()
        if not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_NONBLOCK', 0):
            raise OSError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or (hasattr(os, 'getuid') and info.st_uid != os.getuid())
                or (expected.st_dev, expected.st_ino) != (info.st_dev, info.st_ino) or info.st_size > limit):
            raise OSError()
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        raw = b''.join(chunks)
        binding, separator, context = raw.partition(b'\n')
        if (len(raw) > limit or separator != b'\n'
                or not hmac.compare_digest(binding, _proof_binding(value))):
            raise OSError()
        path.unlink()
    except OSError:
        raise EnrollmentError('enrollment_invalid') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if value['context_digest'] is None:
        if context:
            raise EnrollmentError('enrollment_invalid')
        return None
    if (len(context) > MAX_CONTEXT_BYTES
            or not hmac.compare_digest(hashlib.sha256(context).hexdigest(), value['context_digest'])):
        raise EnrollmentError('enrollment_invalid')
    return context


def withdraw_proof(proofs, line):
    """Client side: remove the proof for a preamble that was never delivered."""
    try:
        nonce = parse_bounded_json(line, max_bytes=MAX_PREAMBLE_BYTES)['nonce']
        if _hex(nonce, 32):
            (proofs / nonce).unlink()
    except Exception:
        pass  # Already consumed, or never created: nothing is left to spend.


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
    if (type(key) is not bytes or len(key) != KEY_BYTES or role not in ROLES
            or not (host_client in HOST_CLIENTS or (host_client == OWNER_CLIENT and role in OWNER_ROLES))
            or not _integer(generation, minimum=1) or not _integer(fields['issued_at_ms']) or not _hex(fields['nonce'], 32)
            or not _subject(subject) or (subject is None) != (role == 'host')):
        raise EnrollmentError('enrollment_invalid')
    line = canonical_json({**fields, 'mac': _mac(key, fields)})
    if len(line) + 1 > MAX_PREAMBLE_BYTES or b'\n' in line or not isinstance(proofs, Path):
        raise EnrollmentError('enrollment_invalid')
    _create_proof(proofs, fields['nonce'], _proof_binding(fields) + b'\n' + (b'' if context is None else context))
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
    # RS-E E2a-5: the MAC verified only under the subject's own wake key, so the project is the verifier's (the
    # immutable id the key was issued for), not the sender's: the credential is bound to that one project.
    bound_project: str | None = None

    @property
    def project_bound(self):
        return self.bound_project is not None


class EnrollmentVerifier:
    """Host side: verify one preamble against this generation's key, once."""

    def __init__(self, key, role, generation, proofs, *, window_ms=30_000, max_nonces=4_096,
                 clock_ms=lambda: time.time_ns() // 1_000_000, subject_keys=None):
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
        if subject_keys is not None and not callable(subject_keys):
            raise EnrollmentError('enrollment_invalid')
        self._subject_keys = subject_keys  # RS-E E2a-5: subject -> (project id or None, key); never the filesystem

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
                or not (value['host_client'] in HOST_CLIENTS
                        or (value['host_client'] == OWNER_CLIENT and value['role'] in OWNER_ROLES))
                or not _integer(value['generation'], minimum=1)
                or not _subject(value['subject'])
                or (value['context_digest'] is not None and not _hex(value['context_digest'], 64))
                or not _integer(value['issued_at_ms']) or not _hex(value['nonce'], 32)
                or not _hex(value['request_digest'], 64) or not _hex(value['mac'], 64)
                or canonical_json(value) != line):
            raise EnrollmentError('enrollment_invalid')
        # Authenticate before any role, generation or freshness decision is disclosed.
        bound = None
        if not hmac.compare_digest(_mac(self._key, value), value['mac']):
            # E2a-5: a project preamble may instead be signed with its subject's own wake key; any failure here
            # (no such project, no key, another project's key) is the same refusal as a bad MAC. The lookup is
            # in memory and a miss is checked against a dummy key, so no subject is probed on disk or timed apart.
            project, other = None, None
            if self._subject_keys is not None and value['role'] == 'project' and value['subject'] is not None:
                try:
                    project, other = self._subject_keys(value['subject'])
                except Exception:
                    project, other = None, None
            if (type(other) is not bytes or len(other) != KEY_BYTES
                    or not hmac.compare_digest(_mac(other, value), value['mac']) or type(project) is not str):
                raise EnrollmentError('enrollment_invalid')
            bound = project
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
            # The MAC shows the key was read; only the proof, written with this
            # exact binding, shows the sender can write the state root. Consuming
            # it is the claim, so a preamble without its proof spends no slot.
            context = _consume_proof(self._proofs, value)
            # Retained for twice the window, so a clock step cannot reopen it.
            self._seen[value['nonce']] = max(now, value['issued_at_ms'])
        return Enrolled(value['role'], value['host_client'], value['generation'], value['request_digest'],
                        value['subject'], context, bound)
