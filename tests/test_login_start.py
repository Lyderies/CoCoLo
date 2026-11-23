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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from test_framework import requirements  # noqa: E402

from setup_wizard import login_start  # noqa: E402

PYTHON = "/opt/homebrew/bin/python3"
CLI = "/srv/example/project/.codex/orchestration/cli.py"
STATE = "/srv/example/.local/state/codex-agents"
ARGV = [PYTHON, "-I", "-B", "-S", "-X", "pycache_prefix=/dev/null", CLI, "learning-serve", "--state-root", STATE]


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
        self.assertTrue(all(item.startswith("/") for item in (data["ProgramArguments"][0], CLI)))
        self.assertIs(True, data["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, data["KeepAlive"])
        self.assertGreaterEqual(data["ThrottleInterval"], 10)
        self.assertTrue(data["StandardOutPath"].startswith(str(self.logs) + "/"))
        self.assertTrue(data["StandardErrorPath"].startswith(str(self.logs) + "/"))
        self.assertEqual(set(), set(data.get("EnvironmentVariables", {})) - {"PATH"})
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
        bad = ["python3"] + ARGV[1:]
        self.assertFalse(login_start.install(self.home, "launchd", bad, self.logs, self.runner, uid=501).ok)
        relative = ARGV[:6] + ["cli.py"] + ARGV[7:]
        self.assertFalse(login_start.install(self.home, "launchd", relative, self.logs, self.runner, uid=501).ok)
        self.assertFalse(login_start.install(self.home, None, ARGV, self.logs, self.runner, uid=501).ok)
        folder = self.home / "Library" / "LaunchAgents"
        folder.mkdir(parents=True)
        (folder / "com.cocolo.collector.plist").symlink_to(self.home / "elsewhere")
        result = login_start.install(self.home, "launchd", ARGV, self.logs, self.runner, uid=501)
        self.assertFalse(result.ok)
        self.assertFalse((self.home / "elsewhere").exists())
        self.assertEqual([], self.runner.calls)
        self.assertTrue(result.reason)


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


class WiringTests(unittest.TestCase):
    @requirements('E1-7', why_not_scenario='Reads the wizard source for the wiring; the consent flow itself is tested above.')
    def test_E1_7_setup_offers_login_start_after_the_collector_is_ready(self):
        source = (ROOT / "scripts" / "setup_wizard" / "flow.py").read_text()
        start = source.index("def step_collector")
        body = source[start:source.index("# Step 6", start)]
        self.assertIn("self.login_start_step()", body)
        self.assertLess(body.index('"Collector status: ready'), body.index("self.login_start_step()"))
        self.assertIn("def login_start_step", source)
        self.assertNotIn("subprocess", (ROOT / "scripts" / "setup_wizard" / "login_start.py").read_text())
        self.assertIn('"Update this project", "Update the install and walk through every step"', source)


if __name__ == "__main__":
    unittest.main()
