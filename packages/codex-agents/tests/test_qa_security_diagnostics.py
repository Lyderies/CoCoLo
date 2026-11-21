from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import MACOS_SANDBOX_ONLY, TEMP_ROOT

from codex_agents.learning_engine import run_operational_check_suite

SENTINEL = "SENTINEL_MATCHED_VALUE"


class SecurityFindingDiagnosticsTests(unittest.TestCase):
    @MACOS_SANDBOX_ONLY
    def test_failed_security_check_records_file_line_and_category_never_values(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEMP_ROOT) as directory:
            root = Path(directory)
            config_root = root / ".codex" / "orchestration"
            config_root.mkdir(parents=True)
            wrapper = root / "qa-security"
            wrapper.write_text(
                f"#!{sys.executable}\nimport sys\n"
                "print('x.txt:9: home-path', file=sys.stderr)\n"
                f"print('free text {SENTINEL}', file=sys.stderr)\n"
                f"print('{SENTINEL}.txt (path):1: denylisted-term', file=sys.stderr)\n"
                f"print('{SENTINEL}.txt:3: denylisted-term', file=sys.stderr)\n"
                "print('a/b.py:12: email', file=sys.stderr)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
            passing = root / "qa-tests"
            passing.write_text(f"#!{sys.executable}\nraise SystemExit(0)\n", encoding="utf-8")
            passing.chmod(0o700)
            interpreter = hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
            (config_root / "operational-checks.json").write_text(json.dumps({
                "version": 1,
                "deterministic_checks": [{
                    "check_id": "tests", "driver": "python-wrapper", "argv": ["./qa-tests"],
                    "executable_sha256": hashlib.sha256(passing.read_bytes()).hexdigest(),
                    "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 10,
                }],
                "security_checks": [{
                    "check_id": "identifiers", "driver": "python-wrapper", "argv": ["./qa-security"],
                    "executable_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                    "interpreter_sha256": interpreter, "runtime_tree": None, "timeout_seconds": 10,
                }],
            }), encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.test",
                            "commit", "-qm", "x"], cwd=root, check=True)
            diagnostics: list = []
            scratch = Path(self.enterContext(tempfile.TemporaryDirectory(dir=TEMP_ROOT)))
            report = run_operational_check_suite(root, scratch_root=scratch, diagnostics=diagnostics)
        self.assertEqual("nonzero_exit", diagnostics[1]["reason_code"])
        self.assertEqual(
            [{"path": "x.txt", "line": 9, "category": "home-path"},
             {"path": "a/b.py", "line": 12, "category": "email"}],
            [f for f in diagnostics[1]["findings"] if f["path"] in ("x.txt", "a/b.py")],
        )
        # A finding whose own path was the value is reported by category only; free text is dropped.
        self.assertEqual(
            [{"path": "[redacted]", "line": n, "category": "denylisted-term"} for n in (1, 3)],
            [f for f in diagnostics[1]["findings"] if f["path"] == "[redacted]"],
        )
        self.assertTrue(all(set(f) <= {"path", "line", "category"} for f in diagnostics[1]["findings"]))
        self.assertNotIn(SENTINEL, json.dumps([report, diagnostics]))


if __name__ == "__main__":
    unittest.main()
