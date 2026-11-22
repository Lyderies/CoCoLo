"""TF-18: deterministic persistence-kind coverage in the volatile/durable model."""
import copy
import errno
import unittest

from codex_agents import merge_library as ml
from tests.merge_library_disk import MemoryDisk, ModelCrash
from tests.merge_library_support import (Fixture, FAULTABLE, ReplayPoint, check_end_state,
                                         replay_points, sample_boundaries, seed_applied, tree, snapshot_fault)


def prepare(kind):
    f = Fixture()
    batch = f.batch()
    if kind in ('reverse', 'recovering', 'cleaning'):
        seed_applied(f, batch, state='landed' if kind == 'cleaning' else 'applying')
    elif kind == 'undoing':
        f.land(batch)
    f.operation_before = tree(f.project)
    disk = MemoryDisk(f.root)
    return f, batch, disk


def operation(kind, f, batch, hook):
    if kind == 'forward':
        return f.land(batch, hook=hook)
    if kind == 'reverse':
        return ml.rollback(f.project, f.private, 1, hook=hook)
    if kind == 'undoing':
        return ml.undo(f.project, f.private, 1, hook=hook)
    reports = (ml.recover if kind == 'recovering' else ml.cleanup)(f.project, f.private, hook=hook)
    return reports[-1] if reports else ml.status(f.project, f.private, hook=hook)[-1]


def discovery(kind):
    f, batch, disk = prepare(kind)
    f.disk = disk
    operation(kind, f, batch, disk)
    points = replay_points(disk.calls, f, lambda call: call.operation in FAULTABLE)
    return f, batch, points


class PersistenceSamples(unittest.TestCase):
    __requirements__ = ('ML-11', 'ML-12', 'ML-19', 'TF-18', 'TF-19')
    __why_not_scenario__ = 'Deterministic persistence-kind samples distinguish volatile data loss from durable SQLite direction at each operation boundary.'

    def exercise(self, kind):
        f, batch, disk = prepare(kind)
        self.addCleanup(f.close)
        baseline = copy.deepcopy(disk)
        f.disk = disk
        operation(kind, f, batch, disk)
        points = replay_points(disk.calls, f, lambda call: call.operation in FAULTABLE)
        selected = sample_boundaries(points, lane='model', reproducer=kind)
        self.assertTrue(selected)
        for key, call in selected:
            for failure in ('power_loss', errno.EIO, errno.ENOSPC):
                with self.subTest(boundary=key, failure=failure):
                    current = f.disk = copy.deepcopy(baseline)
                    f.terminal_states = {}
                    def inject(at):
                        snapshot_fault(f, at)
                        if failure == 'power_loss':
                            current.crash(power_loss=True)
                            raise ModelCrash()
                        raise OSError(failure, 'modeled persistence fault')
                    point = ReplayPoint(key, f, inject)
                    def hook(at):
                        point(0, at)
                        return current(at)
                    try:
                        report = operation(kind, f, batch, hook)
                    except ModelCrash:
                        report = None
                    point.assert_fired(self)
                    if report is not None:
                        check_end_state(self, f, batch, report, kind=kind, call=call, fault=failure)
                    current.resume()
                    ml.recover(f.project, f.private, hook=current)
                    rows = ml.history(f.project, f.private, hook=current)
                    if rows:
                        latest = rows[-1]
                        status = next(r for r in ml.status(f.project, f.private, hook=current) if r.batch_no == latest.batch_no)
                        check_end_state(self, f, batch, status, kind='undoing' if latest.kind == 'undo' else 'forward' if kind == 'undoing' else kind)
                    else:
                        queued = ml.queue(f.project, f.private, hook=current)
                        check_end_state(self, f, batch, ml.Report(queued[-1].state if queued else 'refused', recorded=False), kind=kind)
                    before, history = current.snapshot(f.project), rows
                    current.resume()
                    ml.recover(f.project, f.private, hook=current)
                    self.assertEqual(before, current.snapshot(f.project))
                    self.assertEqual(history, ml.history(f.project, f.private, hook=current))


def make(kind):
    def test(self):
        self.exercise(kind)
    return test


for _kind in ('forward', 'reverse', 'recovering', 'undoing', 'cleaning'):
    setattr(PersistenceSamples, 'test_' + _kind + '_persistence_samples', make(_kind))
