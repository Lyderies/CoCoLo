"""S5: the Python wizard's steps, tested as pure decisions (DESIGN-0.4.0-S5-wizard.md).

Each test ports today's table for its requirement, so the new wizard decides exactly as setup.sh does.
No process, socket or sleep: unit stage.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(1, str(ORCHESTRATION))

from setup_wizard import steps  # noqa: E402


class BillingCurrentTests(unittest.TestCase):
    """S4-3 (DESIGN-0.4.0-S4-straight.md revision 2): an update skips the attestation only when every
    enabled provider was observed and the status says its current account is attested."""

    ROUTING = steps.provider_routing("off", "on", "claude-sonnet-5-5", "off")
    BOTH = steps.provider_routing("on", "on", "claude-sonnet-5-5", "off")

    @staticmethod
    def status(codex="present", claude="present", **extra):
        return {"ready": True, "provider_attestations": {"codex": {"state": codex}, "claude": {"state": claude}},
                **extra}

    def test_S4_3_only_a_complete_current_answer_skips(self) -> None:
        ok = {"codex": True, "claude": True}
        cases = [
            ("every enabled provider present", self.BOTH, ok, self.status(), True),
            ("a disabled provider does not count", self.ROUTING, {"claude": True}, self.status(codex="missing"), True),
            ("one missing", self.BOTH, ok, self.status(codex="missing"), False),
            ("identity missing", self.BOTH, ok, self.status(claude="identity_missing"), False),
            ("unavailable", self.BOTH, ok, self.status(claude="unavailable"), False),
            ("an observe failed", self.BOTH, {"codex": True, "claude": False}, self.status(), False),
            ("an enabled provider not observed", self.BOTH, {"claude": True}, self.status(), False),
            ("status not ready", self.BOTH, ok, self.status(ready=False), False),
            ("status malformed", self.BOTH, ok, {"ready": True, "provider_attestations": []}, False),
            ("status entry malformed", self.BOTH, ok,
             {"ready": True, "provider_attestations": {"codex": "present", "claude": {"state": "present"}}}, False),
            ("no status", self.BOTH, ok, None, False),
            ("routing unreadable", "{", ok, self.status(), False),
            ("routing with nothing enabled", '{"version": 1, "providers": {}}', ok, self.status(), False),
        ]
        for name, routing, observed, status, expected in cases:
            with self.subTest(name):
                self.assertIs(expected, steps.billing_current(routing, observed, status))


class HomeMarkerTests(unittest.TestCase):
    """W3.1: leading home markers are expanded; everything else is left alone."""

    def test_leading_home_markers_are_expanded(self) -> None:
        home = "/Users/<you>"
        cases = {
            "~": home,
            "~/Code/app": f"{home}/Code/app",
            "~/my app": f"{home}/my app",
            "$HOME": home,
            "$HOME/Code/app": f"{home}/Code/app",
            "${HOME}/Code/app": f"{home}/Code/app",
            "  ~/Code/app  ": f"{home}/Code/app",
            "'~/Code/app'": f"{home}/Code/app",
            '"$HOME/Code/app"': f"{home}/Code/app",
            "/Code/app": "/Code/app",
            "Code/app": "Code/app",
            "~other/Code": "~other/Code",
            "/a/~/b": "/a/~/b",
            "$HOMEWORK/x": "$HOMEWORK/x",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(expected, steps.expand_home(value, home))


class RoutingChoiceTests(unittest.TestCase):
    """W4.1-W4.3: four choices make a routing the router's own validator accepts."""

    def test_four_choices_are_required(self) -> None:
        with self.assertRaises(steps.ChoiceError):
            steps.provider_routing("on", "on")

    def test_invalid_provider_choices_are_refused(self) -> None:
        for case in (
            ("off", "off", "-", "off"),
            ("on", "on", "-", "off"),
            ("on", "off", "-", "on"),
            ("on", "on", "bad model", "off"),
            ("maybe", "on", "claude-haiku-5-5", "off"),
        ):
            with self.subTest(case=case), self.assertRaises(steps.ChoiceError):
                steps.provider_routing(*case)

    def test_routing_matches_the_router_validator(self) -> None:
        from codex_agents.routing import load_policy, validate_provider_routing
        policy = load_policy(ORCHESTRATION / "config" / "routing-policy.json")
        models = "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5"
        for case in (
            ("on", "on", models, "off"),
            ("on", "on", "claude-haiku-5-5", "on"),
            ("on", "off", "-", "off"),
            ("off", "on", "claude-sonnet-5-5,claude-opus-5-5", "off"),
        ):
            with self.subTest(case=case):
                raw = steps.provider_routing(*case)
                with tempfile.TemporaryDirectory() as directory:
                    validate_provider_routing(Path(directory), raw.encode(), policy)
                config = json.loads(raw)
                self.assertEqual(case[0] == "on", config["providers"]["codex"]["enabled"])
                self.assertEqual(case[1] == "on", config["providers"]["claude"]["enabled"])
                self.assertEqual(case[3] == "on", config["codex_primary_may_bridge_to_claude"])
                if case[1] == "on":
                    self.assertEqual(case[2].split(","), config["providers"]["claude"]["models"])
                else:
                    self.assertNotIn("models", config["providers"]["claude"])

    def test_routing_is_the_same_as_setup_sh_writes(self) -> None:
        """Byte for byte what today's `setup.sh --print-provider-routing` writes (captured 2026-11-19), so
        an installed routing file does not change."""
        today = {
            ("on", "on", "claude-haiku-5-5", "on"):
                '{\n  "version": 1,\n  "providers": {\n    "codex": {"enabled": true},\n'
                '    "claude": {"enabled": true, "models": ["claude-haiku-5-5"]}\n  },\n'
                '  "codex_primary_may_bridge_to_claude": true\n}\n',
            ("on", "off", "-", "off"):
                '{\n  "version": 1,\n  "providers": {\n    "codex": {"enabled": true},\n'
                '    "claude": {"enabled": false}\n  },\n  "codex_primary_may_bridge_to_claude": false\n}\n',
        }
        for case, expected in today.items():
            with self.subTest(case=case):
                self.assertEqual(expected, steps.provider_routing(*case))



class ProfileTests(unittest.TestCase):
    """W8.1 and W4.6: the orchestrator profile, decided from its bytes (the safe read is an effect)."""

    def test_profile_compares_effective_settings_not_bytes(self) -> None:
        header = "# written by hand SECRET\n\n"
        same = (header + 'sandbox_mode = "workspace-write"  # keep\n\n'
                "[sandbox_workspace_write]\nnetwork_access = true\n")
        self.assertEqual("same", steps.profile_difference(same.encode()))
        self.assertEqual("same", steps.profile_difference((same + 'writable_roots = ["x"]\n').encode()))
        self.assertEqual("network_access", steps.profile_difference(same.replace("= true", "= false").encode()))
        self.assertEqual("sandbox_mode", steps.profile_difference(same.replace("workspace-write", "read-only").encode()))
        self.assertEqual("network_access", steps.profile_difference(b'sandbox_mode = "workspace-write"\n'))
        self.assertEqual("unreadable", steps.profile_difference(b"this is [not toml"))
        self.assertEqual("unreadable", steps.profile_difference(None))

    def test_profile_larger_than_the_read_limit_is_unreadable(self) -> None:
        good = 'sandbox_mode = "workspace-write"\n[sandbox_workspace_write]\nnetwork_access = true\n'
        padded = good + "# pad\n" * 12000 + "network_access = false\n"
        self.assertGreater(len(padded), 65536)
        self.assertEqual("unreadable", steps.profile_difference(padded.encode()))
        self.assertEqual("same", steps.profile_difference((good + "# pad\n" * 100).encode()))

    def test_network_follows_the_last_setting_in_its_own_table(self) -> None:
        secret = "# SECRET\nmodel = \"x\"\n"
        enabled = (secret + 'sandbox_mode = "workspace-write"\n\n[other]\nnetwork_access = false\n\n'
                   "[sandbox_workspace_write]  # allow network\nnetwork_access   =   true # yes\n")
        self.assertTrue(steps.profile_network_on(enabled.encode()))
        for name, profile in {
            "network off": secret + "[sandbox_workspace_write]\nnetwork_access = false\n",
            "setting in another table": secret + "[other]\nnetwork_access = true\n",
            "setting before any table": "network_access = true\n",
            "last assignment wins": "[sandbox_workspace_write]\nnetwork_access = true\nnetwork_access = false\n",
            "commented out": "[sandbox_workspace_write]\n# network_access = true\n",
        }.items():
            with self.subTest(profile=name):
                self.assertFalse(steps.profile_network_on(profile.encode()))
        self.assertFalse(steps.profile_network_on(None))

    def test_codex_hint(self) -> None:
        """W4.6: `codex -p orchestrator` only when the profile turns network on, or is ready; else why."""
        for wanted, ready, network in ((True, False, True), (False, False, True), (True, True, True), (True, True, False)):
            with self.subTest(wanted=wanted, ready=ready, network=network):
                self.assertEqual("codex -p orchestrator", steps.codex_hint(wanted, ready, network)[0])
        command, why = steps.codex_hint(True, False, False)
        self.assertEqual("codex", command)
        self.assertIn("network_access = true", why)
        self.assertIn("missing", why)
        command, why = steps.codex_hint(False, False, False)
        self.assertEqual("codex", command)
        self.assertIn("not chosen", why)


class CodexPrimaryTests(unittest.TestCase):
    """W4.5: whether a Codex session may orchestrate, from the routing file's bytes."""

    def test_codex_primary_follows_the_provider_configuration(self) -> None:
        self.assertTrue(steps.codex_primary(None))  # No file: only Codex is enabled.
        for choices, expected in (
            (("on", "off", "-", "off"), True),
            (("off", "on", "claude-opus-5-5", "on"), True),
            (("on", "on", "claude-opus-5-5", "off"), True),
            (("off", "on", "claude-opus-5-5", "off"), False),
        ):
            with self.subTest(choices=choices):
                self.assertEqual(expected, steps.codex_primary(steps.provider_routing(*choices).encode()))
        self.assertFalse(steps.codex_primary(b"{"))



class RegistrationOutcomeTests(unittest.TestCase):
    """W6.5 and W6.2: what the collector step says after learning-register, one fixed sentence each."""

    def lines(self, reply, moved=None):
        return " ".join(steps.registration_outcome(reply, moved)[1])

    def test_each_failure_says_how_far_it_got_and_never_the_misleading_sentence(self) -> None:
        for reason, said, never in (
            ("ledger_upgrade_failed", "Your records were not upgraded: they are as the earlier version left them.",
             "were upgraded, but"),
            ("registration_incomplete_after_upgrade", "Your records were upgraded, but registering this project did not",
             "as the earlier version left them"),
            ("collector_not_started_after_registration", "This project is registered, but the collector",
             "did not finish"),
            ("ledger_upgrade_unknown", "could not be read afterwards. Delete nothing", "as the earlier version left them"),
        ):
            with self.subTest(reason=reason):
                reply = {"ready": False, "reason_code": reason, "ledger_copy": "/kept/old ledger",
                         "state_moved": "/somewhere/old copy"}
                ok, lines = steps.registration_outcome(reply, None)
                text = " ".join(lines)
                self.assertFalse(ok)
                self.assertIn("Collector registration failed", text)
                self.assertIn(said, text)
                self.assertNotIn(never, text)
                self.assertIn("A copy of the records as they were before the upgrade is at /kept/old ledger", text)
                self.assertIn("stay there; the old copy is at /somewhere/old copy", text)

    def test_a_failed_upgrade_names_its_cause_without_control_characters(self) -> None:
        text = self.lines({"ready": False, "reason_code": "ledger_upgrade_failed", "cause": "disk\x1b[2J full"})
        self.assertIn("What stopped it: disk[2J full.", text)
        self.assertNotIn("\x1b", text)

    def test_success_names_the_kept_copy_and_the_move_without_control_characters(self) -> None:
        ok, lines = steps.registration_outcome({"ready": True, "ledger_copy": "/kept/old ledger"}, "/old\x7f copy")
        text = " ".join(lines)
        self.assertTrue(ok)
        self.assertIn("Upgraded your orchestrator records. A copy of them as they were is at /kept/old ledger", text)
        self.assertIn("Moved your orchestrator records once to a protected folder", text)
        self.assertIn("/old copy", text)
        self.assertNotIn("\x7f", text)

    def test_no_move_no_records_sentence(self) -> None:
        self.assertNotIn("Moved your orchestrator records", self.lines({"ready": True}))



class ProjectChoiceTests(unittest.TestCase):
    """W3.2 and W3.4: which projects are offered, how the list is kept, and what a bad path says."""

    def test_projects_set_up_before_are_offered_and_this_folder_once(self) -> None:
        installed = {"/p/first", "/p/second"}
        lines = ["/p/first", "/p/second", "/p/gone", "relative/is-skipped", "/p/bad\x1bname"]
        paths, items = steps.project_choices(lines, "/p/second", "/orchestrator",
                                             installed.__contains__, lambda path: True)
        self.assertEqual(["/p/first", "/p/second"], paths)
        self.assertEqual(["/p/first", "/p/second", "Another folder...", "Quit"], items)  # This folder listed already.
        paths, items = steps.project_choices(["/p/first"], "/p/other", "/orchestrator",
                                             installed.__contains__, lambda path: True)
        self.assertEqual(["/p/first", "/p/other"], paths)
        self.assertEqual("/p/other (this folder)", items[1])
        self.assertEqual(([], []), steps.project_choices([], "/p/other", "/orchestrator",
                                                         installed.__contains__, lambda path: True))

    def test_the_orchestrator_and_a_folder_outside_git_are_never_offered_as_this_folder(self) -> None:
        installed = {"/p/first"}
        for default, is_git in (("/orchestrator", True), ("/p/plain", False)):
            with self.subTest(default=default):
                paths, _ = steps.project_choices(["/p/first"], default, "/orchestrator",
                                                 installed.__contains__, lambda path, git=is_git: git)
                self.assertEqual(["/p/first"], paths)

    def test_a_chosen_project_goes_first_and_the_list_keeps_ten(self) -> None:
        lines = [f"/p/{n}" for n in range(12)]
        kept = steps.remembered(lines, "/p/5")
        self.assertEqual("/p/5", kept[0])
        self.assertEqual(10, len(kept))
        self.assertEqual(1, kept.count("/p/5"))
        self.assertIsNone(steps.remembered(lines, "relative"))
        self.assertIsNone(steps.remembered(lines, "/p/bad\x01"))

    def test_a_path_that_cannot_be_opened_is_reported_exactly(self) -> None:
        lines = steps.cannot_open("/Code/proj", "/Code/proj", "/Users/<you>/Code/proj")
        self.assertEqual("Cannot open: [/Code/proj]", lines[0])
        self.assertNotIn("You typed", " ".join(lines))
        self.assertIn("Type the full path, for example /Users/<you>/projects/app. Tab completes paths.", lines)
        self.assertIn("That path does not exist, but this one does: /Users/<you>/Code/proj", lines)
        lines = steps.cannot_open("/Users/<you>/Code/nowhere", "~/Code/nowhere", None)
        self.assertIn("You typed: [~/Code/nowhere]", lines)
        self.assertFalse(any("but this one does" in line for line in lines))



class FailureSummaryTests(unittest.TestCase):
    """W10.4 and W5.5: the summary and the installer's refusals hold fixed text only, never output."""

    def test_a_failure_line_holds_the_step_label_exit_code_and_hint(self) -> None:
        self.assertEqual("step 4 (Install): The install failed | exit code 2 | Press ? to see what ran",
                         steps.failure_line(4, "Install", "The install failed", 2, "Press ? to see what ran"))
        self.assertEqual("COCOLO setup: what failed\nstep 4 (Install): x | exit code 2 | y\n",
                         steps.summary_text(["step 4 (Install): x | exit code 2 | y"]))

    def test_a_known_installer_refusal_becomes_its_fixed_message_and_never_echoes(self) -> None:
        secret = "SECRET-TOKEN user@example.invalid"
        for raw, expected in (
            (f"{secret}: Managed paths have uncommitted changes", "Managed paths (.codex, .agents, .claude)"),
            (f"error {secret}: rebase in progress", "A Git operation (merge, rebase, cherry-pick or similar)"),
            (f"{secret} holds files the orchestrator does not install", "holds files the orchestrator does not install"),
            (f"{secret}: not a usable git checkout", "The path is not the top folder of a Git working tree."),
            (f"{secret}: target differs", "collides with a managed file"),
        ):
            with self.subTest(expected=expected):
                message, known = steps.installer_refusal(raw, "python3 install.py install --target /p")
                self.assertTrue(known)
                self.assertIn(expected, message)
                self.assertNotIn("SECRET", message)
                self.assertNotIn("user@example.invalid", message)

    def test_an_unknown_refusal_points_to_the_manual_command_and_never_echoes(self) -> None:
        message, known = steps.installer_refusal("SECRET-TOKEN something new", "python3 install.py install --target /p")
        self.assertFalse(known)
        self.assertEqual("The installer refused: run it manually to see why:", message)

    def test_a_summary_destination_with_a_control_character_is_refused(self) -> None:
        self.assertIsNotNone(steps.summary_destination_refusal("/x/sum\x01mary.txt"))
        self.assertIsNone(steps.summary_destination_refusal("/x/summary.txt"))



class ChangedPathTests(unittest.TestCase):
    """W5.1 and W5.2: which paths need a commit, from Git's change records (None when Git failed)."""

    def test_only_candidates_that_really_changed_and_renames_count_both_names(self) -> None:
        records = [".codex/a", ".codex/new", ".codex/old", "elsewhere"]
        self.assertEqual([".codex/a", ".codex/new", ".codex/old"],
                         steps.changed_paths(records, [".codex/a", ".codex/b", ".codex/new", ".codex/old"]))
        self.assertEqual([], steps.changed_paths([], [".codex/a"]))
        self.assertEqual([], steps.changed_paths(records, []))

    def test_when_git_cannot_answer_every_candidate_counts(self) -> None:
        self.assertEqual([".codex/a", ".codex/b"], steps.changed_paths(None, [".codex/a", ".codex/b"]))

    def test_uncommitted_list_for_display_removes_control_characters(self) -> None:
        self.assertEqual(".codex/a\n.claude/b[2J", steps.uncommitted_display([".codex/a", ".claude/b\x1b[2J"], True))
        self.assertEqual("(not checked: Git could not read the project)", steps.uncommitted_display(None, True))
        self.assertEqual("(not checked: the project's Git settings name programs that Git would run)",
                         steps.uncommitted_display([".codex/a"], False))



class CommitOutcomeTests(unittest.TestCase):
    """W5.3, W5.4, W5.7 and W10.5: the commit screen, the state after it and the summary's commit line."""

    def test_the_commit_screen_names_the_count_and_plumbing_only(self) -> None:
        text = steps.commit_body(3)
        self.assertIn("Only these 3 paths are committed, nothing is pushed.", text)
        self.assertIn("Setup commits with Git's plumbing commands only", text)

    def test_long_lists_show_ten_then_how_many_more(self) -> None:
        paths = [f".codex/{n}" for n in range(13)]
        self.assertEqual(paths[:10] + ["... and 3 more"], steps.first_ten(paths))
        self.assertEqual(paths[:10], steps.first_ten(paths[:10]))
        self.assertEqual("  ... and 3 more", steps.first_ten(paths, "  ")[-1])  # setup.sh: n - shown.

    def test_each_commit_state_has_its_summary_line(self) -> None:
        for state, note in (("committed", "committed"), ("nothing", "nothing to commit"),
                            ("not_committed", "not committed (you chose to commit yourself)"),
                            ("failed", "commit failed")):
            with self.subTest(state=state):
                self.assertEqual((state, note), steps.commit_outcome(state, "", 0))

    def test_a_commit_that_left_files_or_ignored_files_has_failed(self) -> None:
        self.assertEqual(("failed", "commit failed"), steps.commit_outcome("committed", ".codex/a", 0))
        self.assertEqual(("failed", "installed files ignored or could not be checked"),
                         steps.commit_outcome("committed", "", 2))
        self.assertEqual(("failed", "installed files ignored or could not be checked"),
                         steps.commit_outcome("not_committed", "", 1))
        self.assertEqual(("not_committed", "not committed (you chose to commit yourself)"),
                         steps.commit_outcome("not_committed", ".codex/a", 0))

    def test_the_summary_lists_what_is_left_only_when_not_committed(self) -> None:
        self.assertEqual(["Left uncommitted in the project:", "  .codex/a", "  .claude/b"],
                         steps.left_uncommitted(".codex/a\n.claude/b\n", "not_committed"))
        self.assertEqual([], steps.left_uncommitted(".codex/a", "committed"))
        self.assertEqual([], steps.left_uncommitted("", "failed"))
        lines = steps.left_uncommitted("\n".join(f"p{n}" for n in range(12)), "failed")
        self.assertEqual("  ... and 2 more", lines[-1])
        self.assertEqual(12, len(lines))

    def test_git_refusing_names_the_identity(self) -> None:
        self.assertIn("Git identity (user.name and user.email)", steps.COMMIT_REFUSED_HINT)



class SessionWarningTests(unittest.TestCase):
    """W2.1-W2.4: the warnings from the runtime's Codex session scan, never Codex's files themselves."""

    ADVICE = ("Open Codex on the project itself: never on a folder that contains a set-up project, this setup "
              "folder, your home folder, a folder above it or /.")

    def test_each_warning_names_the_date_folder_and_why_with_the_advice_once(self) -> None:
        scan = {"warnings": [
            {"date": "2026-11-18", "holds": "clone", "folder": "/Users/<you>/Code", "message": ""},
            {"date": "2026-11-17", "holds": "state", "folder": "/Users/<you>/.local", "message": ""},
            {"date": "2026-11-16", "holds": "project", "folder": "/Users/<you>/work", "message": ""},
            {"date": "2026-11-15", "holds": "home", "folder": "/Users/<you>", "message": ""},
        ]}
        lines = steps.session_warnings(scan)
        self.assertEqual("A Codex session on 2026-11-18 could write to /Users/<you>/Code. Codex was opened on a "
                         "folder that holds this setup folder.", lines[0])
        self.assertIn("holds the orchestrator's learning state.", lines[1])
        self.assertIn("contains a set-up project.", lines[2])
        self.assertIn("holds your home folder.", lines[3])
        self.assertEqual(self.ADVICE, lines[-1])
        self.assertEqual(1, lines.count(self.ADVICE))

    def test_the_runtimes_own_message_is_used_without_control_characters(self) -> None:
        scan = {"warnings": [{"date": "2026-11-18", "holds": "project", "folder": "/p/demo\x1b[2J",
                              "message": "Its own\x07 reason"}]}
        line = steps.session_warnings(scan)[0]
        self.assertEqual("A Codex session on 2026-11-18 could write to /p/demo[2J. Its own reason.", line)

    def test_no_warning_or_a_failed_scan_warns_about_nothing(self) -> None:
        for scan in ({"warnings": []}, None, {"warnings": "x"}, [], {"warnings": [{"folder": ""}, "x"]}):
            with self.subTest(scan=scan):
                self.assertEqual([], steps.session_warnings(scan))



class CommandsRuleTests(unittest.TestCase):
    """W8.2, W8.4-W8.6: what the commands-rule screen and verify say about the rule."""

    def test_the_screen_says_what_the_rule_does_and_never_root_only(self) -> None:
        text = " ".join(steps.commands_body("/Users/<you>/.codex/rules/x.rules", "missing"))
        self.assertIn("Every Codex session on this machine loads this rule from /Users/<you>/.codex/rules/x.rules "
                      "(mode 0600).", text)
        self.assertIn("Never open Codex on the orchestrator clone", text)
        self.assertIn("To undo it later, use the remove command shown at the end of setup.", text)
        self.assertNotIn("root folder", text)
        self.assertNotIn("already exists and differs", text)
        self.assertIn("already exists and differs", " ".join(steps.commands_body("/r", "stale")))

    def test_an_unknown_status_is_no_status(self) -> None:
        for status in ("installed", "missing", "stale", "unsupported"):
            self.assertEqual(status, steps.rule_status(status))
        for status in ("", "weird", None, 3):
            self.assertEqual("", steps.rule_status(status))

    def test_verify_reports_the_rule_without_writing_it(self) -> None:
        self.assertEqual(("ok", "installed", ["Codex commands rule: installed"]), steps.verify_rule("installed", ""))
        mark, note, lines = steps.verify_rule("missing", "")
        self.assertEqual(("skip", "missing"), (mark, note))
        self.assertEqual("Codex commands rule: missing", lines[0])
        self.assertIn("Codex sessions cannot reach the orchestrator without it.", lines[1])
        self.assertEqual(("skip", "stale"), steps.verify_rule("stale", "")[:2])
        mark, note, lines = steps.verify_rule("", "")
        self.assertEqual(("fail", "not checked"), (mark, note))
        self.assertIn("may be older than this check", " ".join(lines))

    def test_an_unsupported_home_is_explained_with_its_fix_never_may_be_older(self) -> None:
        mark, note, lines = steps.verify_rule("unsupported", "HOME is not this account's home folder\x1b")
        text = " ".join(lines)
        self.assertEqual(("fail", "Codex home unsupported"), (mark, note))
        self.assertIn("cannot be used with this Codex setup", text)
        self.assertIn("Reason: HOME is not this account's home folder ", text)
        self.assertIn("run setup with HOME set to your own home folder", text)
        self.assertNotIn("may be older", text)
        self.assertNotIn("\x1b", text)
        self.assertLessEqual(len(steps.unsupported_home("x" * 500)[1]), len("Reason: ") + 200)



class ReplyTests(unittest.TestCase):
    """setup.sh's JSON helper (get, reason, raw, verifyclass, claude), as pure functions over replies."""

    def test_get_walks_dotted_keys_and_formats_like_the_helper(self) -> None:
        reply = {"a": {"b": True, "c": False, "n": 3, "l": [1, 2], "s": "x"}}
        self.assertEqual(["true", "false", "3", "2", "x", "", ""],
                         [steps.get(reply, f"a.{key}") for key in ("b", "c", "n", "l", "s", "missing", "s.deeper")])
        self.assertEqual("", steps.get(None, "a"))

    def test_reason_shows_a_plain_code_and_a_fixed_message_only_when_asked(self) -> None:
        reply = {"reason_code": "claude_backend_not_subscription", "error": "Plan <SECRET>\n is wrong"}
        self.assertEqual("(claude_backend_not_subscription)", steps.reason(reply, fixed=False))
        self.assertEqual("Plan SECRET is wrong (claude_backend_not_subscription)", steps.reason(reply, fixed=True))
        self.assertEqual("(code)", steps.reason({"account_reason_code": "code", "reason_code": "Not Plain"}, False))
        self.assertEqual("", steps.reason({"reason_code": "x" * 70}, False))
        self.assertEqual("", steps.reason([], True))

    def test_raw_gathers_every_refusal_text_lowercased_on_one_line(self) -> None:
        reply = {"refused": "Top\nLevel", "codex": {"project_errors": ["Managed Paths"], "error": "E"},
                 "claude_settings": {"required_action": "Act"}, "codex_rule": {"errors": [3, "Rule"]}}
        self.assertEqual("top level managed paths e act rule", steps.raw(reply))
        self.assertEqual("", steps.raw(None))

    def test_verify_class(self) -> None:
        limited = {"codex": {"package_ready": True,
                             "project_errors": ["operational check sandbox preflight failed: x"]},
                   "claude": {"result": "verified"}, "claude_settings": {"ready": True}}
        self.assertEqual("linux-limited", steps.verify_class(limited))
        self.assertEqual("ready", steps.verify_class({"ready": True}))
        limited["codex"]["project_errors"].append("other")
        self.assertEqual("failed", steps.verify_class(limited))
        self.assertEqual("failed", steps.verify_class(None))

    def test_claude_login_keeps_only_plain_words(self) -> None:
        self.assertEqual((True, "max", "claude.ai"),
                         steps.claude_login(b'{"loggedIn": true, "subscriptionType": "max", "authMethod": "claude.ai"}'))
        self.assertEqual((True, "unknown", "x"), steps.claude_login(b'{"loggedIn": true, "authMethod": "<x>"}'))
        self.assertEqual((False, "", ""), steps.claude_login(b'{"loggedIn": false}'))
        self.assertEqual((False, "", ""), steps.claude_login(b"not json"))

    def test_shell_quote_matches_bash_printf_q(self) -> None:
        for value, quoted in (("my proj;x", "my\\ proj\\;x"), ("a=b", "a=b"), ("~/x", "~/x"), ("", "''"),
                              ("a,b.c_d+e:f@g%h/i-j", "a\\,b.c_d+e:f@g%h/i-j"), ("it's", "it\\'s"),
                              ("#x", "\\#x"), ("x#y", "x#y"), ("a!b{c}[d]^e*f?$g", "a\\!b\\{c\\}\\[d\\]\\^e\\*f\\?\\$g"),
                              ("a\nb", "$'a\\nb'")):
            with self.subTest(value=value):
                self.assertEqual(quoted, steps.shell_quote(value))



class OwnerKeyNoticeTests(unittest.TestCase):
    """W7.3: the main menu's owner key notice, from the records' owner key report."""

    def test_a_mismatch_or_a_replacement_is_noticed(self) -> None:
        self.assertEqual("mismatch", steps.owner_key_notice({"state": "mismatch", "replacements": 2}))
        import datetime
        when = datetime.datetime(2026, 11, 19, 14, 5)
        line = steps.owner_key_notice({"replacements": 1, "last_replaced_at_ms": when.timestamp() * 1000})
        self.assertEqual("2026-11-19 14:05", line)
        for report in ({"state": "ok"}, {"replacements": 0}, None, "x", {"replacements": 1}):
            with self.subTest(report=report):
                self.assertEqual("", steps.owner_key_notice(report))



class CommitStatusTests(unittest.TestCase):
    """Review 040-s5-wizard-2 (verifier): the git tool's exit status, including a commit made whose index
    update failed, which is committed (with what to do), never reported as not committed."""

    def test_each_exit_status_has_its_commit_state(self) -> None:
        self.assertEqual(("committed", ""), steps.commit_status(0))
        self.assertEqual(("nothing", ""), steps.commit_status(3))
        state, line = steps.commit_status(4)
        self.assertEqual("committed", state)
        self.assertIn("Git could not update the project's index", line)
        self.assertIn("git status", line)
        for status in (1, 2, 130):
            self.assertEqual(("failed", ""), steps.commit_status(status))



class CommitOutcomeAfterReviewTests(unittest.TestCase):
    """Review 040-s5-wizard-3 (a): a commit made with a stale index stays committed in the summary, with
    what to do; nothing to commit is not a failure."""

    def test_a_commit_with_a_stale_index_is_reported_as_committed(self) -> None:
        state, note = steps.commit_outcome("committed_stale", ".codex/a", 0)
        self.assertEqual("committed_stale", state)
        self.assertIn("committed", note)
        self.assertIn("git status", note)
        self.assertEqual([], steps.left_uncommitted(".codex/a", "committed_stale"))

    def test_the_tools_nothing_is_not_a_failure(self) -> None:
        self.assertEqual(("nothing", "nothing to commit"), steps.commit_outcome("nothing", "", 0))


if __name__ == "__main__":
    unittest.main()


class RoutingBridgeTests(unittest.TestCase):
    """Review 040-s1-update: bridging is read from the routing JSON, whatever its whitespace."""

    def test_bridging_is_read_as_json(self):
        for raw, expected in ((b'{"codex_primary_may_bridge_to_claude": true}', True),
                              (b'{"codex_primary_may_bridge_to_claude":\n\ttrue}', True),
                              (b'{"codex_primary_may_bridge_to_claude": false}', False),
                              (b'{"note": "\\"codex_primary_may_bridge_to_claude\\": true"}', False),
                              (b'not json', False)):
            with self.subTest(raw=raw):
                self.assertIs(expected, steps.routing_bridges(raw))
