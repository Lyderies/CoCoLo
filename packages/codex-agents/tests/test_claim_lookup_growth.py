"""D11: a claim lookup is an indexed lookup whose cost does not grow with history.

`WHERE project_id=? AND (handle_digest=? OR run_id=?)` let SQLite pick the
project_id prefix of a covering index and scan every claim of the project.
"""
import ast
import re
import sqlite3
import unittest
from pathlib import Path

import tests.support  # noqa: F401

from codex_agents import learning_store
from codex_agents.learning_store import _LAUNCH_CLAIMS_SCHEMA, _LAUNCH_RECEIPT_PARENT_SCHEMA

PROBES = ("project", "a" * 64, "run-absent")


def claim_lookup_queries():
    """Every launch_claims lookup in the ledger source that matches on a handle or a run id."""
    tree = ast.parse(Path(learning_store.__file__).read_text(encoding="utf-8"))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and node.value.startswith("SELECT 1 FROM launch_claims WHERE")
            and "active_run_capacity" not in node.value and ("run_id=?" in node.value
                                                                          or "handle_digest=?" in node.value)]


class ClaimLookupGrowthTests(unittest.TestCase):
    def database(self, rows):
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE projects(project_id TEXT PRIMARY KEY)")
        connection.execute(_LAUNCH_CLAIMS_SCHEMA)
        connection.execute(_LAUNCH_RECEIPT_PARENT_SCHEMA)
        connection.execute("INSERT INTO projects VALUES('project')")
        connection.executemany(
            "INSERT INTO launch_claims VALUES('project',?,?,'claimed',1,?)",
            [(f"{index:064x}", f"run-{index}", "brg_" + f"{index:032x}") for index in range(rows)])
        return connection

    def steps(self, connection, query, parameters):
        count = [0]

        def tick():
            count[0] += 1
            return 0
        connection.set_progress_handler(tick, 1)
        connection.execute(query, parameters).fetchall()
        connection.set_progress_handler(None, 0)
        return count[0]

    def parameters(self, query):
        probes = {"project_id": PROBES[0], "handle_digest": PROBES[1], "run_id": PROBES[2]}
        return tuple(probes[name] for name in re.findall(r"(project_id|handle_digest|run_id)=\?", query))

    def test_queries_found(self):
        self.assertGreaterEqual(len(claim_lookup_queries()), 1)
        source = Path(learning_store.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"launch_claims WHERE[^\n]*\(handle_digest=\? OR run_id=\?\)")

    def test_plan_has_no_project_wide_scan(self):
        connection = self.database(10)
        for query in claim_lookup_queries():
            plan = " | ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, self.parameters(query)))
            self.assertNotIn("(project_id=?)", plan, query)
            self.assertNotIn("SCAN", plan, query)

    def test_steps_do_not_grow_with_history(self):
        empty, full = self.database(0), self.database(2000)
        for query in claim_lookup_queries():
            parameters = self.parameters(query)
            self.assertLessEqual(self.steps(full, query, parameters), self.steps(empty, query, parameters) + 20, query)


if __name__ == "__main__":
    unittest.main()
