"""Real over-limit directory/session populations at reduced scan ceilings."""
import json
import os
import re
import unittest
from unittest import mock

from test_framework import requirements
from tests.test_session_guard import GuardTestCase
from codex_agents import session_guard as guard


@requirements('S15-O0C-BINDINGS', why_not_scenario='Real temporary directories and rollout files exceed reduced scan limits; no process or socket.')
class SessionCapTests(GuardTestCase):
    def test_folder_entry_scan_stops_with_more_real_entries_than_the_limit(self):
        for name in ('1', '2', '3'):
            (self.base / name).touch()
        descriptor = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(guard, 'MAX_FOLDER_ENTRIES', 2):
                # Include fixture folders: every directory entry consumes work,
                # even when it would not match the requested pattern.
                self.assertEqual(2, len(guard._names(descriptor, re.compile('.*'))))
            self.assertGreater(len(guard._names(descriptor, re.compile('.*'))), 2)
        finally:
            os.close(descriptor)

    def test_day_scan_stops_at_two_of_three_real_days_and_can_scan_again(self):
        for day in ('01', '02', '03'):
            self.sessions.write(self.home, day=('2026', '11', day))
        descriptor = guard._sessions_folder(self.home / '.codex' / 'sessions')
        try:
            with mock.patch.object(guard, 'MAX_DAY_FOLDERS', 2):
                self.assertEqual(['2026-11-03', '2026-11-02'], [day for day, _ in guard._day_folders(descriptor)])
            self.assertEqual(3, len(list(guard._day_folders(descriptor))))
        finally:
            os.close(descriptor)

    def test_setup_project_scan_stops_at_two_of_three_real_bindings(self):
        for number in range(3):
            path = self.home / '.codex' / guard.RUNTIME_CONTAINER / 'abc' / f'{number:064x}'
            path = path.joinpath(*guard.BINDING_PARTS)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'project_root': str(self.project / str(number))}))
        with mock.patch.object(guard, 'MAX_SETUP_PROJECTS', 2):
            self.assertEqual(2, len(guard.bound_projects(self.home / '.codex')))
        self.assertEqual(3, len(guard.bound_projects(self.home / '.codex')))

    def test_recent_session_scan_reads_only_two_of_three_real_rollouts(self):
        for day in ('01', '02', '03'):
            self.sessions.write(self.home, day=('2026', '11', day))
        read = guard._read_session
        with mock.patch.object(guard, 'MAX_SCANNED_RECORDS', 2), mock.patch.object(guard, '_read_session', wraps=read) as observed:
            self.assertEqual(2, len(guard.scan_recent_sessions(self.clone, self.home / '.codex', self.home)))
            self.assertEqual(2, observed.call_count)
        self.assertEqual(3, len(guard.scan_recent_sessions(self.clone, self.home / '.codex', self.home)))


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromTestCase(SessionCapTests)
