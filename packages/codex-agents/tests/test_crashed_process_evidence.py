"""B5a process proof: real children/watchdog, and fail-closed journal writes; no sockets."""
import unittest
from unittest import mock

from tests import test_launch_journal as fixtures
from codex_agents import launch_journal, process_tree


class ProcessGoneTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LaunchJournalTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.journal = launch_journal.LaunchJournal(self.fixture.directory)
        self.addCleanup(self.journal.release)

    @unittest.skipUnless(process_tree.process_table() is not None, 'kernel process listing is unavailable (ps denied)')
    def test_exited_child_and_confirmed_tree_are_recorded_before_settlement(self):
        child = self.fixture.sleeper()
        watchdog = self.fixture.real_watchdog(self.journal, child)
        child.terminate()
        child.wait(5)
        watchdog.wait(5)
        self.journal.process_gone(fixtures.BRIDGE)
        entry = self.journal.entry(fixtures.BRIDGE)
        self.assertEqual('gone', entry['state'])
        self.assertEqual(child.pid, entry['child']['pid'])
        self.assertEqual(watchdog.pid, entry['watchdog']['pid'])
        self.assertEqual('gone', launch_journal.tree_state(entry))
        self.journal.process_gone(fixtures.BRIDGE)  # recorded proof is stable
        self.journal.settled(fixtures.BRIDGE, 'released')
        self.assertEqual('settled', self.journal.entry(fixtures.BRIDGE)['state'])

    def test_live_child_or_missing_watchdog_confirmation_cannot_record_gone(self):
        child, watchdog = self.fixture.sleeper(), self.fixture.sleeper()
        self.fixture.watched(self.journal, child, watchdog)
        with self.assertRaises(launch_journal.JournalError):
            self.journal.process_gone(fixtures.BRIDGE)
        child.terminate()
        watchdog.terminate()
        child.wait(5)
        watchdog.wait(5)
        with self.assertRaises(launch_journal.JournalError):
            self.journal.process_gone(fixtures.BRIDGE)
        self.assertEqual('watched', self.journal.entry(fixtures.BRIDGE)['state'])

    def test_alive_unknown_and_failed_durable_write_leave_the_proof_unrecorded(self):
        child, watchdog = self.fixture.sleeper(), self.fixture.sleeper()
        self.fixture.watched(self.journal, child, watchdog)
        for state in ('alive', 'unknown'):
            with self.subTest(state=state), mock.patch.object(launch_journal, 'tree_state', return_value=state):
                with self.assertRaises(launch_journal.JournalError):
                    self.journal.process_gone(fixtures.BRIDGE)
            self.assertEqual('watched', self.journal.entry(fixtures.BRIDGE)['state'])
        with mock.patch.object(launch_journal, 'tree_state', return_value='gone'), \
                mock.patch.object(self.journal, '_write', side_effect=launch_journal.JournalError('disk refused')):
            with self.assertRaises(launch_journal.JournalError):
                self.journal.process_gone(fixtures.BRIDGE)
        self.assertEqual('watched', self.journal.entry(fixtures.BRIDGE)['state'])

    def test_never_spawned_claim_is_lost_rather_than_an_exited_child(self):
        fixtures.claimed(self.journal)
        with self.assertRaises(launch_journal.JournalError):
            self.journal.process_gone(fixtures.BRIDGE)
        self.assertEqual('claimed', self.journal.entry(fixtures.BRIDGE)['state'])

    def test_positive_kernel_checks_record_gone_but_one_failed_check_never_does(self):
        child, watchdog = self.fixture.sleeper(), self.fixture.sleeper()
        self.fixture.watched(self.journal, child, watchdog)
        for answers in ((None,), (False,), (True, None), (True, False), (True, True)):
            with self.subTest(answers=answers), \
                    mock.patch.object(launch_journal, 'process_record', return_value=None), \
                    mock.patch.object(launch_journal, '_identity_gone', return_value=True), \
                    mock.patch.object(launch_journal, '_kernel_empty', side_effect=answers) as checked, \
                    mock.patch.object(launch_journal, '_confirmed', return_value=True):
                if answers == (True, True):
                    self.journal.process_gone(fixtures.BRIDGE)
                    self.assertEqual('gone', self.journal.entry(fixtures.BRIDGE)['state'])
                    self.assertEqual(2, checked.call_count)
                else:
                    with self.assertRaises(launch_journal.JournalError):
                        self.journal.process_gone(fixtures.BRIDGE)
                    self.assertEqual('watched', self.journal.entry(fixtures.BRIDGE)['state'])


if __name__ == '__main__':
    unittest.main()
