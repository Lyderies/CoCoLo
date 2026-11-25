"""Project-local, volatile recovery observation. No persistence or client imports."""
import time

EXPIRY_SECONDS = 10.0
RECOVERY_REASONS = ('needs_settlement', 'needs_process_evidence', 'has_holds', 'stop_failed', 'account_unreachable')


class RecoveryNotes:
    """One sweep publisher; atomic immutable replacement, read by the project writer.

    Restart deliberately forgets the observation. A failed sweep must not publish.
    Monotonic age avoids wall-clock jumps making an old observation look fresh.
    """

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._observation = None
        self._own = (frozenset(), frozenset(), frozenset())

    def update_own(self, pending):
        pending = frozenset(pending)
        _, gone, failed = self._own
        self._own = (pending, gone & pending, failed & pending)

    def own_gone(self, handle, *, account_unreachable=None):
        pending, gone, failed = self._own
        if account_unreachable is not None:
            failed = (failed | {handle}) if account_unreachable else (failed - {handle})
        self._own = (pending, gone | {handle}, failed)

    def publish(self, reasons, *, runs=None):
        counts = tuple(reasons[name] for name in RECOVERY_REASONS)
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError('invalid recovery counts')
        identities = None if runs is None else tuple(frozenset(runs.get(name, ())) for name in RECOVERY_REASONS)
        self._observation = (self._clock(), counts, identities)

    def snapshot(self, *, pending=()):
        observation = self._observation
        if observation is None:
            result = {'reasons': dict.fromkeys(RECOVERY_REASONS, 0), 'stale': True}
            identities = None
        else:
            observed, counts, identities = observation
            result = {'reasons': dict(zip(RECOVERY_REASONS, counts)),
                      'stale': self._clock() - observed > 2 * EXPIRY_SECONDS}
        own, gone, failed = self._own
        groups = {name: set() if identities is None else set(identities[index])
                  for index, name in enumerate(RECOVERY_REASONS)}
        groups['needs_settlement'].update(set(pending) | own)
        groups['needs_process_evidence'].update(own - gone)
        groups['account_unreachable'].update(failed)
        seen = set()
        # A specific/latest failure replaces the underlying settlement wait.
        for name in ('account_unreachable', 'stop_failed', 'needs_process_evidence', 'needs_settlement'):
            count = len(groups[name] - seen)
            seen.update(groups[name])
            result['reasons'][name] = count + (result['reasons'][name] if identities is None else 0)
        return result


def recovery_pending(history):
    notes = getattr(history, '_recovery_notes', None)
    # Source-held reservations and admitted unbound leases need settlement even
    # when a damaged/missing launch journal supplies no more specific observation.
    db = history._connection
    pending = {row[0] for row in db.execute(
        "SELECT s.run_handle FROM project_holds h INDEXED BY project_holds_due "
        "JOIN result_reservations s ON s.project_id=h.project_id AND s.claim_id=h.identity "
        "WHERE h.project_id=? AND h.state<>'released' AND h.kind='reservation' UNION "
        "SELECT l.run_handle FROM project_holds h INDEXED BY project_holds_due "
        "JOIN result_leases l ON l.project_id=h.project_id "
        "AND l.run_handle=substr(h.identity,1,instr(h.identity,':')-1) "
        "AND l.attempt=CAST(substr(h.identity,instr(h.identity,':')+1) AS INTEGER) "
        "WHERE h.project_id=? AND h.state<>'released' AND h.kind='lease' "
        "AND h.lease_case IN ('closing_denied','admitted_unbound')",
        (history.project_id, history.project_id))}
    result = (notes or RecoveryNotes()).snapshot(pending=pending)
    # Count blocked orchestrations, rather than every hold of the same orchestration.
    # Live indexes exclude tombstones. No checkpoint or result contents are opened.
    from .project_holds_schema import ORCHESTRATION_EXECUTION_HELD
    result['reasons']['has_holds'] = history._connection.execute(
        "SELECT COUNT(*) FROM project_orchestrations o INDEXED BY project_orchestrations_state "
        "WHERE o.project_id=? AND o.state='open' AND " + ORCHESTRATION_EXECUTION_HELD,
        (history.project_id,)).fetchone()[0]
    if result['stale']:
        result['reasons'] = dict.fromkeys(RECOVERY_REASONS)
    return result
