"""Which CLI commands may still open the shared ledger, pinned by reading the source.

Normal commands go through the project and account services. A command branch
that opens the ledger itself (`_learning_ledger`, which pauses the shared
collector) is either a ledger owner command or a named legacy fallback for
runs an earlier version left in the ledger. A new direct use fails this test
until it is listed here with its reason.
"""
from __future__ import annotations

import ast
import unittest

from tests.support import ORCHESTRATION_ROOT

from codex_agents.host_commands import PRIMARY_HOST_COMMANDS

# Commands a primary runs whose branch still contains a ledger fallback, and why.
PRIMARY_LEDGER_FALLBACKS = {
    "learning-route": "routing evidence the project's service does not hold, released candidates, fallback receipts",
    "learning-open-host-run": "a route that is not host-driven (none is produced since every child is bridged)",
    "dispatch": "a run an earlier version opened in the ledger",
    "learning-bind-host-agent": "native runs of earlier versions",
    "learning-observe-host-run": "native runs of earlier versions",
    "learning-classify-host-run-abandoned": "runs an earlier version opened in the ledger",
    "learning-record-host-result": "runs an earlier version opened in the ledger",
    "learning-run-operational-checks": "outcomes an earlier version recorded in the ledger",
    "learning-record-operational-qa": "outcomes an earlier version recorded in the ledger",
    "learning-orchestration-report": "orchestrations an earlier version kept in the ledger",
}
# Helpers that open the ledger on behalf of the branch that calls them.
LEDGER_HELPERS = {"_learning_ledger", "_installed_route", "_validated_assignment_route"}


def command_branches():
    """Each `args.command` branch of `main`, as (command names, branch body)."""
    tree = ast.parse((ORCHESTRATION_ROOT / "cli.py").read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    branches = []
    for node in ast.walk(main):
        if not isinstance(node, ast.If):
            continue
        names = set()
        for test in ast.walk(node.test):
            if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute)
                    and test.left.attr == "command" and len(test.comparators) == 1):
                for constant in ast.walk(test.comparators[0]):
                    if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                        names.add(constant.value)
        if names:
            branches.append((names, node.body))
    return branches


def calls(body, targets):
    return any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in targets
               for statement in body for node in ast.walk(statement))


class CliLedgerBoundaryTests(unittest.TestCase):
    def test_primary_commands_open_the_ledger_only_through_named_legacy_fallbacks(self):
        direct = set()
        for names, body in command_branches():
            if calls(body, {"_learning_ledger"}):
                direct |= names & set(PRIMARY_HOST_COMMANDS)
        self.assertEqual(set(PRIMARY_LEDGER_FALLBACKS), direct)

    def test_every_branch_is_found_and_the_helpers_exist(self):
        found = set().union(*(names for names, _ in command_branches()))
        self.assertLessEqual(set(PRIMARY_HOST_COMMANDS) - {"validate", "inventory", "route"}, found)
        source = (ORCHESTRATION_ROOT / "cli.py").read_text()
        for helper in LEDGER_HELPERS:
            self.assertIn(f"def {helper}(", source)

    def test_commands_that_never_touch_the_ledger_stay_that_way(self):
        clean = {"learning-status", "learning-open-orchestration", "learning-checkpoint-orchestration",
                 "learning-pause-orchestration", "learning-resume-orchestration", "learning-close-orchestration",
                 "learning-orchestration-status", "learning-run-status", "learning-cancel-run", "provider-observe"}
        for names, body in command_branches():
            if names & clean:
                with self.subTest(names=sorted(names)):
                    self.assertFalse(calls(body, LEDGER_HELPERS))


if __name__ == "__main__":
    unittest.main()
