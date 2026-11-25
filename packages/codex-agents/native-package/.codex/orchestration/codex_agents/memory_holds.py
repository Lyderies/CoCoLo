"""RS-B-4: instance-bound memory custody, on the project writer and driver owner.

No memory is reconstructed at open. The registration source and entry hold share
one transaction; a preparation intent precedes submit, and only actual retirement
allows its release. Kept instance locks supply positive stopping evidence.
"""
import fcntl
import json
import os
import re
import stat
from pathlib import Path

from .history_contracts import HistoryError
from .project_history import _operation
from .project_holds_schema import attempt_identity, evidence_json, release_hold, take_hold

KINDS = ('driver_entry', 'preparation')
OPERATIONS = frozenset({'register', 'take_preparation', 'release', 'hand_over', 'recover', 'note_cleanup',
                        'pending_removals', 'removal_proven'})
# PREPCLEAN-4 (decision 309): a preparation folder whose removal is not yet observed is recorded in its
# preparation row's evidence (`removal: pending`, `workspace`), which survives the row's release (a tombstone keeps
# its evidence). A later service reads at most this many such records, the capacity report's run-slot bound.
MAX_PENDING_REMOVALS = 64


def active(history, handle):
    row = history._connection.execute('SELECT orchestration_handle,sequence,checkpoint_digest FROM '
        'project_orchestrations WHERE project_id=? AND orchestration_handle=?',
        (history.project_id, handle)).fetchone()
    snapshot = None if row is None else history._orchestration_owner()._latest_snapshot(row)
    if snapshot is None:
        raise HistoryError('history_orchestration_unavailable')
    if snapshot['status'] != 'active':
        raise HistoryError('history_orchestration_state')


def _row(history, kind, key):
    return history._connection.execute('SELECT * FROM project_holds WHERE project_id=? AND kind=? AND identity=?',
        (history.project_id, kind, attempt_identity(*key))).fetchone()


def _owned(history, kind, key, instance):
    row = _row(history, kind, key)
    if row is None or row['instance'] != instance or row['state'] == 'released':
        raise HistoryError('history_conflict')
    return row


def _stopped(directory, instance, facts):
    if re.fullmatch(r'[0-9a-f]{32}', instance) is None:
        return False
    try:
        descriptor = os.open(directory / (instance + '.lock'), os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or info.st_dev != facts.get('lock_device') or info.st_ino != facts.get('lock_inode')):
            return False
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(descriptor)


def registration_hold(history, handle, run_handle, attempt, instance, now, launches):
    """Inside record_registered_run's transaction, after its source insert.

    `launches` is the registering instance's own launch-journal directory, where its lock lives
    (`<state>/projects/<id>/launches` for a service; never inferred from the history directory)."""
    from . import project_holds_schema
    active(history, handle)
    if re.fullmatch(r'[0-9a-f]{32}', instance) is None:
        raise HistoryError('history_invalid_input')
    path = Path(launches) / (instance + '.lock')
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise OSError('unsafe instance lock')
    except OSError:
        raise HistoryError('history_service_unavailable') from None
    project_holds_schema.take_hold(history._connection, history.project_id, 'driver_entry',
        attempt_identity(run_handle, attempt), instance=instance, now_ms=now,
        evidence=evidence_json(orchestration_handle=handle, lock_device=info.st_dev, lock_inode=info.st_ino))


@_operation
def register(history, handle, run_handle, attempt, run_id, instance, launches=None):
    """Store-level registration requires the instance's launch-journal directory."""
    if launches is None:
        raise HistoryError('history_invalid_input')
    history.record_registered_run(handle, run_handle, run_id, attempt, instance=instance,
                                  now_ms=history._hold_now(), memory=launches)


@_operation
def take_preparation(history, run_handle, attempt, instance):
    key = (run_handle, attempt)
    with history._transaction():
        entry = _owned(history, 'driver_entry', key, instance)
        if entry['state'] != 'held':
            raise HistoryError('history_conflict')
        facts = json.loads(entry['evidence'])
        active(history, facts['orchestration_handle'])
        take_hold(history._connection, history.project_id, 'preparation', attempt_identity(*key),
                  instance=instance, now_ms=history._hold_now(), evidence=entry['evidence'])


@_operation
def release(history, kind, run_handle, attempt, instance, reason):
    if kind not in KINDS or reason not in (('retired', 'handed_over') if kind == 'driver_entry'
                                         else ('retired', 'submit_refused', 'never_launch_local')):
        raise HistoryError('history_invalid_input')
    key = (run_handle, attempt)
    with history._transaction():
        row = _row(history, kind, key)
        if row is not None and row['instance'] == instance and row['state'] == 'released' and row['release_kind'] == reason:
            return  # Lost answer: preserve the first release time/evidence.
        row = _owned(history, kind, key, instance)
        if kind == 'driver_entry':
            prep = _row(history, 'preparation', key)
            duty = _row(history, 'open_binding', key)
            if (prep is not None and prep['state'] != 'released') or (duty is not None and
                    duty['state'] == 'held' and json.loads(duty['evidence']).get('instance') != 'sweep'):
                raise HistoryError('history_result_not_ready')
            if reason == 'handed_over' and row['state'] != 'releasing':
                raise HistoryError('history_conflict')
        evidence = None
        if kind == 'preparation':
            # In-process retirement follows observed removal (or no folder): nothing is left pending.
            facts = json.loads(row['evidence'] or '{}')
            if facts.get('removal') == 'pending':
                evidence = evidence_json(**_removed(facts))
        release_hold(history._connection, history.project_id, kind, attempt_identity(*key), reason,
                     now_ms=history._hold_now(), evidence=evidence)


def _pending(facts, workspace):
    """The preparation evidence with its folder recorded as awaiting observed removal."""
    facts = dict(facts)
    facts['removal'], facts['workspace'] = 'pending', str(workspace)
    return facts


def _removed(facts):
    facts = dict(facts)
    if facts.get('removal') == 'pending':
        facts['removal'] = 'removed'
    facts.pop('workspace', None)
    return facts


@_operation
def note_cleanup(history, run_handle, attempt, instance, workspace=None):
    with history._transaction():
        row = _owned(history, 'preparation', (run_handle, attempt), instance)
        facts = json.loads(row['evidence'])
        facts['reason'] = 'cleanup_unknown'
        if workspace is not None:
            facts = _pending(facts, workspace)
        history._connection.execute('UPDATE project_holds SET evidence=? WHERE project_id=? AND kind=? AND identity=?',
            (evidence_json(**facts), history.project_id, 'preparation', row['identity']))


@_operation
def pending_removals(history, instance):
    """(identity, folder) of every preparation folder an instance other than `instance` left to be removed."""
    found = []
    for row in history._connection.execute("SELECT identity,evidence FROM project_holds WHERE project_id=? "
            "AND kind='preparation' AND instance<>? AND evidence LIKE '%\"removal\":\"pending\"%' "
            "ORDER BY taken_at_ms,identity LIMIT ?", (history.project_id, instance, MAX_PENDING_REMOVALS)):
        facts = json.loads(row['evidence'] or '{}')
        if facts.get('removal') == 'pending' and isinstance(facts.get('workspace'), str):
            found.append((row['identity'], facts['workspace']))
    return found


@_operation
def removal_proven(history, identity):
    """The folder of this preparation row was observed absent: its record no longer says pending."""
    with history._transaction():
        row = history._connection.execute("SELECT evidence FROM project_holds WHERE project_id=? "
            "AND kind='preparation' AND identity=?", (history.project_id, identity)).fetchone()
        if row is None:
            return
        facts = json.loads(row['evidence'] or '{}')
        if facts.get('removal') != 'pending':
            return
        history._connection.execute('UPDATE project_holds SET evidence=? WHERE project_id=? AND kind=? AND identity=?',
            (evidence_json(**_removed(facts)), history.project_id, 'preparation', identity))


@_operation
def hand_over(history, run_handle, attempt, instance):
    """Before detaching: lease and binding duty belong to sweep; entry is releasing."""
    key = (run_handle, attempt)
    with history._transaction():
        entry = _owned(history, 'driver_entry', key, instance)
        lease = _row(history, 'lease', key)
        duty = _row(history, 'open_binding', key)
        if entry['state'] == 'releasing' and entry['release_kind'] == 'handed_over':
            return
        if (lease is None or lease['state'] != 'held' or lease['lease_case'] != 'armed'
                or duty is None or duty['state'] != 'held'
                or json.loads(duty['evidence']).get('instance') != instance):
            raise HistoryError('history_conflict')
        for row in (lease, duty):
            facts = json.loads(row['evidence'] or '{}')
            facts['instance'] = 'sweep'
            history._connection.execute('UPDATE project_holds SET evidence=?,next_check_ms=0 WHERE project_id=? '
                'AND kind=? AND identity=?', (evidence_json(**facts), history.project_id, row['kind'], row['identity']))
        history._connection.execute("UPDATE project_holds SET state='releasing',release_kind='handed_over' "
            "WHERE project_id=? AND kind='driver_entry' AND identity=?", (history.project_id, entry['identity']))


@_operation
def recover(history, directory, current):
    """Start/maintenance pass. Absence and lock contention are never stopping evidence."""
    with history._transaction():
        rows = history._connection.execute("SELECT * FROM project_holds INDEXED BY project_holds_due WHERE "
            "project_id=? AND state<>'released' AND kind IN ('driver_entry','preparation') AND instance<>?",
            (history.project_id, current)).fetchall()
        proofs = {}
        for row in rows:
            instance = row['instance']
            facts = json.loads(row['evidence'] or '{}')
            proof = (instance, facts.get('lock_device'), facts.get('lock_inode'))
            if proof not in proofs:
                proofs[proof] = _stopped(directory, instance, facts)
            if proofs[proof]:
                release_hold(history._connection, history.project_id, row['kind'], row['identity'],
                             'service_stopped', now_ms=history._hold_now())
            elif facts.get('reason') != 'instance_unproven':
                facts['reason'] = 'instance_unproven'
                history._connection.execute('UPDATE project_holds SET evidence=? WHERE project_id=? AND kind=? AND identity=?',
                    (evidence_json(**facts), history.project_id, row['kind'], row['identity']))
        remaining = frozenset(r[0] for r in history._connection.execute(
            "SELECT DISTINCT instance FROM project_holds INDEXED BY project_holds_due WHERE project_id=? "
            "AND state<>'released' AND kind IN ('driver_entry','preparation')", (history.project_id,)))
    return remaining


class DriverMemory:
    """Owner-side pending operations. Requests remain visible until observed.

    `takes` are fenced preparation intents; `releases` are absent memory awaiting
    its durable receipt. A handle remains in `preparations` until retire returns.
    """
    def __init__(self, driver):
        self.driver = driver
        self.takes, self.releases, self.preparations, self.handoffs = {}, {}, {}, {}
        self.cleanup_notes = {}

    def request(self, operation, *args):
        return self.driver._runtime.persistence.memory_hold(operation, *args)

    @property
    def instance(self):
        return self.driver._journal.instance

    def prepare(self, run):
        if run.key not in self.takes:
            request = self.request('take_preparation', *run.key, self.instance)
            self.takes[run.key] = (run, request)
        run.state = 'taking_preparation'

    def release(self, kind, key, reason):
        self.releases.setdefault((kind, key), [reason, None])
        self._send_releases()

    def retired_preparation(self, handle):
        key, reason = self.preparations.pop(handle)
        self.release('preparation', key, reason)

    def unknown_cleanup(self, handle):
        if handle not in self.preparations or handle in self.cleanup_notes:
            return
        key, _ = self.preparations[handle]
        try:
            # PREPCLEAN-4: the folder is recorded with the note, so a later service can keep removing it.
            workspace = self.driver._preparation.workspace_of(handle)
            workspace = str(workspace) if isinstance(workspace, (str, Path)) else None
            self.cleanup_notes[handle] = self.request('note_cleanup', *key, self.instance, workspace)
        except HistoryError:
            pass

    @staticmethod
    def can_hand_over(run):
        return (run.state == 'failed' and run.receipt is None and run.binding is None
                and run.future is None and run.fence_at is None)

    def hand_over(self, run):
        if not self.can_hand_over(run) or run.persistence is None:
            return
        try:
            lane = self.driver._runtime.persistence
            lane.check_detach(run.persistence)
            if run.key not in self.handoffs:
                self.handoffs[run.key] = (run, self.request('hand_over', *run.key, self.instance))
        except HistoryError:
            pass

    def drop(self, run, reason='retired'):
        if (run.key in self.takes or any(key == run.key for key, _ in self.preparations.values())
                or ('preparation', run.key) in self.releases or run.persistence is not None):
            return False
        if self.driver._runs.pop(run.key, None) is None:
            return True
        self.driver._retired[run.key] = (run.host_client, run.prospective.orchestration_handle)
        from .project_driver import MAX_RETIRED_RUNS
        while len(self.driver._retired) > MAX_RETIRED_RUNS:
            self.driver._retired.pop(next(iter(self.driver._retired)))
        request = run.reporting
        if request is not None and request.state == 'done':
            self.release('driver_entry', run.key, reason)
        return True

    def _send_releases(self):
        for (kind, key), entry in list(self.releases.items()):
            reason, request = entry
            if request is not None and request.state == 'done':
                del self.releases[(kind, key)]
            elif request is None or request.state == 'failed':
                try:
                    entry[1] = self.request('release', kind, *key, self.instance, reason)
                except HistoryError:
                    pass

    def step(self):
        self._send_releases()
        for key, (run, request) in list(self.takes.items()):
            if request.state == 'queued':
                continue
            del self.takes[key]
            if request.state != 'done':
                self.driver._fail(run, 'run_failed')
                continue
            # A cancellation while the intent was queued must never submit it.
            if run.state != 'taking_preparation':
                self.release('preparation', key, 'submit_refused')
                continue
            try:
                run.prepared = self.driver._preparation.submit(run.preparation)
            except Exception as error:
                self.release('preparation', key, 'submit_refused')
                self.driver._fail(run, getattr(error, 'reason_code', 'run_unavailable'))
            else:
                self.preparations[run.prepared] = (key, 'retired')
                run.state = 'preparing'
        for key, (run, request) in list(self.handoffs.items()):
            if request.state == 'queued':
                continue
            del self.handoffs[key]
            if request.state != 'done' or not self.can_hand_over(run):
                continue
            try:
                self.driver._runtime.persistence.detach(run.persistence)
            except HistoryError:
                self.hand_over(run)
                continue
            run.persistence, run.closed = None, True
            if run.prepared in self.preparations:
                self.preparations[run.prepared] = (key, 'never_launch_local')
            self.driver._give_back(run)
            self.driver._earlier_listed = False
            self.driver._earlier_at = 0.0
            run.handed_over = True
        for handle, request in list(self.cleanup_notes.items()):
            if request.state != 'queued':
                del self.cleanup_notes[handle]
        for run in list(self.driver._runs.values()):
            if getattr(run, 'handed_over', False):
                self.drop(run, reason='handed_over')
            elif (run.closed and run.state == 'failed' and not run.waiters and run.future is None
                    and (run.claim_sent or run.duty_done_at is not None)):
                self.drop(run)  # A verified denial/withdrawal closes without any later client request.


@_operation
def retire_instance(history, instance, pending=None):
    """Trusted graceful exit only, after actual memory and every runtime thread retired.

    `pending` maps a preparation row's identity to the folder this service still could not remove: its record
    is kept in the released row's evidence for the next service (PREPCLEAN-4), the memory hold is not."""
    pending = {} if pending is None else pending
    with history._transaction():
        rows = history._connection.execute("SELECT kind,identity,evidence FROM project_holds INDEXED BY project_holds_due "
            "WHERE project_id=? AND state<>'released' AND instance=? AND kind IN ('driver_entry','preparation') "
            "ORDER BY kind DESC", (history.project_id, instance)).fetchall()
        for row in rows:
            evidence = None
            if row['kind'] == 'preparation' and isinstance(pending.get(row['identity']), str):
                evidence = evidence_json(**_pending(json.loads(row['evidence'] or '{}'), pending[row['identity']]))
            release_hold(history._connection, history.project_id, row['kind'], row['identity'],
                         'retired', now_ms=history._hold_now(), evidence=evidence)
