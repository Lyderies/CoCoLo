from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.
import hashlib
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import MACOS_SANDBOX_ONLY, TEMP_ROOT

from codex_agents import learning_engine
from codex_agents.git_plumbing import git_executable
from codex_agents.learning_engine import run_operational_check_suite

REPOSITORY = Path(__file__).resolve().parents[3]
SCANNER = REPOSITORY / "scripts" / "check_identifiers.py"
TEMPLATE = REPOSITORY / "scripts" / "qa-security.template"
SENTINEL = "SENTINEL_MATCHED_VALUE"
REAL = "/" + "Users/alice/proj"

_SPEC = importlib.util.spec_from_file_location("check_identifiers_for_qa_tests", SCANNER)
check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = check
_SPEC.loader.exec_module(check)


def security_findings(stderr: Path, root: Path, listed: frozenset[str], denylist: tuple[str, ...]) -> list[dict]:
    """The host parser; ``root`` is passed only while the parser still takes one (it read files for line counts)."""

    keyword = {"root": root} if "root" in inspect.signature(learning_engine._security_findings).parameters else {}
    return learning_engine._security_findings(stderr, listed=listed, denylist=denylist, **keyword)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test", *args],
                   cwd=root, check=True, capture_output=True)


def make_project(testcase: unittest.TestCase) -> Path:
    root = Path(testcase.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT))).resolve()
    git(root, "init", "-q")
    (root / ".gitignore").write_text("/build/\ndoc/*.txt\nforced.txt\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "check_identifiers.py").write_bytes(SCANNER.read_bytes())
    (root / "qa-security").write_text(
        TEMPLATE.read_text(encoding="utf-8").replace("@QA_PYTHON@", sys.executable)
        .replace("@SCANNER_SHA256@", hashlib.sha256(SCANNER.read_bytes()).hexdigest()), encoding="utf-8")
    (root / "qa-security").chmod(0o700)
    (root / "sub" / "build").mkdir(parents=True)
    (root / "sub" / "build" / "x.txt").write_text(f"ok\n{REAL}\n", encoding="utf-8")
    (root / "doc" / "sub").mkdir(parents=True)
    (root / "doc" / "sub" / "a.txt").write_text(f"{REAL}\n", encoding="utf-8")
    (root / "forced.txt").write_text(f"{REAL}\n", encoding="utf-8")
    (root / f"{SENTINEL}-notes.txt").write_text("contact " + "bob" + "@" + "mailhost.io\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "add", "-f", "forced.txt")
    git(root, "commit", "-qm", "x")
    (root / "build").mkdir()
    (root / "build" / "o.txt").write_text(f"{REAL}\n", encoding="utf-8")  # ignored by git
    return root


class SecurityFindingParserTests(unittest.TestCase):
    """Portable: the parser forwards only values it can prove are not wrapper-supplied."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT))).resolve()
        (self.root / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        (self.root / "a").mkdir()
        (self.root / "a" / "b.py").write_text("x\n" * 12, encoding="utf-8")
        self.listed = frozenset({"a.txt", "a/b.py", f"{SENTINEL}.txt"})

    def parse(self, text: str | bytes, denylist: tuple[str, ...] = (SENTINEL.casefold(),)) -> list[dict]:
        stderr = self.root.parent / (self.root.name + ".stderr")
        self.addCleanup(stderr.unlink, missing_ok=True)
        stderr.write_bytes(text if isinstance(text, bytes) else text.encode())
        return security_findings(stderr, self.root, self.listed, denylist)

    def test_listed_clean_path_is_kept_and_its_line_number_is_never_forwarded(self) -> None:
        """A wrapper chooses the number it prints, so a number is a channel for a value (a six-digit
        identifier fits any file of that many lines). The host cannot tell a true line from a chosen one,
        so every record carries line 0; the path and the category stay."""

        self.assertEqual(
            [{"path": "a.txt", "line": 0, "category": "home-path"},
             {"path": "a/b.py", "line": 0, "category": "email"}],
            self.parse("a.txt:2: home-path\nfree text\na/b.py:12: email\n"))

    def test_unlisted_unflagged_path_is_redacted_not_copied(self) -> None:
        records = self.parse(f"{SENTINEL}-unknown.txt:1: email\nx.txt:9: home-path\n", denylist=())
        self.assertEqual([{"path": "[redacted]", "line": 0, "category": c} for c in ("email", "home-path")],
                         records)

    def test_listed_path_holding_a_denylisted_term_is_redacted(self) -> None:
        records = self.parse(f"{SENTINEL}.txt:1: email\n")
        self.assertEqual([{"path": "[redacted]", "line": 0, "category": "email"}], records)
        self.assertNotIn(SENTINEL, json.dumps(records))

    def test_a_wrapper_chosen_line_number_never_reaches_a_record(self) -> None:
        # A file with 123456 lines: a six-digit "line" is in range, so a range check alone forwards it.
        (self.root / "big.txt").write_text("x\n" * 123456, encoding="utf-8")
        self.listed = frozenset({"big.txt", "a.txt"})
        records = self.parse("big.txt:123456: email\nbig.txt:654321: email\na.txt:1: email\na.txt:123456789: email\n")
        self.assertEqual([0, 0, 0, 0], [r["line"] for r in records])
        self.assertNotIn("123456", json.dumps(records))

    def test_path_outside_a_safe_alphabet_or_naming_a_host_is_redacted(self) -> None:
        self.listed = frozenset({"a.txt", "x" + "@" + "y.txt", "my-host" + ".local", "a b.txt", "../a.txt"})
        text = "".join(f"{name}:1: email\n" for name in sorted(self.listed - {"a.txt"}))
        records = self.parse(text, denylist=())
        self.assertEqual({"[redacted]"}, {r["path"] for r in records})
        self.assertEqual(4, len(records))

    def test_a_fragment_cut_by_the_read_bound_is_dropped(self) -> None:
        bound = learning_engine.MAX_QA_FINDING_BYTES
        tail = b"a.txt:2: email"
        data = b"x\n" * ((bound - len(tail)) // 2) + tail + b"-more text\nb.txt:1: email\n"
        self.assertEqual(bound, len(data) - len(b"-more text\nb.txt:1: email\n"))
        self.assertEqual([], self.parse(data))

    def test_findings_past_the_record_limit_are_cut_at_the_limit(self) -> None:
        limit = learning_engine.MAX_QA_FINDING_RECORDS
        records = self.parse("a.txt:1: email\n" * (limit + 1))
        self.assertEqual(limit, len(records))

    def test_a_non_utf8_path_is_redacted(self) -> None:
        self.assertEqual([{"path": "[redacted]", "line": 0, "category": "email"}],
                         self.parse(b"bad\xff.txt:1: email\n"))

    def test_the_redaction_marker_and_the_non_utf8_category_are_accepted(self) -> None:
        self.assertEqual(
            [{"path": "[redacted]", "line": 0, "category": "non-utf8-name"}],
            self.parse("[redacted]:0: non-utf8-name\n"))


class QaScanInputsTests(unittest.TestCase):
    @unittest.skipUnless(git_executable(), "no trusted git binary on this machine")
    def test_host_manifest_is_exactly_the_manifest_the_ci_scanner_uses(self) -> None:
        root = make_project(self)
        (root / ".git" / "info" / "exclude").write_text("local-only.txt\n", encoding="utf-8")
        (root / "local-only.txt").write_text("x\n", encoding="utf-8")
        host = learning_engine._qa_file_manifest(root)
        self.assertIsNotNone(host)
        self.assertEqual(sorted(check.git_manifest(root)), sorted(check.parse_manifest(host)))
        names = {os.fsdecode(n) for n in check.parse_manifest(host)}
        self.assertTrue({"sub/build/x.txt", "doc/sub/a.txt", "forced.txt", "local-only.txt"} <= names)
        self.assertFalse(names & {"build/o.txt", "doc/ignored.txt"})

    @unittest.skipUnless(git_executable(), "no trusted git binary on this machine")
    def test_host_and_ci_manifests_equal_an_independent_literal_under_ignore_case_config(self) -> None:
        """What it checks: the host's file list and the CI scanner's file list both equal a hand-written
        set, in a repository whose own config says ``core.ignorecase=true`` (git then applies that to
        ``.gitignore`` matching and hides ``Doc/Keep.txt`` behind the rule ``doc/keep.txt``). Neither list
        is derived from the other, so a divergence between QA's list and CI's list fails here."""

        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT))).resolve()
        git(root, "init", "-q")
        git(root, "config", "core.ignorecase", "true")
        (root / ".gitignore").write_text("doc/keep.txt\nbuild/\n", encoding="utf-8")
        (root / "Doc").mkdir()
        (root / "Doc" / "Keep.txt").write_text("x\n", encoding="utf-8")
        (root / "build").mkdir()
        (root / "build" / "o.txt").write_text("x\n", encoding="utf-8")
        (root / "a.txt").write_text("x\n", encoding="utf-8")
        expected = [b".gitignore", b"Doc/Keep.txt", b"a.txt"]
        self.assertEqual(expected, check.git_manifest(root))
        self.assertEqual(expected, check.parse_manifest(learning_engine._qa_file_manifest(root)))

    def test_denylist_is_provisioned_from_the_configured_file_and_missing_means_none(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
        terms = directory / "terms.txt"
        terms.write_text("# private\nzebra crossing\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(terms)}):
            self.assertEqual(b"# private\nzebra crossing\n", learning_engine._qa_denylist_bytes())
        with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(directory / "absent.txt")}):
            self.assertEqual(b"", learning_engine._qa_denylist_bytes())


class SecurityDiagnosticsEndToEndTests(unittest.TestCase):
    """The shipped template, the real scanner and the parser; nothing supplies its own flag."""

    def run_template(self, root: Path, terms: Path) -> tuple[subprocess.CompletedProcess, list[dict]]:
        manifest = learning_engine._qa_file_manifest(root)
        self.assertIsNotNone(manifest)
        work = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
        (work / "manifest").write_bytes(manifest)
        env = {**os.environ, "AO_QA_FILE_MANIFEST": str(work / "manifest"), "AO_IDENTIFIER_DENYLIST": str(terms)}
        result = subprocess.run([sys.executable, str(root / "qa-security")], cwd=root, env=env,
                                capture_output=True, text=True)
        (work / "stderr").write_text(result.stderr, encoding="utf-8")
        listed = frozenset(os.fsdecode(n) for n in check.parse_manifest(manifest))
        with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(terms)}):  # the host's own chain
            denylist = learning_engine._qa_denylist_terms(learning_engine._qa_denylist_bytes() or b"")
        return result, security_findings(work / "stderr", root, listed, denylist)

    @unittest.skipUnless(git_executable(), "no trusted git binary on this machine")
    def test_sentinel_never_reaches_scanner_output_or_diagnostics_on_any_platform(self) -> None:
        root = make_project(self)
        terms = root.parent / (root.name + ".terms")
        self.addCleanup(terms.unlink, missing_ok=True)
        terms.write_text(f"{SENTINEL.casefold()}\n", encoding="utf-8")
        result, records = self.run_template(root, terms)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertNotIn(SENTINEL, (result.stdout + result.stderr).upper())
        self.assertNotIn(SENTINEL, json.dumps(records).upper())
        self.assertNotIn("alice", json.dumps(records))
        self.assertTrue(all(set(r) == {"path", "line", "category"} for r in records))
        by_path = {r["path"]: r for r in records}
        self.assertEqual("home-path", by_path["sub/build/x.txt"]["category"])
        self.assertEqual(0, by_path["sub/build/x.txt"]["line"])
        self.assertIn("doc/sub/a.txt", by_path)
        self.assertIn("forced.txt", by_path)
        self.assertEqual([{"path": "[redacted]", "line": 0, "category": c} for c in ("denylisted-term", "email")],
                         [r for r in records if r["path"] == "[redacted]"])

    def sandbox_project(self, security_wrapper: str = "qa-security") -> tuple[Path, Path]:
        """A project whose approved checks are a passing test wrapper and the named security wrapper."""

        root = make_project(self)
        terms = root.parent / (root.name + ".terms")
        self.addCleanup(terms.unlink, missing_ok=True)
        terms.write_text(f"{SENTINEL.casefold()}\n", encoding="utf-8")
        config_root = root / ".codex" / "orchestration"
        config_root.mkdir(parents=True)
        passing = root / "qa-tests"
        passing.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
        passing.chmod(0o700)
        if security_wrapper == "pass-without-scanning":
            ignoring = root / security_wrapper
            ignoring.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
            ignoring.chmod(0o700)
        interpreter = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()

        def check_entry(check_id: str, wrapper: Path) -> dict:
            return {
                "check_id": check_id, "driver": "python-wrapper", "argv": [f"./{wrapper.name}"],
                "executable_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 20,
            }

        (config_root / "operational-checks.json").write_text(json.dumps({
            "version": 1,
            "deterministic_checks": [check_entry("tests", passing)],
            "security_checks": [check_entry("identifiers", root / security_wrapper)],
        }), encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "wrappers")
        return root, terms

    @MACOS_SANDBOX_ONLY
    @unittest.skipUnless(git_executable(), "no trusted git binary on this machine")
    def test_sandboxed_qa_runs_the_shipped_template_with_the_real_denylist_and_manifest(self) -> None:
        root, terms = self.sandbox_project()
        diagnostics: list = []
        scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
        with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(terms)}):
            report = run_operational_check_suite(root, scratch_root=scratch, diagnostics=diagnostics)
        self.assertEqual("nonzero_exit", diagnostics[1]["reason_code"], diagnostics)
        paths = {f["path"] for f in diagnostics[1]["findings"]}
        self.assertTrue({"sub/build/x.txt", "doc/sub/a.txt", "forced.txt", "[redacted]"} <= paths, paths)
        self.assertEqual({0}, {f["line"] for f in diagnostics[1]["findings"]})
        self.assertNotIn(SENTINEL, json.dumps([report, diagnostics]).upper())

    @MACOS_SANDBOX_ONLY
    @unittest.skipUnless(git_executable(), "no trusted git binary on this machine")
    def test_a_security_check_never_launches_without_both_host_inputs(self) -> None:
        """A wrapper that ignores AO_QA_FILE_MANIFEST and AO_IDENTIFIER_DENYLIST used to be launched, and
        pass, when git or the denylist could not be provisioned. The host now rejects before launch."""

        reason = "identifier_inputs_unavailable"
        for missing in ("_qa_file_manifest", "_qa_denylist_bytes"):
            with self.subTest(missing=missing):
                root, terms = self.sandbox_project("pass-without-scanning")
                diagnostics: list = []
                scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
                with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(terms)}), \
                        mock.patch.object(learning_engine, missing, return_value=None):
                    report = run_operational_check_suite(root, scratch_root=scratch, diagnostics=diagnostics)
                self.assertTrue(report["deterministic_checks"][0]["passed"])
                self.assertFalse(report["security_checks"][0]["passed"], diagnostics)
                self.assertEqual(("unavailable", reason),
                                 (diagnostics[1]["status"], diagnostics[1]["reason_code"]))

    def test_an_unreadable_or_oversize_denylist_is_not_provisioned_as_empty(self) -> None:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
        for name, make in (("a directory", lambda: directory), ("oversize", None)):
            with self.subTest(name):
                path = directory if make else directory / "big.txt"
                if not make:
                    path.write_bytes(b"x" * (learning_engine.MAX_QA_DENYLIST_BYTES + 1))
                with mock.patch.dict(os.environ, {"AO_IDENTIFIER_DENYLIST": str(path)}):
                    self.assertIsNone(learning_engine._qa_denylist_bytes())


if __name__ == "__main__":
    unittest.main()
