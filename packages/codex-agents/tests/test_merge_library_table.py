"""TF-19: accept permitted outcomes and reject corrupt outcomes for every labeled table cell.

The authority calls its table '41 cells'; its explicit labels A1..I1 number 40.
This inventory covers every label and separate permitted variants (not an invented 41st label).
"""
import errno
import stat
import unittest
from dataclasses import replace

from codex_agents import merge_library as ml
from tests.merge_library_support import Fixture, check_end_state, put, tree

# Each variant is independently constructed from S/T, owner evidence and immutable rows.
CELLS = {
    'A1': ('refused',), 'A2': ('refused',), 'A3': ('queued',),
    'A4': ('merged', 'owner_only'), 'A5': ('waiting', 'waiting_row'),
    'A6': ('cancelled', 'reverted', 'terminal'), 'A7': ('refused', 'applying_S'),
    'B1': ('reverted',), 'B2': ('CU',), 'B3': ('io_S', 'io_T', 'pending'),
    'B4': ('owner',), 'B5': ('late_owner',), 'B6': ('moved',), 'B7': ('reverted',),
    'C1': ('landed', 'EL_present', 'EL_absent'), 'C2': ('applying_T',), 'C3': ('pending',),
    'D1': ('reverted', 'CU'), 'D2': ('owner',), 'D3': ('owner_deleted', 'owner_folder'),
    'D4': ('io_S', 'io_T'), 'D5': ('moved',),
    'D6': ('terminal', 'terminal_reverted', 'resolved', 'undone'), 'D7': ('unknown',),
    'D8': ('io_S', 'pending'), 'E1': ('EL_present', 'EL_absent'), 'E2': ('reverted',),
    'E3': ('owner',), 'E4': ('undo_landed',), 'F1': ('terminal', 'terminal_reverted', 'undo_landed'),
    'F2': ('retired',), 'G1': ('undo_refused',), 'G2': ('undo_reverted', 'undo_CU'),
    'G3': ('undo_owner',), 'G4': ('undo_landed', 'undo_reverted'), 'G5': ('undo_landed',),
    'G6': ('undo_io_S', 'undo_io_T'), 'H1': ('reverted', 'io_S'),
    'H2': ('resolved',), 'I1': ('foreign_staging',),
}


class TableOutcomes(unittest.TestCase):
    __requirements__ = ('TF-19',)
    __why_not_scenario__ = 'Independent valid and invalid examples certify the single oracle against each reviewed end-state table cell.'

    def exercise(self, cell, variant):
        f = Fixture()
        self.addCleanup(f.close)
        (f.project / 'owner-empty').mkdir()
        f.before = f.operation_before = tree(f.project)
        batch = f.batch({'a.txt': b'child'})
        row = ml.HistoryRow(1, batch, 'reverted')
        report = ml.Report('reverted', batch_no=1, recorded=True)
        rows = [row]
        kind = 'forward'
        extra = {}
        special = {}
        if variant in ('refused', 'queued', 'cancelled', 'waiting', 'unknown'):
            rows = []
            report = ml.Report('waiting_owner' if variant == 'waiting' else variant, recorded=False)
            if variant == 'queued':
                ml.enqueue(f.project, f.private, batch, policy=f.policy)
            if variant == 'unknown':
                def unreadable():
                    raise OSError(errno.EIO, 'independent unreadable history')
                extra['read_history'] = unreadable
        elif variant == 'waiting_row':
            rows = [replace(row, state='needs_owner')]
            report = replace(report, state='waiting_owner', reason='owner', recorded=False)
        elif variant in ('landed', 'merged', 'owner_only', 'late_owner', 'terminal', 'retired'):
            put(f.project / 'a.txt', b'child')
            rows = [replace(row, state='landed', contents_kept=variant != 'retired')]
            report = replace(report, state='landed')
            if variant in ('merged', 'owner_only'):
                data = b'merged owner and child' if variant == 'merged' else b'owner only'
                put(f.project / 'a.txt', data)
                f.target_overrides = {'a.txt': (stat.S_IFREG, 0o644, data)}
                f.merged_paths = {'a.txt'}
                report = replace(report, merged=('a.txt',))
            if variant == 'late_owner':
                put(f.project / 'a.txt', b'late owner')
                f.late_owner_versions = {'a.txt': (stat.S_IFREG, 0o644, b'late owner')}
            if variant in ('terminal', 'retired'):
                put(f.project / 'a.txt', b'later edit')
                f.operation_before = f.terminal_tree = tree(f.project)
                extra['terminal_before'] = 'landed'
        elif variant in ('EL_present', 'EL_absent', 'undo_CU'):
            batch = f.batch({'removed/old.txt': None})
            identity = ((f.project / 'removed').stat().st_dev, (f.project / 'removed').stat().st_ino)
            (f.project / 'removed/old.txt').unlink()
            rows = [replace(row, batch=batch, state='landed', emptied_folders={'removed': identity})]
            report = replace(report, state='landed')
            if variant in ('EL_absent', 'undo_CU'):
                (f.project / 'removed').rmdir()
            if variant == 'undo_CU':
                kind = 'undoing'
                f.operation_before = tree(f.project)
                (f.project / 'removed').mkdir()
                rows.append(ml.HistoryRow(2, batch, 'needs_owner', kind='undo', undoes=1))
                report = replace(report, state='needs_owner', reason='io', batch_no=2)
                special['removed'] = 'owner'
        elif variant == 'CU':
            batch = f.batch({'created/file': b'child'})
            (f.project / 'created').mkdir()
            rows = [replace(row, batch=batch, state='needs_owner')]
            report = replace(report, state='needs_owner', reason='io')
            special['created'] = 'owner'
        elif variant in ('owner', 'owner_deleted', 'owner_folder', 'moved'):
            rows = [replace(row, state='needs_owner')]
            report = replace(report, state='needs_owner', reason='moved' if variant == 'moved' else 'owner')
            if variant == 'owner_deleted':
                (f.project / 'a.txt').unlink()
                f.owner_versions['a.txt'] = {None}
            elif variant == 'owner_folder':
                (f.project / 'a.txt').unlink()
                (f.project / 'a.txt').mkdir()
                f.owner_versions['a.txt'] = {(stat.S_IFDIR, 0o755, None)}
            elif variant == 'moved':
                destination = f.outside / 'moved-a'
                (f.project / 'a.txt').rename(destination)
                info = destination.stat()
                f.moved_paths = {'a.txt': (destination, (info.st_dev, info.st_ino))}
                special['a.txt'] = 'moved'
            else:
                put(f.project / 'a.txt', b'owner')
                f.owner_versions['a.txt'] = {(stat.S_IFREG, 0o644, b'owner')}
        elif variant in ('applying_S', 'applying_T', 'pending', 'io_S', 'io_T'):
            pending = variant.startswith('applying') or variant == 'pending'
            rows = [replace(row, state='applying' if pending else 'needs_owner')]
            report = replace(report, state='needs_owner', reason='io', recorded=not pending)
            if variant.endswith('_T'):
                put(f.project / 'a.txt', b'child')
        elif variant.startswith('undo_'):
            kind = 'undoing'
            put(f.project / 'a.txt', b'child')
            f.operation_before = tree(f.project)
            original = replace(row, state='landed')
            if variant == 'undo_refused':
                rows = [original]
                report = replace(report, state='refused', reason='undo_state', recorded=False)
            else:
                state = 'landed' if variant == 'undo_landed' else 'needs_owner' if variant in ('undo_owner', 'undo_io_S', 'undo_io_T') else 'reverted'
                rows = [replace(original, state='undone' if state == 'landed' else 'landed'),
                        ml.HistoryRow(2, batch, state, kind='undo', undoes=1)]
                report = replace(report, state=state, batch_no=2, reason='owner' if variant == 'undo_owner' else 'io' if state == 'needs_owner' else '')
                if variant in ('undo_landed', 'undo_io_T'):
                    put(f.project / 'a.txt', f.before['a.txt'][2])
                elif variant == 'undo_owner':
                    put(f.project / 'a.txt', b'owner')
                    f.owner_versions['a.txt'] = {(stat.S_IFREG, 0o644, b'owner')}
        elif variant in ('resolved', 'undone', 'terminal_reverted'):
            state = 'reverted' if variant == 'terminal_reverted' else variant
            rows = [replace(row, state=state)]
            report = replace(report, state=state)
            put(f.project / 'a.txt', b'later edit')
            f.operation_before = f.terminal_tree = tree(f.project)
            extra['terminal_before'] = state
        elif variant == 'foreign_staging':
            saved = f.project / '.cocolo/staging' / batch.child_id / 'foreign'
            put(saved, b'foreign bytes')
            report = replace(report, leftovers=(str(saved),))
        elif variant != 'reverted':
            raise AssertionError(variant)

        def observed_report():
            current = tree(f.project)
            paths = []
            for event in batch.events:
                actual = current.get(event.path)
                base = None if event.base is None else (stat.S_IFREG, event.base.mode_bits, event.base.data)
                new = None if event.new is None else (stat.S_IFREG, event.new.mode_bits, event.new.data)
                classification = special.get(event.path, 'absent' if actual is None else 'base' if actual == base else 'new' if actual == new else 'owner')
                paths.append(ml.PathReport(event.path, classification))
            paths += [ml.PathReport(name, classification) for name, classification in special.items() if name not in {e.path for e in batch.events}]
            return replace(report, paths=tuple(paths))

        extra.setdefault('read_history', lambda: tuple(rows))
        check_end_state(self, f, batch, observed_report(), kind=kind, **extra)
        # Corrupt a complete version and recompute the report's class: rejection must
        # come from independent expected state/provenance, not a stale report label.
        target = f.project / ('created/foreign' if variant == 'CU' else 'removed/foreign' if variant == 'undo_CU' else 'a.txt')
        if target.is_dir():
            target.rmdir()
        put(target, b'torn-unapproved-version')
        with self.assertRaises(AssertionError, msg=f'{cell}/{variant} accepted corruption'):
            check_end_state(self, f, batch, observed_report(), kind=kind, **extra)


def make_cell(cell, variants):
    def test(self):
        for variant in variants:
            with self.subTest(cell=cell, outcome=variant):
                self.exercise(cell, variant)
    test.__name__ = 'test_' + cell + '_accepts_permitted_and_rejects_corrupt_outcomes'
    return test


for _cell, _variants in CELLS.items():
    setattr(TableOutcomes, 'test_' + _cell + '_accepts_permitted_and_rejects_corrupt_outcomes', make_cell(_cell, _variants))
