from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/audit_service_ownership.py"
SPEC = importlib.util.spec_from_file_location("service_ownership_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class OwnershipMemoryTests(unittest.TestCase):
    def check(self, sources, root="client.py"):
        return audit.audit_sources(sources, [root])

    def reasons(self, report):
        return {item["reason"] for client in report["clients"] for item in client["violations"]}

    def test_source_is_never_executed_and_arguments_are_not_reported(self):
        report = self.check({"client.py": "raise RuntimeError('CANARY-private-argument')\n"})
        self.assertTrue(report["boundary_conformant"])
        self.assertEqual(1, report["counts"]["all_ast_calls"])
        self.assertNotIn("CANARY", json.dumps(report))

    def test_aliased_sqlite_dependency_and_call_are_detected(self):
        report = self.check({"client.py": "import sqlite3 as db\nconnection = db.connect('CANARY-path')\n"})
        self.assertFalse(report["boundary_conformant"])
        self.assertIn("persistence_dependency", self.reasons(report))
        call = report["modules"]["client"]["calls"][0]
        self.assertEqual("sqlite3.connect", call["import_qualified_candidate"])
        self.assertIn("persistence_constructor_or_call", call["categories"])
        self.assertNotIn("CANARY", json.dumps(report))

    def test_relative_helper_and_cycles_have_deterministic_witnesses(self):
        sources = {
            "pkg/__init__.py": "",
            "pkg/client.py": "from .helper import make\n",
            "pkg/helper.py": "from . import client\nfrom sqlite3 import connect as db\n\ndef make(): return db('CANARY')\n",
        }
        report = self.check(sources, "pkg/client.py")
        violation = next(v for v in report["clients"][0]["violations"] if v["reason"] == "persistence_dependency")
        self.assertEqual(["pkg.client", "pkg.helper", "sqlite3"], violation["via"])
        reversed_sources = dict(reversed(list(sources.items())))
        self.assertEqual(report, self.check(reversed_sources, "pkg/client.py"))

    def test_package_initializer_is_included_even_for_root_module(self):
        report = self.check({"pkg/__init__.py": "import sqlite3\n", "pkg/client.py": ""}, "pkg/client.py")
        self.assertIn("persistence_dependency", self.reasons(report))
        self.assertEqual(["pkg.client", "pkg", "sqlite3"], report["clients"][0]["violations"][0]["via"])

    def test_owner_constructor_and_indirect_owner_import_are_reported(self):
        report = self.check({
            "client.py": "from helper import owner\n",
            "helper.py": "from codex_agents.learning_store import LearningLedger as Owner\nowner = Owner(None)\n",
        })
        self.assertIn("persistence_dependency", self.reasons(report))
        call = report["modules"]["helper"]["calls"][0]
        self.assertEqual("codex_agents.learning_store.LearningLedger", call["import_qualified_candidate"])

    def test_sqlite_c_extension_is_not_an_escape_from_the_import_check(self):
        report = self.check({"client.py": "import _sqlite3 as db\ndb.connect('CANARY')\n"})
        self.assertIn("persistence_dependency", self.reasons(report))
        self.assertFalse(report["boundary_conformant"])

    def test_pure_client_graph_is_not_confused_by_string_literals(self):
        report = self.check({
            "pkg/__init__.py": "",
            "pkg/client.py": "from .contracts import VERSION\nSQL = 'import sqlite3; ledger.execute(CANARY)'\n",
            "pkg/contracts.py": "VERSION = 1\n",
        }, "pkg/client.py")
        self.assertTrue(report["boundary_conformant"])
        self.assertEqual([], report["clients"][0]["violations"])
        self.assertNotIn("CANARY", json.dumps(report))

    def test_dynamic_loading_and_untyped_sql_calls_fail_conservatively(self):
        for source in (
            "from importlib import import_module as load\nload('CANARY')\n",
            "import importlib as lib\nloader = lib.import_module\n",
            "import importlib\nalias = importlib\nalias.import_module('CANARY')\n",
            "from runpy import run_path as run\nrun('CANARY')\n",
            "loader.exec_module('CANARY')\n",
            "import builtins as native\nnative.__import__('CANARY')\n",
            "loader = __import__\n",
            "run = eval\n",
            "def submit(connection): return connection.execute('CANARY')\n",
        ):
            with self.subTest(source_kind=source.splitlines()[0]):
                report = self.check({"client.py": source})
                self.assertFalse(report["boundary_conformant"])
                self.assertTrue(self.reasons(report) & {"dynamic_loading", "possible_sql_call"})
                self.assertNotIn("CANARY", json.dumps(report))

    def test_missing_local_import_wildcard_and_missing_root_do_not_pass(self):
        variants = [
            ({"pkg/__init__.py": "", "pkg/client.py": "from .missing import make\n"}, "pkg/client.py"),
            ({"pkg/__init__.py": "", "pkg/client.py": "from . import missing\n"}, "pkg/client.py"),
            ({"pkg/__init__.py": "", "pkg/client.py": "from pkg import missing\n"}, "pkg/client.py"),
            ({"pkg/client.py": "from . import missing\n"}, "pkg/client.py"),
            ({"client.py": "import client.missing\n"}, "client.py"),
            ({"pkg/__init__.py": "", "pkg/client.py": "from . import *\n"}, "pkg/client.py"),
            ({"client.py": ""}, "absent.py"),
        ]
        for sources, root in variants:
            with self.subTest(root=root):
                self.assertFalse(self.check(sources, root)["boundary_conformant"])

    def test_call_counts_are_ast_sites_and_command_scopes_not_line_regexes(self):
        source = (
            "def main(args, ledger, owner):\n"
            " if args.command == 'learning-status':\n"
            "  ledger.read(); ledger.read()\n"
            " elif args.command == 'learning-open':\n"
            "  owner._append_checked_locked('CANARY')\n"
            " external_id('CANARY')\n"
        )
        report = self.check({"client.py": source})
        self.assertEqual(4, report["counts"]["all_ast_calls"])
        self.assertEqual(2, report["counts"]["ledger_named_receiver"])
        self.assertEqual(1, report["counts"]["selected_method"])
        self.assertEqual(1, report["counts"]["key_derivation_candidate"])
        calls = report["modules"]["client"]["calls"]
        self.assertEqual(["learning-status"], calls[0]["commands"])
        self.assertEqual(["learning-status"], calls[1]["commands"])
        self.assertEqual(["learning-open"], calls[2]["commands"])
        self.assertEqual([], calls[3]["commands"])
        self.assertEqual({"main"}, {call["scope"] for call in calls})
        self.assertNotIn("CANARY", json.dumps(report))

    def test_invalid_and_excessive_inputs_have_fixed_errors(self):
        variants = [
            ({1: ""}, ["client.py"], "audit_invalid_source"),
            ({"../private.py": ""}, ["client.py"], "audit_invalid_path"),
            ({"client.py": ""}, [True], "audit_invalid_path"),
            ({"client.py": "def broken("}, ["client.py"], "audit_invalid_source"),
            ({"client.py": "#" * (audit.MAX_SOURCE_BYTES + 1)}, ["client.py"], "audit_source_limit"),
            ({"client.py": "x=" + "+".join(["1"] * 300)}, ["client.py"], "audit_source_limit"),
        ]
        for sources, roots, reason in variants:
            with self.subTest(reason=reason), self.assertRaises(audit.AuditError) as caught:
                audit.audit_sources(sources, roots)
            self.assertEqual(reason, caught.exception.reason_code)
            self.assertEqual(reason, str(caught.exception))

    def test_compound_receivers_and_key_aliases_do_not_disappear(self):
        report = self.check({
            "client.py": (
                "from helper import external_id as derive\n"
                "def work(factory):\n"
                " factory().execute('CANARY')\n"
                " factory().append('CANARY')\n"
                " factory().digest_key()\n"
                " derive('CANARY')\n"
            ),
            "helper.py": "def external_id(value): return value\n",
        })
        self.assertFalse(report["boundary_conformant"])
        self.assertIn("possible_sql_call", self.reasons(report))
        self.assertEqual(1, report["counts"]["selected_method"])
        self.assertEqual(2, report["counts"]["key_derivation_candidate"])
        self.assertNotIn("CANARY", json.dumps(report))

    def test_compact_output_keeps_conformance_scope_and_full_option(self):
        report = self.check({"client.py": "import sqlite3\n"})
        compact = audit.summary(report)
        self.assertFalse(compact["boundary_conformant"])
        self.assertEqual(report["counts"], compact["counts"])
        self.assertEqual("client.py", compact["clients"][0]["root"])
        self.assertEqual(["persistence_dependency"], compact["clients"][0]["reason_codes"])
        self.assertEqual("--full", compact["full_report_option"])
        self.assertIn("not a completed normal-client census", " ".join(compact["limitations"]))
        self.assertNotIn("modules", compact)

    def test_inventory_success_does_not_make_check_mode_pass(self):
        for mode, expected in (("inventory", 0), ("check", 1)):
            with mock.patch.object(audit, "load_sources", return_value={"client.py": "import sqlite3\n"}), \
                    contextlib.redirect_stdout(io.StringIO()) as stdout:
                status = audit.main([mode, "--client-root", "client.py"])
            self.assertEqual(expected, status)
            self.assertFalse(json.loads(stdout.getvalue())["boundary_conformant"])

    def test_full_output_limit_includes_actual_indentation_and_newline(self):
        sources = {"client.py": "import sqlite3\n"}
        report = self.check(sources)
        compact = len(json.dumps(report, sort_keys=True).encode())
        rendered = len(json.dumps(report, sort_keys=True, indent=2).encode()) + 1
        self.assertLess(compact, rendered)
        with mock.patch.object(audit, "load_sources", return_value=sources), \
                mock.patch.object(audit, "MAX_REPORT_BYTES", (compact + rendered) // 2), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            status = audit.main(["inventory", "--client-root", "client.py", "--full"])
        self.assertEqual(2, status)
        self.assertEqual({"ready": False, "reason_code": "audit_source_limit"}, json.loads(stdout.getvalue()))


class OwnershipFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "source"
        self.root.mkdir()

    def test_loader_reads_only_source_metadata_and_ignores_pycache(self):
        (self.root / "client.py").write_text("VERSION = 1\n")
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "ignored.py").write_text("CANARY\n")
        self.assertEqual({"client.py": "VERSION = 1\n"}, audit.load_sources(self.root))

    def test_symlink_and_fifo_refuse_without_following_or_blocking(self):
        outside = self.base / "outside"
        outside.write_text("CANARY\n")
        link = self.root / "client.py"
        link.symlink_to(outside)
        with self.assertRaisesRegex(audit.AuditError, "audit_unsafe_source"):
            audit.load_sources(self.root)
        link.unlink()
        if not hasattr(os, "mkfifo"):
            return
        os.mkfifo(link, 0o600)
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "inventory", "--source-root", str(self.root)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=False,
        )
        self.assertEqual(2, result.returncode, result.stderr.decode(errors="replace"))
        self.assertEqual({"ready": False, "reason_code": "audit_unsafe_source"}, json.loads(result.stdout))
        self.assertNotIn(b"CANARY", result.stdout)

    def test_file_and_total_limits_are_enforced_while_loading(self):
        (self.root / "client.py").write_text("0123456789\n")
        with mock.patch.object(audit, "MAX_SOURCE_BYTES", 10), \
                self.assertRaisesRegex(audit.AuditError, "audit_source_limit"):
            audit.load_sources(self.root)
        with mock.patch.object(audit, "MAX_TOTAL_BYTES", 10), \
                self.assertRaisesRegex(audit.AuditError, "audit_source_limit"):
            audit.load_sources(self.root)


if __name__ == "__main__":
    unittest.main()

