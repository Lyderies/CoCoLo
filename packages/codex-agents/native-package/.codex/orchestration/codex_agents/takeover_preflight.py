"""Read-only B4t planning. No owner open, enrollment, start or stop happens here."""
from __future__ import annotations

import ast
import base64
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sqlite3
import time

from .collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION, CollectorClient
from .collector_contracts import COLLECTOR_PROTOCOL_VERSION, CollectorError, LEDGER_SCHEMA_VERSION
from .collector_paths import _canonical_project_root
from .learning_contracts import keyed_digest
from .runtime_lineage import release_versions, verified_predecessor
from .takeover_journal import TakeoverJournal, MAX_AUTHORIZATION_BYTES
from .update_takeover import TakeoverError


def _accepted(identity, releases):
    return identity == dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST) or verified_predecessor(
        identity, PACKAGE_VERSION, releases=releases)


def checked_collector_identity(health, authorization, *, releases=None):
    releases = release_versions() if releases is None else releases
    identity = dict(version=health.get('runtime_version'), digest=health.get('runtime_digest'))
    if (not _accepted(identity, releases) or authorization is None
            or authorization.get('runtime_version') != identity['version']
            or authorization.get('runtime_digest') != identity['digest']):
        raise TakeoverError('predecessor_unverified')
    return identity


def trusted_package(root, *, releases=None):
    """Hash an older package's literal runtime list, without importing its code.

    A changed list changes the release digest too. The shipped catalogue must
    recognize the whole resulting digest/version pair, independently of the
    install manifest's per-file integrity check.
    """
    from .contracts import ContractError
    from .learning_engine import _installed_managed_files
    releases = release_versions() if releases is None else releases
    root = Path(root)
    try:
        _, contents = _installed_managed_files(root)
        prefix = '.codex/orchestration/'
        source = ast.parse(contents[prefix + 'codex_agents/runtime_files.py'])
        assignments = [node for node in source.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'RUNTIME_FILES' for target in node.targets)]
        if len(assignments) != 1:
            raise ValueError()
        names = ast.literal_eval(assignments[0].value)
        if (type(names) is not tuple or not names or len(names) != len(set(names))
                or any(type(name) is not str or name.startswith('/') or '..' in Path(name).parts for name in names)):
            raise ValueError()
        manifest = root / prefix / 'install-manifest.json'
        if manifest.exists():
            version = json.loads(manifest.read_bytes())['version']
        else:
            version = (root / 'VERSION').read_text(encoding='ascii').strip()
        hasher = hashlib.sha256()
        for name in names:
            raw, encoded = contents[prefix + name], name.encode('utf-8')
            hasher.update(len(encoded).to_bytes(4, 'big') + encoded)
            hasher.update(len(raw).to_bytes(8, 'big') + raw)
        identity = dict(version=version, digest=hasher.hexdigest())
        if not _accepted(identity, releases):
            raise ValueError()
        return identity
    except (ContractError, OSError, KeyError, TypeError, ValueError, SyntaxError, RecursionError):
        raise TakeoverError('predecessor_unverified') from None


def _lock_held(path):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    except FileNotFoundError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _peer(endpoint):
    from .core_api import peer_pid, peer_uid
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(str(endpoint))
        pid = peer_pid(connection)
        if peer_uid(connection) != os.getuid() or pid is None:
            raise TakeoverError('predecessor_unproven')
        return pid


def _registry(state, root_digest):
    from .active_run_capacity import crashed_claim
    from .legacy_slots import register_legacy_functions
    database = state.root / 'learning.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        row = db.execute("SELECT * FROM projects WHERE root_digest=? AND status='active'", (root_digest,)).fetchone()
        version = db.execute('PRAGMA user_version').fetchone()[0]
        from .takeover_schema import check_floor
        check_floor(db, PACKAGE_VERSION, lambda: TakeoverError('predecessor_unverified'))
        if version > LEDGER_SCHEMA_VERSION:
            raise TakeoverError('predecessor_unverified')
        children = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='active_run_capacity'").fetchone():
            register_legacy_functions(db)
            legacy = db.execute("SELECT 1 FROM active_run_capacity WHERE kind='legacy_unknown' AND released_at_ms IS NULL LIMIT 1").fetchone() is not None
            crashed, parameters = crashed_claim('a', time.time_ns() // 1_000_000, time.monotonic_ns(), legacy_slots=legacy)
            children = {item['project_id']: item['n'] for item in db.execute(
                "SELECT project_id,COUNT(*) n FROM active_run_capacity a WHERE released_at_ms IS NULL "
                "AND kind!='legacy_unknown' AND NOT " + crashed + ' GROUP BY project_id', parameters)}
        return None if row is None else dict(row), version, children


def _project_owner(state, project_id):
    """An offline store's local epoch selects which trusted package to prove."""
    from .collector_paths import _assert_no_symlink_components, _check_owner_mode
    from .takeover_schema import check_floor
    if type(project_id) is not str or re.fullmatch(r'prj_[0-9a-f]{32}', project_id) is None:
        raise TakeoverError('predecessor_unverified')
    path = state.root / 'projects' / project_id / 'history.sqlite3'
    try:
        _assert_no_symlink_components(path)
        try:
            os.lstat(path)
        except FileNotFoundError:
            return None
        _check_owner_mode(path, directory=False)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.execute('PRAGMA query_only=ON')
            check_floor(db, PACKAGE_VERSION, lambda: TakeoverError('predecessor_unverified'))
            if db.execute('PRAGMA user_version').fetchone()[0] > 23:
                raise TakeoverError('predecessor_unverified')
            if db.execute('SELECT project_id FROM history_identity LIMIT 2').fetchall() != [(project_id,)]:
                raise TakeoverError('predecessor_unverified')
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_takeover'").fetchone() is None:
                return None
            rows = db.execute('SELECT new_digest FROM runtime_takeover WHERE singleton=1').fetchall()
            if len(rows) != 1 or type(rows[0][0]) is not str or re.fullmatch(r'[0-9a-f]{64}', rows[0][0]) is None:
                raise TakeoverError('predecessor_unverified')
            return rows[0][0]
    except (OSError, sqlite3.Error, ValueError):
        raise TakeoverError('predecessor_unverified') from None


def client_paths(state):
    """The client paths type the hint readers accept (`runtime_hint_path` refuses any other type).

    Owner commands hold a LearningState; given one, every hint read refused as `dispatch_unavailable`,
    so a live predecessor service was never seen and its held locks read as `project_store_busy`
    (S5 final fix, found by the native archive witness)."""
    from .collector_paths import CollectorPaths
    return state if type(state) is CollectorPaths else CollectorPaths(state.root)


def read_plan(state, project_root, *, runtime_locked=False):
    """Return the trusted intent and a redacted plan. Every refusal precedes J1."""
    from .collector_runtime import _collector_runtime_authorization
    from .project_host_client import _hinted_host
    from .takeover_process import pin_predecessor
    if not runtime_locked:
        deadline = time.monotonic() + 20.0
        while _lock_held(state.runtime_lock):
            if time.monotonic() >= deadline:
                raise TakeoverError('update_busy')
            time.sleep(0.02)
    root = _canonical_project_root(Path(project_root))
    root_digest = keyed_digest(state.digest_key(), str(root))
    try:
        row, schema, children = _registry(state, root_digest)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        raise TakeoverError('ledger_unreadable') from None
    journal = TakeoverJournal(state.root).read()
    if journal is not None:
        if journal['root_digest'] != root_digest or row is None or journal['project_id'] != row['project_id']:
            from .compatibility_message import compatibility_message
            foreign, _, _ = _registry(state, journal['root_digest'])
            if (foreign is None or foreign['project_id'] != journal['project_id']
                    or keyed_digest(state.digest_key(), foreign['canonical_root']) != journal['root_digest']):
                raise TakeoverError('predecessor_unverified')
            message = compatibility_message(None, project_root=Path(foreign['canonical_root']), state_name='project service')
            raise TakeoverError('takeover_unfinished', update_message=message)
        if journal['successor'] != dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST):
            raise TakeoverError('predecessor_unverified')
        return dict(needed=True, intent=dict(journal, prior_authorization=base64.b64decode(
                        journal['prior_authorization'], validate=True)), report=dict(takeover='resume',
                        phase=journal['phase'], records_upgrade=False, children=children.get(row['project_id'], 0),
                        other_children=sum(n for project, n in children.items() if project != row['project_id'])))
    releases = release_versions(project_root=root)
    authorization = None
    prior = b''
    if state.collector_authorization.exists() or state.collector_authorization.is_symlink():
        authorization = _collector_runtime_authorization(state, allow_quiesced=False)
        descriptor = os.open(state.collector_authorization, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            prior = os.read(descriptor, MAX_AUTHORIZATION_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(prior) > MAX_AUTHORIZATION_BYTES:
            raise TakeoverError('predecessor_unverified')
        # Revalidate the same bytes retained for undo, not a second path read.
        if json.loads(prior) != authorization:
            raise TakeoverError('predecessor_unverified')
    collector = None
    unresponsive_writer = False
    try:
        health = CollectorClient(state.socket, timeout=2)._request({'protocol': COLLECTOR_PROTOCOL_VERSION, 'op': 'health'})
    except CollectorError:
        unresponsive_writer = _lock_held(state.root / 'collector.lock')
        if authorization is not None:
            collector = dict(version=authorization['runtime_version'], digest=authorization['runtime_digest'])
            if not _accepted(collector, releases):
                raise TakeoverError('predecessor_unverified')
    else:
        collector = checked_collector_identity(health, authorization, releases=releases)
    from .client_runtime import runtime_directory
    directory = runtime_directory(state, root)
    paths = client_paths(state)
    hinted = _hinted_host(paths, root, any_runtime=True)
    if row is not None and not (hinted is not None and hinted[2] is True):
        deadline = time.monotonic() + 5.0
        while (_lock_held(state.root / 'projects' / row['project_id'] / 'history.lock')
               or _lock_held(directory / 'host.lock')):
            hinted = _hinted_host(paths, root, any_runtime=True)
            if hinted is not None and hinted[2] is True:
                break
            if time.monotonic() >= deadline:
                raise TakeoverError('project_store_busy')
            time.sleep(0.02)
    live = hinted is not None and hinted[2] is True
    service = None
    if row is not None:
        if row['canonical_root'] != str(root):
            raise TakeoverError('predecessor_unverified')
        owner_digest = _project_owner(state, row['project_id'])
        candidates = [root]
        if row.get('launch_copy'):
            candidates.append(Path(row['launch_copy']))
        # Decision 372: an installed service runs from its recorded launch copy, and the project tree holds the
        # same trusted runtime. A1.4 I3's entry is "the install or launch copy": a live candidate whose launch line
        # does not name this tree's entry moves on to the next trusted tree. Only when no trusted tree's entry
        # matches does the unproven outcome apply, to the first trusted tree, exactly as before.
        unproven = None
        captured = []  # one candidate identity (peer pid) per planning pass, shared by every trusted tree
        for candidate in candidates:
            try:
                identity = trusted_package(candidate, releases=releases)
            except TakeoverError:
                continue
            if live and (hinted[0].project_id != row['project_id'] or hinted[0].runtime_digest != identity['digest']):
                continue
            if owner_digest is not None and owner_digest != identity['digest']:
                continue
            if candidate != root and row.get('launch_digest') != identity['digest']:
                continue
            entry = candidate / '.codex/orchestration/project_host.py'
            if len(os.fsencode(entry)) > 4095:
                raise TakeoverError('predecessor_unverified')
            api = candidate / '.codex/orchestration/codex_agents/history_api.py'
            stop_op = api.is_file() and any(isinstance(node, ast.Constant) and node.value == 'service.stop_for_update'
                                           for node in ast.walk(ast.parse(api.read_bytes())))
            service = dict(identity, entry=str(entry), directory=str(directory),
                           project_endpoint=str(directory / 'p.sock'), stop_op=stop_op,
                           service_stop='stop_op' if stop_op else 'none')
            if live and identity['digest'] != COLLECTOR_RUNTIME_DIGEST:
                scope, endpoints, _ = hinted
                endpoint = endpoints[1]  # history/result endpoint; dispatch uses a different protocol
                info = directory.lstat()
                if (endpoint.parent != directory or info.st_uid != os.getuid()
                        or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700):
                    raise TakeoverError('predecessor_unproven', lost=children.get(row['project_id'], 0))
                api = candidate / '.codex/orchestration/codex_agents/history_api.py'
                stop_op = api.is_file() and any(isinstance(node, ast.Constant) and node.value == 'service.stop_for_update'
                                               for node in ast.walk(ast.parse(api.read_bytes())))
                process, pid, stop = None, None, 'stop_op' if stop_op else 'signal'
                try:
                    if not captured:
                        captured.append(_peer(endpoint))
                    pid = captured[0]
                    from .takeover_process import foreign_lock_holder
                    if foreign_lock_holder(pid, (state.root / 'projects' / row['project_id'] / 'history.lock',
                                                 directory / 'host.lock')):
                        raise TakeoverError('project_store_busy')
                    if stop_op:
                        from .takeover_process import Kernel, _trusted
                        process = Kernel().identity(pid)
                        if not _trusted(process, Path(service['entry']), state.root, root):
                            raise TakeoverError('predecessor_unproven')
                    else:
                        pinned = pin_predecessor(pid, entry=Path(service['entry']), state_root=state.root,
                                                 project_root=root)
                        try:
                            process = pinned.identity
                        finally:
                            pinned.close()
                except (OSError, TakeoverError) as error:
                    if isinstance(error, TakeoverError) and error.code == 'project_store_busy':
                        raise
                    if unproven is None:
                        unproven = (service, stop_op, pid, endpoint, scope)
                    service = None
                    continue
                service.update(stop_op=stop_op, service_stop=stop, directory=str(directory),
                               project_endpoint=str(endpoint), service_generation=scope.supervisor_generation)
                if process is not None:
                    service.update(pid=pid, birth=process['birth'])
            break
        if service is None and unproven is not None:
            service, stop_op, pid, endpoint, scope = unproven
            lost = children.get(row['project_id'], 0)
            if stop_op or lost:
                raise TakeoverError('predecessor_unproven', lost=lost, pid=pid)
            service.update(stop_op=stop_op, service_stop='hint', directory=str(directory),
                           project_endpoint=str(endpoint), service_generation=scope.supervisor_generation)
        if live and service is None:
            raise TakeoverError('predecessor_unverified')
        if owner_digest is not None and owner_digest != COLLECTOR_RUNTIME_DIGEST and service is None:
            raise TakeoverError('predecessor_unverified')
        if not live and _lock_held(state.root / 'projects' / row['project_id'] / 'history.lock'):
            raise TakeoverError('project_store_busy')
    needed = row is not None and ((collector is not None and collector['digest'] != COLLECTOR_RUNTIME_DIGEST)
                                    or (service is not None and service['digest'] != COLLECTOR_RUNTIME_DIGEST))
    if unresponsive_writer:
        raise TakeoverError('collector_unresponsive')
    report = dict(takeover='planned' if needed else 'none', collector='replace' if collector is not None
                  and collector['digest'] != COLLECTOR_RUNTIME_DIGEST else 'keep',
                  service='busy' if live and children.get(row['project_id'], 0) else 'idle' if live else 'none',
                  lineage='ok', service_stop='none' if not live or service is None else
                  service.get('service_stop', 'stop_op' if service.get('stop_op') else 'signal'),
                  children=0 if row is None else children.get(row['project_id'], 0),
                  other_children=sum(n for project, n in children.items() if row is None or project != row['project_id']),
                  records_upgrade=5 <= schema < 16,
                  collector_version=None if collector is None else collector['version'],
                  service_version=None if service is None else service['version'])
    if not needed:
        return dict(needed=False, intent=None, report=report)
    intent = dict(project_id=row['project_id'], root_digest=root_digest, predecessor_collector=collector,
                  predecessor_service=service, successor=dict(version=PACKAGE_VERSION, digest=COLLECTOR_RUNTIME_DIGEST),
                  prior_authorization=prior, collector_children=bool(children))
    return dict(needed=True, intent=intent, report=report)
