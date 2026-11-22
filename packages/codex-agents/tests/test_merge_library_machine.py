"""TF-18/19: generated action sequences, real library, modeled persistence, one oracle."""
import errno
import unittest
from dataclasses import replace

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from codex_agents import merge_library as ml
from tests.merge_library_disk import MemoryDisk, ModelCrash
from tests.merge_library_support import Fixture, ReplayPoint, check_end_state, replay_points


class MergeMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.f = Fixture()
        self.batch = self.f.batch({'a.txt': b'generated child'})
        self.disk = self.f.disk = MemoryDisk(self.f.root)
        self.test = unittest.TestCase()
        self.last = None
        # Independent expected operation maps, never obtained from the row/report.
        self.f.operation_before = dict(self.f.before)
        self.undo_start = None

    def rows(self):
        return ml.history(self.f.project, self.f.private, hook=self.disk)

    def invoke(self, action, data, mode):
        import copy
        probe = copy.deepcopy(self.disk)
        probe.calls = []
        action(probe)
        points = replay_points(probe.calls, self.f, lambda call: call.operation in (
            'fsync', 'fullfsync', 'sqlite_commit', 'sqlite_write', 'write', 'mkdir',
            'rmdir', 'unlink', 'exchange', 'rename_noreplace'))
        if mode == 'normal' or not points:
            self.last = action(self.disk)
            return
        key, _ = data.draw(st.sampled_from(points), label='persistence_boundary')
        def inject(call):
            if mode == 'power_loss':
                self.disk.crash()
                raise ModelCrash()
            raise OSError(errno.EIO, 'generated EIO at persistence boundary')
        point = ReplayPoint(key, self.f, inject)
        def hook(call):
            point(0, call)
            return self.disk(call)
        try:
            self.last = action(hook)
        except ModelCrash:
            self.last = ml.recover(self.f.project, self.f.private, hook=self.disk)
        point.assert_fired(self.test)

    @precondition(lambda self: not self.rows() or self.rows()[-1].state == 'reverted')
    @rule(data=st.data(), mode=st.sampled_from(('normal', 'EIO', 'power_loss')), checks=st.booleans())
    def land(self, data, mode, checks):
        self.f.operation_before = self.disk.snapshot(self.f.project, control=False)
        self.undo_start = None
        policy = replace(self.f.policy, checks=lambda project: checks)
        self.invoke(lambda hook: self.f.land(self.batch, hook=hook, policy=policy), data, mode)

    @precondition(lambda self: bool(self.rows()) and self.rows()[-1].state in ('applying', 'needs_owner'))
    @rule(data=st.data(), mode=st.sampled_from(('normal', 'EIO', 'power_loss')))
    def retry(self, data, mode):
        number = self.rows()[-1].batch_no
        self.invoke(lambda hook: ml.retry(self.f.project, self.f.private, number, policy=self.f.policy, hook=hook), data, mode)

    @rule()
    def cancel(self):
        queued = ml.queue(self.f.project, self.f.private, hook=self.disk)
        if not queued:
            queued = (ml.enqueue(self.f.project, self.f.private, self.batch, policy=self.f.policy, hook=self.disk),)
        self.last = ml.cancel(self.f.project, self.f.private, queued[-1].ticket, hook=self.disk)

    @precondition(lambda self: bool(self.rows()) and self.rows()[-1].kind == 'forward' and self.rows()[-1].state == 'landed')
    @rule(data=st.data(), mode=st.sampled_from(('normal', 'EIO', 'power_loss')))
    def undo(self, data, mode):
        number = self.rows()[-1].batch_no
        self.undo_start = self.disk.snapshot(self.f.project, control=False)
        self.f.operation_before = self.undo_start
        self.invoke(lambda hook: ml.undo(self.f.project, self.f.private, number, hook=hook), data, mode)

    @rule()
    def crash(self):
        self.disk.crash()
        self.last = ml.recover(self.f.project, self.f.private, hook=self.disk)

    @rule()
    def recover(self):
        self.last = ml.recover(self.f.project, self.f.private, hook=self.disk)

    @invariant()
    def durable_history_and_tree_obey_the_single_checker(self):
        rows = self.rows()
        reports = ml.status(self.f.project, self.f.private, hook=self.disk)
        if rows:
            latest = rows[-1]
            report = next(report for report in reports if report.batch_no == latest.batch_no)
            check_end_state(self.test, self.f, self.batch, report,
                            kind='undoing' if latest.kind == 'undo' else 'forward')
        else:
            queued = ml.queue(self.f.project, self.f.private, hook=self.disk)
            # A killed call has no report. Admission is observed independently from queue/history.
            observation = ml.Report(queued[-1].state if queued else 'refused', recorded=False)
            check_end_state(self.test, self.f, self.batch, observation)

    def teardown(self):
        self.f.close()


TestMergeMachine = MergeMachine.TestCase
TestMergeMachine.__module__ = __name__
TestMergeMachine.__qualname__ = 'TestMergeMachine'
TestMergeMachine.settings = settings(settings(), stateful_step_count=8)
TestMergeMachine.__requirements__ = ('ML-11', 'ML-12', 'ML-13', 'ML-17', 'ML-19', 'TF-18', 'TF-19')
TestMergeMachine.__why_not_scenario__ = 'Generated action sequences and power loss require volatile/durable disk state and exact persistence boundaries.'
