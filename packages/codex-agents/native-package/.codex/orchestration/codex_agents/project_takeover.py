"""Takeover records and original outbox custody, through the project owner only."""
from __future__ import annotations

from .history_contracts import HistoryError
from .account_claim_contracts import ClaimError
from .collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
from .runtime_lineage import successor_custody, verified_predecessor


def _launches(history):
    from .launch_journal import _entry_name, _read_entry
    directory = history._directory / 'launches'
    try:
        names = sorted(path for path in directory.iterdir() if _entry_name(path.name))
    except FileNotFoundError:
        return
    for path in names:
        entry = _read_entry(path)
        if entry is not None:
            yield entry


def _settlement_input(history, hold):
    from .result_contracts import ResultLeaseBinding
    from .lost_run_contracts import SettleLostInput
    from .claim_storage import verify_pending_storage_evidence
    for entry in _launches(history):
        scope = ResultLeaseBinding(**entry['lease'])
        if scope.project_id != history.project_id:
            continue
        identity = entry['bridge_run_id'] if hold['kind'] == 'reservation' else f'{scope.run_handle}:{scope.attempt}'
        if identity != hold['identity'] or entry['bridge_run_id'] is None:
            continue
        stored = history.result_lease(scope)
        observed = verify_pending_storage_evidence(history._result_key, scope, bytes.fromhex(entry['storage_evidence']))
        if observed.lease_digest != stored.lease_digest:
            raise HistoryError('history_conflict')
        return SettleLostInput(scope.run_handle, entry['identity_digest'], scope.claim_request_id,
                              entry['bridge_run_id'], stored.lease_digest).payload()
    raise HistoryError('history_conflict')


def _request_digest(history, runtime, request_id, payload):
    from .account_api import ACCOUNT_PROTOCOL_VERSION, AccountRequest, account_request_digest
    from .lost_run_closure import CLOSURE_OPERATION
    return account_request_digest(AccountRequest(ACCOUNT_PROTOCOL_VERSION, CLOSURE_OPERATION, request_id,
                                                  history.project_id, runtime, payload))


def adopt_project(history, intent, *, migration=None):
    """Only inside an authorized owner's transaction, before publication."""
    from .learning_contracts import canonical_json
    db = history._connection
    if not db.in_transaction or not history._allow_schema_upgrade or history._fence.epoch is None:
        raise HistoryError('history_not_authorized')
    old = intent.get('predecessor_service')
    if (intent['project_id'] != history.project_id
            or intent['successor'] != {'version': PACKAGE_VERSION, 'digest': COLLECTOR_RUNTIME_DIGEST}):
        raise HistoryError('history_not_authorized')
    if old is None or old['digest'] == COLLECTOR_RUNTIME_DIGEST:
        return
    if not verified_predecessor(old, PACKAGE_VERSION):
        raise HistoryError('history_not_authorized')
    known = db.execute('SELECT * FROM takeover_record WHERE takeover_id=?', (intent['takeover_id'],)).fetchone()
    identity = (intent['takeover_id'], old['digest'], old['version'], COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION)
    if known is None:
        db.execute("INSERT INTO takeover_record VALUES(?,?,?,?,?,'adopting',?,?,?)",
                   (*identity, intent['recorded_at_ms'], len(intent['runs']), intent.get('runs_unlisted', 0)))
    elif tuple(known)[:5] != identity:
        raise HistoryError('history_conflict')
    if len(intent['runs']) > 64:
        raise HistoryError('history_not_authorized')
    for run in intent['runs']:
        prior = db.execute('SELECT run_handle,attempt FROM takeover_runs WHERE takeover_id=? AND bridge_run_id=?',
                           (intent['takeover_id'], run['bridge_run_id'])).fetchone()
        if prior is not None and tuple(prior) != (run['run_handle'], run['attempt']):
            raise HistoryError('history_conflict')
        if prior is None:
            db.execute('INSERT INTO takeover_runs VALUES(?,?,?,?)',
                       (intent['takeover_id'], run['bridge_run_id'], run['run_handle'], run['attempt']))
    # Bindings, signed bytes and outbox request digests remain untouched. Only
    # a recomputable original obligation gains a separate authorization row.
    batches = (migration.batches('takeover_obligation') if migration is not None and migration.active else
               [(None, None, None)])
    for _, _, rowids in batches:
        where = '' if rowids is None else ' AND rowid IN (' + ','.join('?' for _ in rowids) + ')'
        rows = db.execute("SELECT * FROM project_holds WHERE state='releasing'" + where, rowids or ()).fetchall()
        for hold in rows:
            if hold['kind'] not in ('lease', 'reservation'):
                continue
            obligation = db.execute('SELECT * FROM takeover_obligations WHERE kind=? AND identity=?',
                                    (hold['kind'], hold['identity'])).fetchone()
            original = old['digest'] if obligation is None else obligation['original_runtime_digest']
            try:
                payload = _settlement_input(history, hold)
                digest = _request_digest(history, original, hold['request_id'], payload)
                if (digest != hold['request_digest'] or not successor_custody(db, original,
                        COLLECTOR_RUNTIME_DIGEST, project=True)
                        or (obligation is not None and (obligation['original_request_id'] != hold['request_id']
                            or obligation['original_request_digest'] != digest))):
                    continue  # refused_unverifiable; custody and the original request stay held
            except (HistoryError, ClaimError, ValueError, TypeError, KeyError):
                continue
            if obligation is None:
                db.execute('INSERT INTO takeover_obligations VALUES(?,?,?,?,?,?)',
                    (hold['kind'], hold['identity'], intent['takeover_id'], hold['request_id'], digest, original))
    db.execute("UPDATE takeover_record SET state='adopted' WHERE takeover_id=? AND state='adopting'",
               (intent['takeover_id'],))


def successor_settlement(history, hold, request_id, digest, *, payload=None):
    """An offered current request may close the immutable original outbox only."""
    obligation = history._connection.execute('SELECT * FROM takeover_obligations WHERE kind=? AND identity=?',
                                             (hold['kind'], hold['identity'])).fetchone()
    if (obligation is None or request_id != hold['request_id'] or obligation['original_request_id'] != request_id
            or obligation['original_request_digest'] != hold['request_digest']
            or not successor_custody(history._connection, obligation['original_runtime_digest'],
                                     history._fence.digest, project=True)):
        return False
    try:
        payload = _settlement_input(history, hold) if payload is None else payload
        return (hold['request_digest'] == _request_digest(history, obligation['original_runtime_digest'], request_id, payload)
                and digest == _request_digest(history, history._fence.digest, request_id, payload))
    except (HistoryError, ClaimError, ValueError, TypeError, KeyError):
        return False


def stopped_children(history, runs):
    """The final-store count for the wizard, before B4s confirms an orphan.

    A child can survive a forced service exit. Its run is destined for lost
    settlement, but its slot and the run-status annotation still wait for the
    ordinary proof. A result committed during the stop keeps its own outcome.
    """
    history._require_owner(read_only=True)
    count = 0
    for run in runs:
        row = history._connection.execute('SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?',
                                          (history.project_id, run['bridge_run_id'])).fetchone()
        try:
            value = None if row is None else history._reservation(row)
            if (value is not None and value.binding.run_handle == run['run_handle']
                    and value.binding.attempt == run['attempt'] and value.committed_at_ms is None
                    and successor_custody(history._connection, value.binding.runtime_digest,
                                          history._fence.digest, project=True)):
                count += 1
        except (HistoryError, ValueError, TypeError):
            pass
    return count


def _record_on_chain(history, record):
    return (record is not None and record['state'] in ('adopted', 'complete')
            and verified_predecessor(dict(version=record['predecessor_version'], digest=record['predecessor_digest']),
                                     PACKAGE_VERSION)
            and successor_custody(history._connection, record['predecessor_digest'], history._fence.digest, project=True)
            and successor_custody(history._connection, record['successor_digest'], history._fence.digest, project=True))


def takeover_report(history, *, run_handle=None):
    history._require_owner(read_only=True)
    where = '' if run_handle is None else (' WHERE EXISTS(SELECT 1 FROM takeover_runs t '
        'WHERE t.takeover_id=r.takeover_id AND t.run_handle=?)')
    arguments = () if run_handle is None else (run_handle,)
    records = [{name: row[name] for name in ('predecessor_version', 'successor_version', 'state', 'runs_listed', 'runs_unlisted')}
        for row in history._connection.execute('SELECT * FROM takeover_record r' + where
        + ' ORDER BY r.rowid DESC LIMIT 1', arguments) if _record_on_chain(history, row)]
    report = dict(records=records, lost=0, lost_held=0, finished_kept=0, refused_unverifiable=0)
    counted = set()
    for run in history._connection.execute('SELECT DISTINCT takeover_id,bridge_run_id,run_handle,attempt FROM takeover_runs'
            + ('' if run_handle is None else ' WHERE run_handle=?'), arguments):
        try:
            row = history._connection.execute('SELECT * FROM result_reservations WHERE project_id=? AND claim_id=?',
                                              (history.project_id, run['bridge_run_id'])).fetchone()
            reservation = None if row is None else history._reservation(row)
            record = history._connection.execute('SELECT * FROM takeover_record WHERE takeover_id=?',
                                                 (run['takeover_id'],)).fetchone()
            if (reservation is None or reservation.binding.run_handle != run['run_handle']
                    or reservation.binding.attempt != run['attempt']
                    or not _record_on_chain(history, record)
                    or not successor_custody(history._connection, reservation.binding.runtime_digest,
                        record['predecessor_digest'], project=True, current_version=record['predecessor_version'])
                    or not successor_custody(history._connection, reservation.binding.runtime_digest,
                                             history._fence.digest, project=True)):
                raise HistoryError('history_conflict')
        except (HistoryError, ValueError, TypeError, KeyError):
            report['refused_unverifiable'] += 1
            continue
        if run['bridge_run_id'] in counted:
            continue
        counted.add(run['bridge_run_id'])
        hold = history._connection.execute("SELECT * FROM project_holds WHERE kind='reservation' "
                                            'AND identity=?', (run['bridge_run_id'],)).fetchone()
        if hold is not None:
            report['lost'] += int(hold['state'] == 'released' and hold['release_kind'] == 'lost')
            report['lost_held'] += int(hold['state'] == 'releasing')
            if hold['state'] == 'releasing':
                try:
                    payload = _settlement_input(history, hold)
                    offered = _request_digest(history, history._fence.digest, hold['request_id'], payload)
                    if (offered != hold['request_digest'] and not successor_settlement(
                            history, hold, hold['request_id'], offered, payload=payload)):
                        raise HistoryError('history_conflict')
                except (HistoryError, ClaimError, ValueError, TypeError, KeyError):
                    report['refused_unverifiable'] += 1
    for row in history._connection.execute("SELECT evidence FROM project_holds WHERE kind='result_custody' AND state='held'"):
        import json
        try:
            evidence = json.loads(row['evidence'])
        except (ValueError, TypeError):
            evidence = {}
        report['finished_kept'] += int(evidence.get('reason') == 'legacy_finished_result')
        report['refused_unverifiable'] += int(evidence.get('reason') in
                                              ('legacy_result_evidence_missing', 'legacy_result_inputs_missing'))
    return report


def complete_takeovers(history):
    """The new service's successful recovery pass publishes completion itself."""
    history._require_owner()
    with history._transaction():
        history._connection.execute("UPDATE takeover_record SET state='complete' WHERE state='adopted' "
                                    'AND successor_digest=?', (history._fence.digest,))


def adopted_orchestration_scope(history, scope, handle, host_class):
    """Current enrollment may continue an existing, sealed predecessor scope.

    This never authorizes a fresh open under an old runtime. The caller still
    rechecks current enrollment and the exact request before commit.
    """
    history._require_owner(read_only=True)
    if (not successor_custody(history._connection, scope.runtime_digest, history._fence.digest, project=True)
            or scope.runtime_digest == history._fence.digest):
        return False
    try:
        view = history._orchestration_owner()._current(scope, handle)
        return view['snapshot']['client_version'] == host_class
    except HistoryError:
        return False


def takeover_lines(report, *, account=None):
    import time
    lines = []
    if report['records']:
        row = report['records'][-1]
        date = '' if account is None or account['recorded_at_ms'] is None else time.strftime(
            '%Y-%m-%d %H:%M UTC, ', time.gmtime(account['recorded_at_ms'] / 1000))
        ended = row.get('runs_listed', report['lost'] + report['lost_held']) + row.get('runs_unlisted', 0)
        overflow = (f" ({row['runs_unlisted']} of them not listed one by one)" if row.get('runs_unlisted') else '')
        lines.append(f"Update hand-over: {date}CoCoLo {row['predecessor_version']} to {row['successor_version']}. "
                     f"{ended} {'run' if ended == 1 else 'runs'} ended by the update{overflow}. "
                     f"{report['finished_kept']} finished results kept.")
    if report['lost']:
        lines.append(f"Runs ended by the update: {report['lost']}, slot freed.")
    if report['lost_held']:
        lines.append(f"Runs ended by the update: {report['lost_held']}, slot held until the account confirms (clears by itself).")
    if report['refused_unverifiable']:
        source = '' if not report['records'] else f" of CoCoLo {report['records'][0]['predecessor_version']}"
        lines.append(f"Kept unchanged, could not be verified: {report['refused_unverifiable']} records{source}. Nothing was freed. "
                     "See docs/troubleshooting.md, 'Update hand-over'.")
    if account is not None and account['other_projects']:
        projects, runs = account['other_projects'], account['other_runs']
        lines.append(f"Other projects still on an earlier version: {projects}. "
                     f"{'Its' if projects == 1 else 'Their'} {runs} {'run keeps its' if runs == 1 else 'runs keep their'} "
                     'slot until that project is updated.')
        lines.extend(row['update_message'] for row in account['updates'])
    if account is not None and account.get('lineage_message'):
        lines.append(account['lineage_message'])
    return lines


def journal_notice(state_root, project_root):
    """A durable in-progress hand-over remains visible without either service."""
    from .collector_paths import CollectorPaths, _canonical_project_root
    from pathlib import Path
    from .learning_contracts import keyed_digest
    from .takeover_journal import TakeoverJournal, JournalError
    try:
        state = CollectorPaths(state_root)
        document = TakeoverJournal(state.root).read()
        if (document is None or document['root_digest'] != keyed_digest(state.digest_key(),
                str(_canonical_project_root(Path(project_root))))):
            return []
        return [f"Update hand-over not finished ({document['phase'].replace('_', ' ')}). "
                'Run: ./setup.sh --update.']
    except JournalError:
        return ['Update hand-over not finished (intent unreadable). Run: ./setup.sh --update.']
    except (OSError, ValueError, RuntimeError):
        return []


def purge_takeover(db, now, *, transaction=None, checkpoint=None):
    """A1.10: one atomic, complete pass; protected records never count toward 16.

    Called after stage B commits and in the ordinary purge cycle. A failed
    retention pass rolls back independently and never fails an update.
    """
    import json
    from .project_history import SETTLEMENT_REPLAY_WINDOW_MS
    context = db if transaction is None else transaction()
    with context:
        # Unknown attempts have no lease that can keep them inside its window.
        db.execute('DELETE FROM takeover_runs WHERE NOT EXISTS(SELECT 1 FROM result_leases l '
                   'WHERE l.run_handle=takeover_runs.run_handle AND l.attempt=takeover_runs.attempt) '
                   'AND takeover_id IN (SELECT takeover_id FROM takeover_record WHERE recorded_at_ms<?)',
                   (now - SETTLEMENT_REPLAY_WINDOW_MS,))
        db.execute('DELETE FROM takeover_obligations WHERE NOT EXISTS(SELECT 1 FROM project_holds h '
                   'WHERE h.kind=takeover_obligations.kind AND h.identity=takeover_obligations.identity '
                   "AND (h.state<>'released' OR h.evidence IS NOT NULL))")
        records = db.execute('SELECT takeover_id,predecessor_digest,successor_digest,state '
                             'FROM takeover_record ORDER BY rowid DESC').fetchall()
        newest = records[0][0] if records else None
        protected = {r[0] for r in db.execute('SELECT takeover_id FROM takeover_runs UNION '
                                             'SELECT takeover_id FROM takeover_obligations')}
        edges = {r[1]: (r[0], r[2]) for r in records}
        # Every retained obligation keeps its own parent, even when refused.
        for row in db.execute('SELECT o.original_runtime_digest FROM takeover_obligations o '
                              'JOIN project_holds h ON h.kind=o.kind AND h.identity=o.identity '
                              "WHERE h.state<>'released'"):
            digest, seen = row[0], set()
            path = []
            while digest in edges and digest not in seen and len(path) <= 16:
                seen.add(digest)
                identity, digest = edges[digest]
                path.append(identity)
            if len(path) <= 16:
                protected.update(path)
        removable = [r[0] for r in records if r[0] != newest and r[0] not in protected and r[3] == 'complete']
        for identity in removable[16:]:
            db.execute('DELETE FROM takeover_record WHERE takeover_id=?', (identity,))
        if checkpoint is not None:
            checkpoint()
