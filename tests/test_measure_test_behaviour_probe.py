"""PLAN T8d: measuring a real test module records its CPU and what each process start was.

Integration stage: it runs a synthetic test module in its own process.
"""

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import measure_test_behaviour as measure  # noqa: E402


class ProbeTests(unittest.TestCase):
    def test_a_measured_module_has_cpu_and_its_starts_by_kind(self):
        """T8d-1, T8d-2: a product-like script, a git and a shell start, and CPU spent in a child."""
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            orchestration = base / "native-package" / ".codex" / "orchestration"
            orchestration.mkdir(parents=True)
            (orchestration / "busy.py").write_text("sum(i * i for i in range(3_000_000))\n")
            (base / "tests").mkdir()
            (base / "tests" / "test_synthetic.py").write_text(textwrap.dedent(f"""
                import subprocess, sys, unittest
                class Synthetic(unittest.TestCase):
                    def test_starts(self):
                        subprocess.run([sys.executable, "-I", "-B", {str(orchestration / "busy.py")!r}], check=True)
                        subprocess.run(["git", "--version"], check=True, stdout=subprocess.DEVNULL)
                        # close_fds=False lets Popen start it through os.posix_spawn: still one start.
                        subprocess.run(["/bin/sh", "-c", "exit 0"], check=True, close_fds=False)
            """))
            row = measure.measure_module(base, "test_synthetic", {}, base)
        self.assertNotIn("error", row, row)
        self.assertEqual(1, row["tests"])
        self.assertEqual(0, row["failed"])
        self.assertEqual(3, row["processes"])
        self.assertEqual({"product": 1, "fixture": 0, "python": 0, "git": 1, "wizard": 0, "other": 1},
                         row["starts"])
        self.assertEqual(row["processes"], sum(row["starts"].values()))
        self.assertGreater(row["cpu_seconds"], 0.05)  # The child's loop counts: children are included.
        self.assertEqual(json.loads(json.dumps(row["cpu_seconds"])), round(row["cpu_seconds"], 2))


if __name__ == "__main__":
    unittest.main()
