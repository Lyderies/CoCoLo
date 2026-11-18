"""PLAN T8e-1: COCOLO's own code always loads from source; the standard library may use its bytecode.

Unit stage: no process, socket or sleep. Every test restores the import state it changes.
"""

import ast
import importlib
import json
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path

from codex_agents import source_only

ORCHESTRATION = Path(__file__).resolve().parents[1] / "native-package" / ".codex" / "orchestration"
ENTRY_SCRIPTS = ("cli.py", "collector_host.py", "project_host.py", "learning_hook.py")


class ImportStateTestCase(unittest.TestCase):
    def setUp(self):
        saved = (list(sys.path), list(sys.path_hooks), dict(sys.path_importer_cache), sys.pycache_prefix,
                 set(sys.modules))
        self.addCleanup(self._restore, saved)
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.base, True)

    @staticmethod
    def _restore(saved):
        path, hooks, cache, prefix, modules = saved
        sys.path[:] = path
        sys.path_hooks[:] = hooks
        sys.path_importer_cache.clear()
        sys.path_importer_cache.update(cache)
        sys.pycache_prefix = prefix
        for name in set(sys.modules) - modules:
            del sys.modules[name]
        importlib.invalidate_caches()

    def package(self, root: Path, name: str) -> Path:
        """A package `name` with module `mod` whose source says 'source'."""
        (root / name).mkdir(parents=True)
        (root / name / "__init__.py").write_text("")
        source = root / name / "mod.py"
        source.write_text("VALUE = 'source'\n")
        return source

    def plant(self, source: Path, cfile: Path) -> None:
        """A valid bytecode file for `source` that the default loader would use, saying 'planted'."""
        planted = source.with_name("planted_src.py")
        planted.write_text("VALUE = 'planted'\n")
        cfile.parent.mkdir(parents=True, exist_ok=True)
        py_compile.compile(str(planted), cfile=str(cfile), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        planted.unlink()

    def value(self, root: Path, name: str) -> str:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        return importlib.import_module(f"{name}.mod").VALUE


class SourceOnlyTests(ImportStateTestCase):
    def test_a_planted_bytecode_file_beside_a_runtime_module_is_never_used(self):
        """T8e1-2; the control import shows the planted file would be used without the switch."""
        other = self.base / "other"
        self.plant(self.package(other, "control"), other / "control" / "__pycache__"
                   / f"mod.{sys.implementation.cache_tag}.pyc")
        sys.pycache_prefix = None
        self.assertEqual("planted", self.value(other, "control"))
        root = self.base / "runtime"
        source = self.package(root, "pkg")
        self.plant(source, root / "pkg" / "__pycache__" / f"mod.{sys.implementation.cache_tag}.pyc")
        source_only.use_installed_stdlib_bytecode(root)
        self.assertEqual("source", self.value(root, "pkg"))

    def test_a_planted_bytecode_file_under_a_prefix_is_never_used(self):
        """T8e1-2: whatever prefix is set later, a runtime module still loads from source."""
        root = self.base / "runtime"
        source = self.package(root, "pkg")
        prefix = self.base / "prefix"
        source_only.use_installed_stdlib_bytecode(root)
        sys.pycache_prefix = str(prefix)
        self.plant(source, Path(importlib.util.cache_from_source(str(source))))
        self.assertEqual("source", self.value(root, "pkg"))

    def test_a_bytecode_file_with_no_source_under_the_runtime_root_is_never_imported(self):
        """T8e1-2: a bare `.pyc` dropped into the runtime folder is not a module."""
        root = self.base / "runtime"
        self.package(root, "pkg")
        planted = root / "pkg" / "evil_src.py"
        planted.write_text("VALUE = 'planted'\n")
        py_compile.compile(str(planted), cfile=str(root / "pkg" / "evil.pyc"), doraise=True)
        planted.unlink()
        source_only.use_installed_stdlib_bytecode(root)
        sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("pkg.evil")

    def test_a_changed_source_is_seen(self):
        root = self.base / "runtime"
        source = self.package(root, "pkg")
        source_only.use_installed_stdlib_bytecode(root)
        self.assertEqual("source", self.value(root, "pkg"))
        source.write_text("VALUE = 'changed'\n")
        self.assertEqual("changed", importlib.reload(sys.modules["pkg.mod"]).VALUE)

    def test_modules_outside_the_runtime_root_use_pythons_usual_finders(self):
        """T8e1-5: only the runtime root is affected."""
        root = self.base / "runtime"
        self.package(root, "pkg")
        other = self.base / "other"
        self.plant(self.package(other, "elsewhere"), other / "elsewhere" / "__pycache__"
                   / f"mod.{sys.implementation.cache_tag}.pyc")
        source_only.use_installed_stdlib_bytecode(root)
        self.assertEqual("planted", self.value(other, "elsewhere"))

    def test_the_switch_clears_the_prefix_and_is_idempotent_without_changing_sys_path(self):
        """T8e1-3, T8e1-5."""
        root = self.base / "runtime"
        root.mkdir()
        sys.pycache_prefix = "/dev/null"
        path, hooks = list(sys.path), len(sys.path_hooks)
        source_only.use_installed_stdlib_bytecode(root)
        source_only.use_installed_stdlib_bytecode(root)
        self.assertIsNone(sys.pycache_prefix)
        self.assertEqual(path, sys.path)
        self.assertEqual(hooks + 1, len(sys.path_hooks))

    def test_loading_a_runtime_module_writes_no_bytecode(self):
        """T8e1-4, with bytecode writing turned on: the loader itself never writes."""
        root = self.base / "runtime"
        self.package(root, "pkg")
        source_only.use_installed_stdlib_bytecode(root)
        saved, sys.dont_write_bytecode = sys.dont_write_bytecode, False
        self.addCleanup(setattr, sys, "dont_write_bytecode", saved)
        self.value(root, "pkg")
        self.assertEqual([], list(root.rglob("*.pyc")))

    def test_the_switch_module_imports_only_the_standard_library(self):
        """T8e1-1."""
        tree = ast.parse((ORCHESTRATION / "codex_agents" / "source_only.py").read_text())
        names = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
        names |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                  and node.module and node.level == 0}
        self.assertTrue(names <= set(sys.stdlib_module_names) | {"__future__"}, names)
        self.assertFalse(any(isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)))


class EntryScriptTests(unittest.TestCase):
    """T8e1-1: each entry script switches before it imports any other runtime module."""

    def first_runtime_import_and_switch(self, source: str) -> tuple[int | None, int | None]:
        switch = first = None
        for node in ast.walk(ast.parse(source)):
            line = getattr(node, "lineno", None)
            if isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", None)) == \
                    "use_installed_stdlib_bytecode":
                switch = line if switch is None else min(switch, line)
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(n.startswith("codex_agents") and n not in ("codex_agents.source_only",
                                                              "codex_agents.source_only.use_installed_stdlib_bytecode")
                   for n in names):
                first = line if first is None else min(first, line)
        return switch, first

    def test_every_entry_script_switches_first(self):
        for name in ENTRY_SCRIPTS:
            with self.subTest(script=name):
                switch, first = self.first_runtime_import_and_switch((ORCHESTRATION / name).read_text())
                self.assertIsNotNone(switch, f"{name} never calls the switch")
                self.assertIsNotNone(first, f"{name} imports no runtime module")
                self.assertLess(switch, first)

    def test_every_entry_script_switches_before_any_import_but_os_and_sys(self):
        """T8e1-1 (corrected): its own standard-library imports come after the switch too."""
        for name in ENTRY_SCRIPTS:
            with self.subTest(script=name):
                tree = ast.parse((ORCHESTRATION / name).read_text())
                switch, _ = self.first_runtime_import_and_switch((ORCHESTRATION / name).read_text())
                early = []
                for node in tree.body:
                    if getattr(node, "lineno", 0) >= switch:
                        break
                    if isinstance(node, ast.Import):
                        early += [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        early.append(node.module)
                self.assertTrue(set(early) <= {"__future__", "os", "sys", "codex_agents.source_only"}, early)

    def test_the_signature_helper_bootstrap_switches_first_and_may_import_only_the_switch_more(self):
        from codex_agents import signature_execution
        bootstrap = signature_execution._BOOTSTRAP
        switch, first = self.first_runtime_import_and_switch(bootstrap)
        self.assertIsNotNone(switch)
        self.assertLess(switch, first)
        allowed = ast.literal_eval(bootstrap.split("allowed=", 1)[1].split("\nclass", 1)[0])
        self.assertEqual({"codex_agents.contracts", "codex_agents.learning_contracts", "codex_agents.owner_record_payload",
                          "codex_agents.owner_key_contracts", "codex_agents.signature_inputs",
                          "codex_agents.signature_input_wire", "codex_agents.signature_payloads",
                          "codex_agents.signature_process", "codex_agents.signature_sandbox",
                          "codex_agents.source_only"}, allowed)

    def test_the_fixed_isolation_flags_are_unchanged(self):
        """T8e1-4."""
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        self.assertEqual(("-I", "-B", "-S", "-X", "pycache_prefix=/dev/null"), PYTHON_ISOLATION_FLAGS)
        self.assertIn("-X pycache_prefix=/dev/null", json.dumps((ORCHESTRATION / "codex_agents" / "hook_config.py")
                                                                  .read_text()))


if __name__ == "__main__":
    unittest.main()
