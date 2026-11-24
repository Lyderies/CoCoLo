"""T7 S1 (scripts/e2e_checks_run_control.py): the test-only source rewrites the run-control scenes ask the stand-in
Python for must each match the installed product's source exactly once, or the scene would fail loudly at import."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_checks_run_control as control  # noqa: E402

ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "codex_agents"


class PatchTests(unittest.TestCase):
    def source(self, module: str) -> str:
        return (ORCHESTRATION / (module.split(".")[-1] + ".py")).read_text()

    def test_every_rewrite_matches_the_product_source_exactly_once(self):
        patches = [control.liveness_patch(*control.SILENCE), *[patch for patches in control.FAULTS.values() for patch in patches],
                   {"module": "codex_agents.account_billing_admission", "old": "PROBE_REUSE_MS = 300_000"},
                   {"module": "codex_agents.account_billing_admission", "old": "PROBE_COOLDOWN_MS = 300_000"}]
        for patch in patches:
            with self.subTest(module=patch["module"], old=patch["old"][:40]):
                self.assertEqual(1, self.source(patch["module"]).count(patch["old"]))

    def test_scaled_limits_stay_in_the_clock_s_order(self):
        startup, quiet, stale, silence = control.SILENCE
        self.assertTrue(quiet < stale < silence)
        self.assertLess(silence, 30)  # the silent child's own sleep (45 s) outlasts the cutoff

    def test_the_stand_in_python_applies_a_listed_rewrite_and_refuses_one_that_misses(self):
        import json
        import subprocess
        import tempfile
        import e2e_support
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "mod_under_test.py").write_text("VALUE = 1\n")
            boot = e2e_support.BOOT.replace("%HOME%", repr(str(base))).replace("%SHIM%", repr("/x"))
            (base / ".e2e-bin").mkdir()
            (base / "boot.py").write_text(boot)
            script = base / "run.py"
            script.write_text("import sys\nsys.path.insert(0, %r)\nimport mod_under_test\nprint(mod_under_test.VALUE)\n" % str(base))
            patches = base / ".e2e-bin" / "patches.json"
            patches.write_text(json.dumps([{"module": "mod_under_test", "old": "VALUE = 1", "new": "VALUE = 7"}]))
            done = subprocess.run([sys.executable, "-B", str(base / "boot.py"), str(script)], capture_output=True, text=True)
            self.assertEqual("7", done.stdout.strip(), done.stderr)
            patches.write_text(json.dumps([{"module": "mod_under_test", "old": "VALUE = 2", "new": "VALUE = 7"}]))
            done = subprocess.run([sys.executable, "-B", str(base / "boot.py"), str(script)], capture_output=True, text=True)
            self.assertNotEqual(0, done.returncode)
            self.assertIn("does not apply", done.stderr)


if __name__ == "__main__":
    unittest.main()
