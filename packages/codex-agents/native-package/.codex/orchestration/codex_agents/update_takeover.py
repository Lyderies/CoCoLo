"""Resumable owner hand-over coordinator. Persistence stays with existing owners.

The operations port owns transport and authorized owner opens. This coordinator
only journals intent and orders those operations; it has no database capability.
"""
from .takeover_journal import PHASES, TakeoverJournal

START_REFUSALS = frozenset({'runtime_authorization_quiesced', 'runtime_unauthorized', 'runtime_upgrade_required'})
MAX_STRAGGLER_STOPS = 3


A1_WRITE_FREE = {
    'predecessor_unproven': "The update stopped (predecessor_unproven): this project's old service is running {n} {runs}, and it could not be confirmed to be this project's own service, so it was not stopped. Nothing was changed. When that run ends, the old service stops by itself. Then run ./setup.sh --update. See docs/troubleshooting.md, 'Update hand-over'.",
    'project_store_busy': "The update stopped (project_store_busy): another program is using this project's records right now, for example a backup or a records command. Nothing was changed. Let it finish, then run ./setup.sh --update. If this keeps happening, docs/troubleshooting.md, 'Update hand-over', shows how to see which program it is.",
    'update_busy': "The update stopped (update_busy): another setup or records command for CoCoLo is running on this computer. Nothing was changed. Let it finish, then run ./setup.sh --update.",
    'collector_unresponsive': "The update stopped (collector_unresponsive): the collector is running but does not answer. Nothing was changed. Wait a minute and run ./setup.sh --update again. If it still does not answer, docs/troubleshooting.md, 'Update hand-over', shows how to end it safely.",
}


# Decision 372: an idle service has no run to wait for, so the busy text's "when that run ends" is false for it.
A1_IDLE_UNPROVEN = {
    True: ("The update stopped (predecessor_unproven): this project's old service has no child run in progress, but "
           "it could not be confirmed to be this project's own service, so it was not stopped. Nothing was changed. "
           "Check it with: ps -o pid,lstart,command -p {pid}. If that line names this project's project_host.py "
           "with this project's folder, stop it with: kill {pid}. Then run ./setup.sh --update. "
           "See docs/troubleshooting.md, 'Update hand-over'."),
    False: ("The update stopped (predecessor_unproven): this project's old service has no child run in progress, but "
            "its process could not be read, so it could not be confirmed to be this project's own service and it "
            "was not stopped. Nothing was changed. Wait a minute and run ./setup.sh --update again; if it stops the "
            "same way, see docs/troubleshooting.md, 'Update hand-over'."),
}


class TakeoverError(RuntimeError):
    def __init__(self, code, *, phase=None, undone=False, lost=0, update_message=None, pid=None):
        self.code, self.phase, self.undone = code, phase, undone
        if phase is None:
            detail = {
                'predecessor_unverified': "the running version could not be confirmed from this install's records",
                'predecessor_unproven': "the running service could not be proven to be this project's own",
                'takeover_unfinished': 'the update of another project is not finished',
            }.get(code, 'the update pre-check did not pass')
            message = f'The update stopped ({code}): {detail}; nothing was changed. '
            message += ('Finish it first: ' + update_message if code == 'takeover_unfinished' and update_message else
                        "See docs/troubleshooting.md, 'Update hand-over'.")
        elif code == 'collector_stop_failed':
            message = ('The update stopped (collector_stop_failed): the collector was asked to stop and did not stop in time. '
                       'The update was undone. The collector starts again on next use. Run ./setup.sh --update to try again.'
                       if undone else
                       'The update stopped (collector_stop_failed): collectors are paused for the update and the collector '
                       'did not stop. Run ./setup.sh --update to go on.')
        else:
            if undone:
                message = (f'The update stopped ({code}, {phase}). The update was undone. '
                           'The collector starts again on next use. Run ./setup.sh --update to try again.')
            elif code in ('service_stop_failed', 'predecessor_lock_inherited'):
                message = (f"The update stopped partway ({code}): this project's old service was told to stop, and "
                           f"{lost} child {'run ended' if lost == 1 else 'runs ended'} as lost. "
                           'Run ./setup.sh --update to finish.')
            else:
                message = (f'The update stopped partway ({code}, {phase}): the hand-over is not finished. '
                           'Run ./setup.sh --update to finish.')
            if code == 'predecessor_lock_inherited':
                message += " Another program still holds this project's records lock; the update goes on when it ends."
        if phase is None and code == 'predecessor_unproven' and not lost:
            message = A1_IDLE_UNPROVEN[type(pid) is int and pid > 0].format(pid=pid)
        elif phase is None and code in A1_WRITE_FREE:
            message = A1_WRITE_FREE[code].format(n=lost, runs='child run' if lost == 1 else 'child runs')
        elif phase is not None and not undone and code == 'service_still_finishing':
            message = ("The update stopped partway (service_still_finishing): this project's old service was told to stop "
                       f"and is finishing its work first ({lost} child {'run' if lost == 1 else 'runs'}). "
                       "It stops by itself when that ends, at most at the run's deadline. Then run ./setup.sh --update "
                       "to finish. Nothing else needs doing.")
        elif phase is not None and not undone and code == 'predecessor_lock_inherited':
            message = ("The update stopped partway (predecessor_lock_inherited): this project's old service has stopped, "
                       "but another program still holds this project's records lock, so nothing was taken over yet. "
                       "The update goes on when that program ends: run ./setup.sh --update again. "
                       "docs/troubleshooting.md, 'Update hand-over', shows how to see which program it is.")
        super().__init__(message)


def stop_until_excluded(stop, held, *, phase='collector_stopping'):
    for _ in range(MAX_STRAGGLER_STOPS):
        stop()
        if not held():
            return
    raise TakeoverError('predecessor_lock_inherited', phase=phase)


class UpdateTakeover:
    def __init__(self, root, operations, *, checkpoint=None):
        self.journal = TakeoverJournal(root)
        self.operations = operations
        self.checkpoint = checkpoint

    def _phase(self, document, phase):
        document = self.journal.phase(document, phase)
        if self.checkpoint is not None:
            self.checkpoint(phase, document)
        return document

    def run(self):
        # The caller holds collector-runtime.lock throughout this method and the
        # account start. Preflight itself is read-only, including on a resume.
        intent = self.operations.preflight()
        document = self.journal.read()
        if document is not None:
            if (document['project_id'] != intent['project_id'] or document['root_digest'] != intent['root_digest']
                    or document['successor'] != intent['successor']):
                raise TakeoverError('takeover_unfinished')
        else:
            document = self.journal.create(intent, self.operations.snapshot_runs())
            if self.checkpoint is not None:
                self.checkpoint('intent', document)
        start = PHASES.index(document['phase'])
        if start > PHASES.index('account_done'):
            return document
        actions = {
            'quiesced': self.operations.quiesce,
            'collector_stopping': self.operations.stop_collector,
            'hint_withdrawn': self.operations.withdraw_hint,
            'store_adopting': self.operations.adopt_project,
            'ledger_migrating': self.operations.adopt_ledger,
            'authorized': self.operations.authorize,
            'account_done': self.operations.start_collector,
        }
        try:
            for phase in PHASES[max(1, start):PHASES.index('account_done') + 1]:
                # Completed-state markers follow their effect. Intent and
                # stopping/adopting markers precede destructive operations.
                # The preceding durable intent also covers a crash between an
                # atomic authorization replacement and its completed marker.
                if phase in ('quiesced', 'authorized', 'account_done'):
                    actions[phase](document)
                    document = self._phase(document, phase)
                elif phase in ('runs_reconciled', 'late_runs_reconciled'):
                    if hasattr(self.operations, 'reconcile_runs'):
                        document = self.operations.reconcile_runs(document, late=phase == 'late_runs_reconciled')
                    else:
                        document = self.journal.reconcile(document, self.operations.snapshot_runs(
                            include_settled=False))
                    document = self._phase(document, phase)
                else:
                    document = self._phase(document, phase)
                    if phase in actions:
                        actions[phase](document)
                    if phase == 'hint_withdrawn':
                        self.operations.stop_service(document)
        except Exception as error:
            code = error.code if isinstance(error, TakeoverError) else {
                'intent': 'authorization_failed', 'quiesced': 'authorization_failed',
                'collector_stopping': 'collector_stop_failed', 'collector_stopped': 'predecessor_unverified',
                'runs_reconciled': 'predecessor_unverified', 'hint_withdrawn': 'service_stop_failed',
                'service_stopped': 'predecessor_lock_inherited', 'late_runs_reconciled': 'predecessor_unverified',
                'store_adopting': 'store_adoption_failed', 'ledger_migrating': 'ledger_migration_failed',
                'ledger_migrated': 'authorization_failed', 'authorized': 'collector_start_failed',
            }.get(document['phase'], 'takeover_unfinished')
            undone = False
            if PHASES.index(document['phase']) < PHASES.index('hint_withdrawn'):
                try:
                    self.operations.undo(document)
                    self.journal.remove()
                    undone = True
                except (OSError, ValueError, TakeoverError):
                    pass
            raise TakeoverError(code, phase=document['phase'], undone=undone, lost=len(document['runs']) + document.get('runs_unlisted', 0)) from None
        return document

    def installed(self):
        document = self.journal.read()
        if document is None or PHASES.index(document['phase']) < PHASES.index('account_done'):
            raise TakeoverError('takeover_unfinished', phase='installed')
        return self._phase(document, 'installed')

    def complete(self):
        document = self.journal.read()
        if document is None:
            return
        if PHASES.index(document['phase']) < PHASES.index('installed'):
            raise TakeoverError('takeover_unfinished', phase=document['phase'])
        self.operations.finish(document)
        document = self._phase(document, 'complete')
        self.journal.remove()
