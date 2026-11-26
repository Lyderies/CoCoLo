"""Regenerate TF-18 samples after reviewing a persistence-seam change.

This is a test-only developer tool. It discovers real/model calls but does not
run or certify a fault sweep. Inspect and commit the resulting inventory.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def choose(points, lane):
    from tests.merge_library_support import FAULTABLE
    groups = {}
    _rank = {'sqlite_commit': 0, 'exchange': 1, 'rename_noreplace': 2, 'sqlite_write': 3,
            'write': 4, 'fullfsync': 5, 'fsync': 5, 'mkdir': 6, 'unlink': 7, 'rmdir': 8,
            'open': 9, 'sqlite_read': 10}
    for key, call in points:
        if call.operation not in FAULTABLE | {'sqlite_read'}:
            continue
        signature = (call.operation, call.phase, call.when, call.detail.get('commit'))
        if signature not in groups:
            groups[signature] = key, call
    return [dict(operation=call.operation, phase=call.phase, side=call.when,
                 commit=call.detail.get('commit'), occurrence=key[1], key=key)
            for key, call in groups.values()]


def main():
    sys.path.insert(0, str(ROOT / 'packages/codex-agents'))
    from tests.merge_library_support import Fixture, Trace, replay_points, put
    from tests import test_merge_library_adversarial as real
    from tests import test_merge_library_model_sweep as model

    inventory = {'schema': 1, 'profile': 'ci/dev',
                 'soak': 'all discovered boundaries including non-persistence calls',
                 'reproducer': 'scripts/parallel_tests.py --base a1d25560 --module '
                               'codex:tests.test_merge_library_adversarial --module '
                               'codex:tests.test_merge_library_model_sweep',
                 'samples': {'real': {}, 'model': {}}}
    for kind in ('forward', 'reverse', 'recovering', 'undoing', 'cleaning'):
        fixture = Fixture()
        try:
            trace = Trace()
            getattr(real, kind)(fixture, fixture.batch(), trace)
            inventory['samples']['real'][kind] = choose(replay_points(trace.calls, fixture), 'real')
        finally:
            fixture.close()
        fixture, _batch, points = model.discovery(kind)
        try:
            inventory['samples']['model'][kind] = choose(points, 'model')
        finally:
            fixture.close()
    fixture = Fixture()
    try:
        raced = []
        def race(index, call):
            if not raced and call.operation == 'exchange' and call.phase == 'forward' and call.when == 'before':
                put(fixture.project / 'a.txt', b'OWNER captured race\n')
                raced.append(True)
        trace = Trace(race)
        fixture.land(fixture.batch({'a.txt': b'child'}), hook=trace)
        points = replay_points(trace.calls, fixture, lambda call: call.phase == 'compensation')
        inventory['samples']['real']['compensation'] = choose(points, 'real')
    finally:
        fixture.close()
    for phase in ('forward', 'compensation', 'reverse', 'recovery', 'cleanup'):
        fixture = Fixture()
        try:
            action = real.reverse if phase == 'reverse' else real.recovering if phase == 'recovery' else real.forward
            trace = Trace()
            if phase == 'compensation':
                def owner_race(index, call):
                    if call.operation == 'exchange' and call.phase == 'forward' and call.when == 'before':
                        put(fixture.project / 'a.txt', b'FIRST OWNER\n')
                trace.inject = owner_race
            action(fixture, fixture.batch({'a.txt': b'CHILD\n'}), trace)
            points = replay_points(trace.calls, fixture, lambda call: call.phase == phase)
            inventory['samples']['real']['owner_' + phase] = choose(points, 'real')
        finally:
            fixture.close()
    fixture = Fixture()
    try:
        for index in range(11):
            fixture.land(fixture.batch({f'p{index}': str(index).encode()}, f'c{index}'))
        trace = Trace()
        real.ml.cleanup(fixture.project, fixture.private, hook=trace)
        points = replay_points(trace.calls, fixture, lambda call: call.phase == 'retirement')
        inventory['samples']['real']['retirement'] = choose(points, 'real')
    finally:
        fixture.close()
    (ROOT / 'tests/merge-persistence-samples.json').write_text(json.dumps(inventory, indent=1, sort_keys=True) + '\n')
    print({lane: {kind: len(points) for kind, points in items.items()}
           for lane, items in inventory['samples'].items()})


if __name__ == '__main__':
    main()
