"""E1 (RS-11, decision 90.11 A): the collector's login-start entry, installed by setup with consent.

Nothing here touches the real home or runs launchctl or systemctl: every path is a temporary
folder and every command goes to an injected runner.
"""

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
    """Every path under root with its kind, mode and link target: a change anywhere shows."""
    found = {}
    for folder, names, files in os.walk(root):
        for name in names + files:
            path = Path(folder, name)
            info = os.lstat(path)
            found[str(path.relative_to(root))] = (info.st_mode, os.readlink(path) if path.is_symlink() else None,
                                                  info.st_size if path.is_file() and not path.is_symlink() else 0)
    return found


class Runner:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = fail

    def __call__(self, argv):
        self.calls.append(list(argv))
        return 1 if any(word in argv for word in self.fail) else 0


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
                 ("launchd", ARGV), ("launchd", ARGV[:-1] + ["/srv/x\ny"])]
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
        self.assertIn("append:/srv/logs%%x$$y/", text)
        self.assertEqual(1, len([line for line in text.splitlines() if line.startswith("ExecStart=")]))
        for bad in ("/srv/a\nExecStartPre=/bin/x", "/srv/a\rb", "/srv/a\x00b"):
            self.assertFalse(login_start.install(self.home, "systemd", ARGV[:-1] + [bad], self.logs, self.runner,
                                                 uid=1).ok)
            self.assertFalse(login_start.install(self.home, "systemd", ARGV, bad, self.runner, uid=1).ok)
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
        changed = ARGV[:-1] + ["/srv/example/.local/state/other"]
        self.assertTrue(login_start.install(self.home, "systemd", changed, self.logs, self.runner, uid=1).ok)
        names = [c[2] for c in self.runner.calls]
        self.assertEqual(["daemon-reload", "enable", "try-restart"], names)
        self.assertEqual("cocolo-collector.service", self.runner.calls[-1][-1])


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
        (self.home / "missing-folder" / "answer").chmod(0o600)
        (self.home / "missing-folder").chmod(0o500)
        try:
            self.assertEqual("not_remembered", self.offer([1], configure=True))
            self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        finally:
            (self.home / "missing-folder").chmod(0o700)

    @requirements('E1-4', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_4_a_cancelled_question_changes_and_remembers_nothing(self):
        before = tree(self.home)
        self.assertEqual("not_asked", self.offer([None]))
        self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertEqual("installed", self.offer([0]))
        self.runner.calls.clear()
        before = tree(self.home)
        self.assertEqual("not_asked", self.offer([None], configure=True))
        self.assertEqual(before, tree(self.home))
        self.assertEqual([], self.runner.calls)
        self.assertEqual("yes", login_start.read_answer(self.answer))

    @requirements('E1-6', why_not_scenario='Consent flow driven by an injected question.')
    def test_E1_6_a_missing_collector_command_is_checked_before_the_owner_is_asked(self):
        self.shown, self.asked = [], []
        outcome = login_start.offer(home=self.home, kind="launchd", answer_file=self.answer, argv=[],
                                    log_dir=self.logs, runner=self.runner, uid=501,
                                    ask=lambda items: self.asked.append(items) or 0, show=self.shown.append,
                                    configure=False)
        self.assertEqual("no_command", outcome)
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
        self.assertTrue(login_start.entry_path(self.home, "launchd").exists())

    @requirements('E1-7', why_not_scenario='Runs the wizard step against a stand-in terminal and an injected runner.')
    def test_E1_7_configure_walk_through_offers_keep_or_remove_and_the_first_time_asks_yes_or_not_now(self):
        wizard = self.wizard([1], straight=False)  # Configure step by step, first time, not now
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("yes"))
        self.assertEqual("no", login_start.read_answer(self.projects.parent / "login-start"))
        wizard = self.wizard([0])  # Configure again: the answer is no, so Turn it on / Leave it off
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("turn it on"))
        wizard = self.wizard([1])  # Configure again: on, so Keep / Remove
        wizard.login_start_step()
        self.assertTrue(self.screen.menus[0][0].lower().startswith("keep"))
        self.assertTrue(self.screen.menus[0][1].lower().startswith("remove"))
        self.assertFalse(login_start.entry_path(self.home, "launchd").exists())
        self.assertEqual("no", login_start.read_answer(self.projects.parent / "login-start"))

    @requirements('E1-7', why_not_scenario='Reads the wizard source for where the step sits; behaviour is run above.')
    def test_E1_7_setup_offers_login_start_after_the_collector_is_ready(self):
        source = (ROOT / "scripts" / "setup_wizard" / "flow.py").read_text()
        start = source.index("def step_collector")
        body = source[start:source.index("# Step 6", start)]
        self.assertLess(body.index('"Collector status: ready'), body.index("self.login_start_step()"))
        self.assertNotIn("subprocess", (ROOT / "scripts" / "setup_wizard" / "login_start.py").read_text())


if __name__ == "__main__":
    unittest.main()
