from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
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

    def test_scan_set_is_an_independent_literal_even_when_the_repository_config_says_ignore_case(self) -> None:
        """What it checks: the files the scanner lists equal a hand-written list that no scanner code produced,
        in a repository whose own config sets ``core.ignorecase=true``. Git applies that setting to
        ``.gitignore`` matching, so left alone it hides ``Doc/Keep.txt`` behind the rule ``doc/keep.txt`` on
        every machine that sets it and not on CI. The old test wrote ``git_manifest`` output to a file and
        compared it with ``list_scan_files``, which calls ``git_manifest`` again, so it could not fail."""

        root = self.make_repo()
        git(root, "config", "core.ignorecase", "true")
        (root / ".gitignore").write_text("ignored.txt\nbuild/\ndoc/keep.txt\n", encoding="utf-8")
        (root / "Doc").mkdir()
        (root / "Doc" / "Keep.txt").write_text("x\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "deep.txt").write_text("x\n", encoding="utf-8")
        expected = {".gitignore", "Doc/Keep.txt", "sub/deep.txt", "tracked.txt", "untracked.txt"}
        self.assertEqual(expected, set(check.list_scan_files(root)))
        manifest = check.parse_manifest(b"\0".join(os.fsencode(name) for name in sorted(expected)) + b"\0")
        self.assertEqual(sorted(expected), check.list_scan_files(root, manifest=manifest))

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


def fill_template() -> str:
    """The shipped wrapper as a project saves it: interpreter and the approved scanner's digest filled in."""

    return (TEMPLATE.read_text(encoding="utf-8").replace("@QA_PYTHON@", sys.executable)
            .replace("@SCANNER_SHA256@", hashlib.sha256(_SCRIPT.read_bytes()).hexdigest()))


def make_hole_repo(testcase: unittest.TestCase) -> Path:
    """A repository where git lists files that an approximate ignore walk would hide, plus links."""

    root = Path(testcase.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("/build/\ndoc/*.txt\nforced.txt\n", encoding="utf-8")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "check_identifiers.py").write_bytes(_SCRIPT.read_bytes())
    (root / "qa-security").write_text(fill_template(), encoding="utf-8")
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


def scan_env(testcase: unittest.TestCase, root: Path, denylist: tuple[str, ...] = ()) -> dict[str, str]:
    """The inputs the host provides, in a private directory the test removes (never beside the repository)."""

    inputs = Path(testcase.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
    names = inputs / "denylist"
    names.write_text("\n".join(denylist), encoding="utf-8")
    manifest = inputs / "manifest"
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
        self.env = scan_env(self, self.root, (SENTINEL.casefold(),))

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
        env = scan_env(self, root, (SENTINEL.casefold(),))
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
        self.assertIn("load_required_denylist()", rendered)
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
        env = scan_env(self, root, ("zebra crossing",))
        self.assertIn("only-denylisted.txt:1: denylisted-term", run_template(root, env).stderr)


class FailClosedScanTests(unittest.TestCase):
    """QA must not pass where CI fails: anything the scanner cannot read or trust is a refusal (exit 2)."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        git(self.root, "init", "-q")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "check_identifiers.py").write_bytes(_SCRIPT.read_bytes())
        (self.root / "qa-security").write_text(fill_template(), encoding="utf-8")
        (self.root / "tracked.txt").write_text("clean\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "x")
        self.env = scan_env(self, self.root)

    def assert_refused(self, result: subprocess.CompletedProcess, *, hidden: tuple[str, ...] = ()) -> None:
        self.assertEqual(2, result.returncode, (result.stdout, result.stderr))
        self.assertIn("REFUSED", result.stderr)
        self.assertNotIn("PASS", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        for value in hidden:
            self.assertNotIn(value, result.stderr)

    def test_an_unreadable_listed_file_refuses_instead_of_being_skipped(self) -> None:
        locked = self.root / "locked.txt"
        locked.write_text(f"{REAL}\n", encoding="utf-8")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        if os.access(locked, os.R_OK):
            self.skipTest("this account can read files whatever their mode")
        env = scan_env(self, self.root)
        self.assert_refused(run_cli(self.root, env))
        self.assert_refused(run_template(self.root, env))

    def test_a_listed_path_below_a_symbolic_link_refuses(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        (outside / "f.txt").write_text(f"{REAL}\n", encoding="utf-8")
        os.symlink(outside, self.root / "d")
        with self.assertRaises(check.ScanRefused):
            check.scan_tree(self.root, (), manifest=check.parse_manifest(b"tracked.txt\0d/f.txt\0"))

    def test_a_listed_pipe_refuses_instead_of_blocking_or_being_skipped(self) -> None:
        os.mkfifo(self.root / "pipe")
        with self.assertRaises(check.ScanRefused):
            check.scan_tree(self.root, (), manifest=check.parse_manifest(b"pipe\0"))

    def test_a_deleted_file_and_a_directory_entry_are_scanned_by_name_only(self) -> None:
        (self.root / "module").mkdir()
        self.assertEqual([], check.scan_tree(
            self.root, (), manifest=check.parse_manifest(b"gone.txt\0module\0gone-dir/x.txt\0")))

    def test_a_manifest_naming_a_path_outside_the_project_refuses(self) -> None:
        for name in (b"../x", b"/etc/hosts", b"a//b", b"./a"):
            with self.subTest(name=name), self.assertRaises(check.ScanRefused):
                check.scan_tree(self.root, (), manifest=[name])

    def test_template_refuses_when_the_host_supplied_no_denylist_or_an_unreadable_one(self) -> None:
        empty_home = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        without = {k: v for k, v in self.env.items() if k != "AO_IDENTIFIER_DENYLIST"}
        self.assert_refused(run_template(self.root, {**without, "HOME": str(empty_home)}))
        missing = {**self.env, "AO_IDENTIFIER_DENYLIST": str(empty_home / "absent.txt")}
        self.assert_refused(run_template(self.root, missing))
        directory = {**self.env, "AO_IDENTIFIER_DENYLIST": str(empty_home)}
        self.assert_refused(run_template(self.root, directory))

    def test_an_empty_provisioned_denylist_means_no_private_terms(self) -> None:
        self.assertEqual(0, run_template(self.root, self.env).returncode)

    def test_template_refuses_a_scanner_that_changed_after_the_wrapper_was_approved(self) -> None:
        scanner = self.root / "scripts" / "check_identifiers.py"
        scanner.write_text(scanner.read_text(encoding="utf-8") + "\nscan_tree = lambda *a, **k: []\n",
                           encoding="utf-8")
        (self.root / "leak.txt").write_text(f"{REAL}\n", encoding="utf-8")
        self.assert_refused(run_template(self.root, scan_env(self, self.root)))

    def test_an_oversize_manifest_refuses_instead_of_being_truncated(self) -> None:
        manifest = Path(self.env["AO_QA_FILE_MANIFEST"])
        names = (b"%06d" % number + b"x" * 250 for number in range(16 * 1_048_576 // 257 + 2))
        manifest.write_bytes(b"\0".join(names) + b"\0")
        self.assertGreater(manifest.stat().st_size, 16 * 1_048_576)
        self.assert_refused(run_template(self.root, self.env))

    def test_the_cli_refuses_to_scan_only_a_subdirectory(self) -> None:
        (self.root / "sub").mkdir()
        (self.root / "sub" / "a.txt").write_text("clean\n", encoding="utf-8")
        (self.root / "elsewhere.txt").write_text(f"{REAL}\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(self.root / "scripts" / "check_identifiers.py")],
                                cwd=self.root / "sub", capture_output=True, text=True, env=self.env)
        self.assert_refused(result)

    def test_a_git_on_a_path_entry_inside_the_project_is_never_run(self) -> None:
        marker = self.root / "git-ran"
        (self.root / "bin").mkdir()
        fake = self.root / "bin" / "git"
        fake.write_text(f"#!/bin/sh\n: > {marker}\nexit 1\n", encoding="utf-8")
        fake.chmod(0o700)
        real_git = os.path.dirname(subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip())
        env = {k: v for k, v in self.env.items() if k != "AO_QA_FILE_MANIFEST"}
        env["PATH"] = f"{self.root / 'bin'}{os.pathsep}{real_git}"
        run_cli(self.root, env)
        self.assertFalse(marker.exists(), "the scanner ran a git binary that the project controls")

    def test_a_repository_config_that_moves_the_work_tree_is_refused_or_listed_correctly(self) -> None:
        other = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        (other / "other.txt").write_text(f"{REAL}\n", encoding="utf-8")
        git(self.root, "config", "core.worktree", str(other))
        env = {k: v for k, v in self.env.items() if k != "AO_QA_FILE_MANIFEST"}
        result = run_cli(self.root, env)
        self.assertNotEqual(0, result.returncode, result.stderr)
        self.assertNotIn("other.txt", result.stderr)


class ErrorRedactionTests(unittest.TestCase):
    """No error path prints a file name: names can hold a private value."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")))).resolve()
        git(self.root, "init", "-q")
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "check_identifiers.py").write_bytes(_SCRIPT.read_bytes())
        self.env = scan_env(self, self.root, (SENTINEL.casefold(),))

    def test_a_staged_submodule_entry_is_scanned_by_name_only_and_never_traces_back(self) -> None:
        git(self.root, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",submodule-marker")
        result = run_cli(self.root, scan_env(self, self.root), "--staged")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("submodule-marker", result.stdout + result.stderr)

    def test_a_staged_blob_that_cannot_be_read_refuses_without_naming_the_file(self) -> None:
        (self.root / f"{SENTINEL}-notes.txt").write_text("unique staged text 1\n", encoding="utf-8")
        git(self.root, "add", "-A")
        sha = subprocess.run(["git", "rev-parse", f":{SENTINEL}-notes.txt"], cwd=self.root, check=True,
                             capture_output=True, text=True).stdout.strip()
        (self.root / ".git" / "objects" / sha[:2] / sha[2:]).unlink()
        result = run_cli(self.root, self.env, "--staged")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertIn("REFUSED", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(SENTINEL, (result.stdout + result.stderr).upper())

    def test_an_invalid_revision_is_a_refusal_not_a_traceback(self) -> None:
        result = run_cli(self.root, self.env, "--commits", "no-such-revision")
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_unexpected_error_is_reported_without_its_message(self) -> None:
        import contextlib
        import io

        failure = OSError(13, "denied", f"{SENTINEL}-private-name")
        captured = io.StringIO()
        with unittest.mock.patch.object(check, "load_denylist", return_value=()), \
                unittest.mock.patch.object(check, "scan_paths", side_effect=failure), \
                contextlib.redirect_stderr(captured):
            code = check.main([])
        self.assertEqual(2, code)
        self.assertNotIn(SENTINEL, captured.getvalue().upper())

    def test_invalid_utf8_and_control_character_names_are_redacted_on_content_findings(self) -> None:
        content = ("see " + "bob" + "@" + "mailhost.io\n").encode()
        for raw in (b"bad\xff-notes.txt", "a\u202eb.txt".encode(), "x\u200by.txt".encode(), b"\xed\xa0\x80.txt"):
            with self.subTest(raw=raw):
                findings = check._scan_named(raw, lambda: content, ())
                self.assertIn("[redacted]:1: email", [str(f) for f in findings])
                for finding in findings:
                    self.assertTrue(str(finding).startswith("[redacted]:"), str(finding))

    def test_a_real_file_with_an_invalid_utf8_name_is_never_printed(self) -> None:
        raw = os.fsencode(self.root) + b"/bad\xff-notes.txt"
        try:
            with open(raw, "wb") as handle:
                handle.write(("see " + "bob" + "@" + "mailhost.io\n").encode())
        except OSError:
            self.skipTest("this filesystem rejects names that are not valid UTF-8")
        result = run_cli(self.root, scan_env(self, self.root))
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertNotIn("bad", result.stderr)
        self.assertNotIn("\\x", result.stderr)


if __name__ == "__main__":
    unittest.main()
