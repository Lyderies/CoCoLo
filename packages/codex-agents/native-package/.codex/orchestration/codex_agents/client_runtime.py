"""Read existing project-runtime routing hints and use bounded service clients.

These private metadata files do not authenticate a native peer or confer launch
authority. Reviewed native bootstrap must supply/authorize the endpoints; this
client never creates state, starts a runtime or falls back to a database.
"""
import re
import os
from pathlib import Path
import signal
import secrets
import stat
import threading
import time

from .collector_client import (COLLECTOR_RUNTIME_DIGEST, CollectorClient, collector_code_diagnosis,
                               empty_ingestion_snapshot)
from .account_api import ACCOUNT_REASON_CODES, AccountClient, AccountError, integer as account_integer, _ID as _ACCOUNT_ID
from .account_claim_contracts import CLAIM_DENIAL_CODES
from .preparation_reasons import PREPARATION_STAGE_REASONS
from .account_open_contracts import AccountOpenInput, OpenError
from .collector_paths import CollectorPaths, StateError, _assert_no_symlink_components, _canonical_project_root, _check_owner_mode
from .contracts import ContractError
from .collector_contracts import CollectorError, CollectorTransportError
from .collector_runtime import client_health_snapshot
from .control_api import ControlClient, ControlScope
from .contracts import effective_dispatch_bounds, effective_dispatch_limits
from .admission_metadata import api_keys_present
from .dispatch_api import DISPATCH_REASONS, DispatchClient, DispatchError
from .history_api import HistoryClient
from .history_contracts import HISTORY_REASON_CODES, AttemptBinding, HistoryError, _ID, _PROJECT, matches
from .host_result_contracts import HostResultReference
from .hosts import HOST_CLIENTS, OWNER_CLIENT, HostClientMismatch, HostClientUnknown, detect_host_client
from .learning_contracts import ADMISSION_REASON_CODES, canonical_json, content_digest, keyed_digest, parse_bounded_json
from .billing_probe_contracts import BILLING_PROBE_REASONS, PROBE_COMPLETION_REASONS
from .open_scope_contracts import open_inputs_digest
from .orchestration_api import OrchestrationClient, OrchestrationRequest, OrchestrationScope
from .provider_observation_contracts import ObservationError, ObservationInput
from .result_contracts import MAX_RESULT_BODY_BYTES
from .result_read_api import ResultReadClient
from .service_enrollment import (
    EnrollmentError, account_endpoint_path, capability_path, enrollment_preamble, proof_directory, read_capability,
    withdraw_proof,
)
from .service_protocol import ServiceTarget
from .service_transport import exchange_frame, install_enrollment


ACCOUNT_START_SECONDS = 10.0
ACCOUNT_GENERATION = 1  # account_host.GENERATION; that module is a persistence owner.
BRIDGE_PROVIDERS = frozenset({"codex", "claude"})  # Read-only children the project host runs.
# Why the project host could not start a run: its run driver's codes (project_driver.REASON_CODES),
# the account's (a refused claim, say) and a denied claim receipt's. All fixed identifiers, safe to report.
START_REASONS = frozenset({
    'run_invalid_request', 'run_not_found', 'run_conflict', 'run_full', 'run_failed', 'run_unavailable',
    'run_abandoned', 'run_closed', 'run_lost', 'run_lost_held', 'run_client_changed', 'run_account_call_failed',
    # An earlier service's run the project's service is still closing (0.3.3, C3d).
    'run_settling',
    # The project's result store has no free place (0.3.4 R3).
    'result_capacity_full', 'result_capacity_busy',
    # O0c M1: the run's child's cleanup could not be confirmed; its slot is held until its service is replaced.
    'run_cleanup_unconfirmed',
}) | ACCOUNT_REASON_CODES | CLAIM_DENIAL_CODES | PREPARATION_STAGE_REASONS
# A run whose project service stopped or crashed: ended abandoned with an unknown outcome.
# Its capacity is released once the next service saw nothing of it still running.
LOST_CAPACITY = {'run_lost': 'released', 'run_lost_held': 'held'}


def runtime_directory(state, project_root):
    """Short private socket directory; the keyed digest keeps project paths out of it.

    Its host's sockets, host lock and hint lock live here (`service_host`); a client finds it without
    a hint too (CI2: a host still starting has none yet)."""
    return state.root / 'rt' / keyed_digest(state.digest_key(), str(project_root))[:16]


def runtime_hint_path(state, project_root):
    if type(state) is not CollectorPaths:
        raise DispatchError('dispatch_unavailable')
    root = _canonical_project_root(project_root)
    return state.root / 'client-runtimes' / (keyed_digest(state.digest_key(), str(root)) + '.json')


def _read_runtime_endpoints(state, project_root, *, account=False, account_only=False, any_runtime=False):
    descriptor = None
    try:
        root = _canonical_project_root(project_root)
        path = runtime_hint_path(state, root)
        _assert_no_symlink_components(path)
        _check_owner_mode(path.parent, directory=True)
        _check_owner_mode(path, directory=False)
        expected = path.lstat()
        if not 0 < expected.st_size <= 8192 or not getattr(os, 'O_NOFOLLOW', 0) or not getattr(os, 'O_NONBLOCK', 0):
            raise ValueError()
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or stat.S_IMODE(opened.st_mode) != 0o600
                or (hasattr(os, 'getuid') and opened.st_uid != os.getuid())
                or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError()
        raw = os.read(descriptor, 8193)
        after, current = os.fstat(descriptor), path.lstat()
        if (len(raw) != opened.st_size or after.st_size != opened.st_size
                or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError()
        value = parse_bounded_json(raw, max_bytes=8192)
        fields = {'schema_version', 'project_root_digest', 'scope', 'dispatch_endpoint', 'result_endpoint'}
        version = value.get('schema_version')
        if (type(version) is not int or version not in {1, 2}
                or value.keys() != (fields | {'account_endpoint'} if version == 2 else fields)
                or value['project_root_digest'] != keyed_digest(state.digest_key(), str(root))
                or type(value['scope']) is not dict):
            raise ValueError()
        scope = ControlScope(**value['scope'])
        if scope.runtime_digest != COLLECTOR_RUNTIME_DIGEST and not any_runtime:
            raise ValueError()
        endpoints = []
        for name in (('dispatch_endpoint', 'result_endpoint', 'account_endpoint') if version == 2
                     else ('dispatch_endpoint', 'result_endpoint')):
            endpoint = value[name]
            if (type(endpoint) is not str or not 0 < len(endpoint) <= 4096 or '\0' in endpoint
                    or not Path(endpoint).is_absolute() or '..' in Path(endpoint).parts):
                raise ValueError()
            endpoint = Path(endpoint)
            if not endpoint.is_relative_to(state.root):
                raise ValueError()
            if (name == 'account_endpoint' and not account) or (account_only and name != 'account_endpoint'):
                endpoints.append(endpoint)
                continue  # Only endpoints used by this operation must be live.
            _assert_no_symlink_components(endpoint)
            actual = endpoint.lstat()
            if (not stat.S_ISSOCK(actual.st_mode) or actual.st_nlink != 1 or stat.S_IMODE(actual.st_mode) != 0o600
                    or (hasattr(os, 'getuid') and actual.st_uid != os.getuid())):
                raise ValueError()
            endpoints.append(endpoint)
        return scope, endpoints
    except Exception:
        raise DispatchError('dispatch_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _hint_cause(state, project_root):
    """S1-U10c: why the project's runtime hint cannot serve an open, decided before any socket is
    checked; None when it is a current hint. Content-free reasons only."""
    try:
        path = runtime_hint_path(state, _canonical_project_root(project_root))
    except Exception:
        return 'open_service_not_running'
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 'open_service_not_running'
    except OSError:
        return 'open_service_hint_refused'
    try:
        _assert_no_symlink_components(path)
        _check_owner_mode(path.parent, directory=True)
        _check_owner_mode(path, directory=False)
    except Exception:
        return 'open_service_hint_refused'  # not this account's private file: never trusted
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= 8192:
        return 'open_service_hint_refused'
    try:
        value = parse_bounded_json(path.read_bytes(), max_bytes=8192)
    except Exception:
        return 'open_service_not_running'  # unreadable or half written: no service has published it
    if not isinstance(value, dict):
        return 'open_service_not_running'
    if value.get('schema_version') == 1:
        return 'open_service_other_version'
    return None


def read_runtime_hint(state, project_root):
    scope, endpoints = _read_runtime_endpoints(state, project_root)
    return scope, endpoints[0], endpoints[1]


def enroll_clients(state, project_root, scope, endpoints=(), *, account=None, host_client=None):
    """Enroll this checked CLI process for the hinted endpoints it is about to use.

    Each key is read once from the owner-only state root, and each request leaves
    a single-use proof file beside its endpoint. The account preamble also names
    this project's root, which the account owner matches to the registered
    project. A process with no host class enrolls only for the account owner,
    under the owner's terminal label, which the account owner limits to reads
    that spend nothing; for every other endpoint it, or a process missing a
    key, stays unenrolled and its host refuses. There is no database fallback.

    Returns a mapping from endpoint to the protected context for its next
    request; each entry is used once.
    """
    if host_client is None:
        try:
            host_client = detect_host_client(os.environ)
        except HostClientUnknown:
            host_client = None
    preambles, contexts = {}, {}
    if host_client is not None:
        try:
            host_key = read_capability(state, project_root, 'host') if endpoints else None
        except EnrollmentError:
            host_key = None
        if host_key is not None:
            for endpoint in endpoints:
                preambles[Path(endpoint)] = ('host', host_client, host_key, scope.supervisor_generation, None)
    if account is not None:
        try:
            account_key = read_capability(state, None, 'account')
            root = str(_canonical_project_root(Path(project_root)))
        except (EnrollmentError, StateError, OSError):
            account_key = None
        if account_key is not None:
            preambles[Path(account)] = ('account', host_client or OWNER_CLIENT, account_key, ACCOUNT_GENERATION, root)

    def preamble(socket_path, message):
        entry = preambles.get(Path(socket_path))
        if entry is None:
            return None
        role, label, key, generation, subject = entry
        proofs = proof_directory(socket_path)
        try:
            line = enrollment_preamble(key, role, label, generation, message, proofs,
                                       subject=subject, context=contexts.pop(Path(socket_path), None))
        except EnrollmentError:
            raise OSError() from None
        return line, lambda: withdraw_proof(proofs, line)

    install_enrollment(preamble if preambles else None)
    return contexts


def _account_target(state, project_root, *, await_start=False):
    """The account endpoint and this project's identifier, with or without a project host.

    A published project hint names both. Without one, the identifier comes from
    the collector's registry lookup and the endpoint is the fixed account path;
    `await_start` then waits, bounded, for account services that are still starting.
    """
    try:
        scope, endpoints = _read_runtime_endpoints(state, project_root, account=True, account_only=True)
        if len(endpoints) == 3:
            return scope.project_id, endpoints[2]
    except DispatchError:
        pass
    try:
        project_id = CollectorClient(state.socket, timeout=2).project(_canonical_project_root(Path(project_root)))
    except (CollectorError, StateError, OSError):
        raise DispatchError('dispatch_unavailable') from None
    endpoint = account_endpoint_path(state)
    if await_start:
        _await_account_services(state, endpoint)
    return project_id, endpoint


def _await_account_services(state, endpoint):
    """A collector that has just started answers before its account services listen.

    Their signature workers start first, which takes a moment longer on a busy
    machine. Wait, bounded, for the published key and endpoint; nothing is sent
    before they exist, so the request itself is still made exactly once.
    """
    try:
        key = capability_path(state, None, 'account')
    except EnrollmentError:
        return
    end = time.monotonic() + ACCOUNT_START_SECONDS
    while not (endpoint.exists() and key.exists()):
        if time.monotonic() >= end:
            return  # The request then reports the account services unavailable.
        time.sleep(0.02)


def _status_ingestion_unavailable():
    value = empty_ingestion_snapshot(registered=False)
    value.update(registered_project_state='unavailable', ingestion_state='unavailable',
                 freshness={'state': 'unavailable', 'age_ms': None,
                            'message': 'Hook delivery is unknown while the collector is unavailable.'})
    return value


def _status_account_capacity(value):
    """Each account limit with its live count (0.3.4 R9); None from a collector that does not report it."""
    # O0b B0 (narrowed): 0.8.38 also reports held run bindings and billing probes; an earlier collector does not.
    # O0b RS-A round 1 F14: a newer report also carries the holds by kind, shown apart (_status_account_holds).
    # OP-3: a newer report also carries the open requests in flight against their limit.
    counts = {name: item for name, item in value.items() if name != 'holds'} if type(value) is dict else None
    if (counts is None or set(counts) not in ({'observations', 'cleanup_receipts'},
                                              {'observations', 'cleanup_receipts', 'pending_bindings',
                                               'billing_probes'},
                                              {'observations', 'cleanup_receipts', 'pending_bindings',
                                               'billing_probes', 'open_requests'})
            or ('holds' in value and len(counts) < 4)
            or any(type(item) is not dict or set(item) != {'live', 'limit'}
                   or not account_integer(item['live']) or not account_integer(item['limit'])
                   for item in counts.values())):
        return None
    return {name: {'live': item['live'], 'limit': item['limit']} for name, item in counts.items()}


_HOLD_FIELDS = {'held', 'past_due', 'next_eligible', 'deadline'}
_HOLD_NOUNS = {'run_slot': ('run slot', 'run slots'), 'binding': ('binding', 'bindings'),
               'probe': ('probe', 'probes')}


def _status_account_holds(value):
    """O0b RS-A round 1 F14 (RS-A-6, design draft 4 item 4): this project's account holds by kind, with one
    plain line for the owner. None without a capacity report or with a malformed one; an earlier collector's
    report, without holds, is accepted and shown as not reported."""
    from .billing_probe_contracts import PROBE_NO_LAUNCH_EVIDENCE_REASON, PROBE_NO_LAUNCH_EVIDENCE_STATE

    if _status_account_capacity(value) is None:
        return None
    if 'holds' not in value:
        return {'reported': False, 'kinds': None, 'summary': 'Account holds: not reported by this collector version.'}
    holds = value['holds']
    if type(holds) is not dict or holds.keys() != _HOLD_NOUNS.keys():
        return None
    for name, kind in holds.items():
        fields = _HOLD_FIELDS | ({'release_by_evidence'} if name == 'probe' else set())
        if name == 'run_slot' and type(kind) is dict and 'reasons' in kind:
            from .legacy_slots import DEADLINE_REASON, OWNER_REASON, MISSING_REASON
            fields |= {'reasons', 'owner_confirmations'}
            if (type(kind.get('reasons')) is not list
                    or any(reason not in (DEADLINE_REASON, OWNER_REASON, MISSING_REASON) for reason in kind['reasons'])
                    or type(kind.get('owner_confirmations')) is not list
                    or any(type(item) is not dict or set(item) != {'reason', 'command', 'signing_instruction', 'signed_command'}
                           or item['reason'] not in (OWNER_REASON, MISSING_REASON) or type(item['command']) is not str
                           or any(type(item[name]) is not str or '\n' in item[name] for name in ('signing_instruction', 'signed_command')) or '\n' in item['command'] for item in kind['owner_confirmations'])):
                return None
        details = kind.get('admitted_no_launch_evidence') if type(kind) is dict and name == 'probe' else None
        if details is not None:
            fields = fields | {'admitted_no_launch_evidence'}
        if (type(kind) is not dict or kind.keys() != fields
                or not all(account_integer(kind[n]) for n in ('held', 'past_due'))
                or kind['past_due'] > kind['held']
                or any(kind[n] is not None and not account_integer(kind[n]) for n in ('next_eligible', 'deadline'))
                or (name == 'probe' and not (account_integer(kind['release_by_evidence'])
                                             and kind['release_by_evidence'] <= kind['held']))):
            return None
        if details is not None and (type(details) is not list or not details
                or len(details) > kind['held'] - kind['release_by_evidence']
                or any(type(detail) is not dict or detail.keys() != {'state', 'reason', 'release_after_ms'}
                       or detail['state'] != PROBE_NO_LAUNCH_EVIDENCE_STATE
                       or detail['reason'] != PROBE_NO_LAUNCH_EVIDENCE_REASON
                       or not account_integer(detail['release_after_ms']) for detail in details)):
            return None
    kinds = {name: dict(kind) for name, kind in holds.items()}
    return {'reported': True, 'kinds': kinds,
            'summary': 'Account holds: ' + ', '.join(_hold_words(name, kinds[name]) for name in _HOLD_NOUNS) + '.'}


def _hold_words(name, kind):
    """One kind's holds in plain words, e.g. '1 run slot held (next can be freed at <time>; deadline <time>)'."""
    from datetime import datetime, timezone

    def at(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')

    held = kind['held']
    noun = _HOLD_NOUNS[name][held != 1]
    if not held:
        return f"0 {noun}"
    notes = []
    if kind['past_due']:
        notes.append(f"{kind['past_due']} past due, freed at the next check")
    if name == 'probe' and kind['release_by_evidence']:
        notes.append(f"{kind['release_by_evidence']} freed only by its result")
    for detail in kind.get('admitted_no_launch_evidence', ()):
        cutoff = datetime.fromtimestamp(detail['release_after_ms'] / 1000, timezone.utc).isoformat(timespec='milliseconds')
        notes.append(f"{detail['state']}: {detail['reason']} Release after {cutoff}, at the next check")
    if kind['next_eligible'] is not None:
        notes.append(f"next can be freed at {at(kind['next_eligible'])}")
    if name == 'run_slot' and kind['deadline'] is not None:  # draft 4 item 4: the execution deadline beside it
        notes.append(f"{'deadline' if held == 1 else 'nearest deadline'} {at(kind['deadline'])}")
    notes.extend(kind.get('reasons', []))
    text = f"{held} {noun} held" + (f" ({'; '.join(notes)})" if notes else '')
    for item in kind.get('owner_confirmations', []):
        text += '\nOwner command: ' + item['command'] + '\n' + item['signing_instruction'] + '\n' + item['signed_command']
    return text


_CODE_STALE_NOTE = ('collector_code_stale: this collector started before its code changed and still runs the old '
                    'code; restart it before relying on a live check')
_CODE_NOTES = {
    'stale': _CODE_STALE_NOTE,
    'conflict': ('collector_build_conflict: this collector runs a different build of the same package version, and '
                 'its own files have not changed; restarting it will not fix that, so use one install for this state'),
    'invalid': ('collector_identity_invalid: this collector did not state a valid version, schema or digest, so '
                'whether its code is current cannot be told'),
}


def _status_collector_fields(health):
    """Closed display metadata only; a health view grants no execution authority."""
    template = empty_ingestion_snapshot(registered=False)
    ingestion = health['ingestion']
    if (type(ingestion) is not dict or ingestion.keys() != template.keys()
            or ingestion['registered_project_state'] not in {'active', 'unregistered'}
            or ingestion['ingestion_state'] != ('active' if ingestion['registered_project_state'] == 'active' else 'inactive')
            or not account_integer(health['started_at_ms']) or not account_integer(health['schema_version'])):
        raise ValueError()
    for name in ('accepted_hook_event_count', 'rejected_hook_event_count'):
        if not account_integer(ingestion[name]):
            raise ValueError()
    for name in ('last_accepted_hook_event_at_ms', 'last_rejected_hook_event_at_ms'):
        if ingestion[name] is not None and not account_integer(ingestion[name]):
            raise ValueError()
    for name in ('hook_event_counts', 'managed_lifecycle', 'coverage'):
        values = ingestion[name]
        if type(values) is not dict or values.keys() != template[name].keys():
            raise ValueError()
        if any((type(v) is not bool if name == 'coverage' else not account_integer(v)) for v in values.values()):
            raise ValueError()
    freshness = ingestion['freshness']
    messages = {
        'unregistered': 'Project is not registered.',
        'awaiting_hook_review_or_first_event': 'No events since the hooks were installed; open /hooks on the main checkout.',
        'fresh': 'Hooks are delivering events.',
        'idle': 'Hooks have delivered events; no recent activity.',
    }
    if (type(freshness) is not dict or freshness.keys() != {'state', 'age_ms', 'message'}
            or type(freshness['state']) is not str or freshness['state'] not in messages
            or freshness['message'] != messages[freshness['state']]
            or (freshness['state'] in {'fresh', 'idle'} and not account_integer(freshness['age_ms']))
            or (freshness['state'] not in {'fresh', 'idle'} and freshness['age_ms'] is not None)):
        raise ValueError()
    attestations = health['provider_attestations']
    if (type(attestations) is not dict or attestations.keys() != {'codex', 'claude'}
            or any(type(v) is not dict or v.keys() != {'state'} or v['state'] not in
                   {'present', 'missing', 'identity_missing'} for v in attestations.values())):
        raise ValueError()
    stale = health.get('runtime_stale')
    if stale is not None and type(stale) is not bool:
        raise ValueError()
    counters = health.get('collector_health', {})
    if (type(counters) is not dict or counters.keys() - {'admission_rejected', 'queue_full', 'collector_error', 'conflict'}
            or any(type(v) is not dict or v.keys() != {'count', 'updated_at_ms'}
                   or any(not account_integer(n) for n in v.values()) for v in counters.values())):
        raise ValueError()
    from .collector_exit import checked as checked_exit
    previous = health.get('previous_exit')
    phases = health.get('startup_phases')
    if phases is not None and (type(phases) is not dict or phases.keys() - {'ledger_open', 'collector', 'account'}
                               or any(not account_integer(v) for v in phases.values())):
        raise ValueError()
    services = health.get('account_services')
    if services not in {None, 'starting', 'running', 'unavailable'}:
        raise ValueError()
    return {'collector_previous_exit': None if previous is None else checked_exit(previous),
            'account_services': services,  # O10 ST1: from the collector host, or None
            # S1-H1a: passed through as read; checked only when account_health is built, so a
            # malformed new field never costs the collector its ready.
            'account_lanes': health.get('account_lanes'), 'account_slots': health.get('account_slots'),
            'account_restart': health.get('account_restart'),  # S1-H1c: the host's latest decision
            'account_restart_last': health.get('account_restart_last'),  # D5: its last self-restart
            'ledger_waits': health.get('ledger_waits'),  # M4b E1a-3: refused ledger-lock waits by caller
            'collector_startup_ms': phases,  # S1-U9a: milliseconds per start-up phase, or None
            'collector_schema_version': health['schema_version'],
            'collector_runtime_version': health['runtime_version'], 'collector_runtime_digest': health['runtime_digest'],
            # Whether the collector's runtime files changed after it started; its code is then old.
            'collector_code_stale': stale, 'collector_code_note': None if stale is not True else _CODE_STALE_NOTE,
            'started_at_ms': health['started_at_ms'], 'collector_health': counters,
            'ingestion': ingestion, 'provider_attestations': attestations}


class _LedgerBusy(Exception):
    """The collector answered, but its ledger was busy (S1-H1a-6c)."""


def _apply_health(report, health):
    """S1-H1a-6c: one health reading into the report; False when the collector answered that its
    ledger was busy, which leaves the project fields unavailable and the status not ready."""
    if health.get('ledger_busy') is True:
        # G3: every ledger-derived field unavailable, never an earlier reading's values.
        report.update(active=True, reason_code='status_ledger_busy',
                      error="The collector is busy; this project's status could not be read in time.",
                      ingestion=_status_ingestion_unavailable(), account_capacity=None, account_holds=None,
                      provider_attestations={p: {'state': 'unavailable'} for p in ('codex', 'claude')})
        report.pop('collector_health', None)
        report.pop('account_slots', None)
        # M4b E1a-3 (review 040-m4b-e1a-code-2): the refusal counts are read without the ledger lock,
        # and matter most now.
        report['ledger_waits'] = health.get('ledger_waits')
        return False
    report.update(_status_collector_fields(health))
    report['account_capacity'] = _status_account_capacity(health.get('account_capacity'))
    report['account_holds'] = _status_account_holds(health.get('account_capacity'))  # O0b RS-A round 1 F14
    report['health_monitor'] = _status_health_monitor(health.get('health_monitor'))  # HM-0
    return True


LAUNCH_RECORD_MISSING = 'recovery pending: run setup to record the install'


def status_client(state_root, project_root):
    """Observe existing collector/account APIs once each; never start or open a DB."""
    report = {'ready': False, 'active': False, 'client_failures': {},
              'ingestion': _status_ingestion_unavailable(),
              'provider_attestations': {p: {'state': 'unavailable'} for p in ('codex', 'claude')},
              'owner_key': {'state': 'unavailable',
                            'replacements': None, 'last_replaced_at_ms': None},
              # Whether this project's learning history still has to move to its own store.
              'learning_migration': 'unavailable',
              # Runs an earlier version left that still hold a slot, and when the last stops.
              'earlier_version_runs': None,
              # How near this project is to each account limit (0.3.4 R9).
              'account_capacity': None,
              # O0b RS-A round 1 F14 (RS-A-6): this project's account holds by kind, with one plain line.
              'account_holds': None,
              # O10 D6: this project's launch journal entries no running service can settle
              # (never-settling states, unreadable entries), each with its age.
              'unresolved_launches': None,
              # C3 (RS-C-12): this project's run containers by state, each one that could not be
              # stopped named with its reason; each of those is also an owner alert (attention).
              'container_holds': None,
              # HM-0: the health monitor's last pass and the findings this project may see (its own and the
              # machine's); each reported one is also an owner alert (attention). None: not reported.
              'health_monitor': None,
              # RS-E E2a-11: a plain line when setup's launch record is missing (null when it exists).
              'recovery': None}
    try:
        state = CollectorPaths(state_root)
        report['client_failures'] = client_health_snapshot(state, project_root)
        from .learning_migration import start_failure
        # O10 HS-3: a learning migration that failed when the project's service last started.
        report['learning_migration_failure'] = start_failure(state, project_root)
    except (StateError, OSError, ValueError):
        return report | {'reason_code': 'status_runtime_unavailable', 'error': 'Status services are unavailable.',
                         'account_health': {'state': 'unknown', 'reason_code': 'account_health_unavailable',
                                            'reasons': ['account_health_unavailable'], 'lanes': None,
                                            'slots': None, 'attention': []}}
    collector_ready = False
    try:
        health = CollectorClient(state.socket, timeout=1).health(project_root)
        if not _apply_health(report, health):
            raise _LedgerBusy()
        report['active'] = collector_ready = True
        try:
            moved = CollectorClient(state.socket, timeout=1).learning_migration(Path(project_root))
            report['learning_migration'] = (
                ('pending' if moved['activation_pending'] else 'complete') if moved['moved']
                else 'pending' if moved['has_learning'] else 'not_needed')
            report['earlier_version_runs'] = moved['earlier_version_runs']
        except (CollectorError, OSError):
            pass  # Only this field stays unavailable.
        try:
            if not CollectorClient(state.socket, timeout=1).launch_recorded(Path(project_root)):
                report['recovery'] = LAUNCH_RECORD_MISSING
            else:  # RS-E E2c-9: the collector starter's line (null when nothing is due or outstanding)
                report['recovery'] = CollectorClient(state.socket, timeout=1).start_status(Path(project_root))
        except (CollectorError, OSError):
            pass  # Unknown here: no line.
    except CollectorError as error:
        reason = ('status_collector_transport_unavailable' if isinstance(error, CollectorTransportError)
                  else 'status_collector_unavailable')
        report.update(reason_code=reason, error='Collector status is unavailable.')
        diagnosis = collector_code_diagnosis(error)
        if diagnosis is not None:
            # health() refused this collector before it could say anything about its code (a fresh client
            # hashes the new files). Each cause is named for what it is; only `stale` says to restart.
            report.update(collector_code_stale=True if diagnosis == 'stale' else None if diagnosis == 'invalid' else False,
                          collector_code_note=_CODE_NOTES[diagnosis])
            if getattr(error, 'loaded_digest', None) is not None:
                report['collector_runtime_digest'] = error.loaded_digest  # the code it loaded
        if error.code == 'project_unregistered':
            report['ingestion'] = empty_ingestion_snapshot(registered=False)
    except OSError:
        report.update(reason_code='status_collector_transport_unavailable', error='Collector status is unavailable.')
    except _LedgerBusy:
        pass  # Active and busy: the reason is set; never read as unreachable.
    except (KeyError, TypeError, ValueError):
        report.update(reason_code='status_collector_unavailable', error='Collector status is unavailable.')
    owner_ready = False
    this_project = None
    try:
        project_id, endpoint = _account_target(state, project_root)
        this_project = project_id  # S1-H1c E4: marks the caller's own project among those holding work
        from .launch_journal import container_holds, unresolved
        if type(project_id) is str and re.fullmatch(r'prj_[0-9a-f]{32}', project_id):
            report['unresolved_launches'] = unresolved(Path(state.root) / 'projects' / project_id / 'launches')
            report['container_holds'] = container_holds(Path(state.root) / 'projects' / project_id / 'launches')
        enroll_clients(state, project_root, None, account=endpoint)
        owner = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=1).owner_key()
        # D36: status is read by AI helpers, so it says whether a key is configured, never which one.
        report['owner_key'] = {name: owner[name] for name in ('state', 'replacements', 'last_replaced_at_ms')}
        owner_ready = True
        if report.get('account_services') == 'starting':
            # O10 ST2: health was read before the account came up; read it once more so a ready
            # status shows the services running and the host's start-up timings.
            try:
                health = CollectorClient(state.socket, timeout=1).health(project_root)
                if not _apply_health(report, health):
                    collector_ready = False  # A busy second reading never ends ready (review 040-h1a6-design).
            except (CollectorError, OSError, KeyError, TypeError, ValueError):
                pass  # The first reading stands.
        if not collector_ready and report['ingestion']['registered_project_state'] == 'unavailable':
            report['ingestion']['registered_project_state'] = 'active'
    except AccountError as error:
        report['account_reason_code'] = error.reason_code
    except (DispatchError, StateError, OSError, ValueError):
        pass
    if not owner_ready and collector_ready:
        if report.get('account_services') == 'starting':  # O10 ST1
            report.update(reason_code='status_account_starting', error='The account services are still starting.')
        else:
            report.update(reason_code='status_account_unavailable', error='Account status is unavailable.')
    report['ready'] = collector_ready and owner_ready
    report['account_health'] = _account_health(report, this_project)
    _container_attention(report)
    _health_attention(report)
    return report


def _container_attention(report):
    """C3: each run container that could not be stopped is an owner alert; its slot stays held."""
    holds, health = report.get('container_holds'), report.get('account_health')
    if type(holds) is not dict or type(health) is not dict or type(health.get('attention')) is not list:
        return
    lines = [line for line in holds.get('attention') or [] if type(line) is str]
    if lines:
        health['attention'] = [*health['attention'], *lines]
        if health.get('state') == 'ok':
            health['state'] = 'degraded'


_HM_STATES = {'ok', 'findings', 'skipped', 'raised', 'timed_out', 'still_running', 'invalid'}
_HM_FINDING_KEYS = {'check', 'scope', 'subject', 'outcome', 'reason_code', 'text'}
_HM_NAME = re.compile(r'[a-z][a-z0-9_]{1,47}')
_HM_SUBJECT = re.compile(r'[A-Za-z0-9_.:@+-]{1,128}')
_HM_UNKNOWN = {'state': 'unknown', 'reason_code': 'health_monitor_invalid'}


def _status_health_monitor(view):
    """HM-0: the collector's health section, checked closed (names, codes and one plain sentence per
    finding, never more than a bounded list); None when the collector sent none (an older one, or its
    view failed); a malformed one is `unknown`, and never costs the status its ready."""
    if view is None:
        return None
    try:
        if (type(view) is not dict or view.keys() != {'last_pass_at_ms', 'pass_ms', 'summary', 'checks', 'findings', 'more'}
                or not all(v is None or account_integer(v) for v in (view['last_pass_at_ms'], view['pass_ms']))
                or not account_integer(view['more']) or type(view['summary']) is not str
                or len(view['summary']) > 200 or not view['summary'].isprintable()
                or type(view['checks']) is not list or len(view['checks']) > 64
                or type(view['findings']) is not list or len(view['findings']) > 32):
            raise ValueError()
        for check in view['checks']:
            if (type(check) is not dict or check.keys() != {'check', 'state', 'findings'}
                    or not matches(check['check'], _HM_NAME) or check['state'] not in _HM_STATES
                    or not account_integer(check['findings'])):
                raise ValueError()
        for item in view['findings']:
            if (type(item) is not dict or item.keys() != _HM_FINDING_KEYS
                    or not matches(item['check'], _HM_NAME) or item['scope'] not in {'machine', 'project'}
                    or item['outcome'] not in {'fixed', 'reported'} or not matches(item['subject'], _HM_SUBJECT)
                    or not matches(item['reason_code'], _HEALTH_CODE) or type(item['text']) is not str
                    or not 1 <= len(item['text']) <= 240 or not item['text'].isprintable()):
                raise ValueError()
        return {key: view[key] for key in ('last_pass_at_ms', 'pass_ms', 'summary', 'checks', 'findings', 'more')}
    except (ValueError, TypeError):
        return dict(_HM_UNKNOWN)


def _health_attention(report):
    """HM-0: each reported finding (and each failed check or fix) is an owner alert; a fixed one is not."""
    monitor, health = report.get('health_monitor'), report.get('account_health')
    if (type(monitor) is not dict or 'findings' not in monitor or type(health) is not dict
            or type(health.get('attention')) is not list):
        return
    lines = [f"{item['text']} (health check {item['check']}: {item['reason_code']})"
             for item in monitor['findings'] if item['outcome'] == 'reported']
    if lines:
        health['attention'] = [*health['attention'], *lines]
        if health.get('state') == 'ok':
            health['state'] = 'degraded'


_LANE_KEYS = {'faulted', 'kept', 'busy', 'queued', 'last_failure', 'held_since_ms'}
_LANE_LABELS = {'signature': 'signature', 'claims': 'claims', 'observations': 'observations',
                'ingress': 'connections', 'billing': 'billing', 'probe_worker': 'probe worker'}
_HEALTH_CODE = re.compile(r'[a-z][a-z_]{2,63}')
_TYPE_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')


def _checked_lanes(lanes):
    """The six lane notes, closed and content-free; None when missing or not all six; ValueError
    when malformed (S1-H1a C2, C3)."""
    if type(lanes) is not dict or lanes.keys() != _LANE_LABELS.keys():
        if type(lanes) is dict and lanes.keys() - _LANE_LABELS.keys() - {'available'}:
            raise ValueError()
        return None
    for lane in lanes.values():
        failure = lane.get('last_failure') if type(lane) is dict else None
        if (type(lane) is not dict or lane.keys() != _LANE_KEYS or type(lane['faulted']) is not bool
                or not all(account_integer(lane[name]) for name in ('kept', 'busy', 'queued'))
                or (lane['held_since_ms'] is not None and not account_integer(lane['held_since_ms']))
                or (failure is not None and (
                    type(failure) is not dict or failure.keys() != {'cause', 'type', 'at_ms'}
                    or not matches(failure['cause'], _HEALTH_CODE) or not matches(failure['type'], _TYPE_NAME)
                    or not account_integer(failure['at_ms'])))):
            raise ValueError()
    return lanes


def _checked_slots(slots):
    names = ('runs_past_deadline', 'runs_held_without_deadline', 'probes_held', 'probes_past_deadline')
    if slots is None:
        return None
    if type(slots) is not dict or type(slots.get('available')) is not bool:
        raise ValueError()
    if slots['available'] is False:
        if slots.get('reason') not in {'ledger_busy', 'budget_spent'}:
            raise ValueError()
        return None
    if not all(account_integer(slots.get(n)) for n in names):
        raise ValueError()
    return {name: slots[name] for name in names}


def _account_health(report, this_project=None):
    """S1-H1a-4: the account's own state from the collector's reply; `ready` is not changed, and a
    malformed new field only makes this unknown (C2)."""
    raw_lanes, raw_slots = report.pop('account_lanes', None), report.pop('account_slots', None)
    restart = report.pop('account_restart', None)
    last = report.pop('account_restart_last', None)
    waits = report.get('ledger_waits')
    try:
        health = _account_health_of(report, raw_lanes, raw_slots)
    except Exception:
        health = None
    if health is not None:
        try:
            health['attention'] += (_restart_attention(restart, this_project) + _last_restart_attention(last)
                                    + _ledger_waits_attention(waits))
            if type(restart) is dict and restart.get('action') == 'suppressed':
                health['reasons'] = [*health['reasons'], 'account_restart_suppressed']  # C6
        except Exception:
            pass  # a malformed decision is ignored; the rest of the health stands
        return health
    try:
        attention = _ledger_waits_attention(waits)
    except Exception:
        attention = []
    return {'state': 'unknown', 'reason_code': 'account_health_invalid', 'reasons': ['account_health_invalid'],
            'lanes': None, 'slots': None, 'attention': attention}


def _ledger_waits_attention(waits):
    """M4b E1a-3: refused ledger-lock waits since the collector started, as one plain line."""
    if (type(waits) is not dict or not all(type(k) is str and account_integer(v) for k, v in waits.items())):
        return []
    total = sum(waits.values())
    if not total:
        return []
    return [f"The account's records were busy {total} time{'s' if total != 1 else ''}; requests were refused "
            "and can be retried."]


def _last_restart_attention(last):
    """S1-H1c D5: the last self-restart kept, whatever the current decision."""
    from datetime import datetime, timezone
    if (type(last) is not dict or not account_integer(last.get('at_ms')) or last.get('lane') not in _LANE_LABELS
            or not matches(last.get('cause'), _HEALTH_CODE)):
        return []
    when = datetime.fromtimestamp(last['at_ms'] / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    return [f"the account restarted itself at {when}: the {_LANE_LABELS[last['lane']]} lane was stuck "
            f"({last['cause']})"]


def _restart_attention(decision, this_project=None):
    """S1-H1c-4/6: why a stuck account is not restarting itself, in plain words (counts only)."""
    if type(decision) is not dict or decision.get('action') not in {'wait', 'suppressed'}:
        return []
    cause = decision.get('cause')
    cause = f" ({cause})" if type(cause) is str and _HEALTH_CODE.fullmatch(cause) else ''
    if decision['action'] == 'suppressed':
        why = {'history_unreadable': "its record of earlier self-restarts cannot be read",
               'unrecorded': "it could not record this restart, so it did not restart",
               }.get(decision.get('reason'), "it restarted itself less than an hour ago")
        return [f"the account is stuck{cause}; {why}, so it waits (restart the collector when you are ready)"]
    held = decision.get('held') if type(decision.get('held')) is dict else {}
    parts = []
    for name, text in (('run_slots', 'run slots'), ('probes', 'probes')):
        projects = [p for p in held.get(name) or [] if matches(p, _PROJECT)] if type(held.get(name)) is list else []
        if projects:  # C6: named by their opaque project id
            shown = [p + (' (this project)' if p == this_project else '') for p in projects[:8]]
            parts.append(f"{text} in project(s) {', '.join(shown)}")
    lanes = held.get('lanes')
    if type(lanes) is dict and lanes:
        named = [f"{name} ({count})" for name, count in sorted(lanes.items())
                 if name in _LANE_LABELS and account_integer(count)]
        parts.append("work in lane(s) " + ', '.join(named))
    if held.get('unknown') is True:
        parts.append("the ledger could not say what is running")
    children = held.get('children')
    if type(children) is list and children:
        # O0c M8-M10, review M3: it waits until every process its lanes started is proven gone; say which,
        # how to check each, and what to do then.
        named = _waiting_processes(held.get('processes'), children)
        return [f"the account is stuck{cause} and waits to restart itself until it can prove the processes "
                f"it started have ended: {'; '.join(parts + named)}; once nothing is listed, restart the "
                "collector (./setup.sh repairs it)"]
    what = ', '.join(parts) or 'work still running'
    return [f"the account is stuck{cause} but is not restarting itself while something runs: {what}"]


_PROCESS_NAMES = {'signature': 'signature helper process', 'probe_worker': 'probe model process',
                  'observations': 'observation process'}


def _waiting_processes(processes, children):
    """Review M3: one plain phrase per process a self-restart waits for, with the command that checks it."""
    phrases = []
    for item in processes if type(processes) is list else []:
        if type(item) is not dict or item.get('lane') not in _PROCESS_NAMES:
            continue
        lane, why, pid = item['lane'], item.get('why'), item.get('pid')
        name, label = _PROCESS_NAMES[lane], _LANE_LABELS[lane]
        pid = pid if account_integer(pid) and pid > 1 else None
        if why == 'running':
            phrases.append(f"the {label} lane is still running a job (it ends by its own deadline)")
        elif pid is None:
            phrases.append(f"the {label} lane may have started a process whose id it does not have "
                           "(check: ps -ax for a leftover COCOLO helper)")
        elif why == 'not_reaped':
            phrases.append(f"{name} {pid} has not exited (check: ps -p {pid})")
        elif why == 'group_not_empty':
            phrases.append(f"a process is still in the group of {name} {pid} (check: pgrep -g {pid})")
        elif why == 'session_not_empty':
            phrases.append(f"a process is still in the session of {name} {pid} (check: pgrep -s {pid})")
        elif why == 'tree_not_confirmed':
            phrases.append(f"{name} {pid} was not confirmed ended with every process it started "
                           f"(check: pgrep -g {pid})")
        else:
            phrases.append(f"{name} {pid} could not be checked (check: ps -p {pid})")
    if not phrases:
        named = [_LANE_LABELS[name] for name in children if name in _LANE_LABELS]
        phrases.append(f"a process the {', '.join(named)} lane(s) started may not have ended" if named
                       else "it cannot yet say that every process it started has ended")
    return phrases


def _account_health_of(report, raw_lanes, raw_slots):
    from datetime import datetime, timezone
    lanes = _checked_lanes(raw_lanes)
    slots = _checked_slots(raw_slots)
    attention, faulted, reason = [], False, None
    for name, lane in sorted((lanes or {}).items()):
        label = _LANE_LABELS[name]
        if lane['faulted']:
            faulted = True
            failure = lane['last_failure']
            cause = f": {failure['cause']} ({failure['type']})" if failure else ''
            attention.append(f"the {label} lane is faulted{cause} (account-wide)")
        if lane['kept']:
            attention.append(f"{lane['kept']} job(s) kept in the {label} lane (account-wide)")
        if lane['held_since_ms'] is not None:
            since = datetime.fromtimestamp(lane['held_since_ms'] / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
            attention.append(f"the {label} has been held since {since} (account-wide)")
    if slots is not None:
        for name, text in (('runs_past_deadline', 'run slot(s) still counted past their deadline'),
                           ('runs_held_without_deadline', 'run slot(s) from an earlier version still held'),
                           ('probes_past_deadline', 'probe slot(s) past their deadline (wall clock)')):
            if slots[name]:
                attention.append(f"{slots[name]} {text} in this project")
    reasons = []  # C3: every missing source is named
    if lanes is None:
        reasons.append('account_health_starting' if report.get('account_services') == 'starting'
                       else 'account_health_unsupported' if report.get('collector_runtime_digest') not in (
                           None, COLLECTOR_RUNTIME_DIGEST) else 'account_health_unavailable')
    if slots is None:
        reasons.append('account_slots_' + (raw_slots['reason'] if type(raw_slots) is dict else 'unavailable'))
    reason = reasons[0] if reasons else None
    state = 'faulted' if faulted else 'degraded' if attention else 'unknown' if reason else 'ok'
    return {'state': state, 'reason_code': reason, 'reasons': reasons, 'lanes': lanes, 'slots': slots,
            'attention': attention}


class RuntimeOpenError(RuntimeError):
    def __init__(self, reason):
        self.reason_code = reason if type(reason) is str and reason in ACCOUNT_REASON_CODES | HISTORY_REASON_CODES else 'open_failed'
        super().__init__(self.reason_code)


class RuntimeOrchestrationError(RuntimeError):
    def __init__(self, reason, *, request_id=None, expected_sequence=None):
        allowed = HISTORY_REASON_CODES | {'orchestration_client_unavailable', 'host_client_mismatch'}
        self.reason_code = reason if type(reason) is str and reason in allowed else 'orchestration_client_unavailable'
        self.request_id = request_id if matches(request_id, _ID) else None
        self.expected_sequence = expected_sequence
        super().__init__(self.reason_code)


class RuntimeObservationError(RuntimeError):
    def __init__(self, reason, *, request_id=None):
        self.reason_code = reason if type(reason) is str and reason in ACCOUNT_REASON_CODES else 'observation_unavailable'
        self.request_id = request_id if matches(request_id, _ACCOUNT_ID) else None
        super().__init__(self.reason_code)


class RuntimeBillingProbeError(RuntimeError):
    def __init__(self, reason, *, request_id=None, retry_at=None):
        allowed = (ACCOUNT_REASON_CODES | BILLING_PROBE_REASONS | PROBE_COMPLETION_REASONS | ADMISSION_REASON_CODES
                   | {'collector_code_stale'})
        self.reason_code = reason if type(reason) is str and reason in allowed else 'probe_unavailable'
        self.retry_at = retry_at if self.reason_code == 'probe_cooldown' else None
        self.request_id = request_id if matches(request_id, _ACCOUNT_ID) else None
        super().__init__(self.reason_code)


def billing_probe_client(state_root, project_root, *, request_id=None):
    """One fixed account probe; retain the ID for explicit lost-reply recovery."""
    identity = 'cli-billing-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ACCOUNT_ID):
        raise RuntimeBillingProbeError('probe_invalid_request')
    if api_keys_present(os.environ):
        raise RuntimeBillingProbeError('provider_api_key_present', request_id=identity)
    try:
        state = CollectorPaths(state_root)
        try:
            stale = CollectorClient(state.socket, timeout=1).health(project_root).get('runtime_stale')
        except CollectorError as error:
            if collector_code_diagnosis(error) == 'stale':
                # health() refuses a collector whose loaded digest or version differs from this fresh client's
                # files before it can say `runtime_stale`: that is the collector running old code, and a restart
                # fixes it. A malformed identity or another build is not that: the account request reports it.
                raise RuntimeBillingProbeError('collector_code_stale', request_id=identity) from None
            stale = None  # an unreadable collector is the account request's to report
        except (OSError, ValueError, AttributeError):
            stale = None  # an unreadable collector is the account request's to report
        if stale is True:
            # The collector's loaded code is older than its files: a live check would run old code.
            raise RuntimeBillingProbeError('collector_code_stale', request_id=identity)
        project_id, endpoint = _account_target(state, project_root, await_start=True)
        enroll_clients(state, project_root, None, account=endpoint)
        result = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=60).billing_probe(
            request_id=identity)
    except DispatchError:
        raise RuntimeBillingProbeError('account_unavailable', request_id=identity) from None
    except AccountError as error:
        raise RuntimeBillingProbeError(error.reason_code, request_id=identity) from None
    if result['probe'] == 'denied' or result['reason_code'] is not None:
        raise RuntimeBillingProbeError(result['reason_code'], request_id=identity, retry_at=result.get('retry_at'))
    return {'ready': True, **result, 'request_id': identity,
        'probe': 'launched' if result['probe'] == 'completed' else result['probe'],
        'records': [{**{k: v for k, v in record.items() if k != 'client_path'},
            **({'paid_usage_waived_by_attestation': result['billing_waivers'][record['record_id']]}
                if record['record_type'] == 'provider_billing' and 'billing_waivers' in result else {})}
            for record in result['records']]}


def observe_provider_client(state_root, project_root, provider, *, request_id=None, run_handle=None, phase=None):
    """One non-spending account request; no local probe, persistence or retry."""
    identity = 'cli-observe-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ACCOUNT_ID):
        raise RuntimeObservationError('observation_invalid_request')
    try:
        request = ObservationInput(provider, run_handle, phase)
        state = CollectorPaths(state_root)
        project_id, endpoint = _account_target(state, project_root, await_start=True)
        enroll_clients(state, project_root, None, account=endpoint)
        result = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=60).observe_provider(
            request.provider, request_id=identity, run_handle=request.run_handle, phase=request.phase)
    except DispatchError:
        # COCOLO's own services cannot be reached: not a provider client problem.
        raise RuntimeObservationError('account_unavailable', request_id=identity) from None
    except (AccountError, ObservationError) as error:
        raise RuntimeObservationError(error.reason_code, request_id=identity) from None
    # Provider identity may include its local executable path in the owner
    # journal. The existing CLI projection must never print that path.
    return {'ready': True, **result, 'request_id': identity,
            'records': [{**{key: value for key, value in record.items() if key != 'client_path'},
                         **({'paid_usage_waived_by_attestation': result['billing_waivers'][record['record_id']]}
                            if record['record_type'] == 'provider_billing' and 'billing_waivers' in result else {})}
                        for record in result['records']]}


# B2 (4.4, B2-5): the primary's recheck of a recorded result still compares the body it holds with the stored one and
# the original inputs it opened with; each refusal is named and writes nothing.
def recorded_by_from_report(report, run_handle):
    """B2-10: who recorded this run, from the orchestration report's recording section (None when unknown)."""
    recording = report.get('recording') if type(report) is dict else None
    for entry in (recording or {}).get('recorded', ()):
        if entry.get('run_handle') == run_handle:
            return entry.get('recorded_by')
    return None


RECHECK_REASONS = frozenset({'recheck_result_mismatch', 'recheck_inputs_mismatch'})


class RuntimeResultError(RuntimeError):
    def __init__(self, reason):
        allowed = (HISTORY_REASON_CODES | DISPATCH_REASONS | RECHECK_REASONS
                   | {'result_client_unavailable', 'host_client_mismatch'})
        self.reason_code = reason if type(reason) is str and reason in allowed else 'result_client_unavailable'
        super().__init__(self.reason_code)


LEARNING_MAX_AGE_MS = 90 * 24 * 60 * 60 * 1000  # The ledger's retention, the cycle's default window.


def learning_cycle_client(state_root, project_root, *, host_client, full=False,
                          max_age_ms=LEARNING_MAX_AGE_MS, seconds=20.0):
    """Run this project's learning cycle through its host and return its report.

    Called after a result or QA is already committed, so nothing here raises: a
    cycle that fails, is refused or is still running when the wait ends comes back
    as the fixed unavailable shape with its reason. The host finishes a running
    cycle on a later request. The compact summary comes from the host; the full
    report is read in slices only when asked for.
    """
    from .cli_reports import unavailable_cycle

    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=10)
        cycle_id = 'cycle-' + secrets.token_hex(16)
        deadline = time.monotonic() + seconds
        while True:
            enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
            view = history.learning_cycle(cycle_id, max_age_ms=max_age_ms)
            if view['state'] != 'pending':
                break
            if time.monotonic() >= deadline:
                return unavailable_cycle('learning_pending')
            time.sleep(0.05)
        if view['state'] == 'failed':
            return unavailable_cycle(view['reason_code'])
        if not full:
            return view['summary']
        chunks, offset = [], 0
        while offset < view['report_bytes']:
            enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
            part = history.learning_report(cycle_id, offset)
            if not part['chunk'] or part['report_bytes'] != view['report_bytes']:
                return unavailable_cycle('learning_unavailable')
            chunks.append(part['chunk'])
            offset = part['next_offset']
        return parse_bounded_json(''.join(chunks).encode('ascii'), max_bytes=view['report_bytes'])
    except Exception:
        return unavailable_cycle('learning_unavailable')


def owner_learning_client(state_root, project_root, operation, **arguments):
    """One owner learning write on this project's own service: 'feedback' or 'review_case'.

    Enrolled under the owner's terminal label. Returns the service's answer; a
    refused signature or an unreachable service raises HistoryError.
    """
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
    except (DispatchError, StateError, OSError):
        raise HistoryError('history_service_unavailable') from None
    history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=30)
    enroll_clients(state, project_root, scope, (endpoint,), host_client=OWNER_CLIENT)
    if operation == 'feedback':
        return history.learning_feedback(arguments['record'], arguments['signature'])
    return history.learning_review_case(**arguments)


def holds_report_client(state_root, project_root, *, host_client):
    """B7a counts from this project's enrolled service, or None while unavailable."""
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=2)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        return history.holds_report()
    except Exception:
        return None  # A diagnostic; no database fallback or private error text.


def takeover_report_client(state_root, project_root, *, host_client, run_handle=None):
    """New B4t diagnostic operation; existing replies keep their closed shapes."""
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=2)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        return history.takeover_report(run_handle)
    except Exception:
        return None


def account_takeover_report_client(state_root, project_root, *, host_client, predecessor_version=None, await_start=False):
    """Read every bounded diagnostic page; enrollment remains per request."""
    try:
        state = CollectorPaths(state_root)
        project_id, endpoint = _account_target(state, project_root, await_start=await_start)
        client = AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=2)
        offset, result = 0, None
        while True:
            enroll_clients(state, project_root, None, account=endpoint, host_client=host_client)
            page = client.takeover_report(offset, predecessor_version)
            if result is None:
                result = dict(page, updates=[])
            result['updates'].extend(page['updates'])
            if page['next_offset'] is None:
                result['next_offset'] = None
                return result
            offset = page['next_offset']
    except (DispatchError, StateError, OSError, AccountError):
        return None


def capacity_report_client(state_root, project_root, *, host_client):
    """Each limit of this project's store with its live count, or None when its service is not running."""
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=2)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        return history.capacity_report()
    except Exception:
        return None  # A diagnostic: it never stops learning-status (review 034-security).


def orchestration_counts_client(state_root, project_root, *, host_client):
    """This project's orchestrations by state from its service's store (T7 S2, L14.1), or None when its service is
    not running or does not answer; a diagnostic for learning-status, never a refusal of it."""
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=2)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        return history.orchestration_counts()
    except Exception:
        return None


def orchestration_report_client(state_root, project_root, orchestration_id, *, host_client):
    """The statistics of an orchestration this project's service ran, or None if it holds none by that name."""
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=30)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        return history.orchestration_report(orchestration_id)
    except (HistoryError, DispatchError, StateError, OSError):
        return None


def provider_state_snapshot_client(state_root, project_root, *, host_client, unavailable=None):
    """Advisory state built by the account owner; never supplied by task JSON.

    None when there is none; `unavailable`, a list, then receives the reason code. The account
    endpoint is found as every other account read finds it (`_account_target`): a project hint
    published before the account services listened names none, and is not a reason to route blind.
    """
    try:
        state = CollectorPaths(state_root)
        project_id, endpoint = _account_target(state, project_root)
        enroll_clients(state, project_root, None, account=endpoint, host_client=host_client)
        return AccountClient(endpoint, project_id, COLLECTOR_RUNTIME_DIGEST, timeout=10).provider_state_snapshot()
    except (DispatchError, StateError, OSError, AccountError) as error:
        # Advisory only (decision 124f): an unavailable or refused snapshot routes as not_checked and never
        # replaces the refusal the real admission or project path gives. Its reason is kept, not hidden.
        if unavailable is not None:
            reason = getattr(error, 'reason_code', None)
            unavailable.append(reason if type(reason) is str and reason else type(error).__name__)
        return None


def routing_evidence_client(state_root, project_root, task, *, host_client):
    """The task's routing evidence this project's service vouches for, or None.

    None means the service does not hold one of the cited runs (or is not
    running): the legacy ledger may. A cited run the service does hold but that
    is not accepted reconnaissance for this task is a refusal, not None.
    """
    references = task['routing_evidence_refs']
    try:
        state = CollectorPaths(state_root)
        scope, _, endpoint = read_runtime_hint(state, project_root)
        history = HistoryClient(endpoint, scope.project_id, scope.runtime_digest, timeout=10)
        for reference in references:
            kind, _, digest = reference.partition(':')
            if kind != 'outcome':
                raise ContractError('routing evidence must be a typed host outcome digest')
            enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
            if not history.routing_evidence(digest, task_id=task['id'], family=task['family']):
                raise ContractError('routing evidence is not accepted reconnaissance for this task')
    except HistoryError as error:
        if error.reason_code in {'history_not_authorized', 'history_service_unavailable'}:
            return None
        raise ContractError('routing evidence could not be checked') from None
    except (DispatchError, StateError, OSError):
        return None
    return frozenset(references)


class HostOutcomeUnknown(RuntimeError):
    """The outcome is not one this project's host recorded; the legacy ledger owns it."""


def _host_pair(state_root, project_root, host_outcome_digest, verifier_host_outcome_digest, *, host_client):
    """Both recorded runs' bindings from this project's host, or HostOutcomeUnknown."""
    try:
        state = CollectorPaths(state_root)
        scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
    except Exception:
        raise HostOutcomeUnknown() from None  # No state or no running host: nothing it recorded.
    history = HistoryClient(result_endpoint, scope.project_id, scope.runtime_digest, timeout=10)
    pair = []
    for digest in (host_outcome_digest, verifier_host_outcome_digest):
        enroll_clients(state, project_root, scope, (result_endpoint,), host_client=host_client)
        try:
            pair.append(history.worker_input(digest))
        except HistoryError as error:
            if error.reason_code == 'history_not_authorized' and not pair:
                raise HostOutcomeUnknown() from None
            raise
    worker, verifier = (AttemptBinding(**value['worker']['binding']) for value in pair)
    return state, scope, result_endpoint, history, pair, worker, verifier


def operational_checks_client(state_root, project_root, host_outcome_digest, verifier_host_outcome_digest,
                              run_checks, *, host_client):
    """Run the fixed checks for a host-recorded worker and verifier and get a receipt from the project.

    The report reaches the project writer as this request's protected context,
    never as request JSON; the writer binds it to both kept originals.
    """
    state, scope, endpoint, history, _, worker, verifier = _host_pair(
        state_root, project_root, host_outcome_digest, verifier_host_outcome_digest, host_client=host_client)
    contexts = enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
    history.operational_qa_scope(worker, verifier, host_outcome_digest, verifier_host_outcome_digest)
    diagnostics = []
    report = run_checks(diagnostics)
    contexts = enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
    contexts[endpoint] = canonical_json({'purpose': 'checks', 'report': report})
    request_id = 'qa-checks-' + content_digest({'worker': host_outcome_digest, 'verifier': verifier_host_outcome_digest,
                                                'report': report})[:48]
    receipt = history.issue_operational_check_receipt(worker, verifier, host_outcome_digest,
                                                      verifier_host_outcome_digest, request_id=request_id)
    return {'ready': True, **receipt, 'check_report': report, 'diagnostics': diagnostics}


def record_operational_qa_client(state_root, project_root, host_outcome_digest, verifier_host_outcome_digest,
                                 check_receipt, evaluator_assignment, evaluator_result, verifier_assignment,
                                 verifier_result, *, host_client):
    """Commit QA for a host-recorded pair; the files must be the exact recorded originals and results."""
    state, scope, endpoint, history, pair, worker, verifier = _host_pair(
        state_root, project_root, host_outcome_digest, verifier_host_outcome_digest, host_client=host_client)
    for value, binding, assignment, result in ((pair[0], worker, evaluator_assignment, evaluator_result),
                                               (pair[1], verifier, verifier_assignment, verifier_result)):
        if canonical_json(value['worker']['assignment']) != canonical_json(assignment):
            raise HistoryError('history_conflict')
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        body = ResultReadClient(endpoint, timeout=10).read_result(binding, value['reference']['result_digest'])
        if canonical_json(parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)) != canonical_json(result):
            raise HistoryError('history_conflict')
    enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
    committed = history.commit_operational_qa(worker, verifier, host_outcome_digest, verifier_host_outcome_digest,
                                              check_receipt)
    return {'ready': True, **committed}


def record_codex_result_client(state_root, project_root, task, route, assignment, parent_grant, result, observation,
                               *, host_client):
    """Admit only the original published terminal; never transmit a replacement body."""
    try:
        if (type(route) is not dict or route.get('provider') not in BRIDGE_PROVIDERS or route.get('dispatch') != 'bridge'
                or type(observation) is not dict
                or observation.keys() - {'run_handle', 'result_attempts', 'terminal_state'}
                or observation.get('result_attempts') != [{'status': 'accepted', 'reason_code': None, 'receipt_id': None}]):
            raise HistoryError('history_invalid_input')
        submitted = canonical_json(result)
        if not 0 < len(submitted) <= MAX_RESULT_BODY_BYTES:
            raise HistoryError('history_invalid_input')
        inputs = open_inputs_digest(assignment, task, route, parent_grant)
        state = CollectorPaths(state_root)
        scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (endpoint, result_endpoint), host_client=host_client)
        history = HistoryClient(result_endpoint, scope.project_id, scope.runtime_digest, timeout=10)
        # D1: the published attempt is found in the project store from its run handle, attempt and inputs, so it
        # is recorded whether or not the service that ran it still holds it (a successor records it the same way).
        try:
            found = history.result_reference(observation.get('run_handle'), assignment.get('attempt'), inputs)
        except HistoryError as missing:
            # O0d R1: said as before D1: no such dispatch, or one opened with other original inputs.
            if missing.reason_code == 'history_not_found':
                raise DispatchError('dispatch_not_found') from None
            if missing.reason_code == 'history_conflict':
                raise RuntimeResultError('recheck_inputs_mismatch') from None  # B2-5: said by name
            raise
        binding = AttemptBinding(**found['binding'])
        current = OrchestrationClient(result_endpoint, OrchestrationScope(**vars(scope)), timeout=10).call(
            'orchestration.status', {'orchestration_handle': binding.orchestration_handle})
        if current['current']['snapshot']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        delivery = found['delivery']
        reference = HostResultReference(binding, delivery['record_id'], delivery['record_content_digest'], delivery['result_digest'])
        terminal = history.read_record(reference.terminal_record_id)
        record, metadata = terminal['record'], terminal['metadata']
        if (metadata['source'] != 'host' or metadata['content_digest'] != reference.terminal_content_digest
                or record['result_digest'] != reference.result_digest
                or record['bridge_run_id'] != binding.claim_id
                or observation.get('terminal_state') != record['terminal_state']):
            raise HistoryError('history_conflict')
        body = ResultReadClient(result_endpoint, timeout=10).read_result(binding, reference.result_digest)
        if submitted != canonical_json(parse_bounded_json(body, max_bytes=MAX_RESULT_BODY_BYTES)):
            raise RuntimeResultError('recheck_result_mismatch')  # B2-5: the handed body is not the stored one
        # Only this durable reference crosses the mutation boundary. The writer
        # rechecks original inputs, result, current authority and expiry itself.
        admitted = history.admit_host_result(reference)
        records = {}
        for kind in ('event', 'outcome'):
            commit = admitted[kind+'_commit']
            entry = history.read_record(commit['record_id'])
            if (entry['metadata'] != commit or entry['record']['record_type'] != kind
                    or entry['record']['run_id'] != binding.run_id):
                raise HistoryError('history_conflict')
            records[kind] = entry['record']
        try:
            # Tell the project host the result is admitted, so it may retire the run's custody.
            contexts = enroll_clients(state, project_root, scope, (endpoint, Path(endpoint).parent / 'run.sock'),
                                      host_client=host_client)
            if (Path(endpoint).parent / 'run.sock').exists():
                _run_driver(contexts, endpoint, 'run.recorded', binding.run_handle, binding.attempt, inputs,
                            {'purpose': 'recorded',
                             'host_outcome_digest': admitted['admission']['host_outcome_digest']}, timeout=5)
        except Exception:
            pass  # A hint only: the run's dispatch view then stays its short while (D1).
        recorded_by = None
        try:  # B2: who recorded it (best effort): a result the service recorded first is only retrieved here
            recorded_by = recorded_by_from_report(history.orchestration_report(
                current['current']['snapshot']['orchestration_id']), binding.run_handle)
        except Exception:
            pass
        # The validator's safe reason and the client's diagnostics, from the durable terminal record.
        diagnostics = {name: record[name] for name in ('result_validation_reason_code', 'client_diagnostics')
                       if record.get(name) is not None}
        return {'ready': True, **admitted, **records,
                'host_outcome_digest': admitted['admission']['host_outcome_digest'], 'capacity_state': 'retained',
                **({'dispatch_diagnostics': diagnostics} if diagnostics else {}),
                **({'already_recorded_by': 'service', 'recorded_note': 'recorded by the service, not reviewed by the primary'}
                   if recorded_by == 'service' else {})}
    except RuntimeResultError:
        raise
    except (DispatchError, HistoryError, HostClientMismatch) as error:
        raise RuntimeResultError(error.reason_code) from None
    except Exception:
        raise RuntimeResultError('result_client_unavailable') from None


def _goal_usage(snapshot):
    """The goal part of the legacy report; per-run totals come only from recorded runs."""
    tokens, seconds = snapshot['goal_tokens_used'], snapshot['goal_time_used_seconds']
    return {'goal_total_tokens': tokens, 'goal_time_used_seconds': seconds,
            'goal_usage_source': 'unavailable' if tokens is None and seconds is None else 'host_reported'}


def orchestration_lifetime_client(state_root, project_root, *, host_client, orchestration_handle,
                                  expected_sequence, request_id, session_id, next_session_id, cause):
    """Runner safe-point call: enroll normally, then one atomic continuation exchange.

    Persist all arguments before sending. An ambiguous reply is reconciled by
    explicitly calling again with those exact arguments; there is no auto retry.
    Capacity checks run before each new registration or journal mutation, with
    admission serialized by the runner until the returned handle is adopted.
    """
    if not matches(request_id, _ID):
        raise RuntimeOrchestrationError('history_invalid_request', request_id=request_id)
    try:
        state = CollectorPaths(state_root)
        scope, control, endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (control, endpoint), host_client=host_client)
        client = OrchestrationClient(endpoint, OrchestrationScope(**vars(scope)), timeout=10)
        result = client.call('orchestration.rollover', {
            'orchestration_handle': orchestration_handle, 'expected_sequence': expected_sequence,
            'session_id': session_id, 'next_session_id': next_session_id, 'cause': cause}, request_id=request_id)
        if result['current']['snapshot']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        return result
    except DispatchError:
        raise RuntimeOrchestrationError('orchestration_client_unavailable', request_id=request_id) from None
    except (HistoryError, HostClientMismatch) as error:
        raise RuntimeOrchestrationError(error.reason_code, request_id=request_id,
                                       expected_sequence=expected_sequence) from None


def orchestration_client(state_root, project_root, operation, *, host_client, orchestration_handle=None,
                         request_id=None, expected_sequence=None, goal_tokens_used=None, goal_time_used_seconds=None,
                         status=None):
    """One lifecycle mutation, with explicit identity/sequence for reconciliation."""
    identity = 'cli-orc-'+secrets.token_hex(16) if request_id is None else request_id
    if not matches(identity, _ID):
        raise RuntimeOrchestrationError('history_invalid_request')
    captured_sequence = None
    try:
        state = CollectorPaths(state_root)
        scope, control, endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (control, endpoint), host_client=host_client)
        actor = OrchestrationScope(**vars(scope))
        client = OrchestrationClient(endpoint, actor, timeout=10)
        if operation == 'orchestration.open':
            payload = {}
        else:
            payload = {'orchestration_handle': orchestration_handle}
            try:
                current = client.call('orchestration.status', payload,
                    request_id=identity if operation == 'orchestration.status' else 'orc-read-'+content_digest({'request': identity}))
            except HistoryError as error:
                # An enrolled read of an existing orchestration is refused only when
                # another host class owns it: keep the documented lifecycle reason.
                if error.reason_code == 'history_not_authorized':
                    raise HostClientMismatch('host_client_mismatch') from None
                raise
            snapshot = current['current']['snapshot']
            if snapshot['client_version'] != host_client:
                raise HostClientMismatch('host_client_mismatch')
            if operation == 'orchestration.status':
                return {'ready': True, **snapshot, **current, 'usage': _goal_usage(snapshot), 'request_id': identity}
            payload['expected_sequence'] = snapshot['sequence'] if expected_sequence is None else expected_sequence
            if operation in {'orchestration.checkpoint', 'orchestration.close'}:
                payload.update(goal_tokens_used=goal_tokens_used, goal_time_used_seconds=goal_time_used_seconds)
            if operation == 'orchestration.close':
                payload['status'] = status
        request = OrchestrationRequest(1, operation, identity, actor, payload)
        captured_sequence = request.payload.get('expected_sequence')
        result = client.call(operation, request.payload, request_id=identity)
        snapshot = result['current']['snapshot']
        if snapshot['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
    except DispatchError:
        raise RuntimeOrchestrationError('orchestration_client_unavailable', request_id=identity) from None
    except (HistoryError, HostClientMismatch) as error:
        raise RuntimeOrchestrationError(error.reason_code, request_id=identity,
                                       expected_sequence=captured_sequence) from None
    return {'ready': True, **snapshot, **result, 'usage': _goal_usage(snapshot), 'request_id': identity,
            **({'expected_sequence': captured_sequence} if captured_sequence is not None else {})}


def _run_driver(contexts, control, operation, run_handle, attempt, inputs_digest, context, *, timeout):
    """One exchange with the project host's run driver; its reply is metadata only."""
    driver = Path(control).parent / 'run.sock'
    contexts[driver] = canonical_json(context)
    raw = exchange_frame(driver, canonical_json({'protocol': 1, 'operation': operation, 'run_handle': run_handle,
        'attempt': attempt, 'inputs_digest': inputs_digest}), timeout=timeout, response_limit=4_096)
    value = parse_bounded_json(raw, max_bytes=4_096)
    if (value.keys() != {'protocol', 'state', 'reason_code'} or value['protocol'] != 1
            or value['state'] not in {'preparing', 'ready', 'failed', 'refused'}):
        raise ValueError()
    return value


def run_control_client(state_root, project_root, operation, orchestration_handle, run_handle, attempt, *, host_client):
    """One live status read or cancellation request for a run this host class holds.

    The reply is the project host's own observation: health, ages and remaining
    bounds, never content. An accepted cancellation is a request, not confirmed
    cleanup; a lost reply never proves that no stop was requested.
    """
    try:
        state = CollectorPaths(state_root)
        scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
        enroll_clients(state, project_root, scope, (endpoint,), host_client=host_client)
        client = ControlClient(endpoint, scope, timeout=5)
        target = ServiceTarget(orchestration_handle, run_handle, attempt)
        view = client.status(target) if operation == 'project.status' else client.cancel(target)
    except DispatchError:
        return {'ready': False, 'reason_code': 'control_unavailable', 'error': 'run control is unavailable'}
    except Exception as error:
        lost = _lost_run(state_root, project_root, run_handle, host_client)
        if lost is not None:
            return lost
        reason = getattr(error, 'reason_code', 'control_unavailable')
        return {'ready': False, 'reason_code': reason if type(reason) is str else 'control_unavailable',
                'error': 'run control refused'}
    return {'ready': True, 'observed_at_ns': view.observed_at_ns, **view.result}


def _lost_run(state_root, project_root, run_handle, host_client):
    """The fixed report for a run of an earlier, stopped service of this project, or None."""
    try:
        state = CollectorPaths(state_root)
        scope, control, _ = read_runtime_hint(state, project_root)
        driver = Path(control).parent / 'run.sock'
        contexts = enroll_clients(state, project_root, scope, (control, driver), host_client=host_client)
        reply = _run_driver(contexts, control, 'run.describe', run_handle, 1, '0' * 64, {}, timeout=5)
    except Exception:
        return None
    if reply['state'] == 'failed' and reply['reason_code'] == 'run_settling':
        # LRS-1: the new service has not finished learning what its predecessor left; no outcome is claimed.
        return {'ready': False, 'reason_code': 'run_settling',
                'error': "the project's service has just started and is still checking this run; ask again shortly"}
    if reply['state'] != 'failed' or reply['reason_code'] not in LOST_CAPACITY:
        return None
    return {'ready': False, 'reason_code': 'run_lost', 'terminal_state': 'abandoned', 'outcome': 'unknown',
            'capacity_state': LOST_CAPACITY[reply['reason_code']],
            'error': ('the service that ran this run stopped; it ended with an unknown outcome; '
                      + ('its capacity was released' if LOST_CAPACITY[reply['reason_code']] == 'released' else
                         'its capacity stays held while process exit is confirmed and settlement finishes'))}


def bridged_run_owner(state_root, project_root, run_handle, *, host_client):
    """'owned' or 'other' when this project's host holds the bridged run, else None.

    Commands that know only a run handle use it to refuse a run the host drives:
    the other host class gets the lifecycle mismatch, the owner a fixed refusal.
    """
    try:
        state = CollectorPaths(state_root)
        scope, control, result = read_runtime_hint(state, project_root)
        driver = Path(control).parent / 'run.sock'
        if not driver.exists():
            return None
        contexts = enroll_clients(state, project_root, scope, (control, driver), host_client=host_client)
        reply = _run_driver(contexts, control, 'run.describe', run_handle, 1, '0' * 64, {}, timeout=5)
    except Exception:
        return None
    if reply['state'] == 'refused':
        return 'other' if reply['reason_code'] == 'history_not_authorized' else None
    return 'owned'


def abandon_bridged_run(state_root, project_root, run_handle, reason, *, host_client):
    """Drop a host-driven run that never took a lease, or one that already closed by itself.

    A run the account refused, or one that failed before its claim was sent,
    closes without this command; called anyway, it says the run is closed.
    Anything that still holds a lease is refused.
    """
    state = CollectorPaths(state_root)
    scope, control, _ = read_runtime_hint(state, project_root)
    contexts = enroll_clients(state, project_root, scope, (control, Path(control).parent / 'run.sock'),
                              host_client=host_client)
    reply = _run_driver(contexts, control, 'run.abandon', run_handle, 1, '0' * 64, {}, timeout=5)
    if reply['state'] == 'refused' and reply['reason_code'] == 'history_not_authorized':
        raise HostClientMismatch('host_client_mismatch')
    if reply['reason_code'] == 'run_closed':
        return {'ready': True, 'run_handle': run_handle, 'classification': 'already_closed', 'reason': reason,
                'settled_by': 'project_host', 'capacity_state': 'never_claimed'}
    if reply['reason_code'] == 'run_settling':
        # An earlier service's run that this one is still closing (0.3.3, C3d).
        return {'ready': False, 'reason_code': 'run_settling',
                'error': "the project's service is still closing this run; try again in a minute"}
    if reply['reason_code'] in LOST_CAPACITY:
        # Its stopped service's run is settled by the next service on its own, never by this command.
        return {'ready': True, 'run_handle': run_handle, 'classification': 'already_closed', 'reason': reason,
                'settled_by': 'project_host', 'capacity_state': LOST_CAPACITY[reply['reason_code']]}
    if reply['reason_code'] != 'run_abandoned':
        return {'ready': False, 'error': 'only a run that never took a lease can be abandoned here',
                'reason_code': reply['reason_code'] or 'history_conflict'}
    return {'ready': True, 'run_handle': run_handle, 'classification': 'abandoned', 'reason': reason,
            'settled_by': 'project_host', 'capacity_state': 'never_claimed'}


def open_codex_client(state_root, project_root, orchestration_handle, task, route, assignment, parent_grant, *, host_client):
    """Read original project scope, then open once through the account API.

    Trusted native preparation supplies original inputs and reserves its local
    resources independently. Neither this client nor the returned metadata can
    create a launch ticket or replace that protected preparation.
    """
    if (host_client not in HOST_CLIENTS or route.get('provider') not in BRIDGE_PROVIDERS or route.get('dispatch') != 'bridge'
            or route.get('sandbox_mode') != 'read-only' or assignment.get('permissions', {}).get('network') is not False
            or any(not p.endswith(':read') for p in assignment.get('permissions', {}).get('filesystem', []))
            or api_keys_present(os.environ)):
        raise RuntimeOpenError('open_not_authorized')
    try:
        state = CollectorPaths(state_root)
        cause = _hint_cause(state, project_root)
        if cause is not None:
            raise RuntimeOpenError(cause)
        try:
            scope, endpoints = _read_runtime_endpoints(state, project_root, account=True)
        except DispatchError:  # A valid hint whose services cannot be reached.
            raise RuntimeOpenError('open_service_unreachable') from None
        if len(endpoints) != 3:  # a version-1 hint: an earlier version's service, which cannot open
            raise RuntimeOpenError('open_service_other_version')
        contexts = enroll_clients(state, project_root, scope, endpoints[:2], account=endpoints[2], host_client=host_client)
        prospective = OrchestrationScope(**vars(scope))
        digest = open_inputs_digest(assignment, task, route, parent_grant)
        originals = {'assignment': assignment, 'task': task, 'route': route, 'parent_grant': parent_grant}
        # The trusted CLI hands each owner the originals for exactly this request.
        contexts[endpoints[1]] = canonical_json({'purpose': 'open', 'orchestration_handle': orchestration_handle,
                                                 **originals})
        history = HistoryClient(endpoints[1], scope.project_id, scope.runtime_digest, timeout=10)
        project_scope = history.original_open_scope(prospective, orchestration_handle, digest)
        if project_scope['facts']['client_version'] != host_client:
            raise HostClientMismatch('host_client_mismatch')
        # A verifier carries the exact worker this host recorded, as the project bound it.
        worker = None
        if task.get('verification_for_outcome') is not None:
            contexts[endpoints[1]] = None
            worker = history.worker_input(task['verification_for_outcome'])['worker']
            if worker['binding']['orchestration_handle'] != orchestration_handle:
                raise RuntimeOpenError('open_not_authorized')
        opening = AccountOpenInput(prospective, orchestration_handle, digest)
        contexts[endpoints[2]] = canonical_json({'purpose': 'open', 'project_scope': project_scope,
            'prospective': {'scope': prospective.as_data(), 'orchestration_handle': orchestration_handle,
                            **originals, 'worker': worker}})
        identity = 'cli-open-' + content_digest(opening.payload())
        reply = AccountClient(endpoints[2], scope.project_id, scope.runtime_digest, timeout=10).open_host_run(
            opening, request_id=identity)
    except DispatchError:
        raise RuntimeOpenError('open_service_unreachable') from None
    except (AccountError, HistoryError, OpenError) as error:
        raise RuntimeOpenError(error.reason_code) from None
    opened = reply['state'] == 'opened'
    registered = False
    if opened:
        # The project host keeps the run's originals for its dispatch; the account
        # run is open either way, and an unregistered run is refused at dispatch.
        try:
            contexts = enroll_clients(state, project_root, scope, (endpoints[0], Path(endpoints[0]).parent / 'run.sock'),
                                      host_client=host_client)
            registered = _run_driver(contexts, endpoints[0], 'run.register', reply['run_handle'], assignment['attempt'],
                digest, {'purpose': 'register', 'orchestration_handle': orchestration_handle, 'run_id': reply['run_id'],
                         'host_client': host_client, **originals}, timeout=10)['state'] != 'refused'
        except Exception:
            registered = False
    # The legacy open report's fields, projected from the account receipt.
    return {'ready': opened, **reply, 'admission': 'admitted' if opened else 'denied',
            'status': 'started' if opened else 'denied',
            'observation_started_at_ms': reply['recorded_at_ms'] if opened else None,
            # Only a bridged Claude child's reads are contained (its client enforces the tool set).
            'read_containment': 'enforced' if route['provider'] == 'claude' else 'advisory',
            'project_host_registered': registered}


def dispatch_codex_client(state_root, project_root, run_handle, task, route, assignment, parent_grant, *, expected_export_commit=None):
    """Actual CLI path: submit once, poll metadata, read original result chunks."""
    if (route.get('provider') not in BRIDGE_PROVIDERS or route.get('dispatch') != 'bridge'
            or route.get('sandbox_mode') != 'read-only' or assignment.get('permissions', {}).get('network') is not False
            or any(not p.endswith(':read') for p in assignment.get('permissions', {}).get('filesystem', []))
            or api_keys_present(os.environ)):
        raise DispatchError('dispatch_not_authorized')
    state = CollectorPaths(state_root)
    scope, endpoint, result_endpoint = read_runtime_hint(state, project_root)
    contexts = enroll_clients(state, project_root, scope, (endpoint, result_endpoint, Path(endpoint).parent / 'run.sock'))
    client = DispatchClient(endpoint, result_endpoint, scope, timeout=2)
    seconds, _ = effective_dispatch_limits(route, assignment)
    digest = open_inputs_digest(assignment, task, route, parent_grant)
    # The project host prepares the admitted run, claims it and consumes its grant
    # before dispatch can name it. Waiting here never extends a run's own bounds.
    # A runtime without a run driver has prepared the run itself.
    deadline = time.monotonic() + 600
    while (Path(endpoint).parent / 'run.sock').exists():
        try:
            prepared = _run_driver(contexts, endpoint, 'run.prepare', run_handle, assignment['attempt'], digest,
                {'purpose': 'prepare', 'expected_export_commit': expected_export_commit}, timeout=30)
        except Exception:
            raise DispatchError('dispatch_unavailable') from None
        if prepared['state'] == 'ready':
            break
        if prepared['state'] == 'refused' and prepared['reason_code'] == 'history_not_authorized':
            raise HostClientMismatch('host_client_mismatch')
        if prepared['state'] in {'failed', 'refused'}:
            # Say why the run could not start: the driver's or the account's own fixed code.
            raise DispatchError(prepared.get('reason_code'), known=START_REASONS)
        if time.monotonic() >= deadline:
            raise DispatchError('dispatch_unavailable')
    old = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(_signum, _frame):
            raise KeyboardInterrupt()
        for signum in (signal.SIGINT, signal.SIGTERM):
            old[signum] = signal.signal(signum, interrupted)
    try:
        result = client.dispatch(run_handle, assignment['attempt'], digest,
                                 wait_seconds=seconds+60, expected_export_commit=expected_export_commit)
        if result['bridge_report']['requested_model'] != assignment['model'] or result['bridge_report']['effort'] != assignment['effort']:
            raise DispatchError('dispatch_invalid_response')
        result['bridge_report']['effective_bounds'] = effective_dispatch_bounds(route, assignment)
        return result
    except KeyboardInterrupt:
        binding = client.last_binding
        if binding is not None:
            try:
                ControlClient(endpoint, scope, timeout=2).cancel(
                    ServiceTarget(binding.orchestration_handle, binding.run_handle, binding.attempt))
            except Exception:
                pass  # A missing cancellation acknowledgement never confirms cleanup.
        raise DispatchError('dispatch_unavailable') from None
    finally:
        for signum, handler in old.items():
            signal.signal(signum, handler)
