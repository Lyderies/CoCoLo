from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_identifiers.py"
_SPEC = importlib.util.spec_from_file_location("check_identifiers_parity", _SCRIPT)
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)

# Samples are assembled at runtime so this file never contains a matching literal.
PLACEHOLDERS = ("/" + "Users/x/proj", "/" + "home/user/proj", "/" + "Users/you/a", "/" + "Users/name/a")
REAL = "/" + "Users/alice/proj"


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args],
        cwd=root, check=True, capture_output=True,
    )


class PlaceholderAllowlistTests(unittest.TestCase):
    def test_documented_placeholder_home_paths_pass_but_real_looking_names_fail(self) -> None:
        for sample in PLACEHOLDERS:
            with self.subTest(sample=sample):
                self.assertEqual(check.scan_text(sample, "f", ()), [])
        self.assertEqual([f.category for f in check.scan_text(REAL, "f", ())], ["home-path"])
        # A longer name that merely starts with a placeholder is still a real-looking name.
        self.assertEqual(len(check.scan_text("/" + "Users/xavier/p", "f", ())), 1)


class SharedScanTreeTests(unittest.TestCase):
    def make_repo(self) -> Path:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        git(root, "init", "-q")
        (root / ".gitignore").write_text("ignored.txt\nbuild/\n", encoding="utf-8")
        (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
        git(root, "add", ".")
        git(root, "commit", "-qm", "x")
        (root / "untracked.txt").write_text(f"see {PLACEHOLDERS[0]}\n", encoding="utf-8")
        (root / "ignored.txt").write_text(f"{REAL}\n", encoding="utf-8")
        (root / "build").mkdir()
        (root / "build" / "o.txt").write_text(f"{REAL}\n", encoding="utf-8")
        return root

    def test_untracked_placeholder_passes_and_untracked_real_path_fails(self) -> None:
        root = self.make_repo()
        self.assertEqual(check.scan_tree(root, ()), [])
        (root / "evidence.txt").write_text(f"x\n{REAL}\n", encoding="utf-8")
        findings = check.scan_tree(root, ())
        self.assertEqual([str(f) for f in findings], ["evidence.txt:2: home-path"])

    def test_parity_qa_walk_and_check_identifiers_scan_the_same_file_set(self) -> None:
        """QA runs in a sandbox that cannot start git, so it walks the disk; the CLI asks git.

        Both must visit the same files: tracked files plus untracked files that are not
        ignored (``git ls-files --cached --others --exclude-standard``). Untracked review
        evidence in progress is therefore still checked before it is committed, and ignored
        build output is not, in both tools.
        """

        root = self.make_repo()
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("x\n", encoding="utf-8")
        via_git = check.list_scan_files(root, use_git=True)
        via_walk = check.list_scan_files(root, use_git=False)
        self.assertEqual(via_git, via_walk)
        self.assertIn("untracked.txt", via_git)
        self.assertIn("sub/deep.txt", via_git)
        self.assertNotIn("ignored.txt", via_git)
        self.assertNotIn("build/o.txt", via_git)

    def test_cli_scans_untracked_files_and_fails_on_real_path_only(self) -> None:
        root = self.make_repo()
        run = lambda: subprocess.run([sys.executable, str(_SCRIPT)], cwd=root, capture_output=True, text=True,
                                     env={**os.environ, "AO_IDENTIFIER_DENYLIST": str(root / "none")})
        self.assertEqual(run().returncode, 0)
        (root / "evidence.txt").write_text(REAL, encoding="utf-8")
        result = run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("evidence.txt:1: home-path", result.stderr)
        self.assertNotIn("alice", result.stderr)


if __name__ == "__main__":
    unittest.main()
