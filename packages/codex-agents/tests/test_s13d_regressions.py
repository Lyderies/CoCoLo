"""S13d regressions without transport: trusted git and real kernel host locks."""
from tests import support  # noqa: F401
import fcntl
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(support.REPOSITORY_ROOT.parents[1] / 'scripts'))
from test_framework import requirements
from codex_agents import git_plumbing
from codex_agents.project_host_client import _host_alive
from codex_agents.service_host import ProjectServiceHost


class GitPinTests(unittest.TestCase):
    __why_not_scenario__ = 'Resolution work counts and root exclusions; no subprocess or transport.'
    def setUp(self):
        clear = getattr(git_plumbing, '_trusted_git_candidates', None)
        if clear is not None:
            clear.cache_clear()
            self.addCleanup(clear.cache_clear)

    @requirements('S13d-2')
    def test_repeated_git_resolution_validates_candidates_once(self):
        with mock.patch.object(git_plumbing, 'git_candidates', return_value=('/usr/bin/git',)) as candidates, \
                mock.patch.object(git_plumbing.sys, 'platform', 'linux'), \
                mock.patch.object(git_plumbing, 'temp_roots', return_value=set()) as roots, \
                mock.patch.object(git_plumbing, 'executable_location_problem', return_value=None) as validate:
            self.assertEqual(['/usr/bin/git'] * 36, [git_plumbing.git_executable() for _ in range(36)])
        self.assertEqual(1, candidates.call_count)
        self.assertEqual(1, roots.call_count)
        self.assertEqual(1, validate.call_count)

    @requirements('S13d-2')
    def test_cached_git_still_excludes_each_callers_root_and_its_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            alias = Path(folder) / 'alias'
            alias.symlink_to('/usr')
            with mock.patch.object(git_plumbing, 'git_candidates', return_value=('/usr/bin/git',)), \
                    mock.patch.object(git_plumbing.sys, 'platform', 'linux'), \
                    mock.patch.object(git_plumbing, 'temp_roots', return_value=set()), \
                    mock.patch.object(git_plumbing, 'executable_location_problem',
                        side_effect=lambda path, roots: 'untrusted' if any(
                            git_plumbing.within(path, Path(os.path.realpath(root))) for root in roots) else None):
                self.assertEqual('/usr/bin/git', git_plumbing.git_executable())
                self.assertIsNone(git_plumbing.git_executable(untrusted=[Path('/usr')]))
                self.assertIsNone(git_plumbing.git_executable(untrusted=iter([alias])))
                self.assertEqual('/usr/bin/git', git_plumbing.git_executable())


class GitPinRevalidationTests(unittest.TestCase):
    """S13d fix round: the per-process pin is rechecked on use and re-resolved when its identity changes."""
    __why_not_scenario__ = 'Real stand-in binaries on disk; only trust roots are patched. No subprocess or transport.'

    def setUp(self):
        git_plumbing._trusted_git_candidates.cache_clear()
        self.addCleanup(git_plumbing._trusted_git_candidates.cache_clear)
        self.temporary = tempfile.TemporaryDirectory(dir=support.TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(os.path.realpath(self.temporary.name))
        from codex_agents import host_paths
        roots = mock.patch.object(host_paths, 'TRUSTED_INSTALL_ROOTS',
                                  (*host_paths.TRUSTED_INSTALL_ROOTS, str(self.base)))
        roots.start()
        self.addCleanup(roots.stop)
        self.first, self.second = self.fake('first'), self.fake('second')
        patches = [mock.patch.object(git_plumbing, 'git_candidates', return_value=(self.link(), self.second)),
                   mock.patch.object(git_plumbing, 'temp_roots', return_value=set())]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fake(self, name):
        path = self.base / name / 'git'
        path.parent.mkdir()
        path.write_text('#!/bin/sh\nexit 0\n')
        path.chmod(0o755)
        return str(path)

    def link(self):
        # The preferred candidate is a symlink, as Homebrew's bin/git is.
        link = self.base / 'bin-git'
        if not link.is_symlink():
            link.symlink_to(self.first)
        return str(link)

    @requirements('S13d-2')
    def test_a_removed_binary_is_dropped_and_the_fallback_selected(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        os.unlink(self.first)
        self.assertEqual(self.second, git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_a_replaced_binary_is_trust_checked_again(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        replacement = Path(self.first).with_name('git.new')
        replacement.write_text('#!/bin/sh\nexit 0\n')
        replacement.chmod(0o777)  # writable by others: no longer trusted
        os.replace(replacement, self.first)
        self.assertEqual(self.second, git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_a_trusted_replacement_is_revalidated_once_then_cached_again(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        replacement = Path(self.first).with_name('git.new')
        replacement.write_text('#!/bin/sh\nexit 0\n')
        replacement.chmod(0o755)
        os.replace(replacement, self.first)
        with mock.patch.object(git_plumbing, 'executable_location_problem',
                               wraps=git_plumbing.executable_location_problem) as validate:
            self.assertEqual([self.first] * 5, [git_plumbing.git_executable() for _ in range(5)])
        self.assertGreaterEqual(validate.call_count, 1, 'a replaced binary skipped trust revalidation')
        with mock.patch.object(git_plumbing, 'executable_location_problem',
                               wraps=git_plumbing.executable_location_problem) as again:
            git_plumbing.git_executable()
        self.assertEqual(0, again.call_count)

    @requirements('S13d-2')
    def test_a_retargeted_symlink_selects_its_new_trusted_target(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        third = self.fake('third')
        link = self.base / 'bin-git'
        link.unlink()
        link.symlink_to(third)
        self.assertEqual(third, git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_a_retargeted_symlink_to_an_untrusted_target_falls_back(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        third = self.fake('third')
        os.chmod(third, 0o777)
        link = self.base / 'bin-git'
        link.unlink()
        link.symlink_to(third)
        self.assertEqual(self.second, git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_a_permission_change_is_revalidated(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        os.chmod(self.first, 0o777)
        self.assertEqual(self.second, git_plumbing.git_executable())
        os.chmod(self.first, 0o755)  # trusted again: pinned once more
        self.assertEqual(self.first, git_plumbing.git_executable())
        os.chmod(self.first, 0o644)  # no longer executable
        self.assertEqual(self.second, git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_every_candidate_gone_fails_closed(self):
        self.assertEqual(self.first, git_plumbing.git_executable())
        os.unlink(self.first)
        os.unlink(self.second)
        self.assertIsNone(git_plumbing.git_executable())

    @requirements('S13d-2')
    def test_an_unchanged_pin_is_trust_checked_once_and_stays_fast(self):
        import time
        with mock.patch.object(git_plumbing, 'executable_location_problem',
                               wraps=git_plumbing.executable_location_problem) as validate:
            began = time.perf_counter()
            self.assertEqual([self.first] * 36, [git_plumbing.git_executable() for _ in range(36)])
            elapsed = time.perf_counter() - began
        self.assertEqual(2, validate.call_count)  # each of the two candidates, once
        print(f'S13d git: 36 resolutions with per-use identity recheck {elapsed * 1000:.3f} ms', file=sys.stderr)


class HostCloseTests(unittest.TestCase):
    __why_not_scenario__ = 'Real kernel lock with event-free controlled writer drain and final settlement.'
    def host(self, directory):
        host = ProjectServiceHost.__new__(ProjectServiceHost)
        host._pid, host._thread = os.getpid(), threading.current_thread()
        host._closed = host._published = host._issued = False
        host._frontend = None
        host._runtime = mock.Mock()
        host._runtime.threads_stopped.return_value = False
        host._driver = mock.Mock()
        host._lock_fd = os.open(directory / 'host.lock', os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(host._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def release():
            if host._lock_fd is not None:
                os.close(host._lock_fd)
                host._lock_fd = None
        self.addCleanup(release)
        return host

    @requirements('S13d-3')
    def test_host_lock_stays_held_until_writer_drain_and_final_settlement(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            host = self.host(directory)
            host.close()
            self.assertIs(True, _host_alive(directory), 'stop advertised before the writer stopped')
            self.assertFalse(host.settle_lost_runs())
            self.assertIs(True, _host_alive(directory))
            host._runtime.threads_stopped.return_value = True
            def settle():
                self.assertIs(True, _host_alive(directory), 'lock released before final settlement')
                return True
            host._driver.settle_on_exit.side_effect = settle
            self.assertTrue(host.settle_lost_runs())
            self.assertIs(False, _host_alive(directory))
            host.close()

    @requirements('S13d-3')
    def test_faulted_host_keeps_lock_until_threads_stop_then_relinquishes_without_settling(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            host = self.host(directory)
            host.close()
            host.settle_lost_runs_later()
            self.assertIs(True, _host_alive(directory))
            host._driver.settle_on_exit.assert_not_called()
            host._runtime.threads_stopped.return_value = True
            host.settle_lost_runs_later()
            host._driver.settle_on_exit.assert_called_once_with(settle=False)
            self.assertIs(False, _host_alive(directory))


class HostCleanupTests(unittest.TestCase):
    __why_not_scenario__ = 'Deterministically inject the enumerated PID exit at the cleanup boundary.'
    @requirements('S13d-4')
    def test_stop_hosts_tolerates_a_pid_that_exits_after_enumeration(self):
        from tests.test_project_service_startup import ProjectServiceStartupTests
        fixture = ProjectServiceStartupTests(methodName='runTest')
        with mock.patch.object(fixture, 'hosts', side_effect=[[123456], []]), \
                mock.patch('os.kill', side_effect=ProcessLookupError) as kill:
            fixture.stop_hosts()
        kill.assert_called_once_with(123456, __import__('signal').SIGTERM)


class FailedStartupTests(unittest.TestCase):
    __why_not_scenario__ = 'Real runtime/history writer and kernel lock; listener failure is injected before socket bind.'

    @requirements('S13d-3')
    def test_failed_startup_drains_writer_before_releasing_its_host_lock(self):
        from tests.test_project_runtime import limits
        from codex_agents.client_runtime import runtime_directory
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.orchestration_api import OrchestrationLimits, OrchestrationScope
        from codex_agents.project_history import ProjectHistory
        from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES, ResultCapacity
        from codex_agents.service_host import EnrolledFrontend, ServiceHostLimits
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder).resolve()
            root = base / 'project'
            root.mkdir(mode=0o700)
            state_root = base / 's'
            state_root.mkdir(mode=0o700)
            (state_root / 'digest.key').write_bytes(b'h' * 32)
            (state_root / 'digest.key').chmod(0o600)
            state = CollectorPaths(state_root)
            (base / 'history').mkdir(mode=0o700)
            directory = runtime_directory(state, root)
            scope = OrchestrationScope('prj_' + 'b' * 32, 'primary', COLLECTOR_RUNTIME_DIGEST, 'supervisor', 1)
            closing, release, finished = threading.Event(), threading.Event(), threading.Event()
            errors = []
            original = ProjectHistory.close
            def close(history):
                closing.set()
                if not release.wait(10):
                    raise RuntimeError('test did not release writer close')
                original(history)
            def factory():
                return ProjectHistory(base / 'history', scope.project_id, result_key=b'h' * 32,
                    orchestration_limits=OrchestrationLimits(2, 32),
                    result_capacity=ResultCapacity(8, 2 * MAX_RESULT_BODY_BYTES, 1_800_000, 120_000))
            def start():
                try:
                    ProjectServiceHost(state, root, scope, factory, limits(),
                                       ServiceHostLimits(4, 500_000_000, 16, 30_000_000_000, 30_000, 64))
                except Exception as error:
                    errors.append(getattr(error, 'reason_code', type(error).__name__))
                finally:
                    finished.set()
            with mock.patch.object(EnrolledFrontend, 'listen', side_effect=OSError('injected listen failure')), \
                    mock.patch.object(ProjectHistory, 'close', close):
                thread = threading.Thread(target=start)
                thread.start()
                try:
                    self.assertTrue(closing.wait(10), 'writer did not reach close')
                    self.assertIs(True, _host_alive(directory), 'lock released while writer was closing')
                finally:
                    release.set()
                    thread.join(15)
                self.assertFalse(thread.is_alive())
            self.assertTrue(finished.is_set())
            self.assertEqual(['history_service_unavailable'], errors)
            self.assertIs(False, _host_alive(directory), 'failed constructor leaked its host lock')
