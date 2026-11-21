from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_identifiers.py"
_SPEC = importlib.util.spec_from_file_location("check_identifiers", _SCRIPT)
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)

# Samples are assembled at runtime so this file never contains a matching literal.
HOME_MAC = "/" + "Users" + "/alice/project"
HOME_LINUX = "/" + "home" + "/alice/project"
HOME_WINDOWS = "C:" + "\\Users" + "\\alice\\project"
HOSTNAME = "alices-laptop" + ".local"
LAPTOP = "Alices-" + "MacBook" + "-Pro"
EMAIL = "alice" + "@" + "mailhost.io"
NOREPLY = "123+someone" + "@" + "users.noreply.github.com"


def categories(text: str, denylist: tuple[str, ...] = ()) -> list[str]:
    return [finding.category for finding in check.scan_text(text, "sample", denylist)]


class ScanTextTests(unittest.TestCase):
    def test_home_paths_are_flagged_on_each_platform(self) -> None:
        for sample in (HOME_MAC, HOME_LINUX, HOME_WINDOWS):
            with self.subTest(sample=sample):
                self.assertEqual(categories(f"path {sample} here"), ["home-path"])

    def test_placeholders_and_shared_paths_are_allowed(self) -> None:
        self.assertEqual(categories("use /" + "Users/<name> or /" + "Users/Shared/x"), [])

    def test_placeholder_exemption_is_exact_case_sensitive_and_whole_component(self) -> None:
        """Only a bare placeholder token that ends at a separator or the end of the path is exempt."""

        win = "C:" + "\\Users" + "\\"
        for name, sample in {
            "space in the account name": win + "User Smith" + "\\p",
            "non-ASCII suffix": "/" + "home/user" + "\u00f1ame/p",
            "capitalised placeholder": "/" + "Users/User/p",
            "upper-case single letter": "/" + "Users/X/p",
            "capitalised word": "/" + "Users/Example/p",
            "tilde suffix": "/" + "home/user~1/p",
            "plus suffix": "/" + "home/user+a/p",
            "space after the token is ambiguous": "/" + "Users/x y/p",
        }.items():
            with self.subTest(name):
                self.assertEqual(categories(sample), ["home-path"], name)
        for sample in (
            "/" + "Users/x", "/" + "Users/x/", "`" + "/" + "Users/you" + "`", "(" + "/" + "home/user" + ")",
            '"' + "/" + "Users/name" + '"', win + "user" + "\\p", "cd " + "/" + "home/example/p",
        ):
            with self.subTest(sample):
                self.assertEqual(categories(sample), [])

    def test_placeholder_followed_by_punctuation_and_more_name_is_still_an_account(self) -> None:
        """A character that may legally sit inside an account name cannot end the component by itself.

        Windows account names may contain an apostrophe, backquote, brackets, comma and semicolon, so
        ``user'smith`` is one account name, not the placeholder ``user`` followed by quoted prose.
        """

        win = "C:" + "\\Users" + "\\"
        for ending in ("'smith", "`smith", ")smith", "]smith", "}smith", ",smith", ";smith", "'s"):
            for prefix in ("/" + "Users/user", "/" + "home/x", win + "user"):
                with self.subTest(prefix=prefix, ending=ending):
                    self.assertEqual(categories(prefix + ending + "\\notes.txt"), ["home-path"])
        for sample in ("see '" + "/" + "Users/x" + "' here", "(" + "/" + "home/user" + "), then", "paths " + "/" + "Users/you" + ", " + "/" + "Users/x"):
            with self.subTest(sample):
                self.assertEqual(categories(sample), [])

    def test_hostnames_are_flagged(self) -> None:
        self.assertEqual(categories(f"host {HOSTNAME}"), ["hostname"])
        self.assertEqual(categories(f"machine {LAPTOP}"), ["hostname"])

    def test_personal_email_is_flagged_but_noreply_is_allowed(self) -> None:
        self.assertEqual(categories(f"mail {EMAIL}"), ["email"])
        self.assertEqual(categories(f"mail {NOREPLY}"), [])

    def test_reserved_test_domains_and_protocol_names_are_allowed(self) -> None:
        for sample in ("tests" + "@" + "example.invalid", "t" + "@" + "example.test",
                       "sk-ssh-ed25519" + "@" + "openssh.com"):
            with self.subTest(sample=sample):
                self.assertEqual(categories(f"value {sample}"), [])

    def test_local_file_names_and_package_namespaces_are_allowed(self) -> None:
        self.assertEqual(categories("edit settings" + ".local" + ".json"), [])
        self.assertEqual(categories("https://codex-agents" + ".local" + "/schemas/x.json"), [])
        self.assertEqual(categories("ping build-box" + ".local" + " now"), ["hostname"])

    def test_denylisted_terms_match_case_insensitively(self) -> None:
        self.assertEqual(categories("Written by Jane Example", ("jane example",)), ["denylisted-term"])

    def test_findings_never_include_the_matched_value(self) -> None:
        findings = check.scan_text(f"{EMAIL} {HOME_MAC}", "doc.md", ("secret term",))
        rendered = "\n".join(str(finding) for finding in findings)
        self.assertNotIn(EMAIL, rendered)
        self.assertNotIn("alice", rendered)
        self.assertIn("doc.md:1", rendered)


class IdentityTests(unittest.TestCase):
    def test_noreply_identity_passes(self) -> None:
        self.assertEqual(check.check_person("someone", NOREPLY, "author", ()), [])

    def test_personal_email_and_denylisted_name_fail(self) -> None:
        found = check.check_person("Jane Example", EMAIL, "author", ("jane example",))
        self.assertEqual([item.category for item in found], ["non-noreply-email", "denylisted-term"])


class DenylistTests(unittest.TestCase):
    def test_missing_file_yields_no_terms(self) -> None:
        self.assertEqual(check.load_denylist(Path(tempfile.gettempdir()) / "absent-denylist.txt"), ())

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "denylist.txt"
            path.write_text("# private\n\nJane Example\n", encoding="utf-8")
            self.assertEqual(check.load_denylist(path), ("jane example",))


if __name__ == "__main__":
    unittest.main()
