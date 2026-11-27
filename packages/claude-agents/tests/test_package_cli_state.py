"""The package CLI tells its caller, in a structured field, whether it kept unfinished work."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from claude_agents import packaging

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "package.py"


def run_cli(error: str) -> dict:
    spec = importlib.util.spec_from_file_location("claude_package_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = io.StringIO()
    with mock.patch.object(packaging, "package_operation", side_effect=packaging.PackageError(error)), \
            mock.patch("sys.argv", ["package.py", "update", "--target", "/nonexistent"]), \
            contextlib.redirect_stdout(out):
        code = module.main()
    reply = json.loads(out.getvalue())
    reply["exit_code"] = code
    return reply


class PackageCliStateTests(unittest.TestCase):
    def test_a_pending_rollback_is_reported_in_the_structured_field(self) -> None:
        reply = run_cli("rollback_pending")
        self.assertEqual("pending", reply["rollback_state"])
        self.assertEqual(2, reply["exit_code"])

    def test_every_other_failure_reports_no_pending_work(self) -> None:
        for code in ("operation_failed", "untrusted_manifest", "installation_pending", "write_failed"):
            with self.subTest(code=code):
                self.assertEqual("none", run_cli(code)["rollback_state"])


if __name__ == "__main__":
    unittest.main()
