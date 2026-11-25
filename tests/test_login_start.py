"""E1 (RS-11, decision 90.11 A): the collector's login-start entry, installed by setup with consent.

Nothing here touches the real home or runs launchctl or systemctl: every path is a temporary
folder and every command goes to an injected runner.
"""

import hashlib
import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ORCHESTRATION))
sys.path.insert(2, str(ROOT / "packages" / "claude-agents" / "src"))

from test_framework import requirements  # noqa: E402

from setup_wizard import login_start  # noqa: E402

PYTHON = "/opt/homebrew/bin/python3"
CLI = "/srv/example/project/.codex/orchestration/cli.py"
STATE = "/srv/example/.local/state/codex-agents"
ARGV = [PYTHON, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", CLI, "learning-serve", "--state-root", STATE]


def tree(root):
    """Every path under root with its kind, mode, link target and a hash of its contents, and the root's own
    mode: a change anywhere shows, a same-size content change too."""
    found = {".": (os.lstat(root).st_mode,)}
    for folder, names, files in os.walk(root):
        for name in names + files:
            path = Path(folder, name)
            info = os.lstat(path)
            body = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None
            found[str(path.relative_to(root))] = (info.st_mode, os.readlink(path) if path.is_symlink() else None, body)
    return found


class Runner:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 1 if any(word in argv for word in self.fail) else 0


class FakeSystemd:
    """A stand-in systemctl --user that tracks whether the unit is running. `fail` names commands that fail;
    a failed enable or try-restart leaves the unit stopped, as a unit that would not start does."""

    def __init__(self, active=False, fail=(), fail_rollback=()):
        self.active, self.fail, self.fail_rollback = active, fail, fail_rollback
        self.calls, self.rollback = [], False

    def __call__(self, argv):
        self.calls.append(list(argv))
        verb = argv[2] if len(argv) > 2 and argv[1] == "--user" else argv[1]
        if verb == "is-active":
            return 0 if self.active else 3
        if verb in self.fail and not self.rollback:
            self.active = False if verb in ("enable", "try-restart") else self.active
            self.rollback = True  # from here on, the system is being put back
            return 1
        if verb in self.fail_rollback and self.rollback:
            return 1
        if verb in ("enable", "start", "restart"):
            self.active = True
        return 0  # try-restart restarts only a running unit: it never starts a stopped one


class TempHome(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(os.path.realpath(directory.name))
        self.logs = self.home / ".local" / "state" / "cocolo-logs"
        self.answer = self.home / "answer"
        self.runner = Runner()


class InstallTests(TempHome):
    @requirements('E1-2', why_not_scenario='Pure file and command rendering with an injected runner; no process is started.')
    def test_E1_2_launchd_plist_and_load(self):
        result = login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501)
        self.assertTrue(result.ok, result)
        path = self.home / "Library" / "LaunchAgents" / "com.cocolo.collector.plist"
        data = plistlib.loads(path.read_bytes())
        self.assertEqual("com.cocolo.collector", data["Label"])
        self.assertEqual(ARGV, data["ProgramArguments"])
        self.assertTrue(data["ProgramArguments"][0].startswith("/"))
        self.assertIs(True, data["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, data["KeepAlive"])
        self.assertGreaterEqual(data["ThrottleInterval"], 10)
        self.assertTrue(data["StandardOutPath"].startswith(str(self.logs) + "/"))
        self.assertTrue(data["StandardErrorPath"].startswith(str(self.logs) + "/"))
        self.assertEqual({"PATH": login_start.DEFAULT_PATH}, data["EnvironmentVariables"])
        self.assertEqual(0o700, self.logs.stat().st_mode & 0o777)
        self.assertEqual("launchctl", Path(self.runner.calls[-1][0]).name)
        self.assertIn("bootstrap", self.runner.calls[-1])
        self.assertIn("gui/501", self.runner.calls[-1])
        self.assertEqual(str(path), self.runner.calls[-1][-1])

    @requirements('E1-3', why_not_scenario='Pure file and command rendering with an injected runner; no process is started.')
    def test_E1_3_systemd_unit_and_enable(self):
        result = login_start.install(self.home, "systemd", ARGV, self.logs, self.runner, uid=1000)
        self.assertTrue(result.ok, result)
        path = self.home / ".config" / "systemd" / "user" / "cocolo-collector.service"
        text = path.read_text()
        self.assertIn("ExecStart=" + " ".join(ARGV), text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartSec=", text)
        self.assertIn("StartLimitBurst=", text)
        self.assertIn("StartLimitIntervalSec=", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("append:" + str(self.logs) + "/", text)
        self.assertNotIn("Environment=", text)
        self.assertEqual(1, len([c for c in self.runner.calls if "daemon-reload" in c]))
        enable = [c for c in self.runner.calls if "enable" in c][0]
        self.assertEqual(["--user", "enable", "--now", "cocolo-collector.service"], enable[-4:])

    @requirements('E1-6', why_not_scenario='Refusals of pure inputs; nothing runs.')
    def test_E1_6_refusals_change_nothing(self):
        folder = self.home / "Library" / "LaunchAgents"
        folder.mkdir(parents=True)
        (folder / "com.cocolo.collector.plist").symlink_to(self.home / "elsewhere")
        relative = ARGV[:6] + ["cli.py"] + ARGV[7:]
        cases = [("launchd", ["python3"] + ARGV[1:]), ("launchd", relative), (None, ARGV), ("launchd", []),
                 ("launchd", ARGV)]  # the last: the symlinked entry path made above
        for kind, argv in cases:
            before = tree(self.home)
            result = login_start.install(self.home, kind, argv, self.logs, self.runner, uid=501)
            self.assertFalse(result.ok, (kind, argv))
            self.assertTrue(result.reason)
            self.assertEqual(before, tree(self.home), (kind, argv))
            self.assertEqual([], self.runner.calls)
        self.assertFalse((self.home / "elsewhere").exists())

    @requirements('E1-6', why_not_scenario='Refusals of pure inputs; nothing runs.')
    def test_E1_6_a_symlinked_folder_above_the_entry_is_refused_and_nothing_changes(self):
        (self.home / "elsewhere").mkdir()
        (self.home / "Library").symlink_to(self.home / "elsewhere")
        (self.home / ".config").symlink_to(self.home / "elsewhere")
        for kind in ("launchd", "systemd"):
            before = tree(self.home)
            result = login_start.install(self.home, kind, ARGV, self.logs, self.runner, uid=501)
            self.assertFalse(result.ok, kind)
            self.assertIn("link", result.reason)
            self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertEqual([], list((self.home / "elsewhere").iterdir()))

    @requirements('E1-6', why_not_scenario='Refusals of pure inputs; nothing runs.')
    def test_E1_6_a_symlinked_log_folder_is_refused_and_not_changed(self):
        (self.home / "elsewhere").mkdir(mode=0o755)
        self.logs.parent.mkdir(parents=True)
        self.logs.symlink_to(self.home / "elsewhere")
        before = tree(self.home)
        self.assertFalse(login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501).ok)
        self.assertEqual(before, tree(self.home))
        self.assertEqual(0o755, (self.home / "elsewhere").stat().st_mode & 0o777)

    @requirements('E1-3', why_not_scenario='Pure file rendering.')
    def test_E1_3_dollar_percent_and_newline_cannot_change_the_unit(self):
        argv = ARGV[:-1] + ["/srv/a$b%c"]
        text = login_start.render_unit(argv, "/srv/logs%x$y")
        self.assertIn("/srv/a$$b%%c", text)
        self.assertIn("append:/srv/logs%%x$y/", text)  # systemd expands % in a path, not $
        self.assertEqual(1, len([line for line in text.splitlines() if line.startswith("ExecStart=")]))
        for bad in ("/srv/a\nExecStartPre=/bin/x", "/srv/a\rb", "/srv/a\x00b"):
            before = tree(self.home)
            self.assertFalse(login_start.install(self.home, "systemd", ARGV[:-1] + [bad], self.logs, self.runner,
                                                 uid=1).ok)
            self.assertFalse(login_start.install(self.home, "systemd", ARGV, bad, self.runner, uid=1).ok)
            self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertFalse(login_start.entry_path(self.home, "systemd").exists())

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with an injected runner.')
    def test_E1_1_a_failed_reinstall_keeps_the_working_entry_launchd(self):
        self.assertTrue(login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501).ok)
        path = login_start.entry_path(self.home, "launchd")
        old = path.read_bytes()
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        failing = Runner(fail=("bootstrap",))
        # Fail only the first bootstrap (the new entry); the restore of the old one may succeed.
        first = {"n": 0}

        def run(argv):
            failing.calls.append(list(argv))
            if "bootstrap" in argv:
                first["n"] += 1
                return 1 if first["n"] == 1 else 0
            return 0

        result = login_start.install(self.home, "launchd", changed, self.logs, run, uid=501)
        self.assertFalse(result.ok)
        self.assertEqual(old, path.read_bytes())
        self.assertEqual(2, first["n"])  # the old entry was loaded again
        self.assertEqual([], [p.name for p in path.parent.iterdir() if p.name != path.name])

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with an injected runner.')
    def test_E1_1_a_failed_reinstall_keeps_the_working_entry_systemd(self):
        self.assertTrue(login_start.install(self.home, "systemd", ARGV, self.logs, self.runner, uid=1).ok)
        path = login_start.entry_path(self.home, "systemd")
        old = path.read_bytes()
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        result = login_start.install(self.home, "systemd", changed, self.logs, Runner(fail=("enable",)), uid=1)
        self.assertFalse(result.ok)
        self.assertEqual(old, path.read_bytes())

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with an injected runner.')
    def test_E1_1_a_first_failed_install_leaves_no_entry(self):
        self.assertFalse(login_start.install(self.home, "launchd", ARGV, self.logs, Runner(fail=("bootstrap",)),
                                             uid=501).ok)
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())

    @requirements('E1-3', why_not_scenario='Pure file and command rendering with an injected runner.')
    def test_E1_3_a_changed_unit_is_restarted_after_enable_and_an_unchanged_or_new_one_is_not(self):
        login_start.install(self.home, "systemd", ARGV, self.logs, self.runner, uid=1)
        self.assertFalse([c for c in self.runner.calls if "try-restart" in c or "restart" in c])
        self.runner.calls.clear()
        self.assertTrue(login_start.install(self.home, "systemd", ARGV, self.logs, self.runner, uid=1).ok)  # unchanged
        self.assertFalse([c for c in self.runner.calls if "try-restart" in c or "restart" in c])
        self.runner.calls.clear()
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        self.assertTrue(login_start.install(self.home, "systemd", changed, self.logs, self.runner, uid=1).ok)
        names = [c[2] for c in self.runner.calls]
        self.assertEqual(["is-active", "daemon-reload", "enable", "try-restart"], names)
        self.assertEqual("cocolo-collector.service", self.runner.calls[-1][-1])

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with a stateful stand-in systemctl.')
    def test_E1_1_a_failed_reinstall_leaves_a_running_service_running(self):
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        for failing in ("enable", "try-restart"):
            with self.subTest(failing=failing):
                home = Path(tempfile.mkdtemp(dir=self.home))
                logs = home / "logs"
                systemd = FakeSystemd(active=False)
                self.assertTrue(login_start.install(home, "systemd", ARGV, logs, systemd, uid=1).ok)
                self.assertTrue(systemd.active)
                path = login_start.entry_path(home, "systemd")
                old = path.read_bytes()
                systemd.fail, systemd.rollback = (failing,), False
                result = login_start.install(home, "systemd", changed, logs, systemd, uid=1)
                self.assertFalse(result.ok)
                self.assertEqual(old, path.read_bytes())
                self.assertTrue(systemd.active, "the service that was running must be running again")

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with a stateful stand-in systemctl.')
    def test_E1_1_a_failed_reinstall_does_not_start_a_service_that_was_stopped(self):
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        systemd = FakeSystemd(active=False)
        login_start.install(self.home, "systemd", ARGV, self.logs, systemd, uid=1)
        systemd.active = False  # stopped by the owner
        systemd.fail, systemd.rollback = ("enable",), False
        self.assertFalse(login_start.install(self.home, "systemd", changed, self.logs, systemd, uid=1).ok)
        self.assertFalse(systemd.active)
        self.assertFalse([c for c in systemd.calls if c[2] in ("start", "restart")])

    @requirements('E1-9', why_not_scenario='Pure file and command rendering with a stateful stand-in systemctl.')
    def test_E1_9_a_failed_refresh_puts_a_stopped_systemd_unit_back_stopped_and_not_enabled(self):
        class Stateful(FakeSystemd):
            enabled = False

            def __call__(self, argv):
                verb = argv[2]
                if verb == "is-enabled":
                    self.calls.append(list(argv))
                    return 0 if self.enabled else 1
                if verb == "stop":
                    self.calls.append(list(argv))
                    self.active = False
                    return 0
                if verb == "disable":
                    self.calls.append(list(argv))
                    self.enabled = False
                    return 0
                if verb == "enable":
                    self.enabled = True
                if verb == "try-restart" and "try-restart" in self.fail and not self.rollback:
                    self.calls.append(list(argv))
                    self.rollback = True
                    return 1  # the unit enable --now started stays running
                return super().__call__(argv)

        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        systemd = Stateful(active=False)
        login_start.install(self.home, "systemd", ARGV, self.logs, systemd, uid=1)
        systemd.active, systemd.enabled = False, False  # stopped and not enabled by the owner
        path = login_start.entry_path(self.home, "systemd")
        old = path.read_bytes()
        systemd.fail, systemd.rollback = ("try-restart",), False
        self.assertFalse(login_start.install(self.home, "systemd", changed, self.logs, systemd, uid=1).ok)
        self.assertEqual(old, path.read_bytes())
        self.assertFalse(systemd.active)
        self.assertFalse(systemd.enabled)

    @requirements('E1-9', why_not_scenario='Pure file and command rendering.')
    def test_E1_9_a_refresh_does_not_recreate_an_entry_that_vanished(self):
        login_start.install(self.home, "systemd", ARGV, self.logs, FakeSystemd(), uid=1)
        path = login_start.entry_path(self.home, "systemd")
        path.unlink()
        runner = Runner()
        result = login_start._install(self.home, "systemd", ARGV[:-1] + ["/x"], self.logs, runner, uid=1,
                                      refreshing=True)[0]
        self.assertFalse(result.ok)
        self.assertFalse(path.exists())
        self.assertEqual([], runner.calls)

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with a stateful stand-in systemctl.')
    def test_E1_1_a_failed_rollback_is_reported(self):
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        systemd = FakeSystemd(active=False)
        login_start.install(self.home, "systemd", ARGV, self.logs, systemd, uid=1)
        systemd.fail, systemd.fail_rollback, systemd.rollback = ("enable",), ("restart", "start"), False
        result = login_start.install(self.home, "systemd", changed, self.logs, systemd, uid=1)
        self.assertFalse(result.ok)
        self.assertNotIn("earlier one was kept", result.reason)
        self.assertIn("could not be started again", result.reason)

    @requirements('E1-1', why_not_scenario='Pure file and command rendering with an injected runner.')
    def test_E1_1_a_failed_reinstall_does_not_load_an_old_entry_that_was_not_loaded(self):
        self.assertTrue(login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501).ok)
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        calls = []

        def run(argv):
            calls.append(list(argv))
            return 1 if "print" in argv or "bootstrap" in argv else 0  # not loaded before; the new one fails

        result = login_start.install(self.home, "launchd", changed, self.logs, run, uid=501)
        self.assertFalse(result.ok)
        self.assertEqual(1, len([c for c in calls if "bootstrap" in c]))  # only the new entry's try

    @requirements('E1-6', why_not_scenario='Refusals of pure inputs; nothing runs.')
    def test_E1_6_an_unreadable_old_entry_is_refused_not_replaced(self):
        self.assertTrue(login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501).ok)
        path = login_start.entry_path(self.home, "launchd")
        before, calls = tree(self.home), len(self.runner.calls)
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        real = os.open

        def unreadable(name, flags, *args, **kwargs):  # the entry is opened by name below its checked folder
            if name == path.name and kwargs.get("dir_fd") is not None:
                raise PermissionError(13, "Permission denied")
            return real(name, flags, *args, **kwargs)

        with mock.patch.object(login_start.os, "open", unreadable):
            result = login_start.install(self.home, "launchd", changed, self.logs, self.runner, uid=501)
        self.assertFalse(result.ok)
        self.assertIn("could not be read", result.reason)
        self.assertEqual(before, tree(self.home))
        self.assertEqual(calls, len(self.runner.calls))


class RemoveTests(TempHome):
    @requirements('E1-5', why_not_scenario='Pure file removal with an injected runner.')
    def test_E1_5_remove_unloads_and_deletes(self):
        login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501)
        path = login_start.entry_path(self.home, "launchd")
        self.assertTrue(login_start.is_installed(self.home, "launchd"))
        self.runner.calls.clear()
        self.assertTrue(login_start.remove(self.home, "launchd", self.runner, uid=501).ok)
        self.assertFalse(path.exists())
        self.assertFalse(login_start.is_installed(self.home, "launchd"))
        self.assertIn("bootout", self.runner.calls[0])
        login_start.install(self.home, "systemd", ARGV, self.logs, self.runner, uid=501)
        self.runner.calls.clear()
        self.assertTrue(login_start.remove(self.home, "systemd", self.runner, uid=501).ok)
        self.assertFalse(login_start.entry_path(self.home, "systemd").exists())
        self.assertTrue(any("disable" in call and "--now" in call for call in self.runner.calls))

    @requirements('E1-5', why_not_scenario='Pure file removal with an injected runner.')
    def test_E1_5_remove_tolerates_missing_file_and_failed_unload(self):
        self.assertTrue(login_start.remove(self.home, "launchd", self.runner, uid=501).ok)
        login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501)
        failing = Runner(fail=("bootout",))
        self.assertTrue(login_start.remove(self.home, "launchd", failing, uid=501).ok)
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())


class AnswerTests(TempHome):
    @requirements('E1-4', why_not_scenario='Pure file read and write in a temporary folder.')
    def test_E1_4_answer_round_trip_and_refusals(self):
        self.assertIsNone(login_start.read_answer(self.answer))
        login_start.write_answer(self.answer, "yes")
        self.assertEqual("yes", login_start.read_answer(self.answer))
        login_start.write_answer(self.answer, "no")
        self.assertEqual("no", login_start.read_answer(self.answer))
        self.assertEqual(0o600, self.answer.stat().st_mode & 0o777)
        self.answer.write_text("maybe\n")
        self.assertIsNone(login_start.read_answer(self.answer))
        self.answer.unlink()
        (self.home / "real").write_text("yes\n")
        self.answer.symlink_to(self.home / "real")
        self.assertIsNone(login_start.read_answer(self.answer))
        self.assertIsNone(login_start.read_answer(None))


class OfferTests(TempHome):
    def offer(self, picks, *, configure=False, kind="launchd", answer=True, asking=True):
        self.shown, self.asked = [], []
        picks = list(picks)

        def ask(items):
            self.asked.append(list(items))
            return picks.pop(0)

        return login_start.offer(home=self.home, kind=kind, answer_file=self.answer if answer else None,
                                 argv=ARGV, log_dir=self.logs, runner=self.runner, uid=501,
                                 ask=ask if asking else None, show=self.shown.append, configure=configure)

    @requirements('E1-1', why_not_scenario='Consent flow driven by an injected question; nothing is started.')
    def test_E1_1_asks_once_with_reason_pick_list_and_details_and_installs_only_on_yes(self):
        outcome = self.offer([2, 1])  # details first, then not now
        self.assertEqual("declined", outcome)
        self.assertEqual([], self.runner.calls)
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        items = self.asked[0]
        self.assertEqual(3, len(items))
        self.assertTrue(items[0].lower().startswith("yes"))
        self.assertTrue(items[1].lower().startswith("not now"))
        self.assertIn("details", items[2].lower())
        text = " ".join(self.shown).lower()
        for word in ("log in", "restart", "crash", "remove", "configure ->"):
            self.assertIn(word, text)
        self.assertGreaterEqual(len(self.asked), 2)  # asked again after the details
        self.assertEqual("no", login_start.read_answer(self.answer))
        # A fresh answer file: yes installs.
        self.answer.unlink()
        self.assertEqual("installed", self.offer([0]))
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("yes", login_start.read_answer(self.answer))

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_answered_is_not_asked_again(self):
        self.offer([1])
        self.assertEqual("answered", self.offer([]))
        login_start.write_answer(self.answer, "yes")
        self.assertEqual("answered", self.offer([]))
        self.assertEqual([], self.asked)

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_no_place_to_keep_the_answer_means_no_question(self):
        self.assertEqual("no_place", self.offer([], answer=False))
        self.assertEqual("not_asked", self.offer([], asking=False))
        self.assertEqual([], self.asked)
        self.assertEqual([], self.runner.calls)
        self.assertTrue(self.shown)

    @requirements('E1-6', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_6_unsupported_system_is_not_asked(self):
        self.assertEqual("unsupported", self.offer([], kind=None))
        self.assertEqual([], self.asked)
        self.assertEqual([], self.runner.calls)
        self.assertIsNone(login_start.read_answer(self.answer))

    @requirements('E1-5', why_not_scenario='Configure flow driven by an injected question.')
    def test_E1_5_configure_offers_remove_when_on_and_turn_on_when_off(self):
        self.assertEqual("installed", self.offer([0]))
        self.assertEqual("kept", self.offer([0], configure=True))
        self.assertTrue(self.asked[0][0].lower().startswith("keep"))
        self.assertTrue(self.asked[0][1].lower().startswith("remove"))
        self.assertEqual("removed", self.offer([1], configure=True))
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("no", login_start.read_answer(self.answer))
        self.assertEqual("left_off", self.offer([1], configure=True))
        self.assertTrue(self.asked[0][0].lower().startswith("turn it on"))
        self.assertEqual("installed", self.offer([0], configure=True))
        self.assertEqual("yes", login_start.read_answer(self.answer))

    @requirements('E1-1', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_1_a_failed_install_is_reported_and_not_remembered(self):
        self.runner = Runner(fail=("bootstrap",))
        self.assertEqual("failed", self.offer([0]))
        self.assertIsNone(login_start.read_answer(self.answer))

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_a_failed_answer_write_is_reported_not_claimed_as_success(self):
        self.answer = self.home / "missing-folder" / "answer"
        for picks, configure in (([0], False), ([1], False)):
            outcome = self.offer(picks, configure=configure)
            self.assertEqual("not_remembered", outcome)
            self.assertIn("could not keep your answer", " ".join(self.shown).lower())
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())  # the first yes did install
        (self.home / "missing-folder").mkdir()
        self.assertEqual("installed", self.offer([0]))
        with mock.patch.object(login_start, "write_answer", return_value=False):  # injected, so root cannot skip it
            self.assertEqual("not_remembered", self.offer([1], configure=True))
            self.assertFalse(login_start.entry_path(self.home, "launchd").exists())

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_a_failed_answer_write_when_keeping_is_reported_and_the_entry_stays(self):
        self.assertEqual("installed", self.offer([0]))
        with mock.patch.object(login_start, "write_answer", return_value=False):
            self.assertEqual("not_remembered", self.offer([0], configure=True))  # Keep
        self.assertIn("could not keep your answer", " ".join(self.shown).lower())
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_a_cancelled_question_changes_and_remembers_nothing(self):
        before = tree(self.home)
        self.assertEqual("cancelled", self.offer([None]))
        self.assertIn("left as it was", " ".join(self.shown))
        self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertEqual("installed", self.offer([0]))
        self.runner.calls.clear()
        before = tree(self.home)
        self.assertEqual("cancelled", self.offer([None], configure=True))
        self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertEqual("yes", login_start.read_answer(self.answer))

    @requirements('E1-6', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_6_a_missing_collector_command_is_checked_before_the_owner_is_asked(self):
        self.shown, self.asked = [], []
        before = tree(self.home)
        outcome = login_start.offer(home=self.home, kind="launchd", answer_file=self.answer, argv=[],
                                    log_dir=self.logs, runner=self.runner, uid=501,
                                    ask=lambda items: self.asked.append(items) or 0, show=self.shown.append,
                                    configure=False)
        self.assertEqual("no_command", outcome)
        self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.asked)
        self.assertEqual([], self.runner.calls)
        self.assertIsNone(login_start.read_answer(self.answer))
        self.assertTrue(self.shown)


class Screen:
    """A stand-in wizard terminal: scripted menu picks, every question and line recorded."""

    def __init__(self, picks=()):
        self.picks, self.menus, self.lines = list(picks), [], []
        self.no_pauses = False
        self.stat, self.step_log = {}, []

    def menu(self, default, items):
        self.menus.append(list(items))
        return self.picks.pop(0)

    def body(self, text): self.lines.append(text)
    def list_add(self, kind, text): self.lines.append(text)
    def __getattr__(self, name): return lambda *a, **k: None


class WiringTests(TempHome):
    """The wizard's own login_start_step, run for real against a temporary home and an injected runner."""

    def wizard(self, picks=(), *, update=False, straight=False, answers=None):
        from setup_wizard import flow
        project = self.home / "project"
        cli = project / ".codex" / "orchestration" / "cli.py"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("")
        state = self.home / ".local" / "state" / "codex-agents"
        (state / "projects").mkdir(parents=True, exist_ok=True)
        self.screen = Screen(picks)
        wizard = flow.Wizard(self.screen, start="", python="/usr/bin/python3", owner_path="/", bash_version="5",
                             update=update, answers=answers)
        wizard.home, wizard.project, wizard.straight = str(self.home), str(project), straight
        self.projects = state / "projects" / "list"
        self.patches = [mock.patch.object(wizard, "projects_file", return_value=self.projects),
                        mock.patch.object(login_start, "terminal_runner", return_value=self.runner),
                        mock.patch.object(flow.sys, "platform", "darwin")]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        return wizard

    @requirements('E1-7', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_7_set_up_asks_once_on_the_first_run_and_not_on_a_re_run(self):
        wizard = self.wizard([0], straight=True)  # Set up (recommended) on a fresh install
        wizard.login_start_step()
        self.assertEqual(1, len(self.screen.menus))
        self.assertTrue(self.screen.menus[0][0].lower().startswith("yes"))
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("yes", login_start.read_answer(self.projects.parent / "login-start"))
        wizard = self.wizard([], straight=True)  # Set up again: nothing to ask
        wizard.login_start_step()
        self.assertEqual([], self.screen.menus)

    @requirements('E1-7', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_7_an_update_never_asks_and_an_answers_file_can_answer(self):
        wizard = self.wizard([], update=True)
        wizard.login_start_step()
        self.assertEqual([], self.screen.menus)
        self.assertEqual([], self.runner.calls)
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        wizard = self.wizard([], update=True, answers={"login_start": True})
        wizard.login_start_step()
        self.assertEqual([], self.screen.menus)
        self.assertEqual([], self.runner.calls)  # E1-9: with no recorded yes an update writes nothing
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())

    @requirements('E1-7', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_7_configure_walk_through_offers_keep_or_remove_and_the_first_time_asks_yes_or_not_now(self):
        wizard = self.wizard([1], straight=False)  # Configure step by step, first time, not now
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("yes"))
        self.assertEqual("no", login_start.read_answer(self.projects.parent / "login-start"))
        wizard = self.wizard([0])  # Configure again: the answer is no, so Turn it on / Leave it off
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("turn it on"))
        wizard = self.wizard([1])  # Configure again: still no; Leave it off
        wizard.login_start_step()
        wizard = self.wizard([0])  # and turn it on
        wizard.login_start_step()
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())
        wizard = self.wizard([0])  # Configure again: on, so Keep / Remove; Keep (index 0)
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("keep"))
        self.assertTrue(self.screen.menus[0][1].lower().startswith("remove"))
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("yes", login_start.read_answer(self.projects.parent / "login-start"))
        wizard = self.wizard([1])  # Remove (index 1)
        wizard.login_start_step()
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("no", login_start.read_answer(self.projects.parent / "login-start"))

    @requirements('E1-7', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_7_a_cancelled_question_is_summarised_as_left_as_it_was(self):
        wizard = self.wizard([None])
        wizard.login_start_step()
        summary = " ".join(self.screen.lines)
        self.assertIn("Login start: left as it was", summary)
        self.assertNotIn("during an update", summary)

    @requirements('E1-7', why_not_scenario='Runs the wizard project step and the collector step against a stand-in terminal.')
    def test_E1_7_the_real_set_up_choice_makes_the_first_run_ask_once_and_a_re_run_not_ask(self):
        from setup_wizard import effects, flow
        project = self.home / "project"
        (project / ".codex" / "orchestration").mkdir(parents=True)
        wizard = self.wizard([0, 0, 0])  # project step: Set up (recommended); then the question: Yes
        self.assertFalse(wizard.straight)
        wizard.last_reply = {}  # the installer preview the stubbed run_installer would have left
        patches = [mock.patch.object(wizard, "pick_project", return_value=str(project)),
                   mock.patch.object(wizard, "basic_problem", return_value=""),
                   mock.patch.object(wizard, "managed_problem", return_value=""),
                   mock.patch.object(wizard, "prepare_project_inputs"),
                   mock.patch.object(wizard, "run_installer", return_value=0),
                   mock.patch.object(wizard, "remember_project"),
                   mock.patch.object(wizard, "owner_key_notice"),
                   mock.patch.object(effects, "plan_entries", return_value=[]),
                   mock.patch.object(effects, "plan_paths", return_value=[])]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        wizard.step_project(str(project))
        self.assertTrue(wizard.straight)  # derived by the wizard's own menu choice, not assigned here
        wizard.login_start_step()
        questions = [menu for menu in self.screen.menus if menu[0].lower().startswith("yes")]
        self.assertEqual(1, len(questions))  # asked once, with the straight (Set up) answer in place
        self.assertEqual(2, len(self.screen.menus))  # the project's Set up menu, then that one question
        self.assertEqual("yes", login_start.read_answer(self.projects.parent / "login-start"))
        # A re-run: the project now has an install; the wizard's own "Update this project" choice, then no question.
        manifest = project / ".codex" / "orchestration" / "install-manifest.json"
        manifest.write_text("{}")
        again = self.wizard([0])
        again.last_reply = {}
        for patch in (mock.patch.object(again, "pick_project", return_value=str(project)),
                      mock.patch.object(again, "basic_problem", return_value=""),
                      mock.patch.object(again, "managed_problem", return_value=""),
                      mock.patch.object(again, "update_refusal", return_value=None),  # UPDFIX-3: no collector here
                      mock.patch.object(again, "prepare_project_inputs"),
                      mock.patch.object(again, "run_installer", return_value=0),
                      mock.patch.object(again, "remember_project"),
                      mock.patch.object(again, "owner_key_notice")):
            patch.start()
            self.addCleanup(patch.stop)
        again.step_project(str(project))
        self.assertTrue(again.update)  # the real choice "Update this project" on an existing install
        again.login_start_step()
        self.assertEqual(1, len(self.screen.menus))  # only the project's own update menu; nothing about login start

    @requirements('E1-7', why_not_scenario='Reads the wizard source for where the step sits; behaviour is run above.')
    def test_E1_7_setup_offers_login_start_after_the_collector_is_ready(self):
        source = (ROOT / "scripts" / "setup_wizard" / "flow.py").read_text()
        start = source.index("def step_collector")
        body = source[start:source.index("# Step 6", start)]
        self.assertLess(body.index('"Collector status: ready'), body.index("self.login_start_step()"))
        self.assertNotIn("subprocess", (ROOT / "scripts" / "setup_wizard" / "login_start.py").read_text())


DIGEST_A, DIGEST_B = "a" * 64, "b" * 64
from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST as ATTESTED, PACKAGE_VERSION  # noqa: E402


class RuntimeHome(TempHome):
    """A temporary home with a state folder, the answer file and the authorization file the collector checks."""

    def setUp(self):
        super().setUp()
        self.state = self.home / ".local" / "state" / "codex-agents"
        (self.state / "projects").mkdir(parents=True)
        os.chmod(self.state, 0o700)  # an owner-only state folder, as the collector's own check requires
        self.answer = self.state / "projects" / "login-start"
        self.logs = login_start.log_folder(self.state)

    def authorize(self, digest, version=PACKAGE_VERSION):
        path = self.state / "collector-authorization.json"
        path.unlink(missing_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "runtime_version": version, "runtime_digest": digest}))
        os.chmod(path, 0o600)

    def argv(self, project="project"):
        return [PYTHON, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null",
                str(self.home / project / ".codex" / "orchestration" / "cli.py"), "learning-serve",
                "--state-root", str(self.state)]

    def write_entry(self, argv, digest, kind="launchd"):
        """An entry as an earlier setup left it: consent kept, entry loaded, runtime recorded."""
        self.assertTrue(login_start.install(self.home, kind, argv, self.logs, Runner(), uid=501).ok)
        self.assertTrue(login_start.write_answer(self.answer, "yes"))
        self.assertTrue(login_start.write_record(self.answer, digest))

    def status(self, argv=None, digest=DIGEST_A, kind="launchd"):
        return login_start.entry_status(self.home, kind, self.argv() if argv is None else argv, self.logs,
                                        self.answer, digest)


class StatusTests(RuntimeHome):
    @requirements('E1-8', why_not_scenario='Reads temporary files only; no process is started.')
    def test_E1_8_status_is_on_off_or_stale_and_names_no_private_value(self):
        self.assertEqual("off", self.status().state)  # no entry
        self.assertEqual("off", login_start.entry_status(self.home, None, self.argv(), self.logs, self.answer,
                                                         DIGEST_A).state)
        for kind in ("launchd", "systemd"):
            with self.subTest(kind=kind):
                self.write_entry(self.argv(), DIGEST_A, kind)
                self.assertEqual("on", self.status(kind=kind).state)
                for label, kwargs in (
                        ("the path moved", {"argv": self.argv("elsewhere")}),
                        ("the digest changed", {"digest": DIGEST_B}),
                        ("the runtime cannot be read", {"digest": None}),
                        ("no command to compare", {"argv": []})):
                    stale = self.status(kind=kind, **kwargs)
                    self.assertEqual("stale", stale.state, label)
                    self.assertTrue(stale.reason, label)
                    for private in (DIGEST_A, DIGEST_B, str(self.home), str(self.state), "elsewhere"):
                        self.assertNotIn(private, stale.reason, label)
                # an entry with no record of its runtime cannot be shown to match
                login_start.record_path(self.answer).unlink()
                self.assertEqual("stale", self.status(kind=kind).state)
                login_start.remove(self.home, kind, Runner(), uid=501)
        self.assertEqual("off", self.status().state)

    @requirements('E1-8', why_not_scenario='Reads temporary files only; no process is started.')
    def test_E1_8_the_runtime_identity_is_read_only_from_a_private_plain_authorization_file(self):
        self.assertIsNone(login_start.runtime_digest(self.state))
        self.authorize(DIGEST_A)
        self.assertEqual(DIGEST_A, login_start.runtime_digest(self.state))
        path = self.state / "collector-authorization.json"
        os.chmod(path, 0o644)
        self.assertIsNone(login_start.runtime_digest(self.state))  # not owner-only
        os.chmod(path, 0o600)
        path.write_text(json.dumps({"schema_version": 1, "runtime_version": "0.8.55", "runtime_digest": "xyz"}))
        self.assertIsNone(login_start.runtime_digest(self.state))  # not a digest
        path.write_text("{" * 5000)
        self.assertIsNone(login_start.runtime_digest(self.state))  # too large and not JSON
        path.unlink()
        other = self.home / "other.json"
        other.write_text(json.dumps({"schema_version": 1, "runtime_version": "0.8.55", "runtime_digest": DIGEST_A}))
        os.chmod(other, 0o600)
        path.symlink_to(other)
        self.assertIsNone(login_start.runtime_digest(self.state))  # a link is never followed
        self.assertIsNone(login_start.runtime_digest(None))


class UpgradeTests(RuntimeHome):
    """The wizard's own login_start_step in an update: consent already kept, the runtime changed."""

    def wizard(self, *, project="project", answers=None, picks=()):
        from setup_wizard import flow
        cli = self.home / project / ".codex" / "orchestration" / "cli.py"
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_text("")
        self.screen = Screen(picks)
        wizard = flow.Wizard(self.screen, start="", python=PYTHON, owner_path="/", bash_version="5",
                             update=True, answers=answers)
        wizard.home, wizard.project, wizard.straight = str(self.home), str(self.home / project), False
        for patch in (mock.patch.object(wizard, "projects_file", return_value=self.state / "projects" / "list"),
                      mock.patch.object(login_start, "terminal_runner", return_value=self.runner),
                      mock.patch.object(flow.sys, "platform", "darwin")):
            patch.start()
            self.addCleanup(patch.stop)
        return wizard

    def argv(self, project="project"):
        # as the wizard builds it: its trusted interpreter, isolated, then the resolved installed cli
        cli = (self.home / project / ".codex" / "orchestration" / "cli.py").resolve()
        return [PYTHON, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", str(cli), "learning-serve",
                "--state-root", str(self.state)]

    def program(self):
        path = login_start.entry_path(self.home, "launchd")
        return plistlib.loads(path.read_bytes())["ProgramArguments"]

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_an_update_that_moves_the_collector_rewrites_the_entry_without_asking(self):
        self.authorize(ATTESTED)
        self.wizard(project="old")  # the cli file the old entry points at
        self.write_entry(self.argv("old"), ATTESTED)
        self.runner.calls.clear()
        wizard = self.wizard(project="project")  # the update's project: another path, same runtime
        wizard.login_start_step()
        self.assertEqual([], self.screen.menus)  # never asked (E1-4)
        self.assertEqual(self.argv("project"), self.program())
        self.assertTrue(any("bootstrap" in call for call in self.runner.calls))
        self.assertEqual("yes", login_start.read_answer(self.answer))
        self.assertEqual("on", login_start.entry_status(self.home, "launchd", self.argv("project"), self.logs,
                                                        self.answer, ATTESTED).state)
        self.assertIn("Login start: refreshed", " ".join(self.screen.lines))

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_an_update_that_changes_the_runtime_digest_rewrites_the_entry(self):
        self.wizard()
        self.write_entry(self.argv(), DIGEST_A)
        self.authorize(ATTESTED)  # the update authorized a new runtime (this setup's attested one) at the same path
        self.runner.calls.clear()
        self.wizard().login_start_step()
        self.assertEqual([], self.screen.menus)
        self.assertTrue(any("bootstrap" in call for call in self.runner.calls))
        self.assertEqual(ATTESTED, login_start.read_record(self.answer))
        self.assertIn("Login start: refreshed", " ".join(self.screen.lines))
        self.runner.calls.clear()
        self.wizard().login_start_step()  # nothing changed since: nothing rewritten
        self.assertEqual([], self.runner.calls)
        self.assertIn("Login start: on", " ".join(self.screen.lines))

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_an_entry_that_cannot_be_rewritten_is_reported_stale_with_a_reason(self):
        self.wizard()
        self.write_entry(self.argv(), DIGEST_A)
        before = login_start.entry_path(self.home, "launchd").read_bytes()
        self.authorize(ATTESTED)
        self.runner.fail = ("bootstrap",)  # the system's login tool refuses the new entry
        self.wizard().login_start_step()
        self.assertEqual([], self.screen.menus)
        screen = " ".join(self.screen.lines)
        self.assertIn("Login start: stale", screen)
        self.assertNotIn("Login start: refreshed", screen)
        self.assertEqual(before, login_start.entry_path(self.home, "launchd").read_bytes())  # the earlier one kept
        self.assertEqual(DIGEST_A, login_start.read_record(self.answer))
        self.assertNotIn(DIGEST_A, screen)
        self.assertNotIn(ATTESTED, screen)

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_a_current_entry_a_missing_entry_and_no_consent_write_nothing(self):
        self.authorize(DIGEST_A)
        self.wizard()
        self.write_entry(self.argv(), DIGEST_A)
        self.authorize(ATTESTED)
        login_start.write_answer(self.answer, "no")  # the owner said no: an entry that is there is left alone
        self.runner.calls.clear()
        before = login_start.entry_path(self.home, "launchd").read_bytes()
        self.wizard().login_start_step()
        self.assertEqual([], self.runner.calls)
        self.assertEqual(before, login_start.entry_path(self.home, "launchd").read_bytes())
        login_start.remove(self.home, "launchd", Runner(), uid=501)
        login_start.write_answer(self.answer, "yes")  # consent but no entry (removed by hand): not recreated
        self.runner.calls.clear()
        self.wizard().login_start_step()
        self.assertEqual([], self.runner.calls)
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        self.assertIn("Login start: off", " ".join(self.screen.lines))
        self.answer.unlink()  # no answer at all: an update does not ask and writes nothing
        self.wizard().login_start_step()
        self.assertEqual([], self.runner.calls)
        self.assertEqual([], self.screen.menus)


class ReviewFixTests(RuntimeHome):
    """Review 040-rse-s6 r1: the active identity, safe reads, update consent and fixed public reasons."""

    wizard, argv = UpgradeTests.wizard, UpgradeTests.argv

    def authorize_raw(self, value):
        path = self.state / "collector-authorization.json"
        path.unlink(missing_ok=True)
        path.write_text(json.dumps(value))
        os.chmod(path, 0o600)

    def stale_entry(self):
        """An entry an earlier setup wrote for another runtime: consent yes, record DIGEST_A."""
        self.wizard()
        self.write_entry(self.argv(), DIGEST_A)
        self.runner.calls.clear()
        return login_start.entry_path(self.home, "launchd").read_bytes()

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_a_rewrite_needs_the_collectors_active_identity_for_the_attested_runtime(self):
        identity = {"schema_version": 1, "runtime_version": PACKAGE_VERSION, "runtime_digest": ATTESTED}
        for label, value in (("another runtime's digest", {**identity, "runtime_digest": DIGEST_B}),
                             ("another version", {**identity, "runtime_version": "0.0.1"}),
                             ("quiesced for an upgrade", {**identity, "quiesced_for_upgrade": True}),
                             ("another schema", {**identity, "schema_version": 2}),
                             ("an extra field", {**identity, "extra": 1})):
            with self.subTest(label):
                before = self.stale_entry()
                self.authorize_raw(value)
                self.wizard().login_start_step()
                screen = " ".join(self.screen.lines)
                self.assertEqual([], self.runner.calls, label)  # nothing the genuine CLI would refuse is written
                self.assertEqual(before, login_start.entry_path(self.home, "launchd").read_bytes(), label)
                self.assertEqual(DIGEST_A, login_start.read_record(self.answer), label)
                self.assertIn("Login start: stale", screen, label)
                self.assertNotIn("refreshed", screen, label)
        self.authorize_raw(identity)  # the active identity of this setup's attested runtime: rewritten
        self.wizard().login_start_step()
        self.assertEqual(ATTESTED, login_start.read_record(self.answer))
        self.assertIn("Login start: refreshed", " ".join(self.screen.lines))

    @requirements('E1-8', why_not_scenario='Reads temporary files only; no process is started.')
    def test_E1_8_status_never_follows_a_link_or_blocks_on_a_special_file_or_reads_another_users_entry(self):
        import threading
        path = login_start.entry_path(self.home, "launchd")

        def status():
            found = []
            reader = threading.Thread(target=lambda: found.append(self.status()), daemon=True)
            reader.start()
            reader.join(5)
            self.assertFalse(reader.is_alive(), "status blocked")
            return found[0]

        self.write_entry(self.argv(), DIGEST_A)
        self.assertEqual("on", status().state)
        copy = self.home / "copy.plist"
        copy.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(copy)  # a link to an entry with the right bytes
        self.assertEqual("stale", status().state)
        path.unlink()
        os.mkfifo(path)  # a FIFO is never opened for a blocking read
        self.assertEqual("stale", status().state)
        path.unlink()
        folder = path.parent
        folder.rename(self.home / "agents")
        (self.home / "agents" / path.name).write_bytes(copy.read_bytes())
        folder.symlink_to(self.home / "agents")  # a linked folder above the entry
        self.assertEqual("stale", status().state)
        folder.unlink()
        (self.home / "agents").rename(folder)
        self.assertEqual("on", status().state)
        os.chmod(path, 0o666)  # others can change it
        self.assertEqual("stale", status().state)
        os.chmod(path, 0o644)
        with mock.patch.object(login_start.os, "getuid", return_value=os.getuid() + 1):  # another user's file
            found = status()
        self.assertEqual("stale", found.state)
        self.assertNotIn(str(self.home), found.reason)

    @requirements('E1-6', why_not_scenario='Refusals of pure inputs; nothing runs.')
    def test_E1_6_an_entry_another_user_owns_or_others_can_change_is_not_replaced(self):
        self.assertTrue(login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501).ok)
        path = login_start.entry_path(self.home, "launchd")
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        os.chmod(path, 0o666)
        before, calls = tree(self.home), len(self.runner.calls)
        self.assertFalse(login_start.install(self.home, "launchd", changed, self.logs, self.runner, uid=501).ok)
        self.assertEqual(before, tree(self.home))
        self.assertEqual(calls, len(self.runner.calls))
        os.chmod(path, 0o644)
        before = tree(self.home)
        with mock.patch.object(login_start.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(login_start.install(self.home, "launchd", changed, self.logs, self.runner, uid=501).ok)
        self.assertEqual(before, tree(self.home))
        self.assertEqual(calls, len(self.runner.calls))

    @requirements('E1-9', why_not_scenario='Pure file reads in a temporary folder; no process is started.')
    def test_E1_9_the_runtime_record_and_the_answer_are_read_only_from_private_plain_files_of_this_user(self):
        import threading
        self.assertTrue(login_start.write_answer(self.answer, "yes"))
        self.assertTrue(login_start.write_record(self.answer, DIGEST_A))
        record = login_start.record_path(self.answer)
        for path, read, value in ((record, login_start.read_record, DIGEST_A),
                                  (self.answer, login_start.read_answer, "yes")):
            with self.subTest(path.name):
                self.assertEqual(value, read(self.answer))
                os.chmod(path, 0o644)
                self.assertIsNone(read(self.answer))  # not owner-only
                os.chmod(path, 0o600)
                with mock.patch.object(login_start.os, "getuid", return_value=os.getuid() + 1):
                    self.assertIsNone(read(self.answer))  # another user's file
                saved = path.read_text()
                path.unlink()
                os.mkfifo(path)
                found = []
                reader = threading.Thread(target=lambda: found.append(read(self.answer)), daemon=True)
                reader.start()
                reader.join(5)
                self.assertFalse(reader.is_alive(), "the read blocked on a FIFO")
                self.assertIsNone(found[0])
                path.unlink()
                path.write_text(saved)
                os.chmod(path, 0o600)
                self.assertEqual(value, read(self.answer))

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_an_update_never_creates_an_entry_from_an_answers_file(self):
        self.authorize(ATTESTED)
        for answers in ({"login_start": True}, {"login_start": False}):
            with self.subTest(answers=answers):
                self.wizard(answers=answers).login_start_step()
                self.assertEqual([], self.runner.calls)
                self.assertEqual([], self.screen.menus)
                self.assertFalse(os.path.lexists(login_start.entry_path(self.home, "launchd")))
                self.assertIsNone(login_start.read_answer(self.answer))  # no consent is recorded by an update
        login_start.write_answer(self.answer, "no")
        self.wizard(answers={"login_start": True}).login_start_step()
        self.assertEqual([], self.runner.calls)
        self.assertEqual("no", login_start.read_answer(self.answer))

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_a_failed_rewrite_shows_a_fixed_reason_with_no_path(self):
        self.stale_entry()
        self.authorize(ATTESTED)
        real_logs = self.home / "elsewhere-logs"
        real_logs.mkdir()
        if self.logs.is_dir():
            self.logs.rmdir()
        self.logs.symlink_to(real_logs)  # install refuses with a reason that names the folder
        self.wizard().login_start_step()
        screen = " ".join(self.screen.lines)
        self.assertIn("Login start: stale", screen)
        self.assertNotIn(str(self.home), screen)
        self.assertNotIn("elsewhere", screen)
        self.assertEqual(DIGEST_A, login_start.read_record(self.answer))

    @requirements('E1-9', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_9_a_failed_record_write_keeps_the_earlier_entry_and_is_not_shown_as_refreshed(self):
        self.authorize(ATTESTED)
        self.wizard(project="old")
        self.write_entry(self.argv("old"), DIGEST_A)
        before = login_start.entry_path(self.home, "launchd").read_bytes()
        self.runner.calls.clear()
        with mock.patch.object(login_start, "write_record", return_value=False):  # the record cannot be kept
            self.wizard(project="project").login_start_step()
        screen = " ".join(self.screen.lines)
        self.assertNotIn("refreshed", screen)
        self.assertIn("Login start: stale", screen)
        self.assertEqual(before, login_start.entry_path(self.home, "launchd").read_bytes())  # the earlier one is back
        self.assertEqual(DIGEST_A, login_start.read_record(self.answer))
        self.assertNotIn(str(self.home), screen)


class CommandAuthorizationTests(TempHome):
    @requirements('E1-10', why_not_scenario='Runs the installed collector entry point in process with the exec injected; no process is started.')
    def test_E1_10_the_rendered_command_passes_require_authorized_collector_runtime(self):
        import contextlib
        import io
        import cli
        from codex_agents.collector_client import COLLECTOR_RUNTIME_DIGEST, PACKAGE_VERSION
        from codex_agents.learning_store import LearningState
        state = self.home / "collector-state"
        state.mkdir(mode=0o700)
        LearningState(state)  # a real, empty, owner-only collector state
        cli_file = str(self.home / "project" / ".codex" / "orchestration" / "cli.py")
        argv = [PYTHON, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", cli_file, "learning-serve",
                "--state-root", str(state)]
        self.assertTrue(login_start.install(self.home, "launchd", argv, self.logs, self.runner, uid=501).ok)
        self.assertTrue(login_start.install(self.home, "systemd", argv, self.logs, self.runner, uid=501).ok)
        plist = plistlib.loads(login_start.entry_path(self.home, "launchd").read_bytes())["ProgramArguments"]
        unit = login_start.entry_path(self.home, "systemd").read_text()
        line = [row for row in unit.splitlines() if row.startswith("ExecStart=")][0]
        self.assertEqual(plist, line[len("ExecStart="):].split(" "))  # the same command in both entries
        arguments = plist[plist.index(cli_file) + 1:]  # what the interpreter hands cli.py
        self.assertEqual(["learning-serve", "--state-root", str(state)], arguments)
        authorization = state / "collector-authorization.json"
        for digest, starts, refusal in ((COLLECTOR_RUNTIME_DIGEST, True, ""),
                                        (DIGEST_B, False, "differs from the owner-authorized runtime"),
                                        (None, False, "is not authorized")):
            authorization.unlink(missing_ok=True)
            if digest is not None:
                authorization.write_text(json.dumps({"schema_version": 1, "runtime_version": PACKAGE_VERSION,
                                                     "runtime_digest": digest}))
                os.chmod(authorization, 0o600)
            started, out, status = [], io.StringIO(), 0

            def execv(program, args):  # the real exec never returns
                started.append(list(args))
                raise KeyboardInterrupt("collector host started")
            with mock.patch.object(os, "execv", execv), mock.patch.object(sys, "argv", ["cli.py", *arguments]), \
                    contextlib.redirect_stdout(out):
                try:
                    status = cli.main()
                except KeyboardInterrupt:
                    pass
            if starts:
                self.assertEqual(1, len(started), out.getvalue())  # past the check: the collector host is started
                self.assertEqual(["--state-root", str(state)], started[0][-2:])
            else:
                self.assertEqual([], started)
                self.assertNotEqual(0, status)
                self.assertIn(refusal, out.getvalue())


    @requirements('E1-10', why_not_scenario='Runs the rendered entry command as a real process against a temporary '
                                            'installed copy; only the refusals run, so no collector is started.')
    def test_E1_10_the_installed_command_run_as_given_is_refused_with_runtime_unauthorized(self):
        import shutil
        import subprocess
        from codex_agents.collector_contracts import CollectorAuthorizationError
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.collector_runtime import require_authorized_collector_runtime
        from codex_agents.learning_store import LearningState
        project = self.home / "project"
        shutil.copytree(ORCHESTRATION, project / ".codex" / "orchestration",
                        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        shutil.copy(ORCHESTRATION.parents[1] / "VERSION", project / "VERSION")  # as the installer lays it out
        state = self.home / "collector-state"
        state.mkdir(mode=0o700)
        LearningState(state)  # a real, empty, owner-only collector state
        python = os.path.realpath(sys.executable)
        argv = [python, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null",
                str(project / ".codex" / "orchestration" / "cli.py"), "learning-serve", "--state-root", str(state)]
        self.assertTrue(login_start.install(self.home, "launchd", argv, self.logs, self.runner, uid=501).ok)
        entry = plistlib.loads(login_start.entry_path(self.home, "launchd").read_bytes())
        command = entry["ProgramArguments"]
        authorization = state / "collector-authorization.json"
        for value, code in (({"schema_version": 1, "runtime_version": PACKAGE_VERSION, "runtime_digest": DIGEST_B},
                             "runtime_unauthorized"),
                            (None, "runtime_authorization_missing")):
            authorization.unlink(missing_ok=True)
            if value is not None:
                authorization.write_text(json.dumps(value))
                os.chmod(authorization, 0o600)
            run = subprocess.run(command, cwd=entry["WorkingDirectory"], env=entry["EnvironmentVariables"],
                                 capture_output=True, text=True, timeout=120)
            self.assertNotEqual(0, run.returncode, run.stdout + run.stderr)
            # The CLI prints the refusal's message, not its code: the message printed is the one the collector's own
            # check raises with this code for this state.
            with self.assertRaises(CollectorAuthorizationError) as raised:
                require_authorized_collector_runtime(CollectorPaths(state))
            self.assertEqual(code, raised.exception.code)
            self.assertEqual({"ready": False, "error": str(raised.exception)}, json.loads(run.stdout))


if __name__ == "__main__":
    unittest.main()
