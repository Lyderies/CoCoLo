"""S5: the Python wizard keeps the properties setup.sh's source tests checked (retired by mapping).

Each test names the SetupScriptTests test it replaces. They read the wizard's own source, the
launcher's and the entry script's, never running them.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIZARD = ROOT / "scripts" / "setup_wizard"
SOURCES = {path.name: path.read_text(encoding="utf-8") for path in sorted(WIZARD.glob("*.py"))}
SOURCES["setup-wizard.py"] = (ROOT / "scripts" / "setup-wizard.py").read_text(encoding="utf-8")
ALL = "\n".join(SOURCES.values())
LAUNCHER = (ROOT / "setup.sh").read_text(encoding="utf-8")


def launches(source: str):
    """Every list given as the first argument of subprocess.run/Popen, as source text."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("run", "Popen")
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess" and node.args):
            yield ast.get_source_segment(source, node.args[0])


class WizardSourceTests(unittest.TestCase):
    def test_every_python_the_wizard_starts_is_isolated(self) -> None:
        """Replaces SetupScriptTests.test_every_python_the_wizard_starts_is_isolated (W1.3, W1.6)."""
        # Every argument list that starts with the trusted interpreter, whether it goes to subprocess
        # directly or through the runner (self.run, handoff).
        python_launches = [match.group(0) for match in re.finditer(r"\[self\.py\b[^\]]*", ALL)]
        self.assertGreaterEqual(len(python_launches), 8)
        for arg in python_launches:
            with self.subTest(launch=arg):
                self.assertTrue(arg.startswith("[self.py, *PYF,"), arg)
        self.assertIn('PYF = ["-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"]', SOURCES["flow.py"])
        self.assertIn('exec "$PY" "${PYF[@]}" "$ROOT_DIR/scripts/setup-wizard.py" "$@"', LAUNCHER)
        self.assertIn("PYF=(-I -B -S -X pycache_prefix=/dev/null)", LAUNCHER)
        for line in ALL.splitlines():
            if "python3 -I" in line:
                self.assertIn("python3 -I -B -S -X pycache_prefix=/dev/null", line)
        # Owner commands run this clone's CLI, never the project's copy.
        self.assertIn("[self.py, *PYF, str(CLONE_CLI), *args]", SOURCES["flow.py"])
        self.assertNotIn('".codex", "orchestration", "cli.py")]', ALL)

    def test_every_git_call_uses_the_trusted_binary_with_helpers_off(self) -> None:
        """Replaces SetupScriptTests.test_every_git_call_uses_the_trusted_binary_with_helpers_off (W1.4)."""
        for source in SOURCES.values():
            for arg in launches(source):
                with self.subTest(launch=arg):
                    self.assertFalse(re.match(r'\[\s*"git"', arg), arg)
        self.assertIn("[self.git, \"-C\", project, *GIT_SAFE, *args]", SOURCES["flow.py"])
        self.assertIn('"-c", "core.hooksPath=/dev/null"', SOURCES["flow.py"])
        self.assertIn('GIT_ENV = {"xcrun_nocache": "1", "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1"}',
                      SOURCES["flow.py"])
        # No porcelain: status, add and commit go through the installer's plumbing.
        for match in re.finditer(r'self\.g\([^)]*\)', ALL):
            self.assertIsNone(re.search(r'"(status|add|commit|diff)"', match.group(0)), match.group(0))
        self.assertIn("install.commit_paths(", SOURCES["effects.py"])

    def test_every_git_call_with_paths_is_literal(self) -> None:
        """Replaces SetupScriptTests.test_every_git_call_with_paths_is_literal (W5.2)."""
        for needle in (" add -A -- ", " commit -m "):
            lines = [line for line in ALL.splitlines() if needle in line]
            self.assertTrue(lines, needle)
            for line in lines:
                self.assertIn("--literal-pathspecs", line)
        # ls-files runs through the installer's _git_run, which always passes --literal-pathspecs.
        self.assertIn('"--literal-pathspecs", *args]', (ROOT / "scripts" / "install.py").read_text(encoding="utf-8"))
        for match in re.finditer(r'_git_run\([^)]*"ls-files"', ALL):
            self.assertIn("install._git_run", ALL[match.start() - 20:match.end()])

    def test_provider_clients_are_never_run_by_name(self) -> None:
        """Replaces SetupScriptTests.test_provider_clients_are_never_run_by_name (W1.5)."""
        for source in SOURCES.values():
            for arg in launches(source):
                with self.subTest(launch=arg):
                    self.assertFalse(re.match(r'\[\s*"(codex|claude)"', arg), arg)
        self.assertNotRegex(ALL, r"shutil\.which\(\s*\"(codex|claude)\"")
        self.assertIn('self.owner_tool("client-status", name', SOURCES["flow.py"])

    def test_commands_and_commit_screens_say_what_the_rule_and_commit_do(self) -> None:
        """Replaces SetupScriptTests.test_commands_and_commit_screens_say_what_the_rule_and_commit_do (W8.2, W5.3)."""
        self.assertIn("Every Codex session on this machine loads this rule from {rule_file} (mode 0600).", ALL)
        self.assertNotIn("It applies to Codex sessions started in this project's root folder", ALL)
        self.assertIn("Setup commits with Git's plumbing commands only (hash-object, update-index, write-tree, ", ALL)
        self.assertNotIn("no script in the project runs as you", ALL)
        self.assertNotIn("or a commit hook stopped it", ALL)
        self.assertIn("Keep this setup folder outside every folder you open in Codex", ALL)
        self.assertIn("including the Codex commands rule", ALL)

    def test_header_and_welcome_name_the_product(self) -> None:
        """Replaces SetupScriptTests.test_header_and_welcome_name_the_product (W9.2)."""
        self.assertIn(" COCOLO setup ", SOURCES["terminal.py"])
        self.assertNotIn("Agent orchestration setup", ALL)
        self.assertIn('"Welcome to COCOLO\\nClaude Orchestrator, Codex Orchestrator, Local Orchestrator"', ALL)

    def test_wizard_messages_for_the_polished_screens(self) -> None:
        """Replaces SetupScriptTests.test_wizard_messages_for_the_polished_screens (W5.4, W7, W8.1, W10.1)."""
        for text in ("Nothing to commit: the project already has these files committed", "The install can be updated.",
                     "A fresh install is possible.", "Profile: exists with the same settings", "network_access is off",
                     '"Change the owner key passphrase"', '"Replace a lost owner key"',
                     '"--target", self.project, "--replace-owner-key"]', '"--change-passphrase"]',
                     'f"{self.home}/agent-orchestration-setup-summary.txt"'):
            with self.subTest(text=text):
                self.assertIn(text, ALL)
        self.assertNotIn("getcwd()}/agent-orchestration-setup-summary.txt", ALL)


if __name__ == "__main__":
    unittest.main()
