"""Slice D3 (RS-D U1-U10, K1, K2, S1): crash-safe retirement and the context sweep.

DESIGN-0.4.0-RS-D.md draft 3 s4-s6, draft 4 s1; DESIGN-0.4.0-RS-BD-shared.md SH-3 (retirement).
Retirement records `retiring`, unlinks the fixed names without opening them, fsyncs the directory and records
`expired`. Unsafe files (missing, corrupt, wrong mode, extra hard link, symlink, FIFO, link pair) are unlinked as
entries; only a directory under a fixed name needs repair (`repair_required:directory`, still charged, shown, retried).
The context sweep finishes context-only custody and never touches a context with a reservation row.
Integration stage: real stores in temporary folders, the fixtures of the D2 tests; faults are injected by patching
the unlink and fsync calls the store makes, never the real home.
"""
import builtins
import os
import stat
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests import test_deadlines_retention as dr
from tests import test_record_context as rc
from tests import test_result_leases as leases
from tests.learning_support import assignment
from tests.test_project_history import PROJECT_A, binding, terminal
from tests import test_project_results as project_results

from codex_agents import project_history, project_holds_schema, project_result_contexts
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
from codex_agents.result_read_api import RESULT_CHUNK_BYTES, decode_result_read_response, encode_result_read_request, \
    result_read_request
from codex_agents.result_read_service import ResultReadAuthorization, ResultReadService
from codex_agents.result_read_api import result_read_request_digest

WHY = ('D3 checks real project stores in temporary folders (leases, contexts, holds, files); injected unlink, fsync '
       'and crash controls expose each retirement step.')


class Stop(BaseException):
    """A process stop: nothing in the store catches it, so what was written stays."""


class RetirementFixture(dr.LeaseBackedFixture):
    def names(self, number=1):
        body = self.store._result_names(binding(number))
        context = project_result_contexts.names(self.store, *self.attempt(number))
        return body, context

    def path(self, name):
        return self.fixture.directory / name

    def committed(self, *numbers):
        for number in numbers:
            self.bound(number)
            self.publish(number)

    def due(self, number=1):
        return self.reservation(number).retained_until_ms

    def charged(self):
        return self.store._result_counts()[0]

    def pass_(self, **options):
        """One expire pass; the failure it reports, or None."""
        try:
            self.assertIsNotNone(self.store.expire_results(**options))
        except HistoryError as exc:
            return exc.reason_code
        return None

    def attempt_files(self):
        return sorted(path.name for path in self.fixture.directory.iterdir()
                      if path.name.startswith(('.result-', '.context-')))

    def failing_unlink(self, error, only=None):
        original = os.unlink

        def unlink(name, *args, **kwargs):
            if only is None or name in only:
                raise error
            return original(name, *args, **kwargs)
        return mock.patch.object(project_history.os, 'unlink', side_effect=unlink)

    def failing_fsync(self, *, after=False, error=None):
        original = os.fsync
        directory = self.store._directory_fd

        def fsync(descriptor):
            if descriptor == directory:
                if after:
                    original(descriptor)
                raise error or OSError('fsync cut')
            return original(descriptor)
        return mock.patch.object(project_history.os, 'fsync', side_effect=fsync)


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class UnsafeFileRetirementTests(RetirementFixture):
    """U1-U5: reads refuse, the row stays charged, retirement unlinks the entry, unrelated custody keeps retiring."""

    def outside(self):
        return self.fixture.directory.parent / 'outside-alias'

    def damage(self, kind, path):
        if kind == 'missing':
            path.unlink()
        elif kind == 'corrupt':
            path.write_bytes(b'CHANGED-PRIVATE-CANARY')
        elif kind == 'mode':
            os.chmod(path, 0o644)
        elif kind == 'hard_link':
            os.link(path, self.outside())
        elif kind == 'symlink':
            path.unlink()
            path.symlink_to(self.fixture.directory / 'history.sqlite3')
        elif kind == 'fifo':
            path.unlink()
            os.mkfifo(path, 0o600)
        elif kind == 'link_pair':
            partial = path.with_name(path.name.replace('.json', '.partial'))
            os.link(path, partial)
        else:
            raise AssertionError(kind)

    def check(self, kind, *, target):
        self.committed(1, 2)
        body, context = self.names(1)
        self.damage(kind, self.path((body if target == 'body' else context)[1]))
        # Reads refuse and the charge stays until retirement.
        if target == 'body' and kind != 'link_pair':  # a clean pair is recovered by a read, not refused
            with self.assertRaises(HistoryError):
                self.store.read_result(binding(1))
        if target == 'context' and kind != 'missing':  # the unsafe context is never served either
            with self.assertRaises(HistoryError):
                self.store.stored_result_context(binding(1), host_class='codex')
        self.assertEqual(2, self.charged())
        self.now = self.due(1)
        self.assertIsNone(self.pass_())  # the unsafe entry does not make the pass fail
        self.assertEqual(('expired', 'expired'), (self.row_state(1), self.context_state(1)))
        self.assertEqual(('expired', 'expired'), (self.row_state(2), self.context_state(2)))  # unrelated custody too
        self.assertEqual([], self.attempt_files())
        self.assertEqual(('released', 'retention'), self.release_kind(1))
        self.assertEqual(('released', 'retention'), self.release_kind(2))
        self.assertTrue((self.fixture.directory / 'history.sqlite3').is_file())  # a symlink's target is never touched
        self.assertEqual(0, self.charged())
        self.consistent()

    def test_U1_a_missing_file_is_retired_normally(self):
        self.check('missing', target='body')

    def test_U1_a_missing_context_file_is_retired_normally(self):
        self.check('missing', target='context')

    def test_U2_a_corrupt_body_is_retired_normally(self):
        self.check('corrupt', target='body')

    def test_U2_a_corrupt_context_is_retired_normally(self):
        self.check('corrupt', target='context')

    def test_U3_a_wrong_mode_body_is_unlinked_at_retirement(self):
        self.check('mode', target='body')

    def test_U3_a_wrong_mode_context_is_unlinked_at_retirement(self):
        self.check('mode', target='context')

    def test_U4_an_extra_hard_link_body_loses_only_its_store_entry(self):
        self.committed(1)
        body, _ = self.names(1)
        os.link(self.path(body[1]), self.outside())
        self.addCleanup(lambda: self.outside().unlink(missing_ok=True))
        before = self.outside().read_bytes()
        with self.assertRaisesRegex(HistoryError, 'history_unsafe_path'):
            self.store.read_result(binding(1))
        self.now = self.due(1)
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual([], self.attempt_files())
        self.assertEqual(before, self.outside().read_bytes())  # its other name, outside the store, is untouched
        self.assertEqual(('released', 'retention'), self.release_kind(1))
        self.consistent()

    def test_U4_an_extra_hard_link_with_an_unrelated_attempt_still_retiring_in_the_same_pass(self):
        self.addCleanup(lambda: self.outside().unlink(missing_ok=True))
        self.check('hard_link', target='body')

    def test_U4_an_extra_hard_link_context_is_unlinked_at_retirement(self):
        self.addCleanup(lambda: self.outside().unlink(missing_ok=True))
        self.check('hard_link', target='context')

    def test_U5_a_symlink_under_a_fixed_body_name_is_unlinked_not_followed(self):
        self.check('symlink', target='body')

    def test_U5_a_symlink_under_a_fixed_context_name_is_unlinked_not_followed(self):
        self.check('symlink', target='context')

    def test_U5_a_fifo_under_a_fixed_name_is_unlinked_without_blocking(self):
        self.check('fifo', target='body')
        self.assertFalse(any(stat.S_ISFIFO(path.lstat().st_mode) for path in self.fixture.directory.iterdir()))

    def test_U5_a_fifo_under_the_context_name_is_unlinked_without_blocking(self):
        self.check('fifo', target='context')

    def test_U5_a_link_pair_of_partial_and_final_is_removed_by_both_names(self):
        self.check('link_pair', target='body')


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class RepairRequiredTests(RetirementFixture):
    """U6: a directory under a fixed name is `repair_required:directory`; an unlink or fsync error keeps `retiring`."""

    def make_directory(self, number, which):
        body, context = self.names(number)
        path = self.path((body if which == 'body' else context)[1])
        path.unlink()
        path.mkdir()
        return path

    def check_directory(self, which):
        self.committed(1, 2)
        directory = self.make_directory(1, which)
        self.now = self.due(1)
        self.assertEqual('history_unsafe_path', self.pass_())  # the failure stays visible
        # The other row progressed in the same pass.
        self.assertEqual(('expired', 'expired'), (self.row_state(2), self.context_state(2)))
        self.assertEqual('retiring', self.row_state(1))
        self.assertEqual('repair_required', self.context_state(1))
        self.assertEqual('directory', self.store._connection.execute(
            'SELECT repair_reason FROM project_result_contexts WHERE run_handle=?', (self.attempt(1)[0],)).fetchone()[0])
        self.assertEqual(('released', 'retention'), self.release_kind(1))
        self.assertEqual(1, self.charged())  # still charged
        report = self.store.result_retirement_repairs()
        self.assertEqual([(self.attempt(1)[0], self.attempt(1)[1], 'directory', (directory.name,))],
                         [(row['run_handle'], row['attempt'], row['reason'], tuple(row['names'])) for row in report])
        for read in (lambda: self.store.read_result(binding(1)),  # reads refuse while it waits
                     lambda: self.store.stored_result_context(binding(1), host_class='codex')):
            with self.assertRaises(HistoryError):
                read()
        self.assertEqual('history_unsafe_path', self.pass_())  # retried on every pass
        self.assertEqual('repair_required', self.context_state(1))
        self.consistent()
        directory.rmdir()  # the operator's repair
        self.assertIsNone(self.pass_())
        self.assertEqual(('expired', 'expired'), (self.row_state(1), self.context_state(1)))
        self.assertEqual([], self.store.result_retirement_repairs())
        self.assertIsNone(self.store._connection.execute(
            'SELECT repair_reason FROM project_result_contexts WHERE run_handle=?', (self.attempt(1)[0],)).fetchone()[0])
        self.assertEqual([], self.attempt_files())
        self.assertEqual(0, self.charged())
        self.consistent()

    def test_U6_a_directory_under_the_body_name_is_repair_required_charged_shown_and_retried(self):
        self.check_directory('body')

    def test_U6_a_directory_under_the_context_name_is_repair_required_charged_shown_and_retried(self):
        self.check_directory('context')

    def test_U6_an_unlink_error_keeps_the_row_retiring_charged_and_other_rows_progress(self):
        self.committed(1, 2)
        self.now = self.due(1)
        body, context = self.names(1)
        with self.failing_unlink(PermissionError('CANARY-delete'), only={body[1], context[1]}):
            self.assertEqual('history_unavailable', self.pass_())
            self.assertEqual(('expired', 'expired'), (self.row_state(2), self.context_state(2)))
            self.assertEqual(('retiring', 'retiring'), (self.row_state(1), self.context_state(1)))
            self.assertEqual(1, self.charged())
            self.assertEqual('history_unavailable', self.pass_())  # retried every pass
        self.assertIsNone(self.pass_())  # once the fault clears
        self.assertEqual(('expired', 'expired'), (self.row_state(1), self.context_state(1)))
        self.assertEqual([], self.attempt_files())
        self.consistent()

    def test_U6_a_directory_fsync_error_keeps_the_row_retiring_and_charged_until_it_succeeds(self):
        self.committed(1)
        self.now = self.due(1)
        with self.failing_fsync():
            self.assertEqual('history_unavailable', self.pass_())
            self.assertEqual(('retiring', 'retiring'), (self.row_state(), self.context_state()))
            self.assertEqual(1, self.charged())
            self.assertEqual('history_unavailable', self.pass_())
        self.assertIsNone(self.pass_())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual(0, self.charged())
        self.consistent()

    def test_U6_a_directory_row_does_not_starve_a_later_row_when_the_limit_is_one(self):
        self.committed(1, 2)
        self.make_directory(1, 'body')
        self.now = self.due(1)
        results = [self.pass_(limit=1) for _ in range(3)]
        self.assertEqual(('expired', 'expired'), (self.row_state(2), self.context_state(2)))
        self.assertEqual('retiring', self.row_state(1))
        self.assertIn('history_unsafe_path', results)


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class CrashSafeRetirementTests(RetirementFixture):
    """K1: a stop at any step of a retirement is finished by the next pass; charges stay until `expired`."""

    def crash(self, point):
        self.committed(1)
        self.now = self.due(1)
        if point == 'after_retiring':
            patch = self.failing_unlink(Stop())
        elif point == 'after_body_unlink':
            body, context = self.names(1)
            patch = self.failing_unlink(Stop(), only={context[1]})
        elif point == 'body_unlinked_only':  # the body's names went (a partial too), the context's final name stays
            body, context = self.names(1)
            for name in body:
                self.path(name).unlink(missing_ok=True)
            self.path(body[0]).write_bytes(b'LEFTOVER-BODY-PARTIAL')  # a partial name of a stopped writer is present
            self.assertEqual({body[0], context[1]}, set(self.attempt_files()))
            patch = self.failing_unlink(Stop(), only={body[0]})
        elif point == 'context_unlinked_only':  # the context's names went, the body's final name stays
            body, context = self.names(1)
            for name in context:
                self.path(name).unlink(missing_ok=True)
            self.path(context[0]).write_bytes(b'LEFTOVER-CONTEXT-PARTIAL')
            self.assertEqual({body[1], context[0]}, set(self.attempt_files()))
            patch = self.failing_unlink(Stop(), only={body[1]})
        elif point == 'after_context_unlink':
            patch = self.failing_fsync(error=Stop())
        elif point == 'after_directory_fsync':
            patch = self.failing_fsync(after=True, error=Stop())
        else:
            patch = mock.patch.object(project_result_contexts, 'end_retirement', side_effect=Stop())
        with patch, self.assertRaises(Stop):
            self.store.expire_results()
        return point

    def check(self, point):
        self.crash(point)
        self.assertEqual('retiring', self.row_state())  # the retirement is recorded, not finished
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.assertEqual(1, self.charged())  # the charge is kept until `expired`
        body, context = self.names(1)
        remaining = {'body_unlinked_only': {body[0], context[1]}, 'context_unlinked_only': {body[1], context[0]}}
        if point in remaining:  # the distinct on-disk state the stop left is the one the next pass starts from
            self.assertEqual(remaining[point], set(self.attempt_files()))
        if point in ('after_retiring', 'after_body_unlink', 'body_unlinked_only', 'context_unlinked_only'):
            with self.failing_unlink(OSError('still failing')):  # a file remains: no read returns it in between
                for read in (lambda: self.store.read_result(binding(1)),
                             lambda: self.store.stored_result_context(binding(1), host_class='codex')):
                    with self.assertRaises(HistoryError):
                        read()
        self.base.reopen(result_capacity=self.fixture.capacity)  # the restart
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.assertEqual([], self.attempt_files())
        self.assertEqual(('released', 'retention'), self.release_kind())
        self.assertEqual(0, self.charged())
        self.assertEqual(0, self.store.expire_results())
        self.consistent()

    def test_K1_a_stop_after_the_row_is_recorded_retiring_is_finished_by_the_next_pass(self):
        self.check('after_retiring')

    def test_K1_a_stop_after_the_body_unlink_is_finished_by_the_next_pass(self):
        self.check('after_body_unlink')

    def test_K1_a_stop_with_only_the_body_names_unlinked_is_finished_by_the_next_pass(self):
        self.check('body_unlinked_only')

    def test_K1_a_stop_with_only_the_context_names_unlinked_is_finished_by_the_next_pass(self):
        self.check('context_unlinked_only')

    def test_K1_a_stop_after_the_context_unlink_is_finished_by_the_next_pass(self):
        self.check('after_context_unlink')

    def test_K1_a_stop_after_the_directory_fsync_is_finished_by_the_next_pass(self):
        self.check('after_directory_fsync')

    def test_K1_a_stop_before_expired_is_finished_by_the_next_pass(self):
        self.check('before_expired')

    def test_K1_a_repair_required_row_survives_a_restart_and_finishes_when_repaired(self):
        self.committed(1)
        body, _ = self.names(1)
        path = self.path(body[1])
        path.unlink()
        path.mkdir()
        self.now = self.due(1)
        self.assertEqual('history_unsafe_path', self.pass_())
        self.base.reopen(result_capacity=self.fixture.capacity)
        self.assertEqual('repair_required', self.context_state())
        self.assertEqual(1, self.charged())
        path.rmdir()
        self.assertEqual(1, self.store.expire_results())
        self.assertEqual(('expired', 'expired'), (self.row_state(), self.context_state()))
        self.consistent()


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class ContextSweepTests(RetirementFixture):
    """U7-U10: the sweep finishes context-only custody and never touches a context with a reservation row."""

    def lease(self, number):
        return self.store.reserve_result_lease(leases.lease_binding(number), assignment(),
                                               record_context=rc.simple_context(number))

    def writes_to_reservations(self):
        statements = []
        self.store._connection.set_trace_callback(statements.append)
        self.addCleanup(self.store._connection.set_trace_callback, None)
        return statements

    def assert_no_reservation_write(self, statements):
        self.assertEqual([], [text for text in statements if 'result_reservations' in text
                              and text.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))])
        self.assertEqual(0, self.store._connection.execute('SELECT COUNT(*) FROM result_reservations').fetchone()[0])

    def withdrawn_but_not_finished(self, number):
        """An unarmed lease released `withdrawn_unsent` whose file removal failed: the context stays `retiring`."""
        self.lease(number)
        with self.failing_unlink(OSError('stop before the unlink')):
            self.assertEqual(1, self.store.release_earlier_unarmed_leases(set()))
        self.assertEqual('retiring', self.context_state(number))
        self.assertEqual(number, len(rc.context_files(self.fixture.directory)))  # none removed yet

    def test_U7_an_unarmed_context_whose_removal_failed_is_finished_by_the_sweep_with_no_reservation_row(self):
        self.withdrawn_but_not_finished(1)
        statements = self.writes_to_reservations()
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(1))
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assertEqual(('released', 'lost_with_reservation'), self.release_kind(1))
        self.assert_no_reservation_write(statements)
        self.assertEqual(0, rc.live_context_bytes(self.store))
        self.consistent()

    def release_context_only(self, number, kind):
        """What a denied, fenced or lost-unbound lease leaves: its custody released, its context not yet retiring."""
        self.lease(number)
        identity = f'{self.attempt(number)[0]}:{self.attempt(number)[1]}'
        with self.store._transaction():
            project_holds_schema.release_hold(self.store._connection, binding(number).project_id, 'lease', identity,
                                              kind, now_ms=self.now)
            project_holds_schema.release_hold(self.store._connection, binding(number).project_id, 'result_custody',
                                              identity, 'lost_with_reservation', now_ms=self.now,
                                              evidence=project_holds_schema.evidence_json(cause=kind))

    def test_U7_denied_fenced_and_lost_unbound_contexts_are_finished_by_the_sweep(self):
        for number, kind in enumerate(('denied', 'fenced', 'lost_unbound'), 1):
            self.release_context_only(number, kind)
            self.assertEqual('leased', self.context_state(number))
        self.assertEqual(3, len(rc.context_files(self.fixture.directory)))
        self.assertIsNone(self.pass_())
        self.assertEqual(['ended'] * 3, [self.context_state(number) for number in (1, 2, 3)])
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))

    def test_U8_a_stop_after_the_context_is_retiring_is_finished_by_the_next_pass(self):
        self.withdrawn_but_not_finished(1)
        with mock.patch.object(project_result_contexts, 'reconcile'):  # the start-up reconcile must not finish it
            self.base.reopen(result_capacity=self.fixture.capacity)
        self.assertEqual('retiring', self.context_state(1))  # so the pass, not the open, finishes it
        self.assertEqual(1, len(rc.context_files(self.fixture.directory)))
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(1))
        self.assertEqual([], rc.context_files(self.fixture.directory))

    def test_U8_a_stop_after_the_context_unlink_is_finished_by_the_next_pass(self):
        self.withdrawn_but_not_finished(1)
        (name,) = rc.context_files(self.fixture.directory)
        self.path(name).unlink()  # the unlink happened, the stop came before `ended`
        self.assertEqual('retiring', self.context_state(1))
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(1))
        self.assertEqual(0, rc.live_context_bytes(self.store))
        self.consistent()

    def test_U8_the_context_sweep_is_bounded_and_fair_across_passes(self):
        for number in (1, 2, 3):
            self.withdrawn_but_not_finished(number)
        self.assertEqual(['retiring'] * 3, [self.context_state(number) for number in (1, 2, 3)])
        for _ in range(3):
            self.assertIsNone(self.pass_(limit=1))
        self.assertEqual(['ended'] * 3, [self.context_state(number) for number in (1, 2, 3)])

    def test_U9_the_sweep_does_not_select_reserved_committed_or_recorded_attempts_released_bound(self):
        self.bound(1)  # reserved: lease released `bound`, a reservation row
        self.committed(2)
        self.committed(3)
        self.record('a' * 64, 3)
        before = [(self.row_state(n), self.context_state(n), self.release_kind(n)) for n in (1, 2, 3)]
        self.assertEqual(['reserved', 'committed', 'committed'], [state for state, _, _ in before])
        self.assertEqual(['bound', 'committed', 'recorded'], [state for _, state, _ in before])
        self.assertEqual(0, self.store.expire_results())
        self.assertIsNone(self.pass_())
        self.assertEqual(before, [(self.row_state(n), self.context_state(n), self.release_kind(n)) for n in (1, 2, 3)])
        self.assertEqual(3, len(rc.context_files(self.fixture.directory)))
        self.assertEqual(2, len(self.fixture.files()))  # the reserved row has no body yet
        self.assertEqual(3, self.charged())
        self.consistent()

    def test_U10_every_context_only_kind_a_retiring_and_a_repair_required_retry_are_finished_with_no_reservation_row(self):
        self.withdrawn_but_not_finished(1)  # `retiring` (its lease is released `withdrawn_unsent`)
        statements = self.writes_to_reservations()
        for number, kind in enumerate(('withdrawn_unsent', 'denied', 'fenced', 'lost_unbound'), 2):
            self.release_context_only(number, kind)
        self.release_context_only(6, 'denied')
        context = project_result_contexts.names(self.store, *self.attempt(6))[1]
        self.path(context).unlink()
        self.path(context).mkdir()  # a directory under its name: the sweep marks it `repair_required`
        self.assertEqual('history_unsafe_path', self.pass_())
        self.assertEqual('repair_required', self.context_state(6))
        self.assertEqual(['ended'] * 5, [self.context_state(number) for number in range(1, 6)])
        self.assertEqual('history_unsafe_path', self.pass_())  # the retry
        self.path(context).rmdir()
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(6))
        self.assertEqual([], rc.context_files(self.fixture.directory))
        self.assert_no_reservation_write(statements)
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class LargeBodyReadTests(unittest.TestCase):
    """K2: a body of 1 MiB is returned through every chunk, the last one short, and an offset at the size is refused."""

    def test_K2_every_chunk_of_a_one_mib_body_is_returned_the_last_short_and_the_offset_at_the_size_refused(self):
        fixture = project_results.ProjectResultTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        size = MAX_RESULT_BODY_BYTES - 100  # not a multiple of the chunk, so the last chunk is short
        from tests.learning_support import result
        import json
        document = result()
        document['summary'] = ''
        document['summary'] = 'x' * (size - len(canonical_json(document)))
        body = canonical_json(document)
        record = json.loads(terminal())
        record.update(result_digest=keyed_digest(project_results.KEY, body), fenced=False, reviewed=True, terminal_state=None,
                      result_validation_reason_code=None)
        fixture.store.reserve_result(binding(), assignment(), hard_deadline_at_ms=2000)
        fixture.store.commit_result(canonical_json(record), binding(), assignment(), body)
        digest = record['result_digest']
        token = object()

        def authorize(command, context):
            if context is not token or command.binding != binding():
                return None
            return ResultReadAuthorization(command.binding, command.result_digest, result_read_request_digest(command))
        service = ResultReadService(PROJECT_A, binding().runtime_digest, lambda: fixture.store, authorizer=authorize,
                                    close_history=False)
        self.addCleanup(service.close)
        received = bytearray()
        offsets = []
        for offset in range(0, len(body), RESULT_CHUNK_BYTES):
            command = result_read_request(binding(), digest, offset)
            chunk = decode_result_read_response(service.handle(encode_result_read_request(command), token), command)
            offsets.append(offset)
            received += chunk.data
            self.assertEqual(len(body), chunk.body_size)
        self.assertEqual(body, bytes(received))
        self.assertEqual(len(body) // RESULT_CHUNK_BYTES + 1, len(offsets))
        self.assertLess(len(chunk.data), RESULT_CHUNK_BYTES)  # the last chunk is short
        for offset in (len(body), len(body) + RESULT_CHUNK_BYTES):  # refused by the request or by the service
            with self.assertRaises(HistoryError):
                command = result_read_request(binding(), digest, offset)
                decode_result_read_response(service.handle(encode_result_read_request(command), token), command)


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class StartupReconcileTests(RetirementFixture):
    """S1: start-up reconciliation reads no body or context bytes, and its work does not grow with tombstones."""

    LIVE = 64
    TOMBSTONES = 100_000

    def setUp(self):
        super().setUp()
        self.fixture.capacity = ResultCapacity(200, 200 * MAX_RESULT_BODY_BYTES, 60_000, 120_000)
        self.base.reopen(result_capacity=self.fixture.capacity)
        for number in range(1, self.LIVE + 1):
            self.store.reserve_result_lease(leases.lease_binding(number), assignment(),
                                            record_context=rc.simple_context(number))

    def litter(self):
        """Orphans, partial files and link pairs left by stopped writers."""
        directory = self.fixture.directory
        for old in directory.glob('.result-*'):
            old.unlink()
        key = 'f' * 64
        files = {
            f'.context-{key}.json': b'ORPHAN-CONTEXT-CANARY',
            f'.context-{"e" * 64}.partial': b'PARTIAL-CONTEXT-CANARY',
            f'.result-{"d" * 64}.partial': b'LONE-BODY-PARTIAL-CANARY',
        }
        for name, content in files.items():
            (directory / name).write_bytes(content)
        pair = '9' * 64
        (directory / f'.result-{pair}.partial').write_bytes(b'LINK-PAIR-CANARY')
        os.link(directory / f'.result-{pair}.partial', directory / f'.result-{pair}.json')
        context_pair = '8' * 64
        (directory / f'.context-{context_pair}.partial').write_bytes(b'CONTEXT-PAIR-CANARY')
        os.link(directory / f'.context-{context_pair}.partial', directory / f'.context-{context_pair}.json')
        return pair

    def measured_reconcile(self):
        steps = []
        opened = []
        connection = self.store._connection
        connection.set_progress_handler(lambda: steps.append(1), 10)
        real_open, real_os_open, _real_read = builtins.open, os.open, os.read

        def watch(name):
            if str(name).startswith(('.context-', '.result-')) or '/.context-' in str(name) or '/.result-' in str(name):
                opened.append(str(name))

        def traced_open(name, *args, **kwargs):
            watch(name)
            return real_open(name, *args, **kwargs)

        def traced_os_open(name, *args, **kwargs):
            watch(name)
            return real_os_open(name, *args, **kwargs)
        try:
            with mock.patch.object(builtins, 'open', traced_open), mock.patch.object(os, 'open', traced_os_open):
                project_result_contexts.reconcile(self.store)
        finally:
            connection.set_progress_handler(None, 0)
        return len(steps) * 10, opened

    def test_S1_reconciliation_removes_orphans_partials_and_link_pair_names_and_reads_no_bytes(self):
        pair = self.litter()
        _, opened = self.measured_reconcile()
        self.assertEqual([], opened)  # no body or context file is opened, so none of its bytes is read
        names = self.attempt_files()
        self.assertEqual(self.LIVE, len([name for name in names if name.startswith('.context-')]))  # only live contexts
        self.assertNotIn(f'.result-{"d" * 64}.partial', names)  # a lone partial goes
        self.assertNotIn(f'.result-{pair}.partial', names)  # a link pair keeps one name
        self.assertEqual(1, os.lstat(self.fixture.directory / f'.result-{pair}.json').st_nlink)
        self.assertEqual(LIVE_STATES, self.live_states())
        self.assertTrue(project_holds_schema.custody_consistent(self.store._connection, leases.fixtures.KEY))

    def live_states(self):
        return sorted(set(row[0] for row in self.store._connection.execute(
            'SELECT state FROM project_result_contexts')))

    def traced_start_up(self):
        """The whole start-up (a fresh open of the store, its reconcile included) with these routes watched: an open by
        a str or bytes path name (builtins, io, os), and a read, pread or readv of a descriptor that this trace saw
        opened on such a name. A descriptor inherited from outside the trace is not covered."""
        import io
        opened, tracked, read = [], set(), []

        def matches(name):
            return os.fsdecode(name).rsplit('/', 1)[-1].startswith(('.context-', '.result-')) \
                if isinstance(name, (str, bytes, os.PathLike)) else False

        def wrap_open(real):
            def traced(name, *args, **kwargs):
                if matches(name):
                    opened.append(str(name))
                return real(name, *args, **kwargs)
            return traced
        real_os_open = os.open

        def traced_os_open(name, *args, **kwargs):
            descriptor = real_os_open(name, *args, **kwargs)
            if matches(name):
                opened.append(str(name))
                tracked.add(descriptor)
            return descriptor

        def wrap_read(real):
            def traced(descriptor, *args, **kwargs):
                if descriptor in tracked:
                    read.append(descriptor)
                return real(descriptor, *args, **kwargs)
            return traced
        with mock.patch.object(builtins, 'open', wrap_open(builtins.open)), \
                mock.patch.object(io, 'open', wrap_open(io.open)), mock.patch.object(os, 'open', traced_os_open), \
                mock.patch.object(os, 'read', wrap_read(os.read)), mock.patch.object(os, 'pread', wrap_read(os.pread)), \
                mock.patch.object(os, 'readv', wrap_read(os.readv)):
            self.base.reopen(result_capacity=self.fixture.capacity)
        return opened, read

    def test_S1_the_whole_start_up_opens_and_reads_no_body_or_context_file(self):
        pair = self.litter()
        opened, read = self.traced_start_up()
        self.assertEqual([], opened)  # 0 files opened by name
        self.assertEqual([], read)  # 0 reads of any descriptor on one
        self.assertNotIn(f'.result-{pair}.partial', self.attempt_files())  # and the start-up did its work

    def test_S1_one_failing_entry_does_not_stop_the_start_up_and_nothing_outside_the_store_is_followed(self):
        directory = self.fixture.directory
        outside = directory.parent / 'outside-start-up-target'
        outside.write_bytes(b'OUTSIDE-CANARY')
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        broken = f'.context-{"a" * 64}.json'  # a directory under an orphan context name: cannot be removed
        (directory / broken).mkdir()
        (directory / f'.context-{"b" * 64}.json').symlink_to(outside)  # an orphan symlink: unlinked, not followed
        (directory / f'.result-{"c" * 64}.partial').symlink_to(outside)  # a body partial symlink
        (directory / f'.context-{"d" * 64}.json').write_bytes(b'ORPHAN')  # a plain orphan after the broken one
        os.mkfifo(directory / f'.context-{"e" * 64}.json', 0o600)  # a FIFO would block an open
        self.base.reopen(result_capacity=self.fixture.capacity)  # the open does not fail
        names = self.attempt_files()
        self.assertIn(broken, names)  # the directory is left for repair
        for letter in 'bde':
            self.assertNotIn(f'.context-{letter * 64}.json', names)
        self.assertNotIn(f'.result-{"c" * 64}.partial', names)
        self.assertEqual(b'OUTSIDE-CANARY', outside.read_bytes())  # nothing was followed or truncated

    def add_tombstones(self):
        connection = self.store._connection
        project = PROJECT_A
        with self.store._transaction():
            connection.executemany(
                "INSERT INTO project_result_contexts(project_id,run_handle,attempt,state,context_size,lease_deadline_ms) "
                "VALUES(?,?,1,'expired',0,0)", ((project, f'tomb-{n}') for n in range(self.TOMBSTONES)))
            connection.executemany(
                "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,released_at_ms,release_kind) "
                "VALUES(?,'result_custody',?,'released',0,0,'retention')",
                ((project, f'tomb-{n}:1') for n in range(self.TOMBSTONES)))

    def test_S1_reconciliation_work_is_the_same_at_zero_and_at_one_hundred_thousand_tombstones(self):
        runs = 5
        few_runs, many_runs, opened = [], [], []
        for _ in range(runs):
            self.litter()
            steps, seen = self.measured_reconcile()
            few_runs.append(steps)
            opened += seen
        self.add_tombstones()
        for _ in range(runs):
            self.litter()
            steps, seen = self.measured_reconcile()
            many_runs.append(steps)
            opened += seen
        few, many = max(few_runs), max(many_runs)
        self.assertEqual([], opened)
        self.assertGreater(min(few_runs), 0)
        # Measured: the same litter costs 480 steps on every run but one of 490 (the handler counts in ticks of 10),
        # with and without 100 000 tombstones. Two ticks of margin over the worst quiet run; one statement per
        # tombstone would add at least 100 000 steps, so this bound fails for growth with the tombstones; growth of
        # up to 20 steps (two ticks) is tolerated, as it is within the run-to-run difference.
        self.assertLessEqual(many, few + 20, (few_runs, many_runs))
        self.assertEqual(self.LIVE, len([name for name in self.attempt_files() if name.startswith('.context-')]))


@__import__('test_framework').requirements('RS-D-D3', why_not_scenario=WHY)
class ReservationOnlyAndVisibilityTests(RetirementFixture):
    """D3 r1 MED-1 and the io deviation: a reservation without a context row (the test-only non-lease path) with a
    directory under its name is repair-visible and retried; an unlink or fsync error stays `retiring` but is shown."""

    def direct(self, number):
        self.fixture.reserve(number)
        self.fixture.commit(number)

    def reservation_only_names(self, number):
        return self.store._result_names(binding(number))

    def report(self):
        return [(row['run_handle'], row['attempt'], row['reason'], tuple(row['names']))
                for row in self.store.result_retirement_repairs()]

    def test_M1_a_directory_under_a_reservation_only_body_name_is_shown_charged_retried_and_finished_when_removed(self):
        self.direct(7)
        self.assertIsNone(rc.context_row(self.store, *self.attempt(7)))  # no context row at all
        directory = self.path(self.reservation_only_names(7)[1])
        directory.unlink()
        directory.mkdir()
        self.now = self.due(7)
        self.assertEqual('history_unsafe_path', self.pass_())
        self.assertEqual('retiring', self.row_state(7))
        self.assertEqual(1, self.charged())  # charges kept
        expected = [(self.attempt(7)[0], self.attempt(7)[1], 'directory', (directory.name,))]
        self.assertEqual(expected, self.report())
        with self.assertRaises(HistoryError):
            self.store.read_result(binding(7))
        self.assertEqual('history_unsafe_path', self.pass_())  # retried on every pass
        self.base.reopen(result_capacity=self.fixture.capacity)  # and the shown state is durable
        self.assertEqual(expected, self.report())
        directory.rmdir()  # the operator's repair
        self.assertIsNone(self.pass_())
        self.assertEqual('expired', self.row_state(7))
        self.assertEqual([], self.report())
        self.assertEqual(0, self.charged())
        self.assertEqual([], self.attempt_files())

    def test_M1_a_reservation_only_directory_row_does_not_starve_a_later_row_when_the_limit_is_one(self):
        self.direct(7)
        self.committed(1)
        directory = self.path(self.reservation_only_names(7)[1])
        directory.unlink()
        directory.mkdir()
        self.now = max(self.due(7), self.due(1))
        results = [self.pass_(limit=1) for _ in range(4)]
        self.assertIn('history_unsafe_path', results)
        self.assertEqual(('expired', 'expired'), (self.row_state(1), self.context_state(1)))
        self.assertEqual('retiring', self.row_state(7))

    def test_M2_an_unlink_error_stays_retiring_charged_retried_and_is_shown_as_retrying(self):
        self.committed(1)
        self.direct(7)
        self.now = max(self.due(1), self.due(7))
        body, context = self.names(1)
        failing = {body[1], context[1], self.reservation_only_names(7)[1]}
        with self.failing_unlink(PermissionError('CANARY-delete'), only=failing):
            self.assertEqual('history_unavailable', self.pass_())
            self.assertEqual(('retiring', 'retiring', 'retiring'),
                             (self.row_state(1), self.context_state(1), self.row_state(7)))
            self.assertEqual(2, self.charged())
            self.assertEqual({(self.attempt(1), 'retrying', ()), (self.attempt(7), 'retrying', ())},
                             {((handle, attempt), reason, names) for handle, attempt, reason, names in self.report()})
            self.assertEqual('history_unavailable', self.pass_())  # retried every pass
        self.assertIsNone(self.pass_())
        self.assertEqual(('expired', 'expired', 'expired'),
                         (self.row_state(1), self.context_state(1), self.row_state(7)))
        self.assertEqual([], self.report())
        self.assertEqual(0, self.charged())

    def test_M2_a_directory_fsync_error_stays_retiring_and_is_shown_as_retrying(self):
        self.committed(1)
        self.now = self.due(1)
        with self.failing_fsync():
            self.assertEqual('history_unavailable', self.pass_())
            self.assertEqual([(*self.attempt(1), 'retrying', ())], self.report())
        self.assertIsNone(self.pass_())
        self.assertEqual([], self.report())

    def test_M3_a_context_only_sweep_fsync_error_stays_retiring_is_shown_and_recovers(self):
        helper = ContextSweepTests(methodName='runTest')
        helper.base, helper.fixture = self.base, self.fixture
        helper.withdrawn_but_not_finished(1)
        with self.failing_fsync():
            self.assertEqual('history_unavailable', self.pass_())
            self.assertEqual('retiring', self.context_state(1))
            self.assertEqual([(*self.attempt(1), 'retrying', ())], self.report())
            self.assertEqual(1, project_result_contexts_live(self.store))  # still charged
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(1))
        self.assertEqual([], self.report())

    def test_M3_a_context_only_directory_row_survives_a_restart_without_failing_the_open_and_finishes_when_repaired(self):
        helper = ContextSweepTests(methodName='runTest')
        helper.base, helper.fixture = self.base, self.fixture
        for number in (1, 2):
            helper.withdrawn_but_not_finished(number)
        context = project_result_contexts.names(self.store, *self.attempt(1))[1]
        self.path(context).unlink()
        self.path(context).mkdir()
        self.assertEqual('history_unsafe_path', self.pass_())
        self.assertEqual(('repair_required', 'ended'), (self.context_state(1), self.context_state(2)))
        self.base.reopen(result_capacity=self.fixture.capacity)  # the open does not fail, and keeps the repair state
        self.assertEqual('repair_required', self.context_state(1))
        self.assertEqual([(*self.attempt(1), 'directory', (context,))], self.report())
        self.path(context).rmdir()
        self.assertIsNone(self.pass_())
        self.assertEqual('ended', self.context_state(1))
        self.assertEqual([], self.report())

    def test_M3_a_persistently_failing_context_only_row_does_not_starve_later_rows_when_the_limit_is_one(self):
        helper = ContextSweepTests(methodName='runTest')
        helper.base, helper.fixture = self.base, self.fixture
        for number in (1, 2, 3):
            helper.release_context_only(number, 'denied')
        context = project_result_contexts.names(self.store, *self.attempt(1))[1]
        self.path(context).unlink()
        self.path(context).mkdir()
        results = [self.pass_(limit=1) for _ in range(4)]
        self.assertIn('history_unsafe_path', results)
        self.assertEqual(['repair_required', 'ended', 'ended'], [self.context_state(n) for n in (1, 2, 3)])

    def retrying_then_directory(self, retries):
        """`retries` contexts that keep failing to unlink (earlier rows), then one directory repair (a later row)."""
        numbers = list(range(1, retries + 1))
        self.committed(*numbers, retries + 1)
        later = retries + 1
        directory = self.path(self.names(later)[0][1])
        directory.unlink()
        directory.mkdir()
        self.now = max(self.due(n) for n in numbers + [later])
        failing = set()
        for number in numbers:
            body, context = self.names(number)
            failing |= {body[1], context[1]}
        with self.failing_unlink(PermissionError('CANARY-persistent'), only=failing):
            self.assertIn(self.pass_(), ('history_unavailable', 'history_unsafe_path'))
        return numbers, later

    def test_R2_earlier_persistent_retries_do_not_hide_a_later_directory_repair_in_the_bounded_report(self):
        numbers, later = self.retrying_then_directory(4)
        self.assertEqual('repair_required', self.context_state(later))
        report = self.store.result_retirement_repairs(3)
        self.assertEqual(3, len(report))
        self.assertEqual((self.attempt(later)[0], 'directory'), (report[0]['run_handle'], report[0]['reason']))
        self.assertEqual(['retrying', 'retrying'], [row['reason'] for row in report[1:]])

    def test_R2_a_truncated_report_says_so_and_its_continuation_reaches_every_remaining_entry(self):
        numbers, later = self.retrying_then_directory(4)
        seen, cursor, pages = [], None, 0
        while True:
            report = self.store.result_retirement_repairs(2, cursor)
            pages += 1
            self.assertLessEqual(len(report), 2)
            seen += [(row['run_handle'], row['reason']) for row in report]
            if not report.truncated:
                self.assertIsNone(report.cursor)
                break
            self.assertIsNotNone(report.cursor)
            cursor = report.cursor
            self.assertLess(pages, 10)
        expected = {(self.attempt(n)[0], 'retrying') for n in numbers} | {(self.attempt(later)[0], 'directory')}
        self.assertEqual(expected, set(seen))
        self.assertEqual(len(expected), len(seen))  # none twice
        self.assertEqual((self.attempt(later)[0], 'directory'), seen[0])  # the operator-needed entry comes first

    def test_R2_a_report_that_fits_the_limit_is_not_truncated(self):
        self.retrying_then_directory(2)
        report = self.store.result_retirement_repairs(5)
        self.assertEqual(3, len(report))
        self.assertFalse(report.truncated)
        self.assertIsNone(report.cursor)

    def test_R2_a_reservation_only_directory_is_listed_before_earlier_retrying_contexts(self):
        self.committed(1, 2)
        self.direct(7)
        directory = self.path(self.reservation_only_names(7)[1])
        directory.unlink()
        directory.mkdir()
        self.now = max(self.due(1), self.due(2), self.due(7))
        failing = {name for n in (1, 2) for name in (self.names(n)[0][1], self.names(n)[1][1])}
        with self.failing_unlink(PermissionError('CANARY-persistent'), only=failing):
            self.pass_()
        report = self.store.result_retirement_repairs(2)
        self.assertEqual((self.attempt(7)[0], 'directory'), (report[0]['run_handle'], report[0]['reason']))
        self.assertTrue(report.truncated)


def project_result_contexts_live(store):
    return store._connection.execute("SELECT COUNT(*) FROM project_result_contexts WHERE state NOT IN "
                                     "('ended','expired')").fetchone()[0]


LIVE_STATES = ['leased']


if __name__ == '__main__':
    unittest.main()
