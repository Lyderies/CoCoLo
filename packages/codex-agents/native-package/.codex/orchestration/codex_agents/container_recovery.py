"""C4: recovery after a restart, by name and label (PLAN-0.4.0-supervisor-slices.md, C4).

Closes RS-C-6 (reduced) and RS-C-9 for container runs, RS-3 for their processes: a run whose
service died (killed, crashed, or its machine restarted) is found again by the next service of the
project and settled, without waiting for the account's timed rule, but never while its container
may still run. Container mode only (`container_launch.SETTINGS`); v1 (process) entries keep
today's rule (`launch_journal.tree_state`) and are never asked of podman.

At service start and on each sweep (`project_driver.RunDriver._settle_lost`, on the sweep's own
helper thread), for every container entry an earlier instance of this project left unsettled, once
that instance's lock is free:

1. The OS is asked first, by label: the project's listing
   (`podman ps --all --filter label=cocolo.project=<project>`). Podman unreachable (or failing):
   nothing is settled and nothing is written to any entry; the listing is asked again after
   STOP_RETRY_SECONDS doubling to STOP_RETRY_MAX_SECONDS, and status says why the entries wait.
2. The entry is adopted (`LaunchJournal.adopt`): from then on this instance writes it.
3. Unless the entry already says `gone` and the listing shows no container for it, the container is
   stopped through the one stop action (`ContainerLauncher.stop`, reason `service_lost`, first reason
   kept): by its name, only if it carries the run's own labels (a container under that name that is
   not the run's is left alone and the run held), then removed, then "gone" asked of the OS
   (`container_runtime.stop`). A stop whose answer is not "gone" leaves the entry `stop_failed` and
   its slot held; it is tried again with the launcher's back-off (`retry_stops`) and here.
4. Once gone: an entry whose claim is recorded as admitted is settled as lost
   (`account.settle_lost_run`); an `intent` whose admission was never recorded is fenced
   (`account.fence_claim`, `FenceClaimInput`): the account closes the claim request for good, or
   answers that it admitted it after all, and the run is then settled as lost with that receipt's
   bridge run id. The workspace is removed and the entry written `settled` (outcome `settled`,
   `released` or `fenced`), keeping the stop's answer as its evidence. A refused or unreachable
   account call is asked again with the same back-off. Each run is settled once (the account's own
   receipts are idempotent, and a settled entry is never sent again).

Orphans: at start and every ORPHAN_SWEEP_SECONDS, each listed container of this project whose run
has no journal entry (the live set is read after the listing, and every intent is written before
its container is created, so a run being launched is never one) is stopped and reported; other
projects' containers are never listed, and a labelled one whose name is not its run's is reported
`skipped` and left alone (`ContainerRuntime.stop_orphans`).

A machine or VM restart needs nothing of its own: every container is then exited or gone, so each
entry is settled once by the same steps.

Standard library only.
"""
from __future__ import annotations

import time

from .container_launch import StartedContainer, stop_retry_delay
from .container_runtime import RUN_LABEL, Refusal
from .launch_journal import CONTAINER_OUTCOMES, MAX_STOP_DETAIL, JournalError

RECOVERY_REASON = 'service_lost'
ORPHAN_SWEEP_SECONDS = 60.0
# Orphans named in status, newest kept.
MAX_REPORTED_ORPHANS = 16


def _wall_ms():
    return time.time_ns() // 1_000_000


class ContainerRecovery:
    """One project service's recovery of the container entries earlier instances left (see the module notes).

    `fence(entry)` and `settle(entry)` are the account's `fence_claim` and `settle_lost_run` for the
    entry, sent with its custody; `remove_workspace(path)` removes the settled run's workspace and says whether it is gone. All
    run on the caller's thread (the driver's sweep thread), never on its foreground account thread.
    """

    def __init__(self, launcher, journal, *, fence, settle, remove_workspace, clock=time.monotonic):
        self.launcher, self.journal = launcher, journal
        self._fence, self._settle, self._remove_workspace = fence, settle, remove_workspace
        self._clock = clock
        self.adopted = set()  # tokens of entries this instance took over and has not settled yet
        self._later = {}  # token -> (failures, when it is tried again)
        self._listing_failures, self._listing_at = 0, 0.0
        self._orphans_at = 0.0  # the first sweep runs at start
        self._orphans = []

    def owns(self, token):
        return token in self.adopted

    def recover(self, entries, *, orphans=True):
        """One pass over these unsettled container entries (stopped instances' and adopted ones);
        {token: outcome} for each run settled now. Nothing is settled unless podman answered."""
        now = self._clock()
        runtime = self.launcher.runtime
        pending = [entry for entry in entries
                   if entry.get('version') == 2 and entry['state'] != 'settled'
                   and entry['project_id'] == runtime.project_id]
        due = [entry for entry in pending if now >= self._later.get(entry['token'], (0, 0.0))[1]]
        sweep_orphans = orphans and now >= self._orphans_at
        if (not due and not sweep_orphans) or now < self._listing_at:
            return {}
        rows = runtime.list_project()
        if isinstance(rows, Refusal):
            self._listing_failures += 1
            self._listing_at = now + stop_retry_delay(self._listing_failures)
            self._report('unavailable', rows.message, len(pending))
            return {}
        self._listing_failures, self._listing_at = 0, 0.0
        listed = {row['Labels'].get(RUN_LABEL) for row in rows}
        settled = {}
        for entry in due:
            outcome = self._recover_one(entry, entry['token'] in listed)
            if outcome is not None:
                settled[entry['token']] = outcome
        if sweep_orphans:
            live = self.journal.tokens()  # read after the listing: a run being launched is in it
            if live is not None:
                report = runtime.stop_orphans(rows, live, grace_seconds=self.launcher.config.grace_seconds)
                self._record_orphans(report)
                self._orphans_at = now + ORPHAN_SWEEP_SECONDS
        self._report('ok', None, len(pending) - len(settled))
        return settled

    def _recover_one(self, entry, listed):
        token = entry['token']
        if entry['instance'] != self.journal.instance:
            try:
                entry = self.journal.adopt(token, entry['instance'])
            except JournalError:
                return None  # Its instance is not provably stopped, or the entry changed: left as it is.
            self.adopted.add(token)
        if entry['state'] != 'gone' or listed:
            started = StartedContainer(token, entry['container_name'], entry['container_id'] or '',
                                       entry['cgroup_path'] or '')
            try:
                gone = self.launcher.stop(started, RECOVERY_REASON) is True
            except Exception:
                gone = False
            if not gone:
                self._back_off(token)  # `stop_failed` (or not yet due): held
                return None
            entry = self.journal.entry(token) or entry
            if entry['state'] == 'settled':
                self.adopted.discard(token)
                return None
        return self._settle_gone(entry)

    def _settle_gone(self, entry):
        """The container is gone: the account settles the run (or closes its claim); then the entry."""
        token = entry['token']
        try:
            if entry['bridge_run_id'] is None:
                receipt = self._fence(entry)['receipt']
                if receipt['state'] == 'claimed':
                    # Admitted after all (its admission was never recorded): settled as lost with its receipt.
                    outcome = self._settle({**entry, 'bridge_run_id': receipt['bridge_run_id']})['state']
                else:
                    outcome = 'fenced'
            else:
                outcome = self._settle(entry)['state']
            if outcome not in CONTAINER_OUTCOMES:
                raise ValueError('unexpected settlement answer')
        except Exception:
            self._back_off(token)  # The account is unreachable or refused: asked again later.
            return None
        if not self._remove_workspace(entry['workspace']):
            self._back_off(token)  # B4s r1 (MED 2): not confirmed gone; the settlement is replayed later
            return None
        try:
            self.journal.settled(token, outcome)
        except JournalError:
            self._back_off(token)  # The account's answer is stored; asked again, it gives the same one.
            return None
        self.adopted.discard(token)
        self._later.pop(token, None)
        return outcome

    def _back_off(self, token):
        failures = self._later.get(token, (0, 0.0))[0] + 1
        self._later[token] = (failures, self._clock() + stop_retry_delay(failures))

    def _record_orphans(self, report):
        at = _wall_ms()
        found = [{'container': item.name or item.container_id, 'run': item.run_id, 'outcome': item.outcome,
                  'detail': item.detail[:MAX_STOP_DETAIL], 'at_ms': at} for item in report.entries]
        names = {item['container'] for item in found}
        self._orphans = ([item for item in self._orphans if item['container'] not in names] + found)[
            -MAX_REPORTED_ORPHANS:]

    def _report(self, podman, detail, waiting):
        """Status's view of the last pass (`launch_journal.container_holds`); a failed write changes nothing."""
        try:
            self.journal.write_recovery({'checked_at_ms': _wall_ms(), 'podman': podman,
                                         'detail': None if detail is None else str(detail)[:MAX_STOP_DETAIL],
                                         'waiting': max(0, waiting), 'orphans': list(self._orphans)})
        except JournalError:
            pass
