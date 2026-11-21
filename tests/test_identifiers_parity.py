from __future__ import annotations

import ast
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

    def test_qa_manifest_file_and_git_give_the_same_file_set_and_findings(self) -> None:
        """QA reads the git-produced manifest the host wrote; the CLI asks git itself. One file set."""

        root = self.make_repo()
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("x\n", encoding="utf-8")
        via_git = check.list_scan_files(root)
        manifest_file = root.parent / (root.name + ".manifest")
        self.addCleanup(manifest_file.unlink, missing_ok=True)
        manifest_file.write_bytes(b"\0".join(check.git_manifest(root)) + b"\0")
        via_manifest = check.list_scan_files(root, manifest=check.read_manifest(manifest_file))
        self.assertEqual(via_git, via_manifest)
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


SENTINEL = "SENTINEL_MATCHED_VALUE"
TEMPLATE = Path(__file__).resolve().parents[1] / "scripts" / "qa-security.template"


def make_hole_repo(testcase: unittest.TestCase) -> Path:
    """A repository where git lists files that an approximate ignore walk would hide, plus links."""

    root = Path(testcase.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("/build/\ndoc/*.txt\nforced.txt\n", encoding="utf-8")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "check_identifiers.py").write_bytes(_SCRIPT.read_bytes())
    template = TEMPLATE.read_text(encoding="utf-8").replace("@QA_PYTHON@", sys.executable)
    (root / "qa-security").write_text(template, encoding="utf-8")
    # An anchored "/build/" ignores only the top-level directory; git lists the nested one.
    (root / "sub" / "build").mkdir(parents=True)
    (root / "sub" / "build" / "x.txt").write_text(f"{REAL}\n", encoding="utf-8")
    # "doc/*.txt": a single star never crosses a slash, so git lists the deeper file.
    (root / "doc" / "sub").mkdir(parents=True)
    (root / "doc" / "sub" / "a.txt").write_text(f"{REAL}\n", encoding="utf-8")
    (root / "doc" / "ignored.txt").write_text(f"{REAL}\n", encoding="utf-8")
    # A file that is tracked although an ignore rule names it is still listed (and still scanned).
    (root / "forced.txt").write_text(f"{REAL}\n", encoding="utf-8")
    try:
        os.symlink(REAL, root / "link-to-real")
        os.symlink("elsewhere", root / f"{SENTINEL}-link")
    except OSError:
        testcase.skipTest("this filesystem cannot create symbolic links")
    git(root, "add", "-A")
    git(root, "add", "-f", "forced.txt")
    git(root, "commit", "-qm", "x")
    (root / ".git" / "info" / "exclude").write_text("local-only.txt\n", encoding="utf-8")
    (root / "local-only.txt").write_text(f"{REAL}\n", encoding="utf-8")
    return root


def scan_env(root: Path, denylist: tuple[str, ...] = ()) -> dict[str, str]:
    names = root.parent / (root.name + ".denylist")
    names.write_text("\n".join(denylist), encoding="utf-8")
    manifest = root.parent / (root.name + ".manifest")
    manifest.write_bytes(b"\0".join(check.git_manifest(root)) + b"\0")
    return {
        **{k: v for k, v in os.environ.items() if not k.startswith("AO_")},
        "AO_IDENTIFIER_DENYLIST": str(names), "AO_QA_FILE_MANIFEST": str(manifest),
    }


def run_cli(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(root / "scripts" / "check_identifiers.py"), *args],
                          cwd=root, capture_output=True, text=True, env=env)


def run_template(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(root / "qa-security")], cwd=root, capture_output=True,
                          text=True, env=env)


def finding_lines(result: subprocess.CompletedProcess) -> set[str]:
    return {line for line in result.stderr.splitlines() if line.count(":") >= 2}


class EnumerationParityTests(unittest.TestCase):
    EXPECTED = {
        "sub/build/x.txt:1: home-path", "doc/sub/a.txt:1: home-path", "forced.txt:1: home-path",
        "link-to-real:1: home-path", "local-only.txt:1: home-path",
    }

    def setUp(self) -> None:
        self.root = make_hole_repo(self)
        self.env = scan_env(self.root, (SENTINEL.casefold(),))

    def test_qa_and_ci_report_the_same_findings_for_files_an_ignore_approximation_would_hide(self) -> None:
        ci = run_cli(self.root, self.env)
        qa = run_template(self.root, self.env)
        self.assertEqual((ci.returncode, qa.returncode), (1, 1), (ci.stderr, qa.stderr))
        self.assertEqual(finding_lines(ci), finding_lines(qa))
        self.assertTrue(self.EXPECTED <= finding_lines(qa), finding_lines(qa))
        # Ignored by git (single star, same directory) stays out of both.
        self.assertFalse(any(line.startswith("doc/ignored.txt") for line in finding_lines(qa)))

    def test_tracked_symlink_name_and_target_text_are_scanned_and_the_value_never_printed(self) -> None:
        for result in (run_cli(self.root, self.env), run_template(self.root, self.env)):
            self.assertIn("link-to-real:1: home-path", result.stderr)
            # The link whose own name holds a denylisted term is reported by category, never by name.
            self.assertIn("[redacted]:0: denylisted-term", result.stderr)
            self.assertNotIn("alice", result.stderr)
            self.assertNotIn(SENTINEL, result.stderr.upper())

    def test_non_utf8_listed_name_is_a_finding_not_a_skip(self) -> None:
        manifest = check.parse_manifest(b"tracked.txt\0bad\xff-name.txt\0")
        findings = check.scan_tree(self.root, (), manifest=manifest)
        self.assertEqual(["non-utf8-name"], [f.category for f in findings])
        self.assertNotIn("bad", str(findings[0]))

    def test_git_unavailable_is_a_clear_refusal_never_a_smaller_scan(self) -> None:
        env = {**self.env, "PATH": str(self.root / "no-such-dir")}
        env.pop("AO_QA_FILE_MANIFEST")
        result = run_cli(self.root, env)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("REFUSED", result.stderr)
        self.assertIn("git", result.stderr)
        self.assertNotIn("PASS", result.stdout)

    def test_qa_without_a_host_manifest_is_a_clear_refusal(self) -> None:
        env = dict(self.env)
        env.pop("AO_QA_FILE_MANIFEST")
        result = run_template(self.root, env)
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("REFUSED", result.stderr)
        self.assertNotIn("PASS", result.stdout)


class DiagnosticRedactionTests(unittest.TestCase):
    def test_scanner_output_never_prints_a_filename_that_holds_a_value(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        git(root, "init", "-q")
        (root / "scripts").mkdir()
        (root / "scripts" / "check_identifiers.py").write_bytes(_SCRIPT.read_bytes())
        for name in (f"{SENTINEL}-notes.txt", "ok.txt", "ali" + "ce@" + "mailhost.io.txt"):
            (root / name).write_text("see " + "bob" + "@" + "mailhost.io\n", encoding="utf-8")
        env = scan_env(root, (SENTINEL.casefold(),))
        for result in (run_cli(root, env),):
            self.assertEqual(1, result.returncode)
            self.assertIn("ok.txt:1: email", result.stderr)
            self.assertIn("[redacted]:1: email", result.stderr)
            for leaked in (SENTINEL, "alice", "bob", "mailhost"):
                self.assertNotIn(leaked.casefold(), result.stderr.casefold())


class ShippedQaTemplateTests(unittest.TestCase):
    """The QA wrapper that ships with the project must be the shared scanner, with the real inputs."""

    def test_template_calls_scan_tree_with_the_real_denylist_and_the_host_manifest(self) -> None:
        tree = ast.parse(TEMPLATE.read_text(encoding="utf-8").split("\n", 1)[1])
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "scan_tree"]
        self.assertEqual(1, len(calls), "the template must call check_identifiers.scan_tree exactly once")
        rendered = ast.unparse(calls[0])
        self.assertIn("load_denylist()", rendered)
        self.assertIn("manifest_from_environment()", rendered)
        for forbidden in ("use_git", "exclude", "frozenset", "()),"):
            self.assertNotIn(forbidden, rendered)

    def test_documentation_names_the_template_and_no_longer_the_empty_denylist_call(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "docs" / "manual-setup.md").read_text(encoding="utf-8")
        self.assertIn("qa-security.template", text)
        self.assertNotIn("use_git=False", text)
        self.assertNotIn("scan_tree(root, ()", text)

    def test_template_uses_the_real_denylist(self) -> None:
        root = make_hole_repo(self)
        (root / "only-denylisted.txt").write_text("plain words: zebra crossing\n", encoding="utf-8")
        git(root, "add", "-A")
        env = scan_env(root, ("zebra crossing",))
        self.assertIn("only-denylisted.txt:1: denylisted-term", run_template(root, env).stderr)


if __name__ == "__main__":
    unittest.main()
