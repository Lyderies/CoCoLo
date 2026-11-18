"""PLAN T8e-1: real entry scripts never run planted bytecode and write none; the standard library's
installed bytecode is used.

Integration stage: it starts product processes from a copy of the runtime.
"""

import json
import os
import py_compile
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

ORCHESTRATION = Path(__file__).resolve().parents[1] / "native-package" / ".codex" / "orchestration"
FLAGS = ("-I", "-B", "-S", "-X", "pycache_prefix=/dev/null")


class EntryScriptProcessTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        # The whole package, since entry scripts read files beside the runtime (VERSION).
        package = self.base / "native-package"
        shutil.copytree(ORCHESTRATION.parents[1], package, ignore=shutil.ignore_patterns("__pycache__"))
        self.root = package / ".codex" / "orchestration"
        # A valid bytecode file beside a module every entry imports, that would stop the process.
        target = self.root / "codex_agents" / "host_paths.py"
        planted = self.base / "planted.py"
        planted.write_text("raise SystemExit('PLANTED BYTECODE RAN')\n")
        cfile = target.parent / "__pycache__" / f"host_paths.{sys.implementation.cache_tag}.pyc"
        cfile.parent.mkdir()
        py_compile.compile(str(planted), cfile=str(cfile), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.planted = cfile

    def run_python(self, *args):
        env = {"PATH": os.defpath, "HOME": str(self.base), "TMPDIR": str(self.base)}
        return subprocess.run([sys.executable, *FLAGS, *args], cwd=self.base, env=env,
                              capture_output=True, text=True, timeout=120)

    def assert_no_new_bytecode(self):
        written = [path for path in self.root.rglob("*.pyc") if path != self.planted]
        self.assertEqual([], written)

    def test_the_cli_never_runs_planted_bytecode(self):
        """T8e1-2, T8e1-4."""
        done = self.run_python(str(self.root / "cli.py"), "--help")
        self.assertEqual(0, done.returncode, done.stderr[-2000:])
        self.assertNotIn("PLANTED", done.stdout + done.stderr)
        self.assert_no_new_bytecode()

    def test_the_collector_entry_never_runs_planted_bytecode(self):
        done = self.run_python(str(self.root / "collector_host.py"), "--help")
        # It prints its help and then exits through its own stop handling (status 2).
        self.assertIn("usage: collector_host.py", done.stdout, done.stderr[-2000:])
        self.assertNotIn("PLANTED", done.stdout + done.stderr)
        self.assert_no_new_bytecode()

    def test_after_the_switch_the_standard_library_uses_its_installed_bytecode(self):
        """T8e1-3: a standard-library module's cached bytecode is the interpreter's own."""
        program = ("import sys; sys.path.append(sys.argv[1]);"
                   "from codex_agents.source_only import use_installed_stdlib_bytecode;"
                   "use_installed_stdlib_bytecode(sys.argv[1]);"
                   "import email.parser, json;"
                   "print(json.dumps({'prefix': sys.pycache_prefix, 'cached': email.parser.__spec__.cached}))")
        done = self.run_python("-c", program, str(self.root))
        self.assertEqual(0, done.returncode, done.stderr[-2000:])
        reply = json.loads(done.stdout)
        self.assertIsNone(reply["prefix"])
        stdlib = Path(sysconfig.get_paths()["stdlib"]).resolve()
        self.assertTrue(Path(reply["cached"]).resolve().is_relative_to(stdlib), reply)


if __name__ == "__main__":
    unittest.main()
